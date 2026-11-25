#!/usr/bin/env python3
"""The leaf-type pack (BeamNG.Earth's winter white ground, W2, and bare winter trees, W4): ESA CCI Land Cover's 2015
map (v2.0.7, 300 m) condensed to two global 8-bit rasters of 0.05 degrees (7200 x 3600 cells from 180 W, 90 N; 18 x 18
source pixels a cell), one PNG with two grey bands (mode "LA"):

  band 0 "evergreen": the evergreen share of the cell's trees, 0..250 (share = value / 250): evergreen (broadleaved
         evergreen + needleleaved evergreen) tree cover / all tree cover, by the CCI-LC classes' plant functional
         types (the Product User Guide v2.0's Table 7-2, "conversion of CCI-LC classes to PFT", the cross-walk the
         climate modellers confirmed; TREE below). Where a cell has few trees its share leans to its neighbourhood's
         (0.25, 1 and 5 degree blocks, then the globe's): share = (E + K * parent) / (T + K), K = a few forested pixels'
         worth, so a cell with no trees at all (a field, a town) carries its region's mix (for OSM's small woods
         there). 0 on cells with no land (all water or no data).
  band 1 "trees": the cell's tree cover, 0..250 (share of the cell's area, by the same table: a closed broadleaved
         deciduous forest pixel is 70 % trees, a mosaic cropland pixel 10 %).

Source (no account; CEDA's open archive, "Public data: access to these data is available to both registered and
non-registered users"):
  https://dap.ceda.ac.uk/neodc/esacci/land_cover/data/land_cover_maps/v2.0.7/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2015-v2.0.7.tif
  129600 x 64800 px, 1/360 degree, uint8 LCCS classes (0 no data, 10..220).
Licence: the Land Cover CCI terms ("may be used by any user for any purpose", acknowledging the ESA Climate Change
Initiative and the Land Cover CCI project, citing the dataset): ESA Land Cover CCI project team; Defourny, P. (2019):
ESA Land Cover Climate Change Initiative (Land_Cover_cci): Global Land Cover Maps, Version 2.0.7. Centre for
Environmental Data Analysis.

    build_leaftype_pack.py build SRC.tif OUT --version v1     OUT/leaftype.png and OUT/manifest.json
    build_leaftype_pack.py fetch DEST                          the source map (312 MB) to DEST, its size checked

Needs numpy, Pillow and rasterio. Integer sums per cell; the neighbourhood lean in float64 (numpy element-wise, the
same bytes on any machine). Reads the map in strips of 0.5 degrees (180 rows x 129600: 23 MB).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request

import numpy as np

SOURCE = ("https://dap.ceda.ac.uk/neodc/esacci/land_cover/data/land_cover_maps/v2.0.7/"
          "ESACCI-LC-L4-LCCS-Map-300m-P1Y-2015-v2.0.7.tif")
SOURCE_SIZE = 312727481
W, H = 129600, 64800
F = 18                      # source pixels per cell side: 0.05 degrees
CW, CH = W // F, H // F     # 7200 x 3600
RES = 0.05
SCALE = 250
LEVELS = (5, 4, 5)          # the neighbourhood blocks: 0.25, 1 and 5 degrees (then the globe)
K = 4 * 140                 # the lean's weight: 4 pixels of closed forest (70 % trees, in half percent units)
FORMAT = 1
UA = "BeamNG.Earth-data leaf type pack builder"

# CCI-LC class -> (broadleaved evergreen, broadleaved deciduous, needleleaved evergreen, needleleaved deciduous) tree
# cover, percent of the pixel: the Product User Guide v2.0 (2017), Table 7-2. 151 (sparse trees) is not in the table:
# the sparse vegetation class 150's trees. Every class not named has no trees.
TREE = {
    30: (5, 5, 0, 0), 40: (5, 5, 0, 0), 50: (90, 0, 0, 0),
    60: (0, 70, 0, 0), 61: (0, 70, 0, 0), 62: (0, 30, 0, 0),
    70: (0, 0, 70, 0), 71: (0, 0, 70, 0), 72: (0, 0, 30, 0),
    80: (0, 0, 0, 70), 81: (0, 0, 0, 70), 82: (0, 0, 0, 30),
    90: (0, 30, 20, 10), 100: (10, 20, 5, 5), 110: (5, 10, 5, 0),
    150: (1, 3, 1, 0), 151: (1, 3, 1, 0),
    160: (30, 30, 0, 0), 170: (60, 0, 0, 0), 180: (0, 5, 10, 0), 190: (0, 2.5, 2.5, 0),
}
WATER = (210,)


def luts():
    """(tree, evergreen, land) per class: tree and evergreen cover in half percent (integers), land 1 unless water or
    no data."""
    tree = np.zeros(256, np.uint32)
    eg = np.zeros(256, np.uint32)
    land = np.ones(256, np.uint32)
    for c, (be, bd, ne, nd) in TREE.items():
        tree[c] = round(2 * (be + bd + ne + nd))
        eg[c] = round(2 * (be + ne))
    land[0] = 0
    for c in WATER:
        land[c] = 0
    return tree, eg, land


def sums(src, rows=180, progress=True):
    """(T, E, L): per cell the sums of tree and evergreen cover (half percent) and the count of land pixels."""
    import rasterio
    from rasterio.windows import Window
    tree, eg, land = luts()
    T = np.zeros((CH, CW), np.uint32)
    E = np.zeros((CH, CW), np.uint32)
    L = np.zeros((CH, CW), np.uint32)
    with rasterio.open(src) as d:
        if (d.width, d.height, d.dtypes[0]) != (W, H, "uint8"):
            raise ValueError(f"{src}: {d.width}x{d.height} {d.dtypes}, not {W}x{H} uint8")
        t = d.transform
        if abs(t.a - 1 / 360) > 1e-12 or abs(t.e + 1 / 360) > 1e-12 or abs(t.c + 180) > 1e-6 or abs(t.f - 90) > 1e-6:
            raise ValueError(f"{src}: transform {tuple(t)[:6]} is not the global 1/360 degree grid")
        t0 = time.time()
        for r0 in range(0, H, rows):
            px = d.read(1, window=Window(0, r0, W, rows))
            n = rows // F
            j = r0 // F
            T[j:j + n] = tree[px].reshape(n, F, CW, F).sum(axis=(1, 3), dtype=np.uint32)
            E[j:j + n] = eg[px].reshape(n, F, CW, F).sum(axis=(1, 3), dtype=np.uint32)
            L[j:j + n] = land[px].reshape(n, F, CW, F).sum(axis=(1, 3), dtype=np.uint32)
            if progress and (r0 // rows) % 40 == 0:
                print(f"  rows {r0}/{H} ({time.time() - t0:.0f} s)", file=sys.stderr, flush=True)
    return T, E, L


def block(a, f):
    h, w = a.shape
    return a.reshape(h // f, f, w // f, f).sum(axis=(1, 3), dtype=np.float64)


def evergreen_share(T, E):
    """The leaning share (module docstring) per cell, float64 0..1."""
    Ts, Es = [T.astype(np.float64)], [E.astype(np.float64)]
    for f in LEVELS:
        Ts.append(block(Ts[-1], f))
        Es.append(block(Es[-1], f))
    tot = Ts[-1].sum()
    r = np.full(Ts[-1].shape, Es[-1].sum() / tot if tot else 0.0)
    for lvl in range(len(LEVELS), -1, -1):
        r = (Es[lvl] + K * r) / (Ts[lvl] + K)
        if lvl:
            f = LEVELS[lvl - 1]
            r = np.repeat(np.repeat(r, f, axis=0), f, axis=1)
    return r


def to_byte(share):
    return np.clip(np.floor(share * SCALE + 0.5), 0, SCALE).astype(np.uint8)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build(src, out, version, built=None):
    from PIL import Image
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    T, E, L = sums(src)
    t1 = time.time()
    eg = to_byte(evergreen_share(T, E))
    eg[L == 0] = 0
    trees = (T.astype(np.uint64) * SCALE + (F * F * 200) // 2) // (F * F * 200)
    trees = trees.astype(np.uint8)
    png = os.path.join(out, "leaftype.png")
    Image.fromarray(np.dstack([eg, trees]), "LA").save(png + ".part", format="PNG", optimize=True)
    os.replace(png + ".part", png)
    size, sha = os.path.getsize(png), sha256_file(png)
    m = {"format": FORMAT, "name": "leaftype", "version": version,
         "built": built or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "build": sha[:16],
         "grid": {"res_deg": RES, "width": CW, "height": CH, "west": -180, "north": 90, "scale": SCALE,
                  "bands": ["evergreen", "trees"], "mode": "LA"},
         "files": [{"path": "leaftype.png", "size": size, "sha256": sha}],
         "source": "ESA Land Cover CCI v2.0.7, 2015 map (300 m); PFT cross-walk: CCI-LC Product User Guide v2.0, "
                   "Table 7-2",
         "attribution": "ESA Land Cover CCI project team; Defourny, P. (2019): ESA Land Cover Climate Change "
                        "Initiative (Land_Cover_cci): Global Land Cover Maps, Version 2.0.7. Centre for "
                        "Environmental Data Analysis. With thanks to the ESA Climate Change Initiative and the Land "
                        "Cover CCI project."}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, sort_keys=True, separators=(",", ":"))
    print(json.dumps({"build": m["build"], "size": size, "sums_s": round(t1 - t0, 1),
                      "total_s": round(time.time() - t0, 1)}))
    return m


def fetch(dest):
    if os.path.exists(dest) and os.path.getsize(dest) == SOURCE_SIZE:
        return
    req = urllib.request.Request(SOURCE, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest + ".part", "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    if os.path.getsize(dest + ".part") != SOURCE_SIZE:
        raise SystemExit(f"short download: {os.path.getsize(dest + '.part')} of {SOURCE_SIZE}")
    os.replace(dest + ".part", dest)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("build")
    a.add_argument("src")
    a.add_argument("out")
    a.add_argument("--version", required=True)
    a.add_argument("--built")
    a = sub.add_parser("fetch")
    a.add_argument("dest")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        if not re.fullmatch(r"v[1-9][0-9]{0,3}", args.version):
            raise SystemExit("bad version")
        build(args.src, args.out, args.version, args.built)
    else:
        fetch(args.dest)


if __name__ == "__main__":
    main()
