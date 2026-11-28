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
    return {"p_lat": plat, "p_lon": r.integers(-179_000_000, 179_000_000, n_plants).astype(np.int32),
            "s_lat": slat, "s_lon": r.integers(-179_000_000, 179_000_000, n_structs).astype(np.int32),
            "s_osm": np.arange(1, n_structs + 1, dtype=np.int64), "s_osm_type": np.ones(n_structs, np.uint8)}


def take(d, keep_p=None, keep_s=None):
    out = dict(d)
    if keep_p is not None:
        out["p_lat"], out["p_lon"] = d["p_lat"][keep_p], d["p_lon"][keep_p]
    if keep_s is not None:
        for k in ("s_lat", "s_lon", "s_osm", "s_osm_type"):
            out[k] = d[k][keep_s]
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

    def test_bundled_baseline_against_itself(self):
        path = os.path.join(HERE, "..", "power_plants.npz")
        if not os.path.exists(path):
            self.skipTest("the main repo's bundled index")
        d = bp._load_npz(path)
        self.assertEqual(bp.compare(d, d), [])


if __name__ == "__main__":
    unittest.main()
