#!/usr/bin/env python3
"""The data workflow's (.github/workflows/rail.yml) checker for the PMTiles datasets whose parts go to the tile host
(Cloudflare R2 bnge-tiles behind tiles.tgforever.net): osmtags, osmyears, osmcolours, tallbld, osmareas. Each is built
in a job WITHOUT secrets (pip packages, the builders) and handed over as an artifact; the job holding the R2 secrets
runs only this script (standard library only, python3 -I) and aws-cli.

  seal ART NAME       [build job] ART/NAME checked, then ART/NAME.seal.json written: every file's path, size and
                      SHA-256 (osmtags' manifest has no hashes of its own; the seal also pins the manifest's bytes)
  check ART NAME      [publish job] ART holds exactly NAME/ and NAME.seal.json; NAME/ holds exactly manifest.json and
                      the manifest's parts (no links, no other files or folders, plain names only); every part a
                      PMTiles v3 archive under 512 MB with the manifest's size, SHA-256 and head SHA-256, and the
                      seal's; the dataset's own minimums (osmareas: zoom 14, format 1, over 100,000 polygons)
  key ART NAME        the build's folder on the tile host (keyed builds: the manifest's "key"; osmtags: its "built"
                      with every run of other characters made "_", as the job did before)
  todo ART NAME --listing L [--complete]
                      L: aws s3api list-objects-v2 of NAME/<key>/. Refuses a key in that folder that is not one of
                      the parts, or one with another size (a published folder is never overwritten); prints
                      "<local path> <bucket key>" for each part not there yet. --complete: every part there with its
                      size, or refused.
  point ART NAME OUT  the manifest with each part's "archive" made its URL on the tile host, written to OUT (the
                      bytes the jobs published before: json.dump(m, separators=(",", ":")))

The names uploaded come from the checked manifest, never from a listing of the artifact. Exit 1 (REFUSED: ...) on
anything unexpected.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys

BUCKET = "bnge-tiles"
CDN = "https://tiles.tgforever.net"
MAX_PART = 536870912            # bytes, exclusive (Cloudflare's free plan caches files up to 512 MB)
MAX_PARTS = 999                 # <prefix>-NNN.pmtiles
MAX_MANIFEST = 4 << 20
HEAD_BYTES = 16384              # build_osm_building_years.HEAD_BYTES, build_tall_index.HEAD_BYTES
MAGIC = b"PMTiles\x03"
KEY_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")                # write_build / write_pages: build time, parts' digest
BUILT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HEX64 = re.compile(r"[0-9a-f]{64}")
# layout "keyed": NAME/<key>/<prefix>-NNN.pmtiles (each part's "archive" is "<key>/<file>", sha256 required);
# "flat": NAME/<prefix>-NNN.pmtiles (osmtags: no key, no hashes in the manifest; the folder is its "built")
PROFILES = {
    "osmtags": {"layout": "flat"},
    "osmyears": {"layout": "keyed"},
    "osmcolours": {"layout": "keyed", "show": ("rows", "facade", "roof", "unread")},
    "tallbld": {"layout": "keyed"},
    "osmareas": {"layout": "keyed", "zoom": 14, "format": 1, "min_polygons": 100000, "show": ("rows", "counts")},
}


class Refused(Exception):
    pass


def refuse(msg):
    raise Refused(msg)


def profile(name):
    if name not in PROFILES:
        refuse(f"unknown dataset {name!r}")
    return PROFILES[name]


def part_re(name):
    return re.compile(rf"{re.escape(name)}-(\d{{3}})\.pmtiles")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def sha256_file(path, head=None):
    h, hh, n = hashlib.sha256(), hashlib.sha256(), 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
            if n < HEAD_BYTES:
                hh.update(b[:HEAD_BYTES - n])
            n += len(b)
    return h.hexdigest(), hh.hexdigest()


def tree(root):
    """{relative path: size} of every regular file under root; refuses links and special files (lstat: nothing is
    followed), names that are not plain."""
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        refuse(f"{root}: not a folder")
    out, dirs = {}, []

    def rec(path, rel):
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            if not re.fullmatch(r"[0-9A-Za-z_][0-9A-Za-z_.-]{0,127}", e.name):
                refuse(f"{r!r}: not a plain name")
            s = os.lstat(e.path)
            if stat.S_ISLNK(s.st_mode):
                refuse(f"{r}: a link")
            if stat.S_ISDIR(s.st_mode):
                dirs.append(r)
                rec(e.path, r)
            elif stat.S_ISREG(s.st_mode):
                out[r] = s.st_size
            else:
                refuse(f"{r}: not a regular file")

    rec(root, "")
    return out, dirs


def read_small(path, limit):
    with open(path, "rb") as f:
        body = f.read(limit + 1)
    if len(body) > limit:
        refuse(f"{path}: over {limit} bytes")
    return body


def load_manifest(d):
    m = json.loads(read_small(os.path.join(d, "manifest.json"), MAX_MANIFEST).decode("utf-8"))
    if not isinstance(m, dict):
        refuse("manifest.json: not an object")
    return m


def build_key(name, m):
    if profile(name)["layout"] == "keyed":
        k = m.get("key")
        if not isinstance(k, str) or not KEY_RE.fullmatch(k):
            refuse(f"manifest key {k!r}")
        return k
    b = m.get("built")
    if not isinstance(b, str) or not BUILT_RE.fullmatch(b):
        refuse(f"manifest built {b!r}")
    return re.sub(r"[^0-9A-Za-z]+", "_", b)


def check_dataset(d, name):
    """The manifest of the dataset folder d, its key and its parts [(local relative path, file name, part record)],
    every check of the module's docstring made but the seal's."""
    prof = profile(name)
    files, dirs = tree(d)
    if "manifest.json" not in files:
        refuse("no manifest.json")
    m = load_manifest(d)
    key = build_key(name, m)
    if "archive" in m:
        refuse("a single-archive manifest (over the 512 MB a part may have)")
    parts = m.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS:
        refuse(f"parts: {type(parts).__name__} of {len(parts) if isinstance(parts, list) else '-'}")
    for f in ("zoom", "format"):
        if f in prof and m.get(f) != prof[f]:
            refuse(f"manifest {f} {m.get(f)!r}, not {prof[f]}")
    if "min_polygons" in prof:
        c = m.get("counts")
        n = c.get("polygons") if isinstance(c, dict) else None
        if not is_int(n) or n <= prof["min_polygons"]:
            # (a cut-off planet stream makes a small build: not published)
            refuse(f"counts.polygons {n!r}: at most {prof['min_polygons']}")
    for f in prof.get("show", ()):
        if f not in m:
            refuse(f"manifest: no {f}")
    keyed = prof["layout"] == "keyed"
    want_dirs = [key] if keyed else []
    if dirs != want_dirs:
        refuse(f"folders {dirs}, not {want_dirs}")
    pre = f"{key}/" if keyed else ""
    pr = part_re(name)
    out, last, total = [], None, 0
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            refuse(f"part {i}: not an object")
        fn = f"{name}-{i:03d}.pmtiles"
        rel = pre + fn
        if p.get("archive") != rel or not pr.fullmatch(fn):
            refuse(f"part {i}: archive {p.get('archive')!r}, not {rel!r}")
        if rel not in files:
            refuse(f"{rel}: missing")
        size = files[rel]
        for f in ("bytes", "first", "last", "tiles"):
            if not is_int(p.get(f)) or p[f] < 0:
                refuse(f"{rel}: {f} {p.get(f)!r}")
        if p["bytes"] != size or not 0 < size < MAX_PART:
            refuse(f"{rel}: {size} bytes, the manifest says {p['bytes']} (under {MAX_PART})")
        if p["last"] < p["first"] or (last is not None and p["first"] <= last):
            refuse(f"{rel}: tile ids {p['first']}..{p['last']} out of order")
        last = p["last"]
        with open(os.path.join(d, rel), "rb") as fh:
            if fh.read(len(MAGIC)) != MAGIC:
                refuse(f"{rel}: not a PMTiles v3 archive")
        if keyed or "sha256" in p or "head_sha256" in p:
            for f in ("sha256", "head_sha256"):
                if not isinstance(p.get(f), str) or not HEX64.fullmatch(p[f]):
                    refuse(f"{rel}: {f} {p.get(f)!r}")
            full, head = sha256_file(os.path.join(d, rel))
            if full != p["sha256"] or head != p["head_sha256"]:
                refuse(f"{rel}: SHA-256 differs from the manifest's")
        total += size
        out.append((rel, fn, p))
    if m.get("bytes") != total:
        refuse(f"manifest bytes {m.get('bytes')!r}, the parts have {total}")
    extra = sorted(set(files) - {"manifest.json"} - {r for r, _, _ in out})
    if extra:
        refuse(f"files the manifest does not name: {extra[:10]}")
    return m, key, out


def seal_path(art, name):
    return os.path.join(art, f"{name}.seal.json")


def seal_records(d):
    files, _ = tree(d)
    return [{"path": rel, "bytes": size, "sha256": sha256_file(os.path.join(d, rel))[0]}
            for rel, size in sorted(files.items())]


def cmd_seal(a):
    d = os.path.join(a.art, a.name)
    m, key, parts = check_dataset(d, a.name)
    doc = {"format": 1, "name": a.name, "key": key, "files": seal_records(d)}
    with open(seal_path(a.art, a.name), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"{a.name}: sealed {len(doc['files'])} files, key {key}")


def checked(art, name):
    """check: the artifact folder art as the module's docstring says; (manifest, key, parts)."""
    profile(name)
    st = os.lstat(art)
    if not stat.S_ISDIR(st.st_mode):
        refuse(f"{art}: not a folder")
    with os.scandir(art) as it:
        top = {e.name: os.lstat(e.path).st_mode for e in it}
    sp = f"{name}.seal.json"
    if set(top) != {name, sp} or not stat.S_ISDIR(top[name]) or not stat.S_ISREG(top[sp]):
        refuse(f"the artifact holds {sorted(top)[:12]}, not only the folder {name}/ and the file {sp}")
    d = os.path.join(art, name)
    m, key, parts = check_dataset(d, name)
    seal = json.loads(read_small(seal_path(art, name), MAX_MANIFEST).decode("utf-8"))
    if not isinstance(seal, dict) or seal.get("format") != 1 or seal.get("name") != name or seal.get("key") != key:
        refuse("the seal is another build's")
    if seal.get("files") != seal_records(d):
        refuse("the files differ from the seal (size or SHA-256)")
    return m, key, parts


def cmd_check(a):
    m, key, parts = checked(a.art, a.name)
    show = "".join(f", {f} {json.dumps(m.get(f), separators=(',', ':'))}" for f in profile(a.name).get("show", ()))
    print(f"{a.name}: {len(parts)} parts checked, {m['bytes']} bytes, key {key}{show}")


def cmd_key(a):
    _, key, _ = checked(a.art, a.name)
    print(key)


def listing(path, prefix):
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    doc = json.loads(text) if text else {}
    if not isinstance(doc, dict):
        refuse("listing: not an object")
    out = {}
    for c in doc.get("Contents") or []:
        k = c.get("Key", "")
        if not isinstance(k, str) or not k.startswith(prefix):
            refuse(f"listing key {k!r} outside {prefix}")
        out[k[len(prefix):]] = int(c.get("Size", -1))
    return out


def cmd_todo(a):
    _, key, parts = checked(a.art, a.name)
    prefix = f"{a.name}/{key}/"
    there = listing(a.listing, prefix)
    want = {fn: p["bytes"] for _, fn, p in parts}
    stray = sorted(set(there) - set(want))
    if stray:
        refuse(f"{prefix} holds keys that are not this build's parts: {stray[:10]}")
    for fn, size in sorted(there.items()):
        if size != want[fn]:
            refuse(f"{prefix}{fn}: in the bucket with {size} bytes, the part has {want[fn]} (never overwritten)")
    missing = [(rel, fn) for rel, fn, _ in parts if fn not in there]
    if a.complete:
        if missing:
            refuse(f"{prefix}: {len(missing)} parts not in the bucket: {[fn for _, fn in missing][:10]}")
        print(f"{prefix}: all {len(parts)} parts in the bucket with their sizes", file=sys.stderr)
        return
    for rel, fn in missing:
        print(f"{os.path.join(a.art, a.name, rel)} {prefix}{fn}")
    print(f"{prefix}: {len(missing)} of {len(parts)} parts to upload ({len(there)} there already)", file=sys.stderr)


def cmd_point(a):
    checked(a.art, a.name)
    with open(os.path.join(a.art, a.name, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    base = f"{CDN}/{a.name}" + ("" if profile(a.name)["layout"] == "keyed" else f"/{build_key(a.name, m)}")
    for p in m["parts"]:
        p["archive"] = f"{base}/{p['archive']}"
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(m, f, separators=(",", ":"))
    print(f"{a.out}: {len(m['parts'])} parts at {base}/", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("seal", "check", "key", "todo", "point"):
        p = sub.add_parser(c)
        p.add_argument("art")
        p.add_argument("name")
        if c == "todo":
            p.add_argument("--listing", required=True)
            p.add_argument("--complete", action="store_true")
        if c == "point":
            p.add_argument("out")
    a = ap.parse_args()
    cmds = {"seal": cmd_seal, "check": cmd_check, "key": cmd_key, "todo": cmd_todo, "point": cmd_point}
    try:
        cmds[a.cmd](a)
    except (Refused, OSError, ValueError, KeyError, TypeError) as e:
        print(f"REFUSED: {type(e).__name__ + ': ' if not isinstance(e, Refused) else ''}{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
