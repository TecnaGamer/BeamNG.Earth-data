import datetime
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import overture_ci as ci

ROOT = Path(__file__).resolve().parents[1]
APP = 'a' * 40
DEV = 'b' * 40


class FlipTest(unittest.TestCase):
    CDN = ('{"imagery": {"bngs2": {"enabled": true, "max_zoom": 13}}, "dem": {"bngmt": {"enabled": true, '
           '"release": "0.0.13"}}, "decisions": {"enabled": true, "request": false}}')

    def test_adds_the_key_and_keeps_the_rest_byte_identical(self):
        new = ci.flip_text(self.CDN + '\n', '2026-12-16.0')
        self.assertTrue(new.startswith(self.CDN[:-1]))
        self.assertTrue(new.endswith(', "overture": {"release": "2026-12-16.0"}}\n'))
        self.assertEqual(json.loads(new)['overture'], {'release': '2026-12-16.0'})

    def test_replaces_only_the_overture_value(self):
        text = '{"a": 1,\n "overture": {"release": "2026-11-18.0", "x": [1]},\n "b": {"c": "d"}}'
        new = ci.flip_text(text, '2026-12-16.0')
        self.assertEqual(new, '{"a": 1,\n "overture": {"release": "2026-12-16.0", "x": [1]},\n "b": {"c": "d"}}')
        self.assertIs(ci.flip_text(new, '2026-12-16.0'), new)

    def test_never_backwards_and_never_a_bad_id(self):
        text = '{"overture": {"release": "2026-12-16.0"}}'
        with self.assertRaises(SystemExit):
            ci.flip_text(text, '2026-11-18.0')
        with self.assertRaises(SystemExit):
            ci.flip_text(text, '2026-12-16.0; rm -rf /')
        with self.assertRaises(SystemExit):
            ci.flip_text('{"overture": "2026-11-18.0"}', '2026-12-16.0')
        self.assertIn('2026-12-16.10', ci.flip_text('{"overture": {"release": "2026-12-16.9"}}', '2026-12-16.10'))

    def test_empty_object(self):
        self.assertEqual(json.loads(ci.flip_text('{}', '2026-12-16.0')), {'overture': {'release': '2026-12-16.0'}})


class DetectTest(unittest.TestCase):
    def run_detect(self, cdn, app_rel='2026-11-18.0', dev=True, cand=None, want='', listed=None, latest='2026-12-16.0',
                   floor=None):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'cdn.json').write_text(json.dumps(cdn))
            (t / 'app.py').write_text(f'x = 1\nRELEASE = "{app_rel}"\nSUPPORTED_SCHEMAS = ("2.0.0", "2.1.0")\n')
            if floor:
                (t / 'floor.py').write_text(f'RELEASE = "{floor}"\n')
            if dev:
                (t / 'dev.py').write_text('RELEASE = "2026-11-18.0"\n')
            if cand is not None:
                (t / 'cand.json').write_text(json.dumps(cand))
            listed = listed if listed is not None else {'2026-10-21.0', '2026-11-18.0', '2026-12-16.0'}
            return ci.detect(t / 'cdn.json', t / 'app.py', t / 'dev.py', APP, DEV, t / 'cand.json', want,
                             listing=(latest, listed), floor_pick=t / 'floor.py' if floor else None)

    def test_new_release_over_the_apps_own(self):
        out = self.run_detect({})
        self.assertEqual((out['state'], out['release'], out['served'], out['served_listed']),
                         ('new', '2026-12-16.0', '2026-11-18.0', 'true'))
        self.assertEqual(json.loads(out['codes']), ['app', 'dev'])
        self.assertEqual(out['schemas'], '2.0.0,2.1.0')

    def test_never_below_the_latest_published_release(self):
        out = self.run_detect({}, app_rel='2026-10-21.0', floor='2026-12-16.0')     # (an older app_ref)
        self.assertEqual((out['state'], out['served']), ('none', '2026-12-16.0'))
        out = self.run_detect({}, app_rel='2026-10-21.0', floor='2026-11-18.0')
        self.assertEqual((out['state'], out['served']), ('new', '2026-11-18.0'))

    def test_nothing_when_served_is_latest_or_newer(self):
        self.assertEqual(self.run_detect({'overture': {'release': '2026-12-16.0'}})['state'], 'none')
        self.assertEqual(self.run_detect({}, app_rel='2026-12-16.0')['state'], 'none')
        out = self.run_detect({'overture': {'release': '2026-12-16.0'}}, want='2026-11-18.0')
        self.assertEqual(out['state'], 'none')

    def test_published_candidate_goes_to_the_gate(self):
        cand = {'release': '2026-12-16.0', 'app_sha': APP, 'published_at': '2026-12-17T07:00:00Z'}
        out = self.run_detect({}, cand=cand)
        self.assertEqual((out['state'], out['published_at']), ('published', '2026-12-17T07:00:00Z'))
        out = self.run_detect({}, cand=dict(cand, app_sha=DEV))      # (another app release since: again)
        self.assertEqual(out['state'], 'new')

    def test_refusals(self):
        with self.assertRaises(SystemExit):          # (not listed)
            self.run_detect({}, want='2026-12-23.0')
        with self.assertRaises(SystemExit):          # (not a strict id)
            self.run_detect({}, latest='2026-12-16')
        with self.assertRaises(SystemExit):          # (cdn.json's key broken)
            self.run_detect({'overture': {'release': 'latest'}})
        with self.assertRaises(SystemExit):          # (dev cannot run another release)
            self.run_detect({}, dev=False)

    def test_an_app_without_the_key_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'cdn.json').write_text('{}')
            out = ci.detect(t / 'cdn.json', t / 'none.py', t / 'none.py', APP, DEV, listing=('2026-12-16.0', set()))
            self.assertEqual(out['state'], 'none')


class GateTest(unittest.TestCase):
    NAMES = json.dumps({'base': 'base.c1', 'places': 'places.c1', 'buildings': 'buildings.c1',
                        'transportation': 'transportation.c1'})
    ARCH = ('base.c1', 'places.c1', 'buildings.c1', 'transportation.c1')

    def fetch_with(self, answers):
        def fetch(url, max_bytes):
            return answers.get(url, (404, b''))
        return fetch

    def answers(self, tall='2026-12-16.0', dec=True, bnwt_old=False, bnwt_new=False, schema='2.0.0'):
        a = {f'{ci.PAGES}/tallbld/manifest.json': (200, json.dumps({'release': tall, 'parts': [{}]}).encode()),
             f'{ci.STAC}/catalog.json': (200, json.dumps({'links': [
                 {'rel': 'child', 'href': f'{ci.STAC}/2026-12-16.0/catalog.json'}]}).encode()),
             f'{ci.STAC}/2026-12-16.0/catalog.json': (200, json.dumps({
                 'release:version': '2026-12-16.0', 'schema:version': schema}).encode())}
        if dec:
            a[f'{ci.CDN}/dec/d2/{"c" * 32}/manifest.bngm'] = (200, b'BNGM....')
        for rel, on in (('2026-11-18.0', bnwt_old), ('2026-12-16.0', bnwt_new)):
            if on:
                for n in self.ARCH:
                    a[f'{ci.CDN}/bnwt2/{rel}/{n}.index.json'] = (200, b'{}')
        return a

    def gate(self, answers, schema='true', at='', now=None):
        return ci.gate('2026-12-16.0', '2026-11-18.0', 'c' * 32, '2', '2', self.NAMES, schema, at, 10.0,
                       now=now, fetch=self.fetch_with(answers), schemas=['2.0.0'])

    def test_ready_only_with_every_gate(self):
        self.assertEqual(self.gate(self.answers())[:2], (True, []))
        self.assertEqual(self.gate(self.answers(tall='2026-11-18.0'))[:2], (False, ['tallbld']))
        self.assertEqual(self.gate(self.answers(dec=False))[:2], (False, ['decisions']))
        self.assertEqual(self.gate(self.answers(), schema='false')[:2], (False, ['schema']))
        self.assertEqual(self.gate(self.answers(bnwt_old=True))[:2], (False, ['bnwt']))
        self.assertEqual(self.gate(self.answers(bnwt_old=True, bnwt_new=True))[:2], (True, []))
        self.assertEqual(self.gate(self.answers(schema='3.0.0'))[:2], (False, ['stac']))

    def test_no_answer_is_not_ready(self):
        a = self.answers()
        a[f'{ci.CDN}/bnwt2/2026-11-18.0/base.c1.index.json'] = (0, 'timeout')
        self.assertEqual(self.gate(a)[:2], (False, ['bnwt']))
        a = self.answers()
        a[f'{ci.CDN}/bnwt2/2026-11-18.0/base.c1.index.json'] = (403, b'')     # (a denial is not "not built")
        self.assertEqual(self.gate(a)[:2], (False, ['bnwt']))
        a = self.answers()
        a[f'{ci.CDN}/dec/d2/{"c" * 32}/manifest.bngm'] = (200, b'<html>')
        self.assertEqual(self.gate(a)[:2], (False, ['decisions']))

    def test_stuck_after_ten_days(self):
        now = datetime.datetime(2026, 12, 30, tzinfo=datetime.timezone.utc)
        self.assertTrue(self.gate(self.answers(dec=False), at='2026-12-17T07:00:00Z', now=now)[2])
        self.assertFalse(self.gate(self.answers(dec=False), at='2026-12-25T07:00:00Z', now=now)[2])
        self.assertFalse(self.gate(self.answers(), at='2026-12-17T07:00:00Z', now=now)[2])

    def test_bad_inputs(self):
        with self.assertRaises(SystemExit):
            ci.gate('2026-12-16.0', '2026-11-18.0', '../x', '2', '2', self.NAMES, 'true', schemas=['2.0.0'])
        with self.assertRaises(SystemExit):
            ci.gate('2026-12-16.0', '2026-11-18.0', 'c' * 32, '2', '2', '{"base": "../x"}', 'true', schemas=['2.0.0'])
        with self.assertRaises(SystemExit):     # (an incomplete BNWT set would skip the missing archives)
            ci.gate('2026-12-16.0', '2026-11-18.0', 'c' * 32, '2', '2', '{"base": "base.c1"}', 'true',
                    schemas=['2.0.0'])


class StacTest(unittest.TestCase):
    def write(self, tmp, release, doc):
        p = Path(tmp) / f'overture_stac_{release}.json.gz'
        with gzip.open(p, 'wt') as f:
            json.dump(doc, f)
        return str(p)

    def test_the_committed_files_pass(self):
        files = ci.stac_files(str(ROOT / 'tools'))
        self.assertTrue(files)
        for rel, path in files.items():
            ci.check_stac(path, rel)

    def test_foreign_urls_and_empty_collections_fail(self):
        rel = '2026-12-16.0'
        name = 'part-00000-972f15b4-c2b5-59d2-b8c1-4d3eda61bff1-c000.zstd.parquet'
        good = f'https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/{rel}/theme=base/type=water/{name}'
        row = [name, [-1.0, -1.0, 1.0, 1.0], [good], 10]
        with tempfile.TemporaryDirectory() as tmp:
            ok = self.write(tmp, rel, {'release': rel, 'collections': {'base/water': {'files': [row]}}})
            ci.check_stac(ok, rel, upstream={'base/water': 1})
            with self.assertRaises(SystemExit):
                ci.check_stac(ok, rel, upstream={'base/water': 2})
            with self.assertRaises(SystemExit):
                ci.check_stac(self.write(tmp, rel, {'release': rel, 'collections': {'base/water': {'files': [row, row]}}}),
                              rel)
            for bad in ({'release': rel, 'collections': {'base/water': {'files': []}}},
                        {'release': '2026-11-18.0', 'collections': {'base/water': {'files': [row]}}},
                        {'release': rel, 'collections': {'base/water': {'files': [
                            [name, row[1], ['https://evil.example/release/x/' + name], 10]]}}},
                        {'release': rel, 'collections': {'base/water': {'files': [
                            [name, row[1], [good.replace('type=water', 'type=land')], 10]]}}}):
                with self.assertRaises(SystemExit):
                    ci.check_stac(self.write(tmp, rel, bad), rel)


class CorpusTest(unittest.TestCase):
    def test_counts_bounds(self):
        old = {'places': {'p': {'cells': ['14/1/1'], 'layers': {'roads': 1000, 'building_parts': 3}}}}
        ok = {'places': {'p': {'cells': ['14/1/1'], 'layers': {'roads': 1080, 'building_parts': 0}}}}
        self.assertEqual(ci.compare_counts(['p'], old, ok)[1], [])
        drop = {'places': {'p': {'cells': ['14/1/1'], 'layers': {'roads': 960, 'building_parts': 3}}}}
        self.assertEqual(len(ci.compare_counts(['p'], old, drop)[1]), 1)
        grow = {'places': {'p': {'cells': ['14/1/1'], 'layers': {'roads': 1200, 'building_parts': 3}}}}
        self.assertEqual(len(ci.compare_counts(['p'], old, grow)[1]), 1)

    def test_corpus_run_needs_every_place_on_the_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'plans' / 'a').mkdir(parents=True)
            (t / 'plans' / 'a' / 'index.json').write_text(json.dumps({'versions': {'overture': '2026-12-16.0'}}))
            (t / 'build-a.log').write_text('a: 4 cells asked, 16 cells planned (13 complete), 100 facts, 50 s\n')
            (t / 'build-b.log').write_text('b: cell 14/1/2: ValueError: x\n')
            (t / 'log.txt').write_text('a: 4 cells asked, 16 cells planned (13 complete), 100 facts, 50 s\n'
                                       'b: no result (exit 1, see build-b.log)\n')
            (t / 'c.json').write_text(json.dumps({'release': '2026-12-16.0', 'places': {
                'a': {'layers': {}, 'errors': []}, 'b': {'layers': {}, 'errors': ['14/1/2: OSError: gone']}}}))
            runs, counts, problems = ci.corpus_run(['a', 'b'], '2026-12-16.0', str(t), str(t / 'log.txt'),
                                                   str(t / 'c.json'))
            self.assertEqual(list(runs), ['a'])
            self.assertEqual(len(problems), 2)       # (b: no result line; b: a count error)
            runs, counts, problems = ci.corpus_run(['a'], '2026-11-18.0', str(t), str(t / 'log.txt'),
                                                   str(t / 'c.json'))
            self.assertTrue(any('planned on Overture 2026-12-16.0' in p for p in problems))



class NewLayerTest(unittest.TestCase):
    def test_a_layer_without_a_baseline_is_listed_not_bounded(self):
        old = {"places": {"p": {"cells": ["14/1/1"], "layers": {"roads": 100}}}}
        new = {"places": {"p": {"cells": ["14/1/1"], "layers": {"roads": 100, "land": 5000}}}}
        lines, problems = ci.compare_counts(["p"], old, new)
        self.assertEqual(problems, [])
        self.assertTrue(any("land" in ln and "new" in ln for ln in lines))

    def test_a_layer_counted_before_and_missing_now_fails(self):
        old = {"places": {"p": {"cells": ["14/1/1"], "layers": {"roads": 100, "land": 5000}}}}
        new = {"places": {"p": {"cells": ["14/1/1"], "layers": {"roads": 100}}}}
        lines, problems = ci.compare_counts(["p"], old, new)
        self.assertEqual(problems, ["p: land counted before, missing now"])

if __name__ == '__main__':
    unittest.main()


class SyncKeysTest(unittest.TestCase):
    """cdn.json's bnwt / cip keys follow the overture key once the build of the new release is on the tile host."""
    NAMES = json.dumps({a: f'{a}.c1' for a in sorted(ci.BNWT_ARCHIVES)}, separators=(',', ':'))
    OLD, NEW = '2026-11-18.0', '2026-12-16.0'
    BUILD = '0123456789abcdef'
    CDN = ('{"imagery": {"bngs2": {"enabled": true, "max_zoom": 13}},\n "overture": {"release": "2026-11-18.0"},\n'
           ' "bnwt": {"enabled": true, "release": "2026-11-18.0"}, "cip": {"enabled": true, "release": "2026-11-18.0"},\n'
           ' "decisions": {"enabled": true, "request": false}}\n')

    def fetcher(self, bnwt=True, cip=True, latest=None, index=None, missing=()):
        base = ci.CDN
        files = {}
        if bnwt:
            for n in json.loads(self.NAMES).values():
                files[f'{base}/bnwt2/{self.NEW}/{n}.index.json'] = b'{}'
        if cip:
            files[f'{base}/cip1/{self.NEW}/latest.json'] = latest if latest is not None else \
                json.dumps({'build': self.BUILD}).encode()
            files[f'{base}/cip1/{self.NEW}/{self.BUILD}/cells.index.json'] = index if index is not None else \
                json.dumps({'release': self.NEW, 'build': self.BUILD, 'format': 'cip'}).encode()
        for m in missing:
            files.pop(m, None)
        calls = []

        def fetch(url, max_bytes, tries=3):
            calls.append(url)
            return (200, files[url]) if url in files else (404, b'')
        fetch.calls = calls
        return fetch

    def sync(self, text, fetch):
        return ci.sync_keys(text, self.NEW, '2', self.NAMES, '1', fetch=fetch)

    def test_both_published(self):
        new, rep = self.sync(self.CDN, self.fetcher())
        self.assertEqual(new, self.CDN.replace(self.OLD, self.NEW).replace(
            '"overture": {"release": "2026-12-16.0"}', '"overture": {"release": "2026-11-18.0"}'))
        d = json.loads(new)
        self.assertEqual((d['bnwt']['release'], d['cip']['release'], d['overture']['release']),
                         (self.NEW, self.NEW, self.OLD))
        self.assertTrue(d['bnwt']['enabled'] and d['cip']['enabled'])

    def test_one_published_one_missing(self):
        new, rep = self.sync(self.CDN, self.fetcher(cip=False))
        d = json.loads(new)
        self.assertEqual((d['bnwt']['release'], d['cip']['release']), (self.NEW, self.OLD))
        self.assertTrue(any('WAITING: cip' in r for r in rep))
        new, rep = self.sync(self.CDN, self.fetcher(bnwt=False))
        d = json.loads(new)
        self.assertEqual((d['bnwt']['release'], d['cip']['release']), (self.OLD, self.NEW))
        self.assertTrue(any('WAITING: bnwt' in r for r in rep))

    def test_one_bnwt_archive_missing_is_not_published(self):
        gone = f'{ci.CDN}/bnwt2/{self.NEW}/places.c1.index.json'
        new, rep = self.sync(self.CDN, self.fetcher(missing=(gone,)))
        self.assertEqual(json.loads(new)['bnwt']['release'], self.OLD)

    def test_cip_without_its_cells_index_is_not_published(self):
        gone = f'{ci.CDN}/cip1/{self.NEW}/{self.BUILD}/cells.index.json'
        new, rep = self.sync(self.CDN, self.fetcher(missing=(gone,)))
        self.assertEqual(json.loads(new)['cip']['release'], self.OLD)

    def test_neither_published_leaves_the_text_identical(self):
        new, rep = self.sync(self.CDN, self.fetcher(bnwt=False, cip=False))
        self.assertEqual(new, self.CDN)

    def test_absent_or_disabled_keys_are_never_enabled_or_added(self):
        text = '{"overture": {"release": "2026-11-18.0"}, "bnwt": {"enabled": false, "release": "2026-11-18.0"}}'
        f = self.fetcher()
        new, rep = self.sync(text, f)
        self.assertEqual(new, text)
        self.assertEqual(f.calls, [])           # (nothing to move: the host is not even asked)
        self.assertTrue(any('not enabled' in r for r in rep) and any('no such key' in r for r in rep))
        text = '{"bnwt": {"release": "2026-11-18.0"}, "cip": {"enabled": "yes", "release": "2026-11-18.0"}}'
        self.assertEqual(self.sync(text, self.fetcher())[0], text)

    def test_never_backwards_and_same_release_left(self):
        text = '{"bnwt": {"enabled": true, "release": "2027-01-20.0"}, "cip": {"enabled": true, "release": "2026-12-16.0"}}'
        self.assertEqual(self.sync(text, self.fetcher())[0], text)

    def test_malformed_responses_count_as_not_published(self):
        for latest, index in ((b'not json', None), (b'[]', None), (b'{"build": "../../x"}', None),
                              (b'{"build": 5}', None), (None, b'not json'), (None, b'[]'),
                              (None, json.dumps({'release': self.OLD, 'build': self.BUILD}).encode()),
                              (None, json.dumps({'release': self.NEW, 'build': 'f' * 16}).encode())):
            new, rep = self.sync(self.CDN, self.fetcher(bnwt=False, latest=latest, index=index))
            self.assertEqual(new, self.CDN, (latest, index))

    def test_no_answer_is_not_published(self):
        def dead(url, max_bytes, tries=3):
            return 0, 'timed out'
        self.assertEqual(self.sync(self.CDN, dead)[0], self.CDN)

    def test_bad_inputs_stop(self):
        for args in (('2026-12-16.0; x', '2', self.NAMES, '1'), (self.NEW, '2/../x', self.NAMES, '1'),
                     (self.NEW, '2', self.NAMES, '1/x'), (self.NEW, '2', '{"base":"base.c1"}', '1'),
                     (self.NEW, '2', '{"base":"../x","transportation":"transportation.c1","buildings":"buildings.c1",'
                      '"places":"places.c1"}', '1')):
            with self.assertRaises(SystemExit):
                ci.sync_keys(self.CDN, *args, fetch=self.fetcher())

    def test_other_keys_byte_identical_odd_spacing(self):
        text = ('{ "a":[1,  2],\n"bnwt" :{"enabled":true,"release":"2026-11-18.0","x":1} ,"z":  {"q" :  null}  ,'
                '"cip":{"enabled":true,"release":"2026-11-18.0"}\n}\n')
        new, _ = self.sync(text, self.fetcher())
        self.assertIn('{ "a":[1,  2],\n"bnwt" :', new)
        self.assertIn(',"z":  {"q" :  null}  ,', new)
        self.assertTrue(new.endswith('\n}\n'))
        d = json.loads(new)
        self.assertEqual(d['bnwt'], {'enabled': True, 'release': self.NEW, 'x': 1})
        self.assertEqual(d['cip'], {'enabled': True, 'release': self.NEW})

    def test_flip_command_moves_all_in_one_edit(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'cdn.json'
            p.write_text(self.CDN)
            real = ci.get
            f = self.fetcher()
            ci.get = f
            try:
                # (sync_keys' default fetch was bound at definition: call it through the command with the host faked)
                orig = ci.sync_keys
                ci.sync_keys = lambda *a, **k: orig(*a, fetch=f, **k)
                a = argparse.Namespace(file=str(p), release=self.NEW, expect_served=self.OLD, bnwt_format='2',
                                       bnwt_names=self.NAMES, cip_format='1')
                self.assertEqual(ci.cmd_flip_cdn(a), 0)
            finally:
                ci.get = real
                ci.sync_keys = orig
            d = json.loads(p.read_text())
            self.assertEqual((d['overture']['release'], d['bnwt']['release'], d['cip']['release']), (self.NEW,) * 3)
            self.assertEqual(d['imagery'], {'bngs2': {'enabled': True, 'max_zoom': 13}})
            # (a second run changes nothing)
            before = p.read_text()
            ci.sync_keys = lambda *a, **k: orig(*a, fetch=f, **k)
            try:
                self.assertEqual(ci.cmd_flip_cdn(a), 0)
            finally:
                ci.sync_keys = orig
            self.assertEqual(p.read_text(), before)

    def test_sync_command_follows_the_served_release(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'cdn.json'
            p.write_text(self.CDN.replace('"overture": {"release": "2026-11-18.0"}', '"overture": {"release": "2026-12-16.0"}'))
            f = self.fetcher()
            orig = ci.sync_keys
            ci.sync_keys = lambda *a, **k: orig(*a, fetch=f, **k)
            try:
                a = argparse.Namespace(file=str(p), release='', bnwt_format='2', bnwt_names=self.NAMES, cip_format='1')
                self.assertEqual(ci.cmd_sync_cdn(a), 0)
            finally:
                ci.sync_keys = orig
            d = json.loads(p.read_text())
            self.assertEqual((d['bnwt']['release'], d['cip']['release']), (self.NEW, self.NEW))
            # no overture key: nothing to follow
            p.write_text('{"bnwt": {"enabled": true, "release": "2026-11-18.0"}}')
            self.assertEqual(ci.cmd_sync_cdn(a), 0)
            self.assertEqual(p.read_text(), '{"bnwt": {"enabled": true, "release": "2026-11-18.0"}}')
