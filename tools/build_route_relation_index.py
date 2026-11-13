"""Build routes/ from one relation-only pass over the weekly OSM planet.

    python tools/build_route_relation_index.py PLANET.osm.pbf out/routes

PLANET may also be an HTTPS URL (streamed by curl on Linux). Only US road-route
tags and their way memberships are stored; no geometry or sign positions.
"""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

KEYS = ('type', 'route', 'network', 'ref', 'modifier', 'direction', 'destination:ref')


@contextmanager
def source_file(source):
    import osmium
    if not source.startswith(('http://', 'https://')):
        yield source
        return
    # A failed/truncated transfer must not publish a partial database. Do not
    # retry into the same pipe: that would concatenate PBF streams after failure.
    process = subprocess.Popen(['curl', '-sfL', '-A', 'BeamNG.Earth route build', source],
                               stdout=subprocess.PIPE)
    try:
        yield osmium.io.File(f'/proc/self/fd/{process.stdout.fileno()}', 'pbf')
        process.stdout.close()
        if process.wait() != 0:
            raise OSError('Planet download failed')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()


def relations(source):
    import osmium
    with source_file(str(source)) as src:
        processor = osmium.FileProcessor(src, osmium.osm.RELATION)
        for relation in processor.with_filter(osmium.filter.KeyFilter('route')):
            tags = {key: relation.tags[key] for key in KEYS if key in relation.tags}
            if (tags.get('type') == 'route' and tags.get('route') == 'road'
                    and tags.get('network', '').startswith('US:') and tags.get('ref')):
                yield relation.id, tags, [(m.ref, m.role) for m in relation.members if m.type == 'w']


def build(source, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    networks = Counter()
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        database = Path(tmp) / 'routes.sqlite'
        with closing(sqlite3.connect(database)) as con, con:
            con.execute('PRAGMA page_size=4096')
            con.execute('CREATE TABLE routes(id INTEGER PRIMARY KEY,tags TEXT NOT NULL) WITHOUT ROWID')
            con.execute('CREATE TABLE members(way_id INTEGER NOT NULL,route_id INTEGER NOT NULL,role TEXT NOT NULL,'
                        'PRIMARY KEY(way_id,route_id,role)) WITHOUT ROWID')
            for rid, tags, members in relations(source):
                con.execute('INSERT INTO routes VALUES (?,?)',
                            (rid, json.dumps(tags, separators=(',', ':'), sort_keys=True)))
                con.executemany('INSERT OR IGNORE INTO members VALUES (?,?,?)',
                                ((wid, rid, role) for wid, role in members))
                networks[tags['network']] += 1
            count = con.execute('SELECT COUNT(*) FROM routes').fetchone()[0]
            members = con.execute('SELECT COUNT(*) FROM members').fetchone()[0]
            ways = con.execute('SELECT COUNT(DISTINCT way_id) FROM members').fetchone()[0]
            if not count or not members:
                raise ValueError('No US road-route memberships: refusing to publish an empty index')
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Route database failed its integrity check')
        digest = hashlib.sha256()
        archive = Path(tmp) / 'routes.gz'
        with database.open('rb') as src, archive.open('wb') as dst:
            with gzip.GzipFile(filename='', fileobj=dst, mode='wb', compresslevel=9, mtime=0) as gz:
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    gz.write(chunk)
        sha = digest.hexdigest()
        name = f'route_relations-{sha}.sqlite.gz'
        manifest = dict(format=1, built=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        source=str(source), license='ODbL-1.0', attribution='© OpenStreetMap contributors',
                        file=name, database_sha256=sha, database_bytes=database.stat().st_size,
                        relations=count, members=members, ways=ways, networks=dict(sorted(networks.items())))
        archive.replace(output / name)
        manifest_path = Path(tmp) / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        manifest_path.replace(output / 'manifest.json')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('planet')
    parser.add_argument('out')
    args = parser.parse_args()
    print(json.dumps(build(args.planet, args.out), indent=2))
