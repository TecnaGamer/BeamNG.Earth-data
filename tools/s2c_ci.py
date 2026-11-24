#!/usr/bin/env python3
"""The tiles-s2c workflow's own steps (.github/workflows/tiles_s2c.yml, tiles_s2c-shards.yml) around this repo's pinned
copy of BeamNG.Earth's tools/build_s2c_packs.py (the 2025 Sentinel-2 composite packs, prefix s2c/v1 on the tile host,
Cloudflare R2 bnge-tiles behind tiles.tgforever.net). The builder runs only in jobs WITHOUT secrets; the jobs holding the
R2 secrets run nothing but this script (standard library only, python3 -I) and aws-cli.

    s2c_ci.py plan --listing L --planet tools/s2c_shards.txt --jobs 200 [--only "x,y ..."] [--region "x5,y5 ..."]
        the build matrix: the planet's zoom-8 shards (or --only / those under --region's zoom-5 tiles) without a record
        in the bucket (s2c/v1/_build/s2c/v1/shards/X_Y.json), strided over the jobs (a mix of regions each)
    s2c_ci.py coarse-plan --listing L --planet tools/s2c_shards.txt [--region ...]
        the zoom-5 regions whose shards all have records and no coarse record yet (zooms 10..8 from their zoom-11 packs)
    s2c_ci.py world-plan --listing L --planet tools/s2c_shards.txt
        {"ready": true} once every region has its coarse record and the world (zooms 7..3) has none
    s2c_ci.py check IN STAGE --pins tools/s2c_pins.json
        a build job's artifact checked before upload: only regular files (no links), named as packs ({z}/{px}/{py}.bngp)
        or records; every record of this format, builder (the SHA-256 of this checkout's tools/build_s2c_packs.py),
        Pillow and parameters (the pins); its packs on hand with the record's sizes and SHA-256s, each a BNGP pack of the
        record's shard (z, px, py inside it), every tile a WebP; nothing else in the artifact. Accepted shards' files
        moved to STAGE/packs and STAGE/records (a shard goes up whole or not at all; a refused one is reported).
    s2c_ci.py todo STAGE --listing L [--complete]
        L: `aws s3 ls --recursive` of the packs' columns. A staged pack already in the bucket with another size
        refuses the whole upload (published packs are never overwritten); with its size it is not sent again; prints
        "<local path> <bucket key>" for each pack to send. --complete: every staged pack in the bucket with its size.
    s2c_ci.py columns STAGE
        the bucket prefixes (s2c/v1/{z}/{px}/) to list for todo
    s2c_ci.py index --listing L --planet tools/s2c_shards.txt OUT
        once every planet shard, every coarse region and the world have their records: OUT/index.json (the shards and regions done,
        packs, bytes; written to the bucket LAST). Otherwise prints what is left and writes nothing.
    s2c_ci.py untar TAR OUT
        a home machine's output (a release asset, tools in README-s2c.md): only regular files with pack or record
        names, no links, no absolute paths or "..", at most MAX_FILES files and MAX_TAR bytes; extracted to OUT for the
        same check as a runner's artifact.

Exit 1 (REFUSED: ...) on anything unexpected.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tarfile

PREFIX = "s2c/v1"
FORMAT = 1
MAX_PACK = 64 << 20                 # (a zoom-14 pack is ~0.3-3 MB)
MAX_RECORD = 4 << 20
MAX_FILES = 400000
MAX_TAR = 64 << 30
PACK_RE = re.compile(r"(\d{1,2})/(\d{1,6})/(\d{1,6})\.bngp")
REC_RE = re.compile(r"_build/s2c/v1/(shards|coarse|world)/(\d{1,3})_(\d{1,3})\.json")
HEX64 = re.compile(r"[0-9a-f]{64}")
BNGP = struct.Struct("<4sBBBBII")
K = 3
SHARD_Z = 8


class Refused(Exception):
    pass


def refuse(msg):
    raise Refused(msg)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def listing(paths):
    """{bucket key: size} from `aws s3api list-objects-v2 --output json` files (an empty one: nothing there) or
    `aws s3 ls --recursive` output; several files merged."""
    out = {}
    for path in ([paths] if isinstance(paths, str) else paths or []):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.lstrip().startswith("{"):
            for o in json.loads(text).get("Contents") or []:
                out[str(o["Key"])] = int(o["Size"])
            continue
        for line in text.splitlines():
            p = line.split(None, 3)
            if len(p) == 4 and p[2].isdigit():
                out[p[3].strip()] = int(p[2])
    return out


def parse_xy(text, lim):
    out = []
    for tok in (text or "").split():
        m = re.fullmatch(r"(\d+),(\d+)", tok)
        if not m or int(m.group(1)) >= lim or int(m.group(2)) >= lim:
            refuse(f"bad tile {tok!r}")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def planet(path):
    with open(path, encoding="utf-8") as f:
        return parse_xy(f.read(), 1 << SHARD_Z)


def rec_key(kind, x, y):
    return f"{PREFIX}/_build/s2c/v1/{kind}/{x}_{y}.json"


def plan(a):
    keys = listing(a.listing) if a.listing else {}
    shards = parse_xy(a.only, 1 << SHARD_Z) if a.only.strip() else planet(a.planet)
    regions = set(parse_xy(a.region, 32))
    if regions:
        shards = [s for s in shards if (s[0] >> 3, s[1] >> 3) in regions]
    todo = [s for s in shards if rec_key("shards", *s) not in keys]
    print(f"{len(shards)} shards asked, {len(shards) - len(todo)} done, {len(todo)} in this run", file=sys.stderr)
    n = max(1, min(len(todo), a.jobs, 250)) if todo else 0
    entries = [{"id": f"s2c-{j:03d}", "shards": " ".join(f"{x},{y}" for x, y in todo[j::n])} for j in range(n)]
    print(json.dumps({"entries": entries}, separators=(",", ":")))


def coarse_plan(a):
    keys = listing(a.listing)
    shards = planet(a.planet)
    regions = set(parse_xy(a.region, 32))
    by = {}
    for x, y in shards:
        by.setdefault((x >> 3, y >> 3), []).append((x, y))
    ready = []
    for r, ss in sorted(by.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if regions and r not in regions:
            continue
        if rec_key("coarse", *r) in keys:
            continue
        if all(rec_key("shards", *s) in keys for s in ss):
            ready.append(r)
    print(f"{len(ready)} zoom-5 regions ready for zooms 10..8", file=sys.stderr)
    print(json.dumps({"regions": " ".join(f"{x},{y}" for x, y in ready)}))


def world_plan(a):
    """Zooms 7..3 once every region's coarse record is in the bucket and the world's is not."""
    keys = listing(a.listing)
    regions = {(x >> 3, y >> 3) for x, y in planet(a.planet)}
    left = [r for r in regions if rec_key("coarse", *r) not in keys]
    done = rec_key("world", 0, 0) in keys
    print(f"world: {len(left)} of {len(regions)} regions without zooms 10..8; world record {'there' if done else 'not there'}",
          file=sys.stderr)
    print(json.dumps({"ready": not left and not done}))


def check_pack(path, z, px, py, size_max=MAX_PACK):
    """A BNGP pack of (z, px, py), every tile a WebP."""
    size = os.path.getsize(path)
    if size > size_max or size < BNGP.size:
        refuse(f"{path}: {size} bytes")
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, k, pz, _, ppx, ppy = BNGP.unpack_from(data, 0)
    if magic != b"BNGP" or ver != 1 or k != K or (pz, ppx, ppy) != (z, px, py):
        refuse(f"{path}: not the BNGP pack {z}/{px}/{py}")
    n = (1 << K) ** 2
    base = BNGP.size + 8 * n
    tiles = 0
    for i in range(n):
        o, ln = struct.unpack_from("<II", data, BNGP.size + 8 * i)
        if ln:
            t = data[base + o:base + o + ln]
            if len(t) != ln or t[:4] != b"RIFF" or t[8:12] != b"WEBP":
                refuse(f"{path}: tile {i} is not a WebP")
            tiles += 1
    if not tiles:
        refuse(f"{path}: no tile")


def shard_packs_ok(kind, x, y, z, px, py):
    """Is pack (z, px, py) one that record (kind, x, y) may hold?"""
    if kind == "shards":
        if not 11 <= z <= 14:
            return False
        d = z - K - SHARD_Z
        return px >> d == x and py >> d == y
    if kind == "world":
        return 3 <= z <= 7 and (x, y) == (0, 0) and px < (1 << (z - K)) and py < (1 << (z - K))
    if not 8 <= z <= 10:
        return False
    d = z - K - 5
    return px >> d == x and py >> d == y


def check(a):
    with open(a.pins, encoding="utf-8") as f:
        pins = json.load(f)
    builder = sha256(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_s2c_packs.py"))
    src = os.path.realpath(a.input)
    packs, recs, bad = {}, {}, []
    for root, dirs, files in os.walk(src):
        for n in dirs + files:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if os.path.islink(p):
                bad.append(f"{rel}: a link")
            elif n in files:
                if not os.path.isfile(p):
                    bad.append(f"{rel}: not a file")
                elif PACK_RE.fullmatch(rel):
                    packs[rel] = p
                elif REC_RE.fullmatch(rel):
                    recs[rel] = p
                else:
                    bad.append(f"{rel}: not a pack or record")
    ok, used = [], set()
    for rel, p in sorted(recs.items()):
        m = REC_RE.fullmatch(rel)
        kind, x, y = m.group(1), int(m.group(2)), int(m.group(3))
        try:
            if os.path.getsize(p) > MAX_RECORD:
                refuse("record too big")
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            want = [x, y]
            if r.get("format") != FORMAT or r.get("shard" if kind == "shards" else kind) != want:
                refuse(f"another {kind[:-1]}'s record")
            if r.get("builder") != builder:
                refuse(f"builder {r.get('builder')}: not this checkout's tools/build_s2c_packs.py ({builder})")
            prm = r.get("params") or {}
            if prm.get("pillow") != pins["pillow"]:
                refuse(f"Pillow {prm.get('pillow')}: the pin is {pins['pillow']} (WebP bytes differ between versions)")
            if prm.get("lut") != pins["lut"] or prm.get("prefix") != PREFIX:
                refuse(f"tone table {prm.get('lut')} / prefix {prm.get('prefix')}: not the pinned")
            mine = []
            for q in r["packs"]:
                rp = q["path"]
                pm = PACK_RE.fullmatch(rp)
                if not pm or not shard_packs_ok(kind, x, y, *(int(v) for v in pm.groups())):
                    refuse(f"pack {rp!r} is not this {kind[:-1]}'s")
                if rp not in packs:
                    refuse(f"pack {rp} missing")
                if os.path.getsize(packs[rp]) != int(q["bytes"]) or not HEX64.fullmatch(str(q["sha256"])):
                    refuse(f"pack {rp}: {os.path.getsize(packs[rp])} bytes, the record says {q['bytes']}")
                check_pack(packs[rp], *(int(v) for v in pm.groups()))
                if sha256(packs[rp]) != q["sha256"]:
                    refuse(f"pack {rp}: SHA-256 differs from the record")
                if rp in used or rp in mine:
                    refuse(f"pack {rp} named twice")
                mine.append(rp)
            ok.append((rel, mine))
            used.update(mine)
        except (OSError, ValueError, KeyError, TypeError, Refused) as e:
            bad.append(f"{rel}: {e}")
    for f in sorted(set(packs) - used):
        bad.append(f"{f}: no record names it (a shard cut off: built again next run)")
    for rel, mine in ok:
        for f in mine:
            dst = os.path.join(a.stage, "packs", f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(packs[f], dst)
        dst = os.path.join(a.stage, "records", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(recs[rel], dst)
    print(json.dumps({"records": len(ok), "packs": len(used), "rejected": len(bad)}))
    for b in bad[:50]:
        print(f"::warning::{b}", file=sys.stderr)
    return 1 if any("no record" not in b for b in bad) else 0


def staged(stage):
    out = []
    root = os.path.join(stage, "packs")
    for dp, dn, fn in os.walk(root):
        for n in fn:
            rel = os.path.relpath(os.path.join(dp, n), root).replace(os.sep, "/")
            if not PACK_RE.fullmatch(rel) or os.path.islink(os.path.join(dp, n)):
                refuse(f"staged {rel!r}")
            out.append(rel)
    return sorted(out)


def columns(a):
    cols = sorted({rel.rsplit("/", 1)[0] for rel in staged(a.stage)})
    print("\n".join(f"{PREFIX}/{c}/" for c in cols))


def todo(a):
    keys = listing(a.listing)
    lines, missing = [], []
    for rel in staged(a.stage):
        p = os.path.join(a.stage, "packs", rel)
        k = f"{PREFIX}/{rel}"
        size = os.path.getsize(p)
        if k in keys:
            if keys[k] != size:
                refuse(f"{k} is in the bucket with {keys[k]} bytes, ours {size}: a published pack is never overwritten")
            continue
        missing.append(k)
        lines.append(f"{p} {k}")
    if a.complete:
        if missing:
            refuse(f"{len(missing)} packs not in the bucket after the upload, e.g. {missing[0]}")
        print(f"all {len(staged(a.stage))} packs in the bucket with their sizes")
        return
    print("\n".join(lines))


def index(a):
    keys = listing(a.listing)
    shards = planet(a.planet)
    regions = sorted({(x >> 3, y >> 3) for x, y in shards}, key=lambda t: (t[1], t[0]))
    left_s = [s for s in shards if rec_key("shards", *s) not in keys]
    left_r = [r for r in regions if rec_key("coarse", *r) not in keys]
    if rec_key("world", 0, 0) not in keys:
        left_r.append("world")
    if left_s or left_r:
        print(f"not complete: {len(left_s)} of {len(shards)} shards and {len(left_r)} of {len(regions)} regions left "
              f"(e.g. {(left_s or left_r)[0]}): no index", file=sys.stderr)
        return
    packs = {k: v for k, v in keys.items() if k.startswith(PREFIX + "/") and PACK_RE.fullmatch(k[len(PREFIX) + 1:])}
    by_zoom = {}
    for k, v in packs.items():
        z = int(k[len(PREFIX) + 1:].split("/", 1)[0])
        c = by_zoom.setdefault(str(z), [0, 0])
        c[0] += 1
        c[1] += v
    doc = {"format": FORMAT, "prefix": PREFIX, "shards": len(shards), "regions": len(regions),
           "zooms": {z: {"packs": c[0], "bytes": c[1]} for z, c in sorted(by_zoom.items(), key=lambda t: int(t[0]))},
           "credit": "Contains modified Copernicus Sentinel data 2024-2025"}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True, separators=(",", ":"))
    print(json.dumps(doc))


def untar(a):
    if os.path.getsize(a.tar) > MAX_TAR:
        refuse("tar too big")
    os.makedirs(a.out, exist_ok=True)
    n = 0
    with tarfile.open(a.tar, "r:*") as t:
        members = []
        for m in t:
            n += 1
            if n > MAX_FILES:
                refuse("too many files")
            name = m.name.removeprefix("./")
            if m.isdir():
                continue
            if not m.isreg():
                refuse(f"{m.name}: not a regular file")
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                refuse(f"{m.name}: path")
            if not (PACK_RE.fullmatch(name) or REC_RE.fullmatch(name)):
                refuse(f"{m.name}: not a pack or record name")
            m.name = name
            members.append(m)
        t.extractall(a.out, members=members, filter="data")
    print(f"{len(members)} files from {os.path.basename(a.tar)}")


PLAN_RE = re.compile(r"(tiles_-?\d+\.\d{6}_-?\d+\.\d{6}_-?\d+\.\d{6}_-?\d+\.\d{6}|scenes_MGRS-\d{2}[C-X][A-Z]{2})\.json")


def untar_plan(a):
    """The frozen STAC answers (build_s2c_packs.py --stac-only's cache, tarred): JSON files with the cache's names only,
    regular files, at most 200 MB unpacked."""
    os.makedirs(a.out, exist_ok=True)
    total = 0
    with tarfile.open(a.tar, "r:gz") as t:
        members = []
        for m in t:
            if m.isdir():
                continue
            name = m.name.removeprefix("./")
            if not m.isreg() or "/" in name or not PLAN_RE.fullmatch(name):
                refuse(f"{m.name}: not a plan file")
            total += m.size
            if total > 200 << 20 or len(members) > 400000:
                refuse("plan too big")
            m.name = name
            members.append(m)
        t.extractall(a.out, members=members, filter="data")
    for m in members:
        with open(os.path.join(a.out, m.name), encoding="utf-8") as f:
            json.load(f)
    print(f"{len(members)} plan files")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--listing", action="append")
    p.add_argument("--planet", required=True)
    p.add_argument("--jobs", type=int, default=200)
    p.add_argument("--only", default="")
    p.add_argument("--region", default="")
    c = sub.add_parser("coarse-plan")
    c.add_argument("--listing", action="append", required=True)
    c.add_argument("--planet", required=True)
    c.add_argument("--region", default="")
    wp = sub.add_parser("world-plan")
    wp.add_argument("--listing", action="append", required=True)
    wp.add_argument("--planet", required=True)
    k = sub.add_parser("check")
    k.add_argument("input")
    k.add_argument("stage")
    k.add_argument("--pins", required=True)
    t = sub.add_parser("todo")
    t.add_argument("stage")
    t.add_argument("--listing", action="append", required=True)
    t.add_argument("--complete", action="store_true")
    co = sub.add_parser("columns")
    co.add_argument("stage")
    i = sub.add_parser("index")
    i.add_argument("out")
    i.add_argument("--listing", action="append", required=True)
    i.add_argument("--planet", required=True)
    u = sub.add_parser("untar")
    u.add_argument("tar")
    u.add_argument("out")
    up = sub.add_parser("untar-plan")
    up.add_argument("tar")
    up.add_argument("out")
    a = ap.parse_args()
    try:
        return {"plan": plan, "coarse-plan": coarse_plan, "world-plan": world_plan, "check": check, "todo": todo, "columns": columns,
                "index": index, "untar": untar, "untar-plan": untar_plan}[a.cmd](a) or 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
