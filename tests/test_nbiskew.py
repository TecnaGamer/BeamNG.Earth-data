"""tools/build_nbiskew.py (skipped without numpy) and tools/winterpack_ci.py's nbiskew pack."""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import winterpack_ci as ci  # noqa: E402

try:
    import numpy as np  # noqa: F401
    import build_nbiskew as b
except ImportError:         # (the stdlib jobs)
    b = None

HEAD = ("STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A,LAT_016,LONG_017,DEGREES_SKEW_034,"
        "STRUCTURE_LEN_MT_049\n")


def archive(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ALL.txt", HEAD + "".join(rows))
    return buf.getvalue()


@unittest.skipIf(b is None, "numpy")
class BuildTest(unittest.TestCase):
    def test_rows_and_determinism(self):
        rows = ["51,'A',1,38464920,077105400,30,25.6\n",     # 38 46 49.20 N, 77 10 54.00 W
                "51,'B',2,38464920,077105400,12,10\n",       # (a route under: not kept)
                "66,'C',1,13283000,144450000,99,0\n",        # Guam: east, varies kept
                "51,'D',1,38464920,077105400,,10\n",         # (no skew: not kept)
                "51,'E',1,00000000,077105400,5,10\n"]        # (no coordinates: not kept)
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "nbi.zip")
            with open(src, "wb") as f:
                f.write(archive(rows))
            m1 = b.build(src, os.path.join(d, "o1"), "v1", "2025", "2026-11-25T00:00:00Z", min_records=1)
            m2 = b.build(src, os.path.join(d, "o2"), "v1", "2025", "2026-11-25T00:00:00Z", min_records=1)
            self.assertEqual(m1["build"], m2["build"])
            with open(os.path.join(d, "o1", "nbiskew.npz"), "rb") as f1, open(os.path.join(d, "o2", "nbiskew.npz"), "rb") as f2:
                self.assertEqual(f1.read(), f2.read())
            z = np.load(os.path.join(d, "o1", "nbiskew.npz"), allow_pickle=False)
            self.assertEqual(m1["records"], 2)
            self.assertEqual(list(z["skew"]), [99, 30])         # (sorted by lat: Guam first)
            self.assertAlmostEqual(float(z["lon"][0]), 144.75, places=4)
            self.assertAlmostEqual(float(z["lon"][1]), -(77 + 10 / 60 + 54 / 3600), places=4)
            self.assertEqual(list(z["length"]), [0, 256])
            ci.npz_check(os.path.join(d, "o1", "nbiskew.npz"), 2)
            with self.assertRaises(ci.Refused):
                ci.npz_check(os.path.join(d, "o1", "nbiskew.npz"), 3)

    def test_only_fhwas_host(self):
        with self.assertRaises(SystemExit):
            b.URL, old = "https://example.com/{year}.zip", b.URL
            try:
                b.fetch(os.devnull, "2025")
            finally:
                b.URL = old


class CheckTest(unittest.TestCase):
    def test_a_member_too_many_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.npz")
            with zipfile.ZipFile(p, "w") as z:
                for k in ("lat", "lon", "skew", "length", "extra"):
                    z.writestr(k + ".npy", b"")
            with self.assertRaises(ci.Refused):
                ci.npz_check(p, 1)

    def test_manifest_fields(self):
        m = {"format": 1, "name": "nbiskew", "version": "v1", "built": "2026-11-25T00:00:00Z", "build": "0" * 16,
             "attribution": "x" * 50, "records": 623000, "columns": ci.NBI_COLUMNS, "varies": 99,
             "files": [{"path": "nbiskew.npz", "size": 10, "sha256": "a" * 64}]}
        self.assertEqual(list(ci.files_of(m, "nbiskew")), ["nbiskew.npz"])
        with self.assertRaises(ci.Refused):
            ci.files_of(dict(m, records=10), "nbiskew")
        with self.assertRaises(ci.Refused):
            ci.files_of(dict(m, columns={"lat": "<f8"}), "nbiskew")
        self.assertIn('"nbiskew": {"enabled": true, "version": "v1"}',
                      ci.cdn_key_text(json.dumps({"dem": 1}), "nbiskew", "v1"))


if __name__ == "__main__":
    unittest.main()
