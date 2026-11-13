#!/usr/bin/env python3
"""The bnwt workflow's own steps (.github/workflows/bnwt.yml) around BeamNG.Earth's tools/build_bnwt.py, which runs only
in jobs WITHOUT secrets (it is the main repo's code at a pinned commit). This script is this repo's own, standard library
only: the jobs holding the R2 secrets run nothing else (and aws-cli).

    bnwt_ci.py plan --names '{"base": "base.c1", ...}' --release R --format F --listing L.txt --shards 4096 \
        --jobs 200 [--only "0 1"]
        the matrix: for each archive not published yet (no <name>.index.json in the listing) its shards without a
        record (_build/<name>/<N>/<i>.json in the listing), spread over build jobs by the archives' estimated work,
        a job's shards strided over the archive (each job gets a mix of ocean and land). Prints {"entries": [...]}.
    bnwt_ci.py check IN STAGE --release R --format F
        a build job's artifact checked before upload: only regular files named as a shard's parts
        (<name>.s<N>-<IIII>.p<KKK>.pmtiles) or records (_build/<name>/<N>/<i>.json); every record's plan of this release
        and format, its parts on hand with the sizes and SHA-256s it gives, each under 512 MB (the CDN caches files
        up to 512 MB whole) and starting with a PMTiles v3 header. Moves the accepted shards' files to STAGE/parts and
        STAGE/records (a shard is uploaded whole or not at all).
    bnwt_ci.py published DIR --release R --format F --listing L.txt
        build_bnwt --assemble's output (index, manifest, attribution) checked before it is published: every part the
        index names is in the bucket's listing with the manifest's size, ids in order, the release and format right.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys

MAX_PART = 512 * 1000 * 1000 - 1         # (Cloudflare caches files up to 512 MB; build_bnwt cuts parts at 400 MB)
NAME_RE = r"(?:base|transportation|buildings|places)\.c\d+"
PART_RE = re.compile(rf"({NAME_RE})\.s(\d+)-(\d{{4}})\.p(\d{{3}})\.pmtiles")
REC_RE = re.compile(rf"_build/({NAME_RE})/(\d+)/(\d+)\.json")
RELEASE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\d+")
# estimated runner-hours of an archive's planet build (2026-11-13: base ~190, transportation ~200, buildings ~70,
# places ~50 on 4-vCPU runners): its share of the build jobs
WORK = {"base": 0.35, "transportation": 0.35, "buildings": 0.17, "places": 0.13}


def listing(path):
    """{key relative to the release folder: size} from `aws s3 ls --recursive` output."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split(None, 3)
            if len(p) == 4 and p[2].isdigit():
                out[p[3].strip()] = int(p[2])
    return out


def strip_prefix(keys, release, fmt):
    pre = f"bnwt{fmt}/{release}/"
    return {k[len(pre):]: v for k, v in keys.items() if k.startswith(pre)}


def plan(a):
    names = json.loads(a.names)
    keys = strip_prefix(listing(a.listing), a.release, a.format) if a.listing else {}
    only = {int(v) for v in a.only.split()}
    left = {}
    for arch in a.archives.split():
        name = names[arch]
        if not re.fullmatch(NAME_RE, name) or not name.startswith(arch + "."):
            raise SystemExit(f"bad archive name {name!r}")
        if f"{name}.index.json" in keys:
            print(f"{arch}: published already ({name}.index.json)", file=sys.stderr)
            continue
        done = {int(m.group(3)) for k in keys for m in [REC_RE.fullmatch(k)] if m and m.group(1) == name
                and int(m.group(2)) == a.shards}
        todo = [i for i in range(a.shards) if i not in done and (not only or i in only)]
        print(f"{arch}: {len(done)} of {a.shards} shards done, {len(todo)} in this run", file=sys.stderr)
        if todo:
            left[arch] = todo
    entries = []
    if left:
        jobs = max(len(left), min(a.jobs, 250))
        tot = sum(WORK[k] * len(v) / a.shards for k, v in left.items())
        for arch, todo in left.items():
            n = max(1, min(len(todo), round(jobs * WORK[arch] * len(todo) / a.shards / tot)))
            for j in range(n):
                s = todo[j::n]
                entries.append({"id": f"{arch}-{j:03d}", "archive": arch, "name": names[arch],
                                "shards": " ".join(map(str, s))})
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
            if pl["release"] != a.release or pl["format"] != a.format or f"{pl['archive']}.c{pl['content']}" != name:
                raise ValueError(f"another build: {pl}")
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
    keys = strip_prefix(listing(a.listing), a.release, a.format)
    files = sorted(os.listdir(a.dir))
    for f in files:
        if not re.fullmatch(rf"({NAME_RE})\.(index|manifest)\.json|ATTRIBUTION-overture-[a-z]+\.txt", f) or \
                os.path.islink(os.path.join(a.dir, f)):
            raise SystemExit(f"{f}: not an index, manifest or attribution file")
    for f in files:
        if not f.endswith(".index.json"):
            continue
        name = f[:-len(".index.json")]
        with open(os.path.join(a.dir, f), encoding="utf-8") as fh:
            idx = json.load(fh)
        with open(os.path.join(a.dir, f"{name}.manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        if idx.get("release") != a.release or idx.get("version") != a.format or f"{idx['archive']}.c{idx['content']}" != name:
            raise SystemExit(f"{f}: another build ({idx.get('release')}, format {idx.get('version')})")
        if [p["file"] for p in idx["parts"]] != [p["file"] for p in man["parts"]]:
            raise SystemExit(f"{f}: the index and the manifest name other parts")
        last = -1
        for p in man["parts"]:
            if not PART_RE.fullmatch(p["file"]) or not p["file"].startswith(name + "."):
                raise SystemExit(f"{f}: part {p['file']!r}")
            if keys.get(p["file"]) != int(p["bytes"]):
                raise SystemExit(f"{f}: part {p['file']} is not in the bucket with {p['bytes']} bytes")
            if int(p["first"]) <= last:
                raise SystemExit(f"{f}: tile ids out of order at {p['file']}")
            last = int(p["last"])
        print(f"{name}: {len(man['parts'])} parts, {man['tiles']} tiles, {man['bytes'] / 1e9:.1f} GB, all in the bucket")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--names", required=True)
    p.add_argument("--archives", default="base transportation buildings places")
    p.add_argument("--listing")
    p.add_argument("--release", required=True)
    p.add_argument("--format", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--jobs", type=int, default=200)
    p.add_argument("--only", default="")
    c = sub.add_parser("check")
    c.add_argument("input")
    c.add_argument("stage")
    c.add_argument("--release", required=True)
    c.add_argument("--format", type=int, required=True)
    q = sub.add_parser("published")
    q.add_argument("dir")
    q.add_argument("--release", required=True)
    q.add_argument("--format", type=int, required=True)
    q.add_argument("--listing", required=True)
    a = ap.parse_args()
    if getattr(a, "release", None) is not None and not RELEASE_RE.fullmatch(a.release):
        raise SystemExit(f"bad release {a.release!r}")
    if a.cmd == "plan":
        if not 1 <= a.shards <= 65536:
            raise SystemExit("bad shards")
        return plan(a)
    if a.cmd == "check":
        return check(a)
    return published(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
