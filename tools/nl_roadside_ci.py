"""Check an attribute-only Dutch sign artifact before merging it into the site.

Standard library only, owned by the data repository. No producer code, photos,
national source exports or executable files enter the publishing job.
"""
import argparse
import gzip
import json
import math
from pathlib import Path
import re


def read_json(path, compressed=False):
    if path.is_symlink() or path.stat().st_size>16_000_000:
        raise ValueError('invalid or oversized data file')
    opener=gzip.open if compressed else open
    with opener(path,'rb') as stream:
        raw=stream.read(32_000_001)
    if len(raw)>32_000_000:raise ValueError('oversized decoded tile')
    return json.loads(raw)


def check(root):
    root=Path(root).resolve()
    manifest=read_json(root/'manifest.json')
    if manifest.get('format')!=2 or manifest.get('zoom')!=12 or manifest.get('license')!='CC0-1.0':
        raise ValueError('unexpected Dutch sign schema')
    if manifest.get('source')!='https://opendata.ndw.nu/verkeersborden_actueel_beeld.csv.gz':
        raise ValueError('unexpected national source')
    if not re.fullmatch(r'[0-9a-f]{64}',manifest.get('source_sha256','')):
        raise ValueError('missing source digest')
    tiles=manifest.get('tiles')
    if not isinstance(tiles,list) or len(tiles)>16_384 or len(tiles)!=len(set(tiles)):
        raise ValueError('invalid tile index')
    allowed={'manifest.json'};count=0;ids=set()
    for name in tiles:
        if not isinstance(name,str) or not re.fullmatch(r'\d{1,4}/\d{1,4}',name):
            raise ValueError('invalid tile path')
        x,y=map(int,name.split('/'))
        if not 0<=x<4096 or not 0<=y<4096:raise ValueError('tile outside zoom')
        relative=name+'.json.gz';allowed.add(relative)
        path=root/relative
        if not path.resolve().is_relative_to(root):raise ValueError('tile escapes dataset')
        data=read_json(path,True)
        if set(data)!={'format','records'} or data['format']!=2 or not isinstance(data['records'],list):
            raise ValueError('unexpected tile payload')
        if len(data['records'])>100_000:raise ValueError('oversized record list')
        for row in data['records']:
            if not isinstance(row,dict) or set(row)!={'id','lat','lon','bearing','code','role','speed','plates','source_ids'}:
                raise ValueError('non-attribute fields in sign record')
            ident=row['id']
            if not isinstance(ident,str) or not 1<=len(ident)<=128 or ident in ids:raise ValueError('invalid source id')
            ids.add(ident)
            lat,lon,bearing=row['lat'],row['lon'],row['bearing']
            if not all(type(v) in (int,float) and math.isfinite(v) for v in (lat,lon,bearing)):
                raise ValueError('invalid coordinates')
            if not 50<=lat<=54 or not 3<=lon<=8 or not 0<=bearing<360:raise ValueError('invalid Dutch location')
            tx=int((lon+180)/360*4096)
            ty=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*4096)
            if (tx,ty)!=(x,y):raise ValueError('record in wrong tile')
            role,speed=row['role'],row['speed']
            if not ((isinstance(role,str) and re.fullmatch(r'[a-z][a-z0-9_.]{1,63}',role) and speed is None)
                    or (role is None and type(speed) is int and 5<=speed<=130)):
                raise ValueError('invalid sign meaning')
            if not isinstance(row['code'],str) or not re.fullmatch(r'[A-Z][0-9]{1,2}[A-Z0-9]*',row['code']):
                raise ValueError('invalid RVV code')
            sources=row['source_ids']
            if not isinstance(sources,list) or not 1<=len(sources)<=10_000 or ident not in sources or not all(isinstance(s,str) and 1<=len(s)<=128 for s in sources):
                raise ValueError('invalid source identities')
            plates=row['plates']
            if not isinstance(plates,list) or len(plates)>3:raise ValueError('invalid qualifiers')
            for plate in plates:
                if not isinstance(plate,dict) or set(plate)!={'kind','text'} or plate['kind'] not in ('VRIJ','UIT','VOOR'):
                    raise ValueError('invalid qualifier fields')
                text=plate['text']
                if not isinstance(text,str) or not 1<=len(text)<=96 or any(ord(c)<32 for c in text):raise ValueError('invalid qualifier text')
                if plate['kind']=='UIT' and not text.casefold().startswith('uitgezonderd '):raise ValueError('incomplete exception')
            count+=1
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual!=allowed:raise ValueError('unexpected files in public sign artifact')
    if manifest.get('records')!=count:raise ValueError('record count mismatch')
    return dict(dataset='nl-rvv',tiles=len(tiles),records=count)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset')
    print(json.dumps(check(parser.parse_args().dataset)))
