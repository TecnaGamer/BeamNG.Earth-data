#!/usr/bin/env python3
"""The water-share pack (BeamNG.Earth's winter white ground, W2: the far views' lakes, rivers and coasts): JRC Global
Surface Water's occurrence (Pekel et al. 2016, v1.5 1984-2024, 30 m) condensed to the share of water in cells of
0.004 degrees (16 x 16 source pixels, ~445 m), in shards of 2 x 2 degrees (500 x 500 cells, an 8-bit grey PNG each).

Source: the provider's own HTTPS listing (no account; linked from https://global-surface-water.appspot.com/download):
  https://s3.waw4-1.cloudferro.com/swift/v1/global-surface-water/download2024/Aggregated/VER1-5/occurrence/
  occurrence_<10W|0E|...>_<80N|...|50S>_v1_5_2024.tif: 10 x 10 degrees from its north-west corner, 40000 x 40000 px
  (0.00025 degrees), uint8: 0-100 the percent of the valid observations 1984-2024 that saw water, 255 no data (the open
  sea past the mapped coastal band, and places never observed). Licence: produced under the Copernicus Programme,
  "provided free of charge, without restriction of use"; credit "Source: EC JRC/Google" and the Nature article.

Each cell's value (0..250, share = value / 250; 255 = no data): the mean of its 256 pixels' occurrence / 100, a pixel
with no data counted as water (in GSW that is the open sea next to the mapped coast), 255 when every pixel has none.
The mean is the expected share of water on a random day of 1984-2024 (a lake ~1, a seasonal marsh its wet share).
Occurrence above 100 (sporadic pixels of an earlier release) counts as 100. Integer arithmetic only.

Shard <x>/<y>.png: x = floor((lon + 180) / 2) (0..179), y = floor((90 - lat) / 2) (0..89); row 0 the north edge,
column 0 the west edge. A shard whose cells are all one value is not written: the manifest's "uniform" names its value
(0: land without water, 255 would be no data; such shards are left out of the manifest altogether).

    build_water_pack.py tiles [--listing L.json]           the source tiles' names (the provider's listing, or L)
    build_water_pack.py tile SRC.tif OUT                     one source tile -> OUT/<x>/<y>.png and OUT/_uniform/<x>_<y>
    build_water_pack.py run OUT --tiles T1 T2 ... [--jobs N] [--source URL] [--keep DIR]
                                                             download each tile (one at a time per job, deleted after),
                                                             reduce it; resumable (a tile done has OUT/_done/<tile>)
    build_water_pack.py manifest OUT --version v1            OUT/manifest.json from the shards and uniform markers

Needs numpy, Pillow and rasterio (but "tiles", "manifest" and "stage": standard library; GDAL: the tiles are tiled Deflate GeoTIFFs). Each source tile reads in windows of one shard
(8000 x 8000 px, 64 MB), so a job holds ~200 MB.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

SOURCE = ("https://s3.waw4-1.cloudferro.com/swift/v1/global-surface-water/download2024/Aggregated/VER1-5/"
          "occurrence")
LISTING = ("https://s3.waw4-1.cloudferro.com/swift/v1/global-surface-water/"
           "?prefix=download2024/Aggregated/VER1-5/occurrence/occurrence_&format=json&limit=10000")
TILE_RE = re.compile(r"(\d{1,3})([EW])_(\d{1,2})([NS])")
NAME_RE = re.compile(r"occurrence_(\d{1,3}[EW]_\d{1,2}[NS])_v1_5_2024\.tif")
SRC_PX = 40000              # pixels per source tile side (10 degrees)
SRC_DEG = 10
F = 16                      # source pixels per cell side: 0.004 degrees
SHARD_DEG = 2
SHARD = SHARD_DEG * SRC_PX // SRC_DEG // F      # 500 cells per shard side
RES = SHARD_DEG / SHARD     # 0.004
NODATA = 255
SCALE = 250                 # share = value / SCALE
FORMAT = 1
UA = "BeamNG.Earth-data water pack builder"


def tile_corner(name):
    """(west lon, north lat) of a source tile name like 100W_50N."""
    m = TILE_RE.fullmatch(name)
    if not m:
        raise ValueError(f"bad tile name {name!r}")
    lon = int(m.group(1)) * (-1 if m.group(2) == "W" else 1)
    lat = int(m.group(3)) * (-1 if m.group(4) == "S" else 1)
    if lon % SRC_DEG or lat % SRC_DEG or not -180 <= lon < 180 or not -90 < lat <= 90:
        raise ValueError(f"bad tile corner {name!r}")
    return lon, lat


def shard_xy(west, north):
    return (west + 180) // SHARD_DEG, (90 - north) // SHARD_DEG


def reduce_block(px):
    """A (SHARD*F, SHARD*F) uint8 block of occurrence -> (SHARD, SHARD) uint8 cell values (module docstring)."""
    import numpy as np
    n = px.shape[0] // F
    m = px.shape[1] // F
    nod = px == NODATA
    occ = np.minimum(px, 100).astype(np.uint32)
    occ[nod] = 100
    s = occ.reshape(n, F, m, F).sum(axis=(1, 3), dtype=np.uint32)          # <= 25600
    k = nod.reshape(n, F, m, F).sum(axis=(1, 3), dtype=np.uint32)          # <= 256
    full = 100 * F * F
    v = (s * SCALE + full // 2) // full
    v = v.astype(np.uint8)
    v[k == F * F] = NODATA
    return v


def write_png(path, arr):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    Image.fromarray(arr, "L").save(tmp, format="PNG", optimize=True)
    os.replace(tmp, path)


def do_tile(src, out, name=None):
    """One source tile -> its 25 shards (or uniform markers). Returns {shard: 'png' | value}."""
    import rasterio
    from rasterio.windows import Window
    name = name or NAME_RE.fullmatch(os.path.basename(src)).group(1)
    west, north = tile_corner(name)
    got = {}
    with rasterio.open(src) as d:
        if (d.width, d.height, d.count, d.dtypes[0]) != (SRC_PX, SRC_PX, 1, "uint8"):
            raise ValueError(f"{src}: {d.width}x{d.height}x{d.count} {d.dtypes}, not {SRC_PX}^2 uint8")
        t = d.transform
        if abs(t.a - SRC_DEG / SRC_PX) > 1e-12 or abs(t.e + SRC_DEG / SRC_PX) > 1e-12 or \
                abs(t.c - west) > 1e-6 or abs(t.f - north) > 1e-6:
            raise ValueError(f"{src}: transform {tuple(t)[:6]} is not the tile {name}'s")
        step = SHARD * F
        for j in range(SRC_DEG // SHARD_DEG):
            for i in range(SRC_DEG // SHARD_DEG):
                px = d.read(1, window=Window(i * step, j * step, step, step))
                v = reduce_block(px)
                x, y = shard_xy(west + i * SHARD_DEG, north - j * SHARD_DEG)
                lo, hi = int(v.min()), int(v.max())
                if lo == hi:
                    got[(x, y)] = lo
                    if lo != NODATA:
                        p = os.path.join(out, "_uniform", f"{x}_{y}")
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        with open(p, "w") as fh:
                            fh.write(str(lo))
                else:
                    write_png(os.path.join(out, str(x), f"{y}.png"), v)
                    got[(x, y)] = "png"
    return got


def listing(path=None):
    """The source tiles' names, sorted."""
    if path:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh)
    else:
        req = urllib.request.Request(LISTING, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            items = json.load(r)
    names = sorted({m.group(1) for o in items if (m := NAME_RE.fullmatch(o["name"].rsplit("/", 1)[-1]))})
    return names


def download(url, dest, tries=4):
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest + ".part", "wb") as fh:
                n = int(r.headers.get("Content-Length") or -1)
                shutil.copyfileobj(r, fh, 1 << 20)
            if n >= 0 and os.path.getsize(dest + ".part") != n:
                raise OSError(f"short read: {os.path.getsize(dest + '.part')} of {n}")
            os.replace(dest + ".part", dest)
            return
        except Exception as e:
            if t == tries - 1:
                raise
            print(f"  {url}: {type(e).__name__}: {e}; again", file=sys.stderr, flush=True)
            time.sleep(10 * (t + 1))


def run_one(args):
    name, out, source, keep = args
    done = os.path.join(out, "_done", name)
    if os.path.exists(done):
        return name, "done before", 0.0
    t0 = time.time()
    fn = f"occurrence_{name}_v1_5_2024.tif"
    local = os.path.join(keep, fn) if keep else None
    tmpd = None
    if local and os.path.exists(local):
        src = local
    else:
        tmpd = tempfile.mkdtemp(prefix="gsw-", dir=keep or None)
        src = os.path.join(tmpd, fn)
        download(f"{source}/{fn}", src)
    try:
        got = do_tile(src, out, name)
    finally:
        if tmpd:
            shutil.rmtree(tmpd, ignore_errors=True)
    os.makedirs(os.path.dirname(done), exist_ok=True)
    with open(done, "w") as fh:
        json.dump({f"{x}/{y}": v for (x, y), v in sorted(got.items())}, fh)
    return name, f"{sum(v == 'png' for v in got.values())} shards", time.time() - t0


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def make_manifest(out, version, built=None):
    """manifest.json: every shard's size and SHA-256 and every uniform shard's value; "build" the SHA-256 of the
    sorted shard list (the reader's cache key)."""
    shards = {}
    for xs in sorted(os.listdir(out), key=lambda s: (len(s), s)):
        if not xs.isdigit():
            continue
        for fn in sorted(os.listdir(os.path.join(out, xs))):
            m = re.fullmatch(r"(\d{1,2})\.png", fn)
            if not m:
                raise ValueError(f"stray file {xs}/{fn}")
            p = os.path.join(out, xs, fn)
            shards[f"{xs}/{m.group(1)}"] = [os.path.getsize(p), sha256_file(p)]
    uniform = {}
    ud = os.path.join(out, "_uniform")
    if os.path.isdir(ud):
        for fn in sorted(os.listdir(ud)):
            x, y = fn.split("_")
            with open(os.path.join(ud, fn)) as fh:
                uniform[f"{int(x)}/{int(y)}"] = int(fh.read())
    both = set(shards) & set(uniform)
    if both:
        raise ValueError(f"shards both written and uniform: {sorted(both)[:5]}")
    body = json.dumps({"shards": shards, "uniform": uniform}, sort_keys=True, separators=(",", ":")).encode()
    m = {"format": FORMAT, "name": "watershare", "version": version,
         "built": built or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "build": hashlib.sha256(body).hexdigest()[:16],
         "grid": {"res_deg": RES, "cells_per_shard": SHARD, "shard_deg": SHARD_DEG, "scale": SCALE,
                  "nodata": NODATA, "path": "{x}/{y}.png", "x": "floor((lon + 180) / 2)", "y": "floor((90 - lat) / 2)"},
         "source": "JRC Global Surface Water v1.5 (1984-2024) occurrence, 30 m",
         "attribution": "Source: EC JRC/Google. Pekel, J.-F., Cottam, A., Gorelick, N., Belward, A. S. (2016): "
                        "High-resolution mapping of global surface water and its long-term changes. Nature 540, "
                        "418-422. doi:10.1038/nature20584. Produced under the Copernicus Programme.",
         "shards": shards, "uniform": uniform}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, sort_keys=True, separators=(",", ":"))
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("tiles")
    a.add_argument("--listing")
    a = sub.add_parser("tile")
    a.add_argument("src")
    a.add_argument("out")
    a = sub.add_parser("run")
    a.add_argument("out")
    a.add_argument("--tiles", nargs="+", required=True)
    a.add_argument("--jobs", type=int, default=1)
    a.add_argument("--source", default=SOURCE)
    a.add_argument("--keep", help="folder of source tiles already downloaded (and where new ones go while read)")
    a = sub.add_parser("manifest")
    a.add_argument("out")
    a.add_argument("--version", required=True)
    a.add_argument("--built")
    a.add_argument("--expect", help="a file of source tile names: each must be done (OUT/_done/<tile>)")
    a = sub.add_parser("stage")
    a.add_argument("out")
    a.add_argument("stage")
    a.add_argument("--attribution", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "tiles":
        print("\n".join(listing(args.listing)))
    elif args.cmd == "tile":
        got = do_tile(args.src, args.out)
        print(json.dumps({f"{x}/{y}": v for (x, y), v in sorted(got.items())}))
    elif args.cmd == "run":
        for t in args.tiles:
            tile_corner(t)
        if not args.source.startswith("https://"):
            raise SystemExit("--source must be https")
        work = [(t, args.out, args.source.rstrip("/"), args.keep) for t in args.tiles]
        t0 = time.time()
        if args.jobs > 1:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(args.jobs) as ex:
                for i, (name, what, dt) in enumerate(ex.map(run_one, work), 1):
                    print(f"[{i}/{len(work)}] {name}: {what} ({dt:.1f} s)", flush=True)
        else:
            for i, w in enumerate(work, 1):
                name, what, dt = run_one(w)
                print(f"[{i}/{len(work)}] {name}: {what} ({dt:.1f} s)", flush=True)
        print(f"{len(work)} tiles in {time.time() - t0:.0f} s", flush=True)
    elif args.cmd == "manifest":
        if not re.fullmatch(r"v[1-9][0-9]{0,3}", args.version):
            raise SystemExit("bad version")
        if args.expect:
            with open(args.expect) as fh:
                want = fh.read().split()
            miss = [t for t in want if not os.path.exists(os.path.join(args.out, "_done", t))]
            if miss or not want:
                raise SystemExit(f"{len(miss)} of {len(want)} source tiles not done, e.g. {miss[:5]}")
        m = make_manifest(args.out, args.version, args.built)
        print(json.dumps({k: m[k] for k in ("format", "version", "built", "build")}),
              f"{len(m['shards'])} shards, {len(m['uniform'])} uniform")
    elif args.cmd == "stage":
        stage(args.out, args.stage, args.attribution)


def stage(out, dest, attribution):
    """The published folder: manifest.json, ATTRIBUTION.md and the shards the manifest names (not _done/_uniform)."""
    with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
        m = json.load(fh)
    os.makedirs(dest)
    for k in m["shards"]:
        os.makedirs(os.path.join(dest, k.split("/")[0]), exist_ok=True)
        shutil.copyfile(os.path.join(out, f"{k}.png"), os.path.join(dest, f"{k}.png"))
    shutil.copyfile(os.path.join(out, "manifest.json"), os.path.join(dest, "manifest.json"))
    shutil.copyfile(attribution, os.path.join(dest, "ATTRIBUTION.md"))


if __name__ == "__main__":
    main()
