"""tools/winterpack_ci.py (the publish job's checker) and the builders' cell arithmetic (tools/build_water_pack.py,
tools/build_leaftype_pack.py; skipped without numpy)."""
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import winterpack_ci as ci  # noqa: E402

try:
    import numpy as np
except ImportError:         # (the stdlib jobs)
    np = None


def png(w, h, colour, rows=None):
    """A minimal PNG (8-bit, filter 0 rows of zeros unless given)."""
    ch = {0: 1, 4: 2}[colour]

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = rows if rows is not None else b"".join(b"\x00" + bytes(w * ch) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, colour, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


ATT = "# x\n\n" + "Produced under the Copernicus Programme. " * 10


def water_art(root, n=2, version="v1"):
    d = os.path.join(root, "watershare")
    shards = {}
    for i in range(n):
        body = png(500, 500, 0)
        os.makedirs(os.path.join(d, str(40 + i)), exist_ok=True)
        with open(os.path.join(d, str(40 + i), "20.png"), "wb") as f:
            f.write(body)
        shards[f"{40 + i}/20"] = [len(body), hashlib.sha256(body).hexdigest()]
    m = {"format": 1, "name": "watershare", "version": version, "built": "2026-11-25T00:00:00Z",
         "build": "0123456789abcdef", "attribution": "Source: EC JRC/Google. " * 4,
         "grid": {"res_deg": 0.004, "cells_per_shard": 500, "shard_deg": 2, "scale": 250, "nodata": 255},
         "shards": shards, "uniform": {"41/21": 0}}
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(m, f)
    with open(os.path.join(d, "ATTRIBUTION.md"), "w") as f:
        f.write(ATT)
    return d


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, minimum=1):
        ci.cmd_check(Args(art=self.art, pack="watershare", min=minimum))

    def test_a_good_build_passes(self):
        water_art(self.art)
        self.check()

    def test_a_stray_file_is_refused(self):
        d = water_art(self.art)
        open(os.path.join(d, "40", "x.txt"), "w").close()
        with self.assertRaises(ci.Refused):
            self.check()

    def test_a_link_is_refused(self):
        d = water_art(self.art)
        os.symlink("/etc/passwd", os.path.join(d, "41", "21.png"))
        with self.assertRaises(ci.Refused):
            self.check()

    def test_a_changed_shard_is_refused(self):
        d = water_art(self.art)
        p = os.path.join(d, "40", "20.png")
        body = bytearray(open(p, "rb").read())
        body[-20] ^= 1
        open(p, "wb").write(bytes(body))
        with self.assertRaises(ci.Refused):
            self.check()

    def test_a_png_of_another_size_is_refused(self):
        d = water_art(self.art)
        body = png(256, 256, 0)
        p = os.path.join(d, "40", "20.png")
        open(p, "wb").write(body)
        m = json.load(open(os.path.join(d, "manifest.json")))
        m["shards"]["40/20"] = [len(body), hashlib.sha256(body).hexdigest()]
        json.dump(m, open(os.path.join(d, "manifest.json"), "w"))
        with self.assertRaises(ci.Refused):
            self.check()

    def test_a_small_build_is_refused(self):
        water_art(self.art)
        with self.assertRaises(ci.Refused):
            self.check(minimum=3000)


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = os.path.join(self.tmp.name, "art")
        water_art(self.art)

    def tearDown(self):
        self.tmp.cleanup()

    def listing(self, keys):
        p = os.path.join(self.tmp.name, "l.json")
        json.dump({"Contents": [{"Key": "watershare/v1/" + k, "Size": s} for k, s in keys.items()]}, open(p, "w"))
        return p

    def run_todo(self, keys, complete=False):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ci.cmd_todo(Args(art=self.art, pack="watershare", listing=self.listing(keys), version="v1",
                             complete=complete))
        return out.getvalue().split("\n")

    def test_new_lists_every_file_but_the_manifest(self):
        lines = [l for l in self.run_todo({}) if l]
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("watershare/v1/ATTRIBUTION.md"))
        self.assertFalse(any("manifest" in l for l in lines))

    def test_a_stray_key_or_size_refuses(self):
        with self.assertRaises(ci.Refused):
            self.run_todo({"9/9.png": 10})
        size = os.path.getsize(os.path.join(self.art, "watershare", "40", "20.png"))
        with self.assertRaises(ci.Refused):
            self.run_todo({"40/20.png": size + 1})

    def test_complete(self):
        d = os.path.join(self.art, "watershare")
        keys = {p: os.path.getsize(os.path.join(d, p)) for p in ("40/20.png", "41/20.png", "ATTRIBUTION.md")}
        self.assertEqual(self.run_todo(keys, complete=True)[0], "files")
        keys["manifest.json"] = os.path.getsize(os.path.join(d, "manifest.json"))
        self.assertEqual(self.run_todo(keys, complete=True)[0], "published")
        del keys["41/20.png"]
        with self.assertRaises(ci.Refused):
            self.run_todo(keys, complete=True)


class CdnKeyTest(unittest.TestCase):
    def test_only_the_key_changes(self):
        text = '{"imagery": {"bngs2": {"enabled": true}}, "dem": {"x": 1}}'
        new = ci.cdn_key_text(text, "watershare", "v1")
        self.assertEqual(json.loads(new), {"imagery": {"bngs2": {"enabled": True}}, "dem": {"x": 1},
                                           "watershare": {"enabled": True, "version": "v1"}})
        self.assertTrue(new.startswith('{"imagery": {"bngs2": {"enabled": true}}, "dem": {"x": 1}'))
        self.assertEqual(ci.cdn_key_text(new, "watershare", "v1"), new)
        with self.assertRaises(ci.Refused):
            ci.cdn_key_text(text, "watershare", "v1/../x")


@unittest.skipIf(np is None, "numpy")
class CellsTest(unittest.TestCase):
    def test_water_cells(self):
        import build_water_pack as bw
        px = np.zeros((32, 48), np.uint8)
        px[:16, :16] = 100                  # all water
        px[:16, 16:32] = 255                # no data: open sea, counted as water, and all of it: no data
        px[16:, :16] = 50                   # half the time
        px[16:, 16:32] = 255
        px[16, 16] = 0                      # one land pixel beside the sea
        px[:16, 32:] = 200                  # occurrence over 100 counts as 100
        v = bw.reduce_block(px)
        self.assertEqual(v.tolist(), [[250, 255, 250], [125, 249, 0]])

    def test_evergreen_lean(self):
        import build_leaftype_pack as bl
        T = np.zeros((bl.CH, bl.CW), np.uint32)
        E = np.zeros((bl.CH, bl.CW), np.uint32)
        T[100, 100], E[100, 100] = 324 * 140, 324 * 140            # a closed evergreen forest cell
        T[100, 101], E[100, 101] = 324 * 140, 0                    # a closed deciduous one beside it
        r = bl.evergreen_share(T, E)
        self.assertGreater(r[100, 100], 0.98)
        self.assertLess(r[100, 101], 0.02)
        self.assertAlmostEqual(r[100, 102], 0.5, 2)                # no trees: its block's mix (half and half)
        tree, eg, land = bl.luts()
        self.assertEqual((tree[70], eg[70], tree[60], eg[60], tree[90], eg[90]), (140, 140, 140, 0, 120, 40))
        self.assertEqual((land[210], land[0], land[10]), (0, 0, 1))


@unittest.skipIf(np is None, "numpy")
class PowerPlantsTest(unittest.TestCase):
    """The power plants' index (powerplants.yml): its npz's members, headers and counts."""

    def art(self, root, plants=6000, structs=12000, extra=None, drop=None, version="v4"):
        d = os.path.join(root, "powerplants")
        os.makedirs(d)
        cols = {}
        for k, (descr, n_of) in ci.PP_COLUMNS.items():
            n = {"p": plants, "s": structs, "p1": plants + 1, "one": 1, "any": 3 * plants}[n_of]
            cols[k] = np.zeros(n, np.dtype(descr))
        cols["p_name_off"] = np.arange(plants + 1, dtype=np.int32) * 3
        if extra:
            cols.update(extra)
        for k in drop or ():
            cols.pop(k)
        path = os.path.join(d, "power_plants.npz")
        with open(path, "wb") as f:
            np.savez_compressed(f, **cols)
        body = open(path, "rb").read()
        m = {"format": 1, "name": "powerplants", "version": version, "build": hashlib.sha256(body).hexdigest()[:16],
             "built": "2026-11-28T00:00:00Z", "attribution": "(c) OpenStreetMap contributors and the others " * 2,
             "plants": plants, "structures": structs,
             "files": [{"path": "power_plants.npz", "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}]}
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(m, f)
        with open(os.path.join(d, "ATTRIBUTION.md"), "w") as f:
            f.write("# x\n\n" + "Derived from OpenStreetMap contributors' data. " * 10)
        return root

    def run_ci(self, *argv):
        try:
            ci.main(list(argv))
        except SystemExit as e:
            return e.code
        return 0

    def test_a_good_build_passes(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t), "powerplants"), 0)

    def test_a_missing_or_extra_column_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t, drop=["s_plant"]), "powerplants"), 1)
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t, extra={"evil": np.zeros(3, np.int8)}), "powerplants"), 1)

    def test_a_wrong_length_or_dtype_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t, extra={"s_lat": np.zeros(5, np.int32)}), "powerplants"), 1)
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t, extra={"p_mw": np.zeros(6000, np.float64)}),
                                         "powerplants"), 1)

    def test_a_cut_off_build_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.run_ci("check", self.art(t, plants=100), "powerplants"), 1)

    # --- the release's version from the whole bucket prefix (powerplants.yml publish)
    def allocate(self, art, keys, served=""):
        import contextlib
        import io
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"Contents": [{"Key": k, "Size": sz, "ETag": f'"{et}"'} for k, sz, et in keys]}, f)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = self.run_ci("allocate", art, "powerplants", "--listing", f.name, "--served", served)
        finally:
            os.unlink(f.name)
        return code, out.getvalue().split()

    def npz_of(self, art):
        p = os.path.join(art, "powerplants", "power_plants.npz")
        return os.path.getsize(p), hashlib.md5(open(p, "rb").read()).hexdigest()

    def test_first_release_is_v1(self):
        with tempfile.TemporaryDirectory() as t:
            art = self.art(t)
            self.assertEqual(self.allocate(art, []), (0, ["new", "v1"]))

    def test_past_every_used_prefix(self):
        with tempfile.TemporaryDirectory() as t:
            art = self.art(t)
            keys = [("powerplants/v1/power_plants.npz", 10, "a" * 32), ("powerplants/v1/manifest.json", 5, "b" * 32),
                    ("powerplants/v3/power_plants.npz", 11, "c" * 32)]          # (v3: an upload cut off)
            self.assertEqual(self.allocate(art, keys, served="v1"), (0, ["new", "v4"]))
            self.assertEqual(self.allocate(art, keys, served="v7"), (0, ["new", "v8"]), "a served rollback past them")

    def test_this_build_published_is_reused(self):
        with tempfile.TemporaryDirectory() as t:
            art = self.art(t)
            size, md5 = self.npz_of(art)
            keys = [("powerplants/v2/power_plants.npz", size, md5), ("powerplants/v2/manifest.json", 300, "d" * 32),
                    ("powerplants/v5/power_plants.npz", 10, "e" * 32)]
            self.assertEqual(self.allocate(art, keys, served="v1"), (0, ["reuse", "v2"]))
            keys[1] = ("powerplants/v2/ATTRIBUTION.md", 300, "d" * 32)      # (no manifest: finished there)
            self.assertEqual(self.allocate(art, keys, served="v1"), (0, ["resume", "v2"]))

    def test_a_stray_key_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            art = self.art(t)
            self.assertEqual(self.allocate(art, [("powerplants/latest/x", 1, "a" * 32)])[0], 1)

    def test_restamp(self):
        with tempfile.TemporaryDirectory() as t:
            art = self.art(t, version="v1")
            self.assertEqual(self.run_ci("restamp", art, "powerplants", "v9"), 0)
            with open(os.path.join(art, "powerplants", "manifest.json")) as f:
                self.assertEqual(json.load(f)["version"], "v9")
            self.assertEqual(self.run_ci("restamp", art, "powerplants", "x9"), 1)

    def test_next_version(self):
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "cdn.json")
            with open(f, "w") as fh:
                fh.write('{"nbiskew": {"enabled": true, "version": "v1"}}')
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ci.main(["next-version", f, "powerplants"])
            self.assertEqual(out.getvalue().strip(), "v1")
            with open(f, "w") as fh:
                fh.write('{"powerplants": {"enabled": true, "version": "v41"}}')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ci.main(["next-version", f, "powerplants"])
            self.assertEqual(out.getvalue().strip(), "v42")


if __name__ == "__main__":
    unittest.main()
