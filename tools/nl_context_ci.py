"""Validate bounded ODbL context shards before the existing data-site publisher."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import re


def read(path):
    if path.is_symlink() or path.stat().st_size>8_000_000:raise ValueError('context file bounds')
    opener=gzip.open if path.name.endswith('.gz') else open
    with opener(path,'rb') as stream:raw=stream.read(32_000_001)
    if len(raw)>32_000_000:raise ValueError('decoded context bounds')
    return json.loads(raw)


def check(root):
    root=Path(root).resolve();manifest=read(root/'manifest.json')
    if manifest.get('format')!=1 or manifest.get('zoom')!=14 or manifest.get('license')!='ODbL-1.0':
        raise ValueError('context format or licence')
    tiles=manifest.get('tiles')
    built=manifest.get('built')
    unsigned={k:v for k,v in manifest.items() if k!='built'}
    if not isinstance(built,str) or not re.fullmatch('[0-9a-f]{64}',built) or built!=hashlib.sha256(
            json.dumps(unsigned,sort_keys=True,separators=(',',':')).encode()).hexdigest():
        raise ValueError('context manifest identity')
    if not isinstance(tiles,dict) or len(tiles)>30000:raise ValueError('context tile index')
    expected={'manifest.json'};identities={}
    for name,meta in tiles.items():
        if not re.fullmatch(r'\d{1,5}/\d{1,5}\.json\.gz',name):raise ValueError('context tile path')
        x,y=map(int,name.removesuffix('.json.gz').split('/'))
        if not 0<=x<16384 or not 0<=y<16384:raise ValueError('context tile coordinate')
        path=root/name;expected.add(name)
        if path.is_symlink() or path.stat().st_size!=meta.get('bytes'):raise ValueError('context bytes')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=meta.get('sha256'):raise ValueError('context checksum')
        tile=read(path);rows=tile.get('records')
        if tile.get('format')!=1 or not isinstance(rows,list) or len(rows)>100000:raise ValueError('context records')
        for row in rows:
            if not isinstance(row,dict) or set(row)-{'kind','id','geometry','tags','road_class'}:
                raise ValueError('context is attributes only')
            kind,ident,geometry=row.get('kind'),row.get('id'),row.get('geometry')
            if kind not in ('road','signal','urban_section') or not isinstance(ident,str) or not re.fullmatch(r'\d{1,20}',ident):
                raise ValueError('context identity')
            if not isinstance(geometry,list) or not 1<=len(geometry)<=65535 or kind=='signal' and len(geometry)!=1:
                raise ValueError('context geometry')
            if kind!='signal' and len(geometry)<2:raise ValueError('context line')
            for p in geometry:
                if not isinstance(p,list) or len(p)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in p):
                    raise ValueError('context point')
                if not 3<=p[0]<=8 or not 50<=p[1]<=54:raise ValueError('context bounds')
            tags=row.get('tags',{})
            if not isinstance(tags,dict) or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>4096 for k,v in tags.items()):
                raise ValueError('context tag values')
            key=(kind,ident);digest=hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':')).encode()).digest()
            if key in identities and identities[key]!=digest:raise ValueError('conflicting context halo identity')
            identities[key]=digest
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual!=expected or any(p.is_symlink() for p in root.rglob('*')):raise ValueError('extra context artifact files')
    if len(identities)!=manifest.get('records'):raise ValueError('context record total')
    return dict(tiles=len(tiles),records=len(identities),built=manifest.get('built'))


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('root');print(json.dumps(check(ap.parse_args().root)))
