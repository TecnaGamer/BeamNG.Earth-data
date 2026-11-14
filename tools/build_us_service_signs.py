"""Resumable US bulk build, independent of the running BeamNG server.

Downloads a checksum-verified Geofabrik US PBF, indexes relevant features in
disk-backed SQLite/RTree, and runs bounded regional graphs with a route-length
halo. Completed cells are atomically published; incomplete cells never replace
the bundled fallback. No Overpass load, gameplay routing or cache purge.
"""
import argparse
import gc
import gzip
import hashlib
import json
import math
import re
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import serviceindex as index
import servicecoverage as coverage

URL = 'https://download.geofabrik.de/north-america/us-latest.osm.pbf'
SCHEMA = 3


def status(work, phase, **values):
    coverage.atomic_json(work/'status.json', dict(phase=phase, pid=os.getpid(), updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), **values))
    print(json.dumps(dict(phase=phase,**values)),flush=True)


def download(work, source_url=None):
    path = work/'us-latest.osm.pbf'
    pin = work/'download.json'
    if pin.exists():
        info = json.loads(pin.read_text())
    else:
        if source_url is None:
            with urllib.request.urlopen('https://download.geofabrik.de/north-america/us.html', timeout=45) as response:
                names = re.findall(r'us-\d{6}\.osm\.pbf', response.read().decode())
            if not names: raise ValueError('No dated US extract found')
            source_url = 'https://download.geofabrik.de/north-america/' + max(names)
        if not re.fullmatch(r'https://download\.geofabrik\.de/north-america/us-\d{6}\.osm\.pbf', source_url):
            raise ValueError('Use a dated Geofabrik US source URL')
        req = urllib.request.Request(source_url+'.md5', headers={'User-Agent':'BeamNG.Earth-service-sign-build/1'})
        checksum = urllib.request.urlopen(req,timeout=45).read().decode().split()[0]
        if len(checksum)!=32 or any(c not in '0123456789abcdef' for c in checksum): raise ValueError('Invalid source checksum')
        info = dict(url=source_url,md5=checksum)
        coverage.atomic_json(pin,info)
    if not path.exists():
        temporary = path.with_suffix('.pbf.part')
        status(work,'downloading',url=info['url'],partial_file=str(temporary))
        curl = shutil.which('curl.exe') or shutil.which('curl')
        if not curl: raise RuntimeError('curl is required for resumable bulk downloads')
        subprocess.run([curl,'--fail','--location','--retry','5','--retry-delay','10','--connect-timeout','30',
                        '--speed-limit','1024','--speed-time','180','--continue-at','-',
                        '--output',str(temporary),info['url']],check=True)
        status(work,'verifying_download',bytes=temporary.stat().st_size)
        with temporary.open('rb') as f: actual = hashlib.file_digest(f,'md5').hexdigest()
        if actual != info['md5']:
            raise ValueError('Source checksum changed or download is corrupt. Retain the partial file; use a fresh --work directory for a new snapshot.')
        temporary.replace(path)
        coverage.atomic_json(work/'verified.json',dict(md5=actual,bytes=path.stat().st_size))
    with path.open('rb') as f:
        if hashlib.file_digest(f,'md5').hexdigest() != info['md5']: raise ValueError('Existing PBF does not match pinned checksum')
    with path.open('rb') as f: fingerprint = hashlib.file_digest(f,'sha256').hexdigest()
    coverage.atomic_json(work/'source.json', dict(url=info['url'], sha256=fingerprint, md5=info['md5']))
    return path,fingerprint


def relevant(tags):
    return (tags.get('highway') in index.DRIVABLE or tags.get('highway') == 'motorway_junction'
            or tags.get('amenity') in index.CATEGORIES or tags.get('tourism') in index.CATEGORIES
            or bool(tags.get('barrier')) or tags.get('landuse') == 'military' or bool(tags.get('military')))


def connection(path):
    db = sqlite3.connect(path)
    db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS features(id INTEGER PRIMARY KEY, body BLOB NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS bounds USING rtree(id,x0,x1,y0,y1);
        CREATE TABLE IF NOT EXISTS cores(x INTEGER,y INTEGER,PRIMARY KEY(x,y));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    ''')
    return db


def feature_id(kind, ident):
    return int(ident)*3+{'node':0,'way':1,'relation':2}[kind]


def put(db, element, points):
    if not points: return
    fid = feature_id(element['type'],element['id'])
    db.execute('INSERT OR REPLACE INTO features VALUES(?,?)',(fid,zlib.compress(json.dumps(element,separators=(',',':')).encode(),1)))
    db.execute('INSERT OR REPLACE INTO bounds VALUES(?,?,?,?,?)',
               (fid,min(p[0] for p in points),max(p[0] for p in points),min(p[1] for p in points),max(p[1] for p in points)))


def get(db,kind,ident):
    row = db.execute('SELECT body FROM features WHERE id=?',(feature_id(kind,ident),)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None


def import_pbf(path, db, work, fingerprint):
    complete = db.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone()
    if complete and complete[0] == f'{fingerprint}:{SCHEMA}': return
    db.commit()
    database = db.execute('PRAGMA database_list').fetchone()[2]
    # libosmium file-backed location indexes can retain Windows mappings until
    # process exit. Isolate import so those handles close before cell routing.
    command = [sys.executable,str(Path(__file__).resolve()),'--import-stage','--pbf',str(path),
               '--work',str(work),'--database',database,'--fingerprint',fingerprint]
    process = subprocess.Popen(command)
    code = process.wait()
    for cache in work.glob(f'locations-{process.pid}-*.idx'):
        if cache.resolve().parent == work.resolve(): cache.unlink(missing_ok=True)
    if code: raise RuntimeError(f'PBF import worker exited with code {code}')


def _import_pbf(path, db, work, fingerprint):
    import osmium
    complete = db.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone()
    if complete and complete[0] == f'{fingerprint}:{SCHEMA}': return
    status(work,'indexing_relations')
    relations, members, via_nodes = [],set(),set()
    for obj in osmium.FileProcessor(str(path),osmium.osm.RELATION):
        tags = dict(obj.tags)
        if tags.get('type') != 'restriction' and not relevant(tags): continue
        row = dict(type='relation',id=obj.id,tags=tags,members=[])
        for m in obj.members:
            kind = {'w':'way','n':'node','r':'relation'}[m.type]
            row['members'].append(dict(type=kind,ref=m.ref,role=m.role))
            if kind == 'way': members.add(m.ref)
            if kind == 'node': via_nodes.add(m.ref)
        relations.append(row)
    cache = work/f'locations-{os.getpid()}-{time.time_ns()}.idx'
    status(work,'indexing_roads_and_pois',relations=len(relations),location_cache=str(cache))
    processor = osmium.FileProcessor(str(path),osmium.osm.NODE|osmium.osm.WAY).with_locations('sparse_file_array,'+str(cache))
    count = 0
    for obj in processor:
        # Most OSM nodes are untagged geometry. The location handler has already
        # cached them; avoid allocating Python tag dictionaries for all of them.
        if obj.is_node() and obj.id not in via_nodes and not obj.tags: continue
        tags = dict(obj.tags)
        if obj.is_node():
            if obj.id not in via_nodes and not relevant(tags): continue
            point = [obj.location.lon,obj.location.lat]
            element = dict(type='node',id=obj.id,lon=point[0],lat=point[1],tags=tags)
            put(db,element,[point])
        else:
            if obj.id not in members and not relevant(tags): continue
            if any(not n.location.valid() for n in obj.nodes): raise ValueError(f'Missing coordinates on way {obj.id}')
            points = [[n.lon,n.lat] for n in obj.nodes]
            element = dict(type='way',id=obj.id,nodes=[n.ref for n in obj.nodes],tags=tags,
                           geometry=[dict(lon=p[0],lat=p[1]) for p in points])
            put(db,element,points)
            if tags.get('highway') in ('motorway_link','trunk_link') and points:
                start = points[-1] if index.exitsigns._direction(tags) == -1 else points[0]
                db.execute('INSERT OR IGNORE INTO cores VALUES(?,?)',coverage.cell(*start))
                if index.exitsigns._direction(tags) == 0:
                    db.execute('INSERT OR IGNORE INTO cores VALUES(?,?)',coverage.cell(*points[-1]))
        count += 1
        if count%10000 == 0:
            db.commit()
            if shutil.disk_usage(work).free < 15*1024**3:
                raise RuntimeError('Less than 15 GiB free; build stopped with resumable data retained')
            status(work,'indexing_roads_and_pois',features=count)
    del processor
    gc.collect()
    # Only this run's closed location index is removed, never a server cache.
    if cache.resolve().parent == work.resolve():
        try: cache.unlink()
        except PermissionError: pass
    status(work,'indexing_relation_geometry',features=count,relations=len(relations))
    for row in relations:
        points = []
        for m in row['members']:
            feature = get(db,m['type'],m['ref']) if m['type'] != 'relation' else None
            if feature is None: continue
            if m['type'] == 'way':
                m['geometry'] = feature['geometry']
                points.extend([p['lon'],p['lat']] for p in feature['geometry'])
            else: points.append([feature['lon'],feature['lat']])
        put(db,row,points)
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('import_complete',f'{fingerprint}:{SCHEMA}'))
    db.commit()


def halo(lat, policy_path):
    data = json.loads(Path(policy_path).read_text(encoding='utf-8'))
    distances = [data['default']['distance_m']]+[v['distance_m'] for v in data.get('states',{}).values() if 'distance_m' in v]
    metres = max(v for d in distances for v in d.values())+2500
    dy = metres/110000
    dx = dy/max(.02,math.cos(math.radians(min(89,abs(lat)+coverage.CELL+dy))))
    return dx,dy


def build_cells(db, output, work, fingerprint, policy_path):
    import servicefiction
    snapshot = hashlib.sha256((fingerprint+str(SCHEMA)).encode()+Path(policy_path).read_bytes()
                              +Path(index.__file__).read_bytes()+Path(__file__).read_bytes()
                              +Path(index.exitsigns.__file__).read_bytes()+Path(servicefiction.__file__).read_bytes()
                              +index.signstates.DATA.read_bytes()).hexdigest()[:24]
    manifest_path = output/'manifest.json'
    progress_path = work/f'cells-{snapshot}.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    cells = progress.get('cells', {})
    cores = set(db.execute('SELECT x,y FROM cores ORDER BY x,y').fetchall())
    if not cores: raise ValueError('No candidate motorway ramps; published dataset retained')
    cores.update(tuple(map(int,k.split('_'))) for k in previous.get('cells', {}))
    cores = list(cores)
    # Publish Wyoming first, followed by all other national motorway cells.
    cores.sort(key=lambda c: (not(-112<=c[0]*coverage.CELL<=-103 and 40<=c[1]*coverage.CELL<=46),c))
    total = len(cores)
    output.mkdir(parents=True, exist_ok=True)
    def save_progress():
        coverage.atomic_json(progress_path, dict(built=snapshot, cells=cells, total_cells=total))
    for x,y in cores:
        k = coverage.key(x,y)
        entry = cells.get(k)
        if entry and entry.get('snapshot') == snapshot:
            path = output/entry['path']
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']: continue
        dx,dy = halo(y*coverage.CELL,policy_path)
        box = x*coverage.CELL-dx,y*coverage.CELL-dy,(x+1)*coverage.CELL+dx,(y+1)*coverage.CELL+dy
        status(work,'building_cells',cell=k,completed_cells=sum(e.get('snapshot')==snapshot for e in cells.values()),total_cells=total)
        rows = db.execute('SELECT f.body FROM bounds b JOIN features f ON f.id=b.id WHERE b.x1>=? AND b.y1>=? AND b.x0<=? AND b.y0<=? ORDER BY f.id',box)
        elements = [json.loads(zlib.decompress(row[0])) for row in rows]
        result = index.build(dict(elements=elements),policy_path,owner=(x,y))
        result['objects'] = [o for o in result['objects'] if coverage.cell(o['lon'],o['lat']) == (x,y)]
        result['source_fingerprint'] = fingerprint
        raw = gzip.compress(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(),mtime=0)
        relative = Path(snapshot)/(k+'.json.gz')
        path = output/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary = path.with_suffix('.part')
        temporary.write_bytes(raw)
        temporary.replace(path)
        summary = summarize(result)
        cells[k] = dict(path=relative.as_posix(),sha256=hashlib.sha256(raw).hexdigest(),
                        bytes=len(raw),objects=len(result['objects']),snapshot=snapshot,audit=summary)
        save_progress()
    audit = combine_summaries([entry['audit'] for entry in cells.values()])
    if not audit['statuses'].get('with_services'):
        raise ValueError('No qualifying service exits; published dataset retained')
    source_path = work/'source.json'
    source = json.loads(source_path.read_text()) if source_path.exists() else dict(sha256=fingerprint, kind='local_extract')
    report = dict(format=1, built=snapshot, complete=True, name_origin='fictional', **audit)
    report_path = output/snapshot/'audit.json'
    coverage.atomic_json(report_path, report)
    report_raw = report_path.read_bytes()
    manifest = dict(format=1,built=snapshot,cell_degrees=coverage.CELL,cells=cells,
                    complete=True,completed_cells=len(cells),total_cells=total,
                    name_origin='fictional',naming_version=servicefiction.VERSION,
                    license='ODbL-1.0',attribution='© OpenStreetMap contributors',
                    source=source,source_fingerprint=fingerprint,
                    audit=dict(path=f'{snapshot}/audit.json',sha256=hashlib.sha256(report_raw).hexdigest()))
    validate_dataset(output, manifest)
    # The public manifest changes once, only after every cell and audit validates.
    coverage.atomic_json(manifest_path, manifest)
    status(work,'complete',completed_cells=len(cells),total_cells=total,objects=sum(c['objects'] for c in cells.values()),output=str(output))


def summarize(result):
    summary = dict(statuses={}, states={}, categories={})
    for check in result.get('audit', []):
        state, outcome = check['state'] or 'outside_us', check['status']
        summary['statuses'][outcome] = summary['statuses'].get(outcome, 0) + 1
        counts = summary['states'].setdefault(state, {})
        counts[outcome] = counts.get(outcome, 0) + 1
        for category, count in check['categories'].items():
            summary['categories'][category] = summary['categories'].get(category, 0) + count
    return summary


def combine_summaries(summaries):
    result = dict(statuses={}, states={state: {} for state, *_ in index.signstates._polygons()}, categories={})
    for summary in summaries:
        for field in ('statuses', 'categories'):
            for key, count in summary[field].items():
                result[field][key] = result[field].get(key, 0) + count
        for state, counts in summary['states'].items():
            dst = result['states'].setdefault(state, {})
            for key, count in counts.items(): dst[key] = dst.get(key, 0) + count
    result['processed_ramps'] = sum(result['statuses'].values())
    return result


def validate_dataset(output, manifest=None):
    """Audit every published cell, including authoritative empty cells."""
    if manifest is None: manifest = json.loads((output/'manifest.json').read_text())
    coverage.manifest_key(manifest)
    if manifest['completed_cells'] != manifest['total_cells'] or len(manifest['cells']) != manifest['total_cells']:
        raise ValueError('Incomplete service coverage')
    summary, seen = [], set()
    for key, entry in manifest['cells'].items():
        path = (output/entry['path']).resolve()
        if not path.is_relative_to(output.resolve()): raise ValueError('Cell path escapes output')
        raw = path.read_bytes()
        if len(raw) > coverage.MAX_COMPRESSED or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Cell checksum/size mismatch')
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream: body = stream.read(coverage.MAX_BODY+1)
        if len(body) > coverage.MAX_BODY: raise ValueError('Cell decompression limit')
        data = json.loads(body)
        if data.get('name_origin') != 'fictional' or len(data['objects']) != entry['objects']:
            raise ValueError('Unexpected service cell format')
        for check in data['audit']:
            if coverage.key(*coverage.cell(check['lon'], check['lat'])) != key:
                raise ValueError('Audit entry belongs to another cell')
            if check['ramp_id'] in seen: raise ValueError('Duplicate audited ramp')
            seen.add(check['ramp_id'])
        for obj in data['objects']:
            if coverage.key(*coverage.cell(obj['lon'], obj['lat'])) != key: raise ValueError('Exit belongs to another cell')
            for business in obj['exit']['services']:
                if business != index.servicefiction.business(business):
                    raise ValueError('Service name is not fictional')
        current = summarize(data)
        if current != entry['audit']: raise ValueError('Cell audit mismatch')
        summary.append(current)
    report = (output/manifest['audit']['path']).read_bytes()
    if hashlib.sha256(report).hexdigest() != manifest['audit']['sha256']: raise ValueError('Audit checksum mismatch')
    totals = combine_summaries(summary)
    if any(json.loads(report).get(k) != v for k, v in totals.items()): raise ValueError('National audit totals mismatch')
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,default=ROOT/'build/service-signs-us')
    parser.add_argument('--output',type=Path,default=ROOT/'build/service-signs-output')
    parser.add_argument('--pbf',type=Path,help='Use a local extract instead of downloading the United States')
    parser.add_argument('--policy',type=Path,default=index.POLICY)
    parser.add_argument('--source-url', help='Dated Geofabrik US .osm.pbf URL')
    parser.add_argument('--check', action='store_true', help='Validate an existing output and print its coverage audit')
    parser.add_argument('--import-stage',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--database',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--fingerprint',help=argparse.SUPPRESS)
    args = parser.parse_args()
    work,output = args.work.resolve(),args.output.resolve()
    if args.check:
        print(json.dumps(validate_dataset(output), sort_keys=True))
        return
    work.mkdir(parents=True,exist_ok=True)
    if args.import_stage:
        db = connection(args.database)
        try: _import_pbf(args.pbf,db,work,args.fingerprint)
        finally: db.close()
        return
    output.mkdir(parents=True,exist_ok=True)
    # OS-held lock is released after crashes; no stale PID lock to remove manually.
    lock = (work/'build.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x4000)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            os.nice(10)
    except OSError:
        raise SystemExit('Another national service-sign build already owns this work directory')
    try:
        if args.pbf:
            path = args.pbf.resolve()
            with path.open('rb') as f: fingerprint = hashlib.file_digest(f,'sha256').hexdigest()
        else: path,fingerprint = download(work,args.source_url)
        database = work/f'network-{fingerprint[:16]}-v{SCHEMA}.sqlite'
        db = connection(database)
        try:
            import_pbf(path,db,work,fingerprint)
            build_cells(db,output,work,fingerprint,args.policy)
        finally: db.close()
    except Exception as error:
        status(work,'failed',error=f'{type(error).__name__}: {error}')
        raise
    finally: lock.close()


if __name__ == '__main__': main()

