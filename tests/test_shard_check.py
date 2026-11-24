"""tools/shard_check.py: the weekly shard datasets checked between their build job and their publish job, keyed as
tools/mirror_data.py keys them, a published folder never overwritten, the manifest last."""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import mirror_data  # noqa: E402
import shard_check as sc  # noqa: E402


def build(art, name='lanes', n=3, built='2026-11-24T03:02:54Z'):
    d = os.path.join(art, name)
    for i in range(n):
        os.makedirs(os.path.join(d, str(i)), exist_ok=True)
        with gzip.open(os.path.join(d, str(i), f'{i + 1}.json.gz'), 'wt') as f:
            json.dump({'ways': {str(i): {'lanes': '2'}}}, f)
    with open(os.path.join(d, 'planet.txt'), 'w') as f:
        f.write('https://example.invalid/planet-261116.osm.pbf\n')
    with open(os.path.join(d, 'manifest.json'), 'w') as f:
        json.dump({'format': 1, 'zoom': 7, 'built': built, 'shards': n}, f)
    return d


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sc.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class ShardCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.art = os.path.join(self.tmp, 'art')
        p = mock.patch.dict(sc.PROFILES, {'lanes': 3})
        p.start()
        self.addCleanup(p.stop)

    def sealed(self, **kw):
        build(self.art, **kw)
        self.assertEqual(run('seal', self.art, 'lanes')[0], 0)

    def listing(self, keys):
        path = os.path.join(self.tmp, 'listing.json')
        with open(path, 'w') as f:
            json.dump({'Contents': [{'Key': k, 'Size': s} for k, s in keys]} if keys else {}, f)
        return path

    def test_seal_check_key(self):
        self.sealed()
        self.assertEqual(run('check', self.art, 'lanes')[0], 0)
        code, out, _ = run('key', self.art, 'lanes')
        self.assertEqual(code, 0)
        key = out.strip()
        self.assertRegex(key, r'^2026_11_24T03_02_54Z-[0-9a-f]{12}$')
        site = os.path.join(self.tmp, 'site')
        shutil.copytree(self.art, site)
        os.remove(os.path.join(site, 'lanes.seal.json'))
        files, _inner = mirror_data.files_of(site, 'lanes', ['lanes'])
        self.assertEqual(mirror_data.key_of(site, 'lanes', files, '2026-11-24T03:02:54Z'), key,
                         "mirror_data's key: a build it mirrored is the same folder")

    def test_two_datasets_in_one_artifact(self):
        """The rail job's artifact: rail and fra, each checked on its own; anything else refused."""
        with mock.patch.dict(sc.PROFILES, {'rail': 3, 'fra': 3}):
            build(self.art, 'rail')
            build(self.art, 'fra')
            self.assertEqual(run('seal', self.art, 'rail')[0], 0)
            self.assertEqual(run('seal', self.art, 'fra')[0], 0)
            self.assertEqual(run('check', self.art, 'rail')[0], 0)
            self.assertEqual(run('check', self.art, 'fra')[0], 0)
            open(os.path.join(self.art, 'evil.sh'), 'w').close()
            self.assertEqual(run('check', self.art, 'rail')[0], 1)

    def test_a_changed_file_after_the_seal_is_refused(self):
        self.sealed()
        with gzip.open(os.path.join(self.art, 'lanes', '1', '2.json.gz'), 'wt') as f:
            json.dump({'ways': {}}, f)
        self.assertEqual(run('check', self.art, 'lanes')[0], 1)

    def test_odd_trees_are_refused(self):
        for bad in ('extra file', 'link', 'short', 'count', 'data', 'not json', 'big xy'):
            with self.subTest(bad):
                shutil.rmtree(self.art, True)
                d = build(self.art)
                if bad == 'extra file':
                    open(os.path.join(d, 'run.sh'), 'w').close()
                elif bad == 'link':
                    os.symlink('/etc/passwd', os.path.join(d, '0', '9.json.gz'))
                elif bad == 'short':
                    shutil.rmtree(self.art)
                    build(self.art, n=2)
                elif bad == 'count':
                    m = json.load(open(os.path.join(d, 'manifest.json')))
                    m['shards'] = 4
                    json.dump(m, open(os.path.join(d, 'manifest.json'), 'w'))
                elif bad == 'data':
                    m = json.load(open(os.path.join(d, 'manifest.json')))
                    m['data'] = 'https://elsewhere.invalid/x'
                    json.dump(m, open(os.path.join(d, 'manifest.json'), 'w'))
                elif bad == 'not json':
                    with gzip.open(os.path.join(d, '0', '1.json.gz'), 'wb') as f:
                        f.write(b'[1, 2]')
                elif bad == 'big xy':
                    os.makedirs(os.path.join(d, '128'))
                    os.rename(os.path.join(d, '2', '3.json.gz'), os.path.join(d, '128', '3.json.gz'))
                self.assertEqual(run('seal', self.art, 'lanes')[0], 1)

    def test_todo_never_overwrites_and_the_manifest_comes_last(self):
        self.sealed()
        key = run('key', self.art, 'lanes')[1].strip()
        pre = f'data/lanes/{key}/'
        code, out, _ = run('todo', self.art, 'lanes', '--listing', self.listing([]))
        lines = out.split('\n')[:-1]
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 4, 'three shards and planet.txt; not the manifest')
        self.assertTrue(all(ln.split()[1].startswith(pre) for ln in lines))
        self.assertFalse(any('manifest.json' in ln for ln in lines))
        size = os.path.getsize(os.path.join(self.art, 'lanes', '0', '1.json.gz'))
        self.assertEqual(run('todo', self.art, 'lanes', '--listing', self.listing([(pre + '0/1.json.gz', size + 1)]))[0],
                         1, 'another size there: refused')
        self.assertEqual(run('todo', self.art, 'lanes', '--listing', self.listing([(pre + '9/9.json.gz', 5)]))[0], 1,
                         'a foreign file there: refused')
        self.assertEqual(run('todo', self.art, 'lanes', '--listing',
                             self.listing([(pre + '0/1.json.gz', size), (pre + 'manifest.json', 9)]))[0], 1,
                         'a whole-copy marker over a part copy: refused')
        got = [(pre + rel, s) for rel, s in ((ln.split()[0][len(self.art) + len('/lanes/'):], None) for ln in lines)]
        full = [(k, os.path.getsize(os.path.join(self.art, 'lanes', k[len(pre):]))) for k, _ in got]
        code, out, _ = run('todo', self.art, 'lanes', '--listing', self.listing(full), '--complete')
        self.assertEqual((code, out.strip()), (0, 'files'))
        code, out, _ = run('todo', self.art, 'lanes', '--listing', self.listing(full + [(pre + 'manifest.json', 9)]),
                           '--complete')
        self.assertEqual((code, out.strip()), (0, 'whole'), 'already whole: the manifest not uploaded again')
        self.assertEqual(run('todo', self.art, 'lanes', '--listing', self.listing(full[:2]), '--complete')[0], 1)

    def test_point_names_the_folder(self):
        self.sealed()
        key = run('key', self.art, 'lanes')[1].strip()
        pub = os.path.join(self.tmp, 'pub')
        self.assertEqual(run('point', self.art, 'lanes', pub)[0], 0)
        m = json.load(open(os.path.join(pub, 'lanes', 'manifest.json')))
        self.assertEqual(m['data'], f'{sc.CDN}/data/lanes/{key}')
        self.assertEqual(m['built'], '2026-11-24T03:02:54Z')
        self.assertTrue(os.path.exists(os.path.join(pub, 'lanes', '1', '2.json.gz')), 'the files for Pages too')
        out = os.path.join(self.tmp, 'remote.json')
        self.assertEqual(run('remote', self.art, 'lanes', out)[0], 0)
        self.assertEqual(json.load(open(out)), m)


class MirrorSkipTest(unittest.TestCase):
    def test_a_published_dataset_is_left_alone(self):
        with tempfile.TemporaryDirectory() as site:
            os.makedirs(os.path.join(site, 'lanes'))
            url = f'{mirror_data.PUBLIC}/data/lanes/2026_11_24T03_02_54Z-0123456789ab'
            with open(os.path.join(site, 'lanes', 'manifest.json'), 'w') as f:
                json.dump({'built': 'x', 'data': url}, f)
            with mock.patch.object(mirror_data, 'aws', side_effect=AssertionError('no upload')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mirror_data.mirror(site, 'lanes', ['lanes']), url)


if __name__ == '__main__':
    unittest.main()
