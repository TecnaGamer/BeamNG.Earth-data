"""Fetch reproducible public WKD and OSM inputs; never download artwork."""
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
import zipfile

WKD='https://downloads.rijkswaterstaatdata.nl/wkd/geogegevens/Geopackage/Komgrenzen/01-10-2026.zip'
WKD_SHA='22a4a65be7a13b00cbbb63b7205e6d51e6bac825f5420d2d1ca352400084072e'
OVERPASS='https://overpass-api.de/api/interpreter'


def fetch(url,target,data=None):
    target=Path(target)
    for attempt in range(3):
        try:
            request=urllib.request.Request(url,data=data,headers={'User-Agent':'BeamNG.Earth-nl-context/1.0'})
            digest=hashlib.sha256();size=0
            with urllib.request.urlopen(request,timeout=240) as response, target.open('wb') as out:
                while chunk:=response.read(1024*1024):
                    size+=len(chunk)
                    if size>1024*1024*1024:raise ValueError('source larger than 1 GiB')
                    digest.update(chunk);out.write(chunk)
            return digest.hexdigest()
        except Exception:
            target.unlink(missing_ok=True)
            if attempt==2:raise
            time.sleep(15*(attempt+1))


def main(folder):
    from build_nl_signal_context import TURN_QUERY,SIGNAL_QUERY
    root=Path(folder)
    if root.exists() and any(root.iterdir()):raise ValueError('source directory must be empty')
    root.mkdir(parents=True,exist_ok=True)
    package=root/'wkd.zip'
    if fetch(WKD,package)!=WKD_SHA:raise ValueError('WKD archive checksum changed')
    with zipfile.ZipFile(package) as archive:
        candidates=[m for m in archive.infolist() if m.filename.lower().endswith('.gpkg')]
        if len(candidates)!=1 or candidates[0].file_size>1024*1024*1024:
            raise ValueError('expected one bounded WKD GeoPackage')
        # No extractall: an archive member name never becomes a local path.
        with archive.open(candidates[0]) as src,(root/'WKD_KOM_BORDEN.gpkg').open('wb') as dst:
            shutil.copyfileobj(src,dst)
    (root/'WKD_KOM_BORDEN.gpkg.source.json').write_text(json.dumps(dict(url=WKD,archive_sha256=WKD_SHA)))
    for name,query in [('turns',TURN_QUERY),('signals',SIGNAL_QUERY)]:
        path=root/(name+'.json')
        digest=fetch(OVERPASS,path,urllib.parse.urlencode({'data':query}).encode())
        body=json.loads(path.read_text())
        if body.get('remark') or not isinstance(body.get('elements'),list):
            raise ValueError('Overpass returned incomplete data')
        path.with_suffix('.json.source.json').write_text(json.dumps(dict(url=OVERPASS,query=query,sha256=digest)))
    print('Public WKD/OSM source snapshots saved; publish only derived tiles.')


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('folder');main(ap.parse_args().folder)
