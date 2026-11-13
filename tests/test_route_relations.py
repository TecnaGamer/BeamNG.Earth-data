import gzip
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_route_relation_index as builder


@unittest.skipUnless(importlib.util.find_spec('osmium'), 'requires build dependency osmium')
class RouteBuildTest(unittest.TestCase):
    def test_pbf_relations_roles_filters_and_reproducible_archive(self):
        import osmium
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml = root / 'input.osm'
            xml.write_text('''<osm version="0.6">
              <relation id="1" version="1"><member type="way" ref="10" role="forward"/>
                <member type="way" ref="10" role="forward"/><member type="way" ref="11" role="backward"/>
                <member type="node" ref="99" role=""/>
                <tag k="type" v="route"/><tag k="route" v="road"/><tag k="network" v="US:WY"/>
                <tag k="ref" v="210"/><tag k="name" v="discarded"/></relation>
              <relation id="2" version="1"><member type="way" ref="10" role=""/>
                <tag k="type" v="route"/><tag k="route" v="road"/><tag k="network" v="US:US"/>
                <tag k="ref" v="30"/></relation>
              <relation id="3" version="1"><member type="way" ref="12" role=""/>
                <tag k="type" v="route"/><tag k="route" v="bicycle"/><tag k="network" v="US:WY"/>
                <tag k="ref" v="1"/></relation>
              <relation id="4" version="1"><member type="way" ref="13" role=""/>
                <tag k="type" v="route"/><tag k="route" v="road"/><tag k="network" v="DE:BAB"/>
                <tag k="ref" v="1"/></relation></osm>''', encoding='utf-8')
            pbf = root / 'input.osm.pbf'
            with osmium.SimpleWriter(str(pbf)) as writer:
                for item in osmium.FileProcessor(str(xml)):
                    writer.add(item)
            out = root / 'out'
            spec = builder.build(pbf, out)
            self.assertEqual((spec['relations'], spec['members'], spec['ways']), (2, 3, 2))
            self.assertEqual(spec['networks'], {'US:US': 1, 'US:WY': 1})
            raw = (out / spec['file']).read_bytes()
            body = gzip.decompress(raw)
            self.assertEqual(hashlib.sha256(body).hexdigest(), spec['database_sha256'])
            database = root / 'check.sqlite'
            database.write_bytes(body)
            with closing(sqlite3.connect(database)) as con:
                self.assertEqual(con.execute('SELECT role FROM members WHERE route_id=1 ORDER BY way_id').fetchall(),
                                 [('forward',), ('backward',)])
                self.assertNotIn('name', json.loads(con.execute('SELECT tags FROM routes WHERE id=1').fetchone()[0]))
            again = builder.build(pbf, out)
            self.assertEqual(raw, (out / again['file']).read_bytes())
            self.assertEqual(json.loads((out / 'manifest.json').read_text())['file'], spec['file'])

    def test_empty_or_truncated_input_does_not_replace_published_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'out'
            output.mkdir()
            manifest = output / 'manifest.json'
            manifest.write_text('previous build')
            for body in ('<osm version="0.6"></osm>', '<osm version="0.6"><relation'):
                source = root / 'bad.osm'
                source.write_text(body)
                with self.assertRaises((ValueError, RuntimeError)):
                    builder.build(source, output)
                self.assertEqual(manifest.read_text(), 'previous build')


if __name__ == '__main__':
    unittest.main()
