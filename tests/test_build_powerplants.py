"""tools/build_powerplants.py's weekly checks (the data repo's powerplants workflow runs a pinned copy): an Overpass
answer holds only its band's elements, the main instance only with its back-off, and a build is held for review
when it changes too much against its baseline (overall, per region, by OSM id)."""
import importlib.util
import os
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for root in (os.path.join(HERE, "..", "..", "tools"), os.path.join(HERE, "..", "tools")):
    if os.path.exists(os.path.join(root, "build_powerplants.py")):
        TOOLS = os.path.abspath(root)
        break
spec = importlib.util.spec_from_file_location("build_powerplants", os.path.join(TOOLS, "build_powerplants.py"))
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def index(n_plants=6000, n_structs=12000, seed=1):
    """A synthetic index (the columns compare reads), sorted by latitude."""
    r = np.random.default_rng(seed)
    plat = np.sort(r.integers(-60_000_000, 70_000_000, n_plants)).astype(np.int32)
    slat = np.sort(r.integers(-60_000_000, 70_000_000, n_structs)).astype(np.int32)
    osm = np.where(np.arange(n_plants) % 5 == 0, 0, np.arange(1, n_plants + 1)).astype(np.int64)   # (a fifth: GPPD)
    return {"p_lat": plat, "p_lon": r.integers(-179_000_000, 179_000_000, n_plants).astype(np.int32),
            "p_osm": osm, "p_osm_type": np.ones(n_plants, np.uint8), "p_fuel": (np.arange(n_plants) % 6).astype(np.uint8),
            "p_state": np.zeros(n_plants, np.uint8),
            "s_lat": slat, "s_lon": r.integers(-179_000_000, 179_000_000, n_structs).astype(np.int32),
            "s_osm": np.arange(1, n_structs + 1, dtype=np.int64), "s_osm_type": np.ones(n_structs, np.uint8),
            "s_kind": (np.arange(n_structs) % 3).astype(np.uint8),
            "s_height": np.where(np.arange(n_structs) % 2, 100.0, np.nan).astype(np.float32),
            "s_diam": np.full(n_structs, 8.0, np.float32),
            "s_plant": (np.arange(n_structs) % n_plants).astype(np.int32)}


def take(d, keep_s=None):
    out = dict(d)
    if keep_s is not None:
        for k in [c for c in d if c.startswith("s_")]:
            out[k] = d[k][keep_s]
    return out


def changed(d, **cols):
    """d with some columns replaced (copies)."""
    out = dict(d)
    for k, f in cols.items():
        out[k] = f(d[k].copy())
    return out


class Bands(unittest.TestCase):
    def test_in_band(self):
        doc = {"elements": [{"type": "node", "id": 1, "lat": 10.0, "lon": 5.0},
                            {"type": "way", "id": 2, "bounds": {"minlat": 1, "maxlat": 2, "minlon": -0.5, "maxlon": 0.2}},
                            {"type": "node", "id": 3, "lat": 0.0, "lon": 15.0}]}
        self.assertEqual(bp.band_problems(doc, 0, 15), [], "a way crossing the band's edge, a node on it")

    def test_out_of_band(self):
        doc = {"elements": [{"type": "node", "id": 1, "lat": 10.0, "lon": 25.0},
                            {"type": "way", "id": 2, "bounds": {"minlat": 1, "maxlat": 2, "minlon": 20, "maxlon": 21}},
                            {"type": "node", "id": 3, "lat": 95.0, "lon": 5.0},
                            {"type": "node", "id": 4, "lat": "x", "lon": 5.0},
                            {"type": "area", "id": 5}]}
        self.assertEqual(len(bp.band_problems(doc, 0, 15)), 5)

    def test_a_moved_band_file_is_refused(self):
        import json
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "band_0.json"), "w") as f:
                json.dump({"elements": [{"type": "node", "id": 1, "lat": 10.0, "lon": 120.0,
                                         "tags": {"man_made": "chimney", "height": "100"}}]}, f)
            with self.assertRaises(SystemExit):
                bp.read_structures(t)

    def test_main_instance_only_with_backoff(self):
        from urllib.parse import urlparse
        self.assertEqual(urlparse(bp.OVERPASS).hostname, "overpass-api.de")
        self.assertFalse(hasattr(bp, "OVERPASS_HOSTS"), "no mirror list")
        self.assertGreaterEqual(bp.OVERPASS_GAP_S, 10)
        self.assertEqual(list(bp.OVERPASS_BACKOFF_S), sorted(bp.OVERPASS_BACKOFF_S))

    def test_bad_answers_are_retried_never_kept(self):
        import io
        import json
        from unittest import mock
        answers = [json.dumps({"elements": [{"type": "node", "id": 1, "lat": 1.0, "lon": 200.0}]}).encode(),
                   json.dumps({"elements": [{"type": "node", "id": 1, "lat": 1.0, "lon": -170.0}]}).encode()]
        calls, sleeps = [], []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            body = answers[min(len(calls) - 1, 1)]
            return io.BytesIO(body)
        with tempfile.TemporaryDirectory() as t, mock.patch.object(bp, "BAND_DEG", 360), \
                mock.patch.object(bp.urllib.request, "urlopen", opener):
            bp.fetch_osm(t, tries=3, sleep=sleeps.append)
            with open(os.path.join(t, "band_-180.json")) as f:
                self.assertEqual(json.load(f)["elements"][0]["lon"], -170.0, "the off-band answer was never kept")
        self.assertEqual(len(calls), 2, "the moved answer was asked again")
        self.assertTrue(all(u == bp.OVERPASS for u in calls))
        self.assertEqual(sleeps[0], bp.OVERPASS_BACKOFF_S[0])


class Compare(unittest.TestCase):
    def test_the_same_passes(self):
        d = index()
        self.assertEqual(bp.compare(d, d), [])

    def test_a_region_emptied_is_held(self):
        d = index()
        lon = d["s_lon"] / 1e6
        cut = (lon >= 0) & (lon < 15) & (d["s_lat"] >= 0)
        new = take(d, keep_s=np.flatnonzero(~cut))
        got = bp.compare(new, d)
        self.assertTrue(any("region 0..15E N" in g for g in got), got)

    def test_moved_structures_are_held(self):
        d = index()
        new = dict(d)
        new["s_lon"] = d["s_lon"].copy()
        k = np.arange(0, len(d["s_lon"]), 20)         # 5 % moved ~1 km east
        new["s_lon"][k] += 15000
        got = bp.compare(new, d)
        self.assertTrue(any("moved" in g for g in got), got)

    def test_a_few_changes_pass(self):
        d = index()
        new = take(d, keep_s=np.arange(10, len(d["s_lat"])))     # 10 structures gone of 12000
        self.assertEqual(bp.compare(new, d), [])

    def test_plants_moved_within_their_regions_are_held(self):
        d = index()
        k = np.flatnonzero(d["p_osm"] > 0)[::25]          # 3 % of the plants OSM maps, ~2 km north: same regions
        new = changed(d, p_lat=lambda a: (a.__setitem__(k, a[k] + 20000), a)[1])
        self.assertTrue(any("plants moved" in g for g in bp.compare(new, d)), bp.compare(new, d))
        self.assertFalse(any("region" in g for g in bp.compare(new, d)), "the regions' counts alone never saw it")

    def test_unmapped_plants_moved_far_are_held(self):
        d = index()
        k = np.flatnonzero(d["p_osm"] <= 0)[::3]           # GPPD points moved ~5 km: unmatched, counted gone
        new = changed(d, p_lat=lambda a: (a.__setitem__(k, a[k] + 45000), a)[1])
        self.assertTrue(any("plants removed" in g for g in bp.compare(new, d)))

    def test_a_reclassified_structure_is_held(self):
        d = index()
        k = np.arange(0, len(d["s_kind"]), 30)             # 3 %: a chimney a cooling tower, same id and place
        new = changed(d, s_kind=lambda a: (a.__setitem__(k, (a[k] + 1) % 3), a)[1])
        got = bp.compare(new, d)
        self.assertTrue(any("another kind" in g for g in got), got)
        self.assertFalse(any("moved" in g or "removed" in g for g in got))

    def test_fuel_state_size_and_plant_changes_are_held(self):
        d = index()
        k = np.arange(0, len(d["p_fuel"]), 20)
        self.assertTrue(any("another fuel" in g for g in
                            bp.compare(changed(d, p_fuel=lambda a: (a.__setitem__(k, (a[k] + 1) % 6), a)[1]), d)))
        self.assertTrue(any("another state" in g for g in
                            bp.compare(changed(d, p_state=lambda a: (a.__setitem__(np.arange(0, len(a), 10), 1), a)[1]), d)))
        j = np.arange(0, len(d["s_kind"]), 10)
        self.assertTrue(any("another size" in g for g in
                            bp.compare(changed(d, s_height=lambda a: (a.__setitem__(j, 300.0), a)[1]), d)))
        self.assertTrue(any("another plant" in g for g in
                            bp.compare(changed(d, s_plant=lambda a: (a.__setitem__(j, (a[j] + 7) % len(d["p_lat"])), a)[1]),
                                       d)))

    def test_a_handful_of_real_edits_pass(self):
        d = index()
        new = changed(d, s_kind=lambda a: (a.__setitem__(np.arange(5), (a[:5] + 1) % 3), a)[1],
                      p_state=lambda a: (a.__setitem__(np.arange(3), 1), a)[1])
        self.assertEqual(bp.compare(new, d), [])

    def test_bundled_baseline_against_itself(self):
        path = os.path.join(HERE, "..", "power_plants.npz")
        if not os.path.exists(path):
            self.skipTest("the main repo's bundled index")
        d = bp._load_npz(path)
        self.assertEqual(bp.compare(d, d), [])


if __name__ == "__main__":
    unittest.main()
