"""tools/r2prune.py on a synthetic bucket: what the weekly prune keeps and deletes, and its refusals."""
import datetime
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import r2prune as rp  # noqa: E402

NOW = datetime.datetime(2026, 11, 27, 12, 0, tzinfo=datetime.timezone.utc)
GB = 10 ** 9


def ago(days=0, hours=0):
    return NOW - datetime.timedelta(days=days, hours=hours)


class Fake:
    """An in-memory bucket: add(key, size, mtime), list/delete as rp.S3."""

    def __init__(self):
        self.o = {}

    def add(self, key, size=GB, mtime=None):
        self.o[key] = rp.Obj(key, size, mtime or ago(10))

    def list(self, prefix, delimiter=None):
        objs, prefs = [], set()
        for k in sorted(self.o):
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if delimiter and delimiter in rest:
                prefs.add(prefix + rest.split(delimiter)[0] + delimiter)
            else:
                objs.append(self.o[k])
        return objs, sorted(prefs)

    def delete(self, keys):
        for k in keys:
            del self.o[k]


def tags(fake, ts, parts=3, size=GB, age=10):
    for i in range(parts):
        fake.add('osmtags/%s/osmtags-%03d.pmtiles' % (ts, i), size, ago(age))


def pages(url_by_ds):
    def fetch(url):
        ds = url[len(rp.PAGES):].split('/')[0]
        if ds not in url_by_ds:
            raise OSError('404')
        return {'parts': [{'archive': rp.PUBLIC + u} for u in url_by_ds[ds]]}
    return fetch


def verdict(plan, folder):
    return [r[2] for r in plan.rows if r[1] == folder][0]


class PackTest(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        for d in range(1, 8):
            tags(self.f, '2026_11_%02dT10_00_00Z' % d, age=27 - d)

    def plan(self, live, **kw):
        fetch = pages({'osmtags': [live + 'osmtags-000.pmtiles']})
        return rp.build_plan(self.f, {}, fetch=fetch, now=NOW, **kw)

    def test_keeps_newest_three_and_deletes_the_rest(self):
        p = self.plan('osmtags/2026_11_07T10_00_00Z/')
        for d in (1, 2, 3, 4):
            self.assertEqual(verdict(p, 'osmtags/2026_11_%02dT10_00_00Z/' % d), 'delete')
        for d in (5, 6, 7):
            self.assertEqual(verdict(p, 'osmtags/2026_11_%02dT10_00_00Z/' % d), 'keep')
        self.assertEqual(len(p.delete_keys), 12)

    def test_pointer_to_an_old_snapshot_is_kept(self):
        p = self.plan('osmtags/2026_11_02T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_11_02T10_00_00Z/'), 'keep')
        self.assertEqual(verdict(p, 'osmtags/2026_11_01T10_00_00Z/'), 'delete')
        self.assertEqual(verdict(p, 'osmtags/2026_11_03T10_00_00Z/'), 'delete')

    def test_snapshot_in_progress_and_newer_is_kept_and_does_not_count(self):
        self.f.o.pop('osmtags/2026_11_07T10_00_00Z/osmtags-002.pmtiles')      # a gap at the end: 2 of 3 parts
        tags(self.f, '2026_11_27T11_00_00Z', parts=1, age=0)                  # uploading now
        p = self.plan('osmtags/2026_11_06T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_11_27T11_00_00Z/'), 'keep')
        self.assertEqual(verdict(p, 'osmtags/2026_11_07T10_00_00Z/'), 'keep')   # newer than the newest complete (06)
        self.assertEqual(verdict(p, 'osmtags/2026_11_03T10_00_00Z/'), 'delete')
        self.assertEqual(verdict(p, 'osmtags/2026_11_04T10_00_00Z/'), 'keep')   # 3 newest complete: 06, 05, 04

    def test_part_missing_in_the_middle_is_incomplete(self):
        del self.f.o['osmtags/2026_11_07T10_00_00Z/osmtags-001.pmtiles']
        p = self.plan('osmtags/2026_11_06T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_11_07T10_00_00Z/'), 'keep')

    def test_too_few_complete_deletes_nothing(self):
        f = Fake()
        tags(f, '2026_11_01T10_00_00Z')
        tags(f, '2026_11_02T10_00_00Z')
        p = rp.build_plan(f, {}, fetch=pages({'osmtags': ['osmtags/2026_11_02T10_00_00Z/osmtags-000.pmtiles']}), now=NOW)
        self.assertEqual(p.delete_keys, [])

    def test_a_snapshot_still_being_written_is_not_complete(self):
        tags(self.f, '2026_11_26T10_00_00Z', age=0)                           # all parts present, written now
        p = self.plan('osmtags/2026_11_07T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_11_26T10_00_00Z/'), 'keep')
        self.assertEqual(verdict(p, 'osmtags/2026_11_04T10_00_00Z/'), 'delete')

    def test_small_build_is_incomplete(self):
        tags(self.f, '2026_11_26T10_00_00Z', parts=1, size=1000, age=1)
        p = self.plan('osmtags/2026_11_07T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_11_26T10_00_00Z/'), 'keep')   # newer than the newest complete
        self.assertEqual(verdict(p, 'osmtags/2026_11_04T10_00_00Z/'), 'delete')

    def test_unreadable_pointer_keeps_everything(self):
        p = rp.build_plan(self.f, {}, fetch=pages({}), now=NOW)
        self.assertEqual(p.delete_keys, [])

    def test_old_incomplete_junk_goes_after_a_week_only(self):
        self.f.add('osmtags/2026_10_01T10_00_00Z/osmtags-000.pmtiles', 1000, ago(40))
        self.f.add('osmtags/2026_11_02T09_00_00Z/osmtags-001.pmtiles', GB, ago(2))   # no part 000, 2 days old
        p = self.plan('osmtags/2026_11_07T10_00_00Z/')
        self.assertEqual(verdict(p, 'osmtags/2026_10_01T10_00_00Z/'), 'delete')
        self.assertEqual(verdict(p, 'osmtags/2026_11_02T09_00_00Z/'), 'keep')


class KeyedAndDataTest(unittest.TestCase):
    def test_keyed_pack_and_data_datasets(self):
        f = Fake()
        for i, d in enumerate(('20261110', '20261112', '20261114', '20261116', '20261118')):
            f.add('tallbld/%sT010101Z-abcdef%02d/tallbld-000.pmtiles' % (d, i), GB, ago(20 - i))
        for i in range(5):
            f.add('data/rail/2026_11_%02dT01_00_00Z-%012x/manifest.json' % (10 + i, i), 10, ago(10))
            f.add('data/rail/2026_11_%02dT01_00_00Z-%012x/1/2.json.gz' % (10 + i, i), 100, ago(10))
        f.add('data/rail/2026_11_20T01_00_00Z-%012x/1/2.json.gz' % 9, 100, ago(0))          # no manifest yet
        f.add('data/lod2/2026_11_09-ab3874818bed/manifest.json', 10)
        f.add('data/regional/c02f3d4300959998-e9ed98fb404e/manifest.json', 10)
        f.add('s2wc/v1/3/1/1.webp', 5)
        f.add('dec/d2/797768b8c71a828aa66065c4e1eafbda/x', 5)
        f.add('zzz/other', 5)
        fetch = pages({'tallbld': ['tallbld/20261118T010101Z-abcdef04/tallbld-000.pmtiles'],
                       'rail': ['data/rail/2026_11_14T01_00_00Z-%012x/' % 4],
                       'lod2': ['data/lod2/2026_11_09-ab3874818bed/'],
                       'regional': ['data/regional/c02f3d4300959998-e9ed98fb404e/']})
        p = rp.build_plan(f, {}, fetch=fetch, now=NOW)
        self.assertEqual(verdict(p, 'tallbld/20261110T010101Z-abcdef00/'), 'delete')
        self.assertEqual(verdict(p, 'tallbld/20261112T010101Z-abcdef01/'), 'delete')
        self.assertEqual(verdict(p, 'tallbld/20261114T010101Z-abcdef02/'), 'keep')
        self.assertEqual(verdict(p, 'data/rail/2026_11_10T01_00_00Z-%012x/' % 0), 'delete')
        self.assertEqual(verdict(p, 'data/rail/2026_11_11T01_00_00Z-%012x/' % 1), 'delete')
        self.assertEqual(verdict(p, 'data/rail/2026_11_20T01_00_00Z-%012x/' % 9), 'keep')
        self.assertEqual(verdict(p, 'data/regional/c02f3d4300959998-e9ed98fb404e/'), 'report')
        self.assertEqual(verdict(p, 'data/lod2/2026_11_09-ab3874818bed/'), 'keep')
        self.assertEqual(verdict(p, 's2wc/'), 'report')
        self.assertEqual(verdict(p, 'zzz/'), 'report')
        for k in p.delete_keys:
            self.assertTrue(rp.safe_key(k), k)
        self.assertEqual(len(p.delete_keys), 2 + 4)


class VersionedTest(unittest.TestCase):
    def bnwt(self, f, rel, age):
        f.add('bnwt2/%s/base.c1.index.json' % rel, 10, ago(age))
        f.add('bnwt2/%s/base.c1.s4096-0001.p000.pmtiles' % rel, GB, ago(age))
        f.add('bnwt2/%s/_build/x' % rel, 5, ago(age))

    def test_two_versions_nothing_deleted(self):
        f = Fake()
        self.bnwt(f, '2026-10-21.0', 40)
        self.bnwt(f, '2026-11-18.0', 9)
        p = rp.build_plan(f, {}, fetch=pages({}), now=NOW)
        self.assertEqual(p.delete_keys, [])

    def test_old_version_deleted_only_with_a_14_day_newer_one(self):
        f = Fake()
        for rel, age in (('2026-08-19.0', 100), ('2026-09-17.0', 70), ('2026-10-21.0', 40), ('2026-11-18.0', 9)):
            self.bnwt(f, rel, age)
        p = rp.build_plan(f, {}, fetch=pages({}), now=NOW)
        self.assertEqual(verdict(p, 'bnwt2/2026-08-19.0/'), 'delete')
        self.assertEqual(verdict(p, 'bnwt2/2026-09-17.0/'), 'delete')
        self.assertEqual(verdict(p, 'bnwt2/2026-10-21.0/'), 'keep')
        self.assertEqual(verdict(p, 'bnwt2/2026-11-18.0/'), 'keep')
        # the newest is 9 days old: the third newest, if it were deletable, needs a 14-day-old newer one
        f2 = Fake()
        for rel, age in (('2026-09-17.0', 70), ('2026-10-21.0', 8), ('2026-11-18.0', 3)):
            self.bnwt(f2, rel, age)
        p2 = rp.build_plan(f2, {}, fetch=pages({}), now=NOW)
        self.assertEqual(p2.delete_keys, [])

    def test_cdn_json_and_the_app_floor_protect(self):
        f = Fake()
        for rel, age in (('2026-08-19.0', 100), ('2026-09-17.0', 70), ('2026-10-21.0', 40), ('2026-11-18.0', 9)):
            self.bnwt(f, rel, age)
        p = rp.build_plan(f, {'overture': {'release': '2026-08-19.0'}}, fetch=pages({}), now=NOW)
        self.assertEqual(verdict(p, 'bnwt2/2026-08-19.0/'), 'keep')
        self.assertEqual(verdict(p, 'bnwt2/2026-09-17.0/'), 'delete')
        p = rp.build_plan(f, {}, floor='2026-09-17.0', fetch=pages({}), now=NOW)
        self.assertEqual(verdict(p, 'bnwt2/2026-09-17.0/'), 'keep')
        self.assertEqual(verdict(p, 'bnwt2/2026-08-19.0/'), 'delete')

    def test_unmarked_and_incomplete_versions_are_kept(self):
        f = Fake()
        for rel, age in (('2026-10-21.0', 40), ('2026-11-18.0', 30), ('2026-11-25.0', 20)):
            self.bnwt(f, rel, age)
        f.add('bnwt2/2026-09-01.0/base.c1.s4096-0001.p000.pmtiles', GB, ago(90))      # no index: not complete
        p = rp.build_plan(f, {}, fetch=pages({}), now=NOW)
        self.assertEqual(verdict(p, 'bnwt2/2026-09-01.0/'), 'report')
        self.assertEqual(verdict(p, 'bnwt2/2026-10-21.0/'), 'delete')

    def test_dem_and_cliff_follow_cdn_json(self):
        f = Fake()
        f.add('dem/mapterhorn/0.0.12/release.json', 10, ago(60))
        f.add('dem/mapterhorn/0.0.13/release.json', 10, ago(30))
        f.add('dem/mapterhorn/0.0.14/release.json', 10, ago(20))
        f.add('cliff/4k/v1/manifest.json', 10, ago(60))
        cdn = {'dem': {'bngmt': {'release': '0.0.12'}}, 'cliffs': {'base': 'https://tiles.tgforever.net/cliff/4k/v1/'}}
        p = rp.build_plan(f, cdn, fetch=pages({}), now=NOW)
        self.assertEqual(p.delete_keys, [])
        self.assertEqual(verdict(p, 'dem/mapterhorn/0.0.12/'), 'keep')

    def test_safe_key_refuses_everything_else(self):
        for k in ('osmtags/', 'osmtags/x', 'osmtags/2026_11_01T10_00_00Z', 'data/rail/manifest.json', 's2wc/v1/1/1',
                  'bnwt2/2026-10-21.0', 'cdn.json', 'dec/d2/aa/b', 'dem/mapterhorn/0.0.13'):
            self.assertFalse(rp.safe_key(k), k)
        self.assertTrue(rp.safe_key('bnwt2/2026-10-21.0/x.json'))
        self.assertTrue(rp.safe_key('osmtags/2026_11_01T10_00_00Z/osmtags-000.pmtiles'))


class MainTest(unittest.TestCase):
    def run_main(self, fake, args):
        import tempfile
        orig = rp.S3
        rp.S3 = lambda: fake
        try:
            with tempfile.TemporaryDirectory() as d:
                cdn = Path(d) / 'cdn.json'
                cdn.write_text(json.dumps({}))
                real_fetch, real_now = rp.fetch_json, rp.now_utc
                rp.fetch_json = lambda u: {'parts': [{'archive': rp.PUBLIC + 'osmtags/2026_11_07T10_00_00Z/osmtags-000.pmtiles'}]}
                rp.build_plan.__defaults__ = (None, rp.fetch_json, None)
                try:
                    return rp.main(['--cdn', str(cdn), '--summary', str(Path(d) / 's.md')] + args)
                finally:
                    rp.fetch_json = real_fetch
                    rp.build_plan.__defaults__ = (None, real_fetch, None)
        finally:
            rp.S3 = orig

    def bucket(self):
        f = Fake()
        for d in range(1, 8):
            tags(f, '2026_11_%02dT10_00_00Z' % d, size=GB, age=300)
        return f

    def test_dry_run_deletes_nothing_and_cap_refuses(self):
        f = self.bucket()
        n = len(f.o)
        self.assertEqual(self.run_main(f, []), 0)
        self.assertEqual(len(f.o), n)
        self.assertEqual(self.run_main(f, ['--delete', '--max-delete-gb', '5']), 1)
        self.assertEqual(len(f.o), n)
        self.assertEqual(self.run_main(f, ['--delete', '--max-delete-gb', '20']), 0)
        self.assertEqual(len(f.o), n - 12)


if __name__ == '__main__':
    unittest.main()
