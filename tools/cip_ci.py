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
    cip_ci.py pointer OPEN.json
        the build a plan pointer (cip<FORMAT>/<release>/open/<plan key>.json, {"build": B}) names, or nothing.
    cip_ci.py pins --fresh FRESH.json --release R --format F --plan-key K --out PINS.json
                   [--resume RESUME.json --resume-build B --published 0|1 --explicit 0|1]
        the build's pin set (build_cip --write-pins: every weekly dataset's manifest, the inputs and the build id):
        the resumed build's (its pins.json from the bucket) when it is of the same code and switches as this run's
        fresh plan (FRESH, made by the pinned code) and not published yet, else (or none to resume) the fresh one.
        Each checked: the build id the SHA-256 of its inputs, its inputs' pins the SHA-256s of its manifests, this
        release and format. --explicit 1 (the dispatch named the build): anything but resuming it is an error.
        Prints build=<id> and new=true|false (new: its pins.json and the pointer are uploaded by the caller).
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


# inputs.data's parts that name weekly data builds (BeamNG.Earth tools/build_cip.py PIN_KEYS): two builds that differ
# only there are the same code and switches
PIN_KEYS = ("rails", "lanes", "osmtags", "stations", "pins")
MAX_PINS = 8 * 1000 * 1000


def build_id(inputs):
    """BeamNG.Earth server/cip.build_id: the hash of the whole inputs document."""
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def load_pins(path, release, fmt):
    """A pin set document, checked (raises ValueError)."""
    if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_PINS:
        raise ValueError(f"{path}: not a pin set file")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("format") != 1:
        raise ValueError("not a pin set of format 1")
    b, inputs, ds = doc.get("build"), doc.get("inputs"), doc.get("datasets")
    if not isinstance(b, str) or not BUILD_RE.fullmatch(b) or not isinstance(inputs, dict) or not isinstance(ds, dict):
        raise ValueError("no build, inputs or datasets")
    if build_id(inputs) != b:
        raise ValueError(f"build {b} is not its inputs' ({build_id(inputs)})")
    code, data = inputs.get("code"), inputs.get("data")
    if not isinstance(code, dict) or not isinstance(data, dict) or code.get("overture") != release \
            or not isinstance(code.get("cip"), list) or code["cip"][:1] != [fmt]:
        raise ValueError(f"inputs of another release or format ({code.get('overture') if isinstance(code, dict) else None})")
    pins = data.get("pins")
    if not isinstance(pins, dict):
        raise ValueError("its inputs pin no weekly data")
    names = {}
    for ident, body in ds.items():
        if not isinstance(ident, str) or "|" not in ident or (body is not None and not isinstance(body, str)):
            raise ValueError(f"dataset {ident!r}")
        name = ident.split("|", 1)[0]
        if name in names:
            raise ValueError(f"two datasets named {name}")
        names[name] = body
    if set(names) != set(pins):
        raise ValueError(f"datasets {sorted(names)} vs the inputs' pins {sorted(pins)}")
    for name, body in names.items():
        p = pins[name]
        if body is None:
            if p is not None:
                raise ValueError(f"{name}: pinned without a manifest")
            continue
        if not isinstance(json.loads(body), dict) or not isinstance(p, dict) \
                or p.get("sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest():
            raise ValueError(f"{name}: its manifest is not the one the inputs pin")
    return doc


def same_settings(a, b):
    """Are pin sets a and b of the same code and switches (their inputs alike but for the weekly data)?"""
    def cut(doc):
        i = doc["inputs"]
        return {"code": i["code"], "data": {k: v for k, v in i["data"].items() if k not in PIN_KEYS}}
    return cut(a) == cut(b)


def pointer(a):
    try:
        if os.path.islink(a.file) or os.path.getsize(a.file) > 4096:
            return
        with open(a.file, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return
    b = got.get("build") if isinstance(got, dict) else None
    if isinstance(b, str) and BUILD_RE.fullmatch(b):
        print(b)


def pins(a):
    fresh = load_pins(a.fresh, a.release, a.format)
    if not BUILD_RE.fullmatch(a.plan_key) or fresh.get("plan_key") != a.plan_key:
        raise SystemExit(f"the fresh pin set is of plan key {fresh.get('plan_key')!r}, not {a.plan_key!r}")
    chosen, src, why = fresh, a.fresh, "no build in progress: a fresh pin set"
    if a.resume_build:
        if not BUILD_RE.fullmatch(a.resume_build):
            raise SystemExit(f"bad build {a.resume_build!r}")
        try:
            got = load_pins(a.resume, a.release, a.format) if a.resume else None
            if got is None:
                raise ValueError("no pins.json in the bucket")
            if got["build"] != a.resume_build:
                raise ValueError(f"its pins.json is of build {got['build']}")
            if not same_settings(got, fresh):
                raise ValueError("of other code or switches than this run's")
            if a.published:
                raise ValueError("published already")
            chosen, src, why = got, a.resume, f"resuming build {a.resume_build} (its pin set)"
        except (OSError, ValueError) as e:
            if a.explicit:
                raise SystemExit(f"build {a.resume_build}: {e}")
            why = f"build {a.resume_build} not resumed ({e}): a fresh pin set"
    print(why, file=sys.stderr)
    shutil.copyfile(src, a.out)
    print(f"build={chosen['build']}")
    print(f"new={'true' if chosen is fresh else 'false'}")


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
    o = sub.add_parser("pointer")
    o.add_argument("file")
    n = sub.add_parser("pins")
    n.add_argument("--fresh", required=True)
    n.add_argument("--release", required=True)
    n.add_argument("--format", type=int, required=True)
    n.add_argument("--plan-key", required=True)
    n.add_argument("--out", required=True)
    n.add_argument("--resume")
    n.add_argument("--resume-build", default="")
    n.add_argument("--published", type=int, default=0)
    n.add_argument("--explicit", type=int, default=0)
    q = sub.add_parser("published")
    q.add_argument("dir")
    q.add_argument("--release", required=True)
    q.add_argument("--build", required=True)
    q.add_argument("--format", type=int, required=True)
    q.add_argument("--listing", required=True)
    a = ap.parse_args()
    if getattr(a, "release", None) is not None and not RELEASE_RE.fullmatch(a.release):
        raise SystemExit(f"bad release {a.release!r}")
    if a.cmd == "pointer":
        return pointer(a)
    if a.cmd == "pins":
        try:
            return pins(a)
        except (OSError, ValueError) as e:
            raise SystemExit(f"pins: {type(e).__name__}: {e}")
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
