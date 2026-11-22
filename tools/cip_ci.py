#!/usr/bin/env python3
"""The cip workflow's own steps (.github/workflows/cip.yml) around BeamNG.Earth's tools/build_cip.py (architecture v2's
compact per-cell inputs), which runs only in jobs WITHOUT secrets (the main repo's code at a pinned commit). This script
is this repo's own, standard library only: the jobs holding the R2 secrets run nothing else (and aws-cli). A copy of
bnwt_ci.py's checks for the CIP layout: one archive "cells" per build, under cip<FORMAT>/<release>/<build>/.

    cip_ci.py plan --build B --release R --format F --listing L.txt --shards 4096 --jobs 200 [--only "0 1"]
        the matrix: unless the build is published (cells.index.json in the listing), its shards without a record
        (_build/cells/<N>/<i>.json), a job's shards strided over the planet (a mix of ocean and land each).
    cip_ci.py check IN STAGE --release R --format F --build B
        a build job's artifact checked before upload: only regular files named as a shard's parts
        (<name>.s<N>-<IIII>.p<KKK>.pmtiles) or records (_build/<name>/<N>/<i>.json); every record's plan of this release
        and format, its parts on hand with the sizes and SHA-256s it gives, each under 512 MB (the CDN caches files
        up to 512 MB whole) and starting with a PMTiles v3 header. Moves the accepted shards' files to STAGE/parts and
        STAGE/records (a shard is uploaded whole or not at all).
    cip_ci.py published DIR --release R --format F --build B --listing L.txt
        build_cip --assemble's output (cells.index.json, the airports snapshot, ATTRIBUTION.txt, latest.json) checked
        before it is published: every part the index names is in the bucket's listing with its size, ids in order, the
        release, format and build right, the airports files' SHA-256 as the index says.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys

MAX_PART = 64 * 1000 * 1000               # (build_cip cuts parts at 32 MB: a CDN edge caches a part whole at its first read)
NAME_RE = r"cells"
BUILD_RE = re.compile(r"[0-9a-f]{16}")
PART_RE = re.compile(rf"({NAME_RE})\.s(\d+)-(\d{{4}})\.p(\d{{3}})\.pmtiles")
REC_RE = re.compile(rf"_build/({NAME_RE})/(\d+)/(\d+)\.json")
RELEASE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\d+")


def listing(path):
    """{key relative to the release folder: size} from `aws s3 ls --recursive` output."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split(None, 3)
            if len(p) == 4 and p[2].isdigit():
                out[p[3].strip()] = int(p[2])
    return out


def strip_prefix(keys, release, fmt, build):
    pre = f"cip{fmt}/{release}/{build}/"
    return {k[len(pre):]: v for k, v in keys.items() if k.startswith(pre)}


def plan(a):
    keys = strip_prefix(listing(a.listing), a.release, a.format, a.build) if a.listing else {}
    only = {int(v) for v in a.only.split()}
    if "cells.index.json" in keys:
        print(f"build {a.build}: published already", file=sys.stderr)
        print(json.dumps({"entries": []}))
        return
    done = {int(m.group(3)) for k in keys for m in [REC_RE.fullmatch(k)] if m and int(m.group(2)) == a.shards}
    todo = [i for i in range(a.shards) if i not in done and (not only or i in only)]
    print(f"build {a.build}: {len(done)} of {a.shards} shards done, {len(todo)} in this run", file=sys.stderr)
    n = max(1, min(len(todo), a.jobs, 250)) if todo else 0
    entries = [{"id": f"cells-{j:03d}", "shards": " ".join(map(str, todo[j::n]))} for j in range(n)]
    print(json.dumps({"entries": entries}, separators=(",", ":")))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def check(a):
    src = os.path.realpath(a.input)
    parts, recs, bad = {}, {}, []
    for root, dirs, files in os.walk(src):
        for n in dirs + files:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if os.path.islink(p):
                bad.append(f"{rel}: a link")
            elif n in files:
                if not os.path.isfile(p):
                    bad.append(f"{rel}: not a file")
                elif PART_RE.fullmatch(rel):
                    parts[rel] = p
                elif REC_RE.fullmatch(rel):
                    recs[rel] = p
                else:
                    bad.append(f"{rel}: not a part or record")
    ok, used = [], set()
    for rel, p in sorted(recs.items()):
        m = REC_RE.fullmatch(rel)
        name, sn, si = m.group(1), int(m.group(2)), int(m.group(3))
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            pl = r["plan"]
            if (r["shard"], r["of"]) != (si, sn) or not 0 <= si < sn:
                raise ValueError("another shard's record")
            if pl.get("build") != a.build or pl["inputs"]["code"]["overture"] != a.release \
                    or pl["inputs"]["code"]["cip"][0] != a.format:
                raise ValueError(f"another build: {pl.get('build')}")
            mine = []
            last = -1
            for q in r["parts"]:
                f = q["file"]
                pm = PART_RE.fullmatch(f)
                if not pm or pm.group(1) != name or (int(pm.group(2)), int(pm.group(3))) != (sn, si):
                    raise ValueError(f"part {f!r} is not this shard's")
                if f not in parts:
                    raise ValueError(f"part {f} missing")
                size = os.path.getsize(parts[f])
                if size != int(q["bytes"]) or size > MAX_PART:
                    raise ValueError(f"part {f}: {size} bytes, the record says {q['bytes']} (at most {MAX_PART})")
                with open(parts[f], "rb") as fh:
                    if fh.read(8) != b"PMTiles\x03":
                        raise ValueError(f"part {f}: not PMTiles v3")
                if sha256(parts[f]) != q["sha256"]:
                    raise ValueError(f"part {f}: SHA-256 differs")
                if int(q["first"]) <= last or int(q["last"]) < int(q["first"]):
                    raise ValueError(f"part {f}: tile ids out of order")
                last = int(q["last"])
                mine.append(f)
            ok.append((rel, mine))
            used.update(mine)
        except (OSError, ValueError, KeyError, TypeError) as e:
            bad.append(f"{rel}: {e}")
    for f in sorted(set(parts) - used):
        bad.append(f"{f}: no record names it (a shard cut off: built again next run)")
    for rel, mine in ok:
        for f in mine:
            dst = os.path.join(a.stage, "parts", f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(parts[f], dst)
        dst = os.path.join(a.stage, "records", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(recs[rel], dst)
    print(json.dumps({"shards": len(ok), "parts": len(used), "rejected": len(bad)}))
    for b in bad[:50]:
        print(f"::warning::{b}", file=sys.stderr)
    return 1 if any("no record" not in b for b in bad) else 0


def published(a):
    keys = strip_prefix(listing(a.listing), a.release, a.format, a.build)
    files = sorted(os.listdir(a.dir))
    for f in files:
        if f not in ("cells.index.json", "latest.json", "ATTRIBUTION.txt", "airports.csv.gz", "runways.csv.gz") or \
                os.path.islink(os.path.join(a.dir, f)):
            raise SystemExit(f"{f}: not an index, pointer, attribution or airports file")
    with open(os.path.join(a.dir, "cells.index.json"), encoding="utf-8") as fh:
        idx = json.load(fh)
    with open(os.path.join(a.dir, "latest.json"), encoding="utf-8") as fh:
        if json.load(fh) != {"build": a.build}:
            raise SystemExit("latest.json names another build")
    if idx.get("release") != a.release or idx.get("version") != a.format or idx.get("build") != a.build \
            or idx.get("format") != "cip":
        raise SystemExit(f"cells.index.json: another build ({idx.get('release')}, {idx.get('build')})")
    last = -1
    for p in idx["parts"]:
        if not PART_RE.fullmatch(p["file"]):
            raise SystemExit(f"part {p['file']!r}")
        if keys.get(p["file"]) != int(p["bytes"]):
            raise SystemExit(f"part {p['file']} is not in the bucket with {p['bytes']} bytes")
        if int(p["first"]) <= last:
            raise SystemExit(f"tile ids out of order at {p['file']}")
        last = int(p["last"])
    for n, h in (idx.get("airports") or {}).items():
        if sha256(os.path.join(a.dir, n)) != h:
            raise SystemExit(f"{n}: SHA-256 differs from the index")
    print(f"build {a.build}: {len(idx['parts'])} parts, {idx['cells']} cells, "
          f"{sum(int(p['bytes']) for p in idx['parts']) / 1e9:.1f} GB, all in the bucket")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--listing")
    p.add_argument("--release", required=True)
    p.add_argument("--build", required=True)
    p.add_argument("--format", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--jobs", type=int, default=200)
    p.add_argument("--only", default="")
    c = sub.add_parser("check")
    c.add_argument("input")
    c.add_argument("stage")
    c.add_argument("--release", required=True)
    c.add_argument("--build", required=True)
    c.add_argument("--format", type=int, required=True)
    q = sub.add_parser("published")
    q.add_argument("dir")
    q.add_argument("--release", required=True)
    q.add_argument("--build", required=True)
    q.add_argument("--format", type=int, required=True)
    q.add_argument("--listing", required=True)
    a = ap.parse_args()
    if getattr(a, "release", None) is not None and not RELEASE_RE.fullmatch(a.release):
        raise SystemExit(f"bad release {a.release!r}")
    if not BUILD_RE.fullmatch(a.build):
        raise SystemExit(f"bad build {a.build!r}")
    if a.cmd == "plan":
        if not 1 <= a.shards <= 65536:
            raise SystemExit("bad shards")
        return plan(a)
    if a.cmd == "check":
        return check(a)
    return published(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
