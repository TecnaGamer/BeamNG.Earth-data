"""Standalone data-pipeline checks; also run unchanged in BeamNG.Earth-data."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'tools').is_dir() and (p/'server').is_dir())
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'tools')]
import build_us_service_signs as builder
import serviceindex
import servicefiction
import servicecoverage


def fixture():
    origin = -105., 41.7
    def point(x, y): return origin[0]+x/83000., origin[1]+y/111320.
    elements = []
    for nid, x, y, tags in ((1,-1900,0,{}), (2,-1000,0,{}), (3,0,0,{'highway':'motorway_junction','ref':'7'}),
                             (4,100,-100,{}), (5,200,-200,{}),
                             (6,300,-200,{'amenity':'restaurant','name':'Real Corporate Cafe','brand':'Real Brand'})):
        lon, lat = point(x, y)
        elements.append(dict(type='node', id=nid, lon=lon, lat=lat, tags=tags))
    for wid, refs, tags in ((10,[1,2,3],{'highway':'motorway','oneway':'yes'}),
                            (20,[3,4,5],{'highway':'motorway_link','oneway':'yes'}),
                            (30,[5,6],{'highway':'residential'})):
        elements.append(dict(type='way', id=wid, nodes=refs, tags=tags))
    return dict(elements=elements)


class ServiceDataTest(unittest.TestCase):
    def test_audit_distinguishes_checked_empty_and_excluded(self):
        data = fixture()
        result = serviceindex.build(data)
        self.assertEqual(result['audit'][0]['status'], 'with_services')
        data['elements'][5]['tags'] = {}
        self.assertEqual(serviceindex.build(data)['audit'][0]['status'], 'no_services')
        data['elements'][2]['tags'].pop('ref')
        result = serviceindex.build(data)
        self.assertEqual(result['audit'][0]['reason'], 'unnumbered_exit')
        self.assertEqual(result['objects'], [])

    def test_fictional_identity_is_stable_and_does_not_leak_source_names(self):
        data = fixture()
        result = serviceindex.build(data)
        text = json.dumps(result)
        self.assertNotIn('Real Corporate Cafe', text)
        self.assertNotIn('Real Brand', text)
        business = result['objects'][0]['exit']['services'][0]
        self.assertEqual(business['name_origin'], 'fictional')
        for e in data['elements']: e['tags'].update(name='Changed Name', brand='Changed Brand')
        self.assertEqual(business['name'], serviceindex.build(data)['objects'][0]['exit']['services'][0]['name'])

    def test_atomic_dataset_publication_failure_retains_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output, work = root/'out', root/'work'
            output.mkdir(); work.mkdir()
            sentinel = b'{"built":"previous-good-build"}'
            (output/'manifest.json').write_bytes(sentinel)
            db = builder.connection(root/'network.sqlite')
            try:
                with self.assertRaisesRegex(ValueError, 'No candidate'):
                    builder.build_cells(db, output, work, 'empty', serviceindex.POLICY)
                self.assertEqual((output/'manifest.json').read_bytes(), sentinel)
            finally: db.close()

    def test_real_pbf_import_publication_audit_corruption_and_resume(self):
        import osmium
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pbf = root/'sample.osm.pbf'
            with osmium.SimpleWriter(str(pbf)) as writer:
                for e in fixture()['elements']:
                    if e['type'] == 'node':
                        writer.add_node(osmium.osm.mutable.Node(id=e['id'], location=(e['lon'],e['lat']), tags=e['tags']))
                    else: writer.add_way(osmium.osm.mutable.Way(id=e['id'], nodes=e['nodes'], tags=e['tags']))
            db = builder.connection(root/'network.sqlite')
            out = root/'out'
            try:
                builder.import_pbf(pbf, db, root, 'fixture')
                builder.build_cells(db, out, root, 'fixture', serviceindex.POLICY)
                before = (out/'manifest.json').read_bytes()
                manifest = json.loads(before)
                audit = builder.validate_dataset(out)
                self.assertEqual(audit['statuses']['with_services'], 1)
                self.assertEqual(len([k for k in audit['states'] if len(k)==2]), 51)
                builder.build_cells(db, out, root, 'fixture', serviceindex.POLICY)
                self.assertEqual(before, (out/'manifest.json').read_bytes())
                cell = out/next(iter(manifest['cells'].values()))['path']
                cell.write_bytes(b'corrupt')
                with self.assertRaises(ValueError): builder.validate_dataset(out)
            finally: db.close()

    def test_truncated_pbf_does_not_mark_import_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'truncated.osm.pbf'; path.write_bytes(b'not a PBF')
            db = builder.connection(root/'network.sqlite')
            try:
                with self.assertRaises(RuntimeError): builder.import_pbf(path, db, root, 'broken')
                self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone())
            finally: db.close()


if __name__ == '__main__': unittest.main()
