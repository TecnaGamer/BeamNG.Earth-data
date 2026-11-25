#!/usr/bin/env python3
"""The bridge skew table (BeamNG.Earth's deck ends: roadshape's skew, the main repo's server/nbiskew.py): the US
National Bridge Inventory's skew angle (item 34) and structure length (item 49) at every structure's coordinates
(items 16 / 17), one npz of four columns, rows sorted by (lat, lon, skew, length):

  lat     float32   item 16, decimal degrees (DDMMSS.ss packed in the source)
  lon     float32   item 17, decimal degrees east (the source's are west for the states, east for Guam and the
                    Northern Mariana Islands)
  skew    uint8     item 34, degrees 0..90; 99 is the inventory's own "major variation" code, kept as it is
  length  uint16    item 49, the structure's length in decimetres (65535 at most; 0 unknown)

Only records of item 5A type 1 (the route carried ON the structure: one row per structure), with valid coordinates and
an item 34 of 0..90 or 99. Source: FHWA's delimited files of every state in one archive, only from fhwa.dot.gov
(public domain, US DOT):
  https://www.fhwa.dot.gov/bridge/nbi/<year>hwybronefiledel.zip      (comma separated, ' quoted, Latin-1)

    build_nbiskew.py fetch DEST.zip --year 2025       the archive (its host checked; ~53 MB)
    build_nbiskew.py build SRC.zip OUT --version v1   OUT/nbiskew.npz and OUT/manifest.json

Deterministic: the rows sorted, each .npy written by numpy's format writer and the zip written here with fixed member
dates, order and compression level (the same bytes for the same source on any machine with the pinned numpy and the
workflow's Python). The manifest's "build" is the first 16 hex of the npz's SHA-256. Needs numpy.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile

import numpy as np

HOST = "www.fhwa.dot.gov"
URL = "https://www.fhwa.dot.gov/bridge/nbi/{year}hwybronefiledel.zip"
UA = "BeamNG.Earth-data bridge skew builder"
MAX_ZIP = 400 << 20             # the archive's size at most (2025: 53 MB)
MAX_MEMBER = 3 << 30            # a member's uncompressed size at most (2025: ~1 GB of text)
FORMAT = 1
NAME = "nbiskew"
VARIES = 99
EAST = {"66", "69"}             # item 1 state codes whose longitudes are east (Guam, Northern Mariana Islands)
COLUMNS = (("lat", "<f4"), ("lon", "<f4"), ("skew", "|u1"), ("length", "<u2"))
csv.field_size_limit(1 << 20)


def dms(v, deg_digits):
    """NBI packed degrees-minutes-seconds (seconds to hundredths) -> decimal degrees, or None."""
    v = v.strip()
    if not v.isdigit() or len(v) < deg_digits + 4:
        return None
    v = v.zfill(deg_digits + 6)
    d, m, s = int(v[:deg_digits]), int(v[deg_digits:deg_digits + 2]), int(v[deg_digits + 2:]) / 100.0
    if m >= 60 or s >= 60 or d == 0:
        return None
    return d + m / 60.0 + s / 3600.0


def rows_of(text_stream):
    """(lat, lon, skew, length dm) of the delimited file's type-1 records with coordinates and a skew."""
    out = []
    for r in csv.DictReader(text_stream, quotechar="'"):
        if (r.get("RECORD_TYPE_005A") or "").strip() != "1":
            continue
        try:
            sk = int((r.get("DEGREES_SKEW_034") or "").strip())
        except ValueError:
            continue
        if not (0 <= sk <= 90 or sk == VARIES):
            continue
        la, lo = dms(r.get("LAT_016") or "", 2), dms(r.get("LONG_017") or "", 3)
        if la is None or lo is None or not 0 < la < 90 or not 0 < lo < 180:
            continue
        if (r.get("STATE_CODE_001") or "").strip() not in EAST:
            lo = -lo
        try:
            ln = float((r.get("STRUCTURE_LEN_MT_049") or "").strip() or 0.0)
        except ValueError:
            ln = 0.0
        dm = int(round(ln * 10.0)) if ln > 0 else 0
        out.append((np.float32(la), np.float32(lo), sk, min(dm, 65535)))
    return out


def read_archive(src):
    """Every row of the archive's delimited members (.txt), member by member in name order."""
    rows = []
    with zipfile.ZipFile(src) as z:
        names = sorted(i.filename for i in z.infolist() if not i.is_dir())
        for name in names:
            info = z.getinfo(name)
            if not re.fullmatch(r"[0-9A-Za-z_. -]{1,80}\.txt", name) or "/" in name or "\\" in name:
                raise SystemExit(f"unexpected member {name!r}")
            if info.file_size > MAX_MEMBER:
                raise SystemExit(f"{name}: {info.file_size} bytes uncompressed")
            with z.open(info) as fh:
                rows += rows_of(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
    return rows


def npz_bytes(rows):
    """The table as npz bytes, deterministic (module doc)."""
    rows = sorted(rows, key=lambda r: (float(r[0]), float(r[1]), r[2], r[3]))
    cols = {
        "lat": np.array([r[0] for r in rows], "<f4"),
        "lon": np.array([r[1] for r in rows], "<f4"),
        "skew": np.array([r[2] for r in rows], "|u1"),
        "length": np.array([r[3] for r in rows], "<u2"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, dt in COLUMNS:
            a = io.BytesIO()
            np.lib.format.write_array(a, cols[name].astype(dt), version=(1, 0), allow_pickle=False)
            zi = zipfile.ZipInfo(name + ".npy", date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, a.getvalue(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue(), len(rows)


def build(src, out, version, year, built=None, min_records=100000):
    t0 = time.time()
    rows = read_archive(src)
    if len(rows) < min_records:
        raise SystemExit(f"{len(rows)} records: a cut-off source?")
    body, n = npz_bytes(rows)
    sha = hashlib.sha256(body).hexdigest()
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "nbiskew.npz"), "wb") as f:
        f.write(body)
    m = {"format": FORMAT, "name": NAME, "version": version,
         "built": built or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "build": sha[:16],
         "records": n, "source": URL.format(year=year), "source_year": int(year),
         "columns": {k: dt for k, dt in COLUMNS}, "varies": VARIES,
         "files": [{"path": "nbiskew.npz", "size": len(body), "sha256": sha}],
         "attribution": "US Department of Transportation, Federal Highway Administration: National Bridge Inventory "
                        f"({year}), public domain; items 16, 17, 34 and 49 of the structures carrying a route."}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    sk = np.array([r[2] for r in rows])
    print(json.dumps({"build": m["build"], "records": n, "size": len(body),
                      "skewed_5_90": int(((sk >= 5) & (sk <= 90)).sum()), "varies": int((sk == VARIES).sum()),
                      "seconds": round(time.time() - t0, 1)}))
    return m


def fetch(dest, year):
    url = URL.format(year=year)
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname != HOST:
        raise SystemExit(f"not FHWA's host: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as r:
        final = urllib.parse.urlsplit(r.geturl())
        if final.scheme != "https" or final.hostname != HOST:
            raise SystemExit(f"redirected off FHWA's host: {r.geturl()}")
        with open(dest + ".part", "wb") as fh:
            got = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                got += len(b)
                if got > MAX_ZIP:
                    raise SystemExit(f"over {MAX_ZIP} bytes")
                fh.write(b)
    if not zipfile.is_zipfile(dest + ".part"):
        raise SystemExit("not a zip archive")
    os.replace(dest + ".part", dest)
    print(f"{url}: {os.path.getsize(dest)} bytes")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("build")
    a.add_argument("src")
    a.add_argument("out")
    a.add_argument("--version", required=True)
    a.add_argument("--year", default="2025")
    a.add_argument("--built")
    a.add_argument("--min-records", type=int, default=100000)
    a = sub.add_parser("fetch")
    a.add_argument("dest")
    a.add_argument("--year", default="2025")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"20[2-9][0-9]", args.year):
        raise SystemExit("bad year")
    if args.cmd == "build":
        if not re.fullmatch(r"v[1-9][0-9]{0,3}", args.version):
            raise SystemExit("bad version")
        build(args.src, args.out, args.version, args.year, args.built, args.min_records)
    else:
        fetch(args.dest, args.year)


if __name__ == "__main__":
    sys.exit(main())
