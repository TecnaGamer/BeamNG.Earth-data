#!/usr/bin/env python3
"""The data workflow's (.github/workflows/rail.yml) checker for the weekly z7 shard datasets (rail, fra, lanes, signs,
stations, drive: <name>/<x>/<y>.json.gz, manifest.json, planet.txt), whose files go to the tile host (Cloudflare R2
bnge-tiles behind tiles.tgforever.net) under data/<name>/<key>/ before their manifest goes on Pages naming that folder
("data"). Each is built in a job WITHOUT secrets (pip packages, BeamNG.Earth's builders) and handed over as an artifact;
the job holding the R2 secrets runs only this script (standard library only, python3 -I) and aws-cli
(tools/shard_publish.sh).

Why (BeamNG.Earth 2026-11-24, ~/bnge-tests/manifestaudit R3): a dataset whose manifest names no "data" folder is read
from Pages, whatever build Pages serves at that moment, and a server kept those files under the build it had in use:
two builds under one key. tools/mirror_data.py mirrored the datasets in the site job, but continue-on-error: a failed
mirror published the week's manifest without "data". Now a dataset's manifest reaches Pages only after its whole
build is on the tile host, and a failed publish keeps last week's manifest and files (the site job's fallback).

  seal ART NAME       [build job] ART/NAME checked, then ART/NAME.seal.json written: every file's path, size and SHA-256
  check ART NAME      [publish job] ART holds NAME/ and NAME.seal.json (and other shard datasets' only); NAME/ holds
                      exactly manifest.json, an
                      optional planet.txt and <x>/<y>.json.gz shards (x, y < 128; plain names, no links, nothing else);
                      every file the seal's size and SHA-256; every shard a gzip of one JSON object; as many shards as
                      the manifest's "shards", at least the dataset's minimum (a cut-off planet stream makes a small
                      build); the manifest's "built" a UTC time, no "data" of its own
  key ART NAME        the build's folder on the tile host: "<built, each run of other characters "_", 40 at most>-<12 hex
                      of the files' digest>" (tools/mirror_data.py's key: a build it mirrored is the same folder)
  todo ART NAME --listing L [--complete]
                      L: aws s3api list-objects-v2 of data/NAME/<key>/. Refuses a key there that is not one of the
                      build's files, or one with another size (a published folder is never overwritten); prints
                      "<local path> <bucket key>" for each file not there yet (manifest.json last: the marker of a whole
                      copy). --complete: every file there with its size, or refused; prints "whole" when the
                      marker (manifest.json) is there too (never uploaded again), else "files".
  remote ART NAME OUT the manifest uploaded to data/NAME/<key>/manifest.json (with its "data"), written to OUT
  verify ART NAME     one shard read back from the public host, its SHA-256 the seal's (5 tries)
  point ART NAME OUT  OUT/NAME/: the build's files for Pages (clients from before "data" read them there) and its
                      manifest naming the tile host's folder ("data"), json.dump(m, separators=(",", ":"))

The names uploaded come from the checked tree and seal, never from the listing. Exit 1 (REFUSED: ...) on anything
unexpected.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.request

BUCKET = "bnge-tiles"
PREFIX = "data"
CDN = "https://tiles.tgforever.net"
MAX_MANIFEST = 1 << 20
MAX_SHARD = 256 << 20           # bytes stored (the densest lane shard is ~40 MB)
MAX_INFLATED = 2 << 30          # bytes a shard may inflate to
BUILT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SHARD_RE = re.compile(r"(\d{1,3})/(\d{1,3})\.json\.gz")
KEY_RE = re.compile(r"[0-9A-Za-z_]{1,40}-[0-9a-f]{12}")
# a build with fewer shards than this is not published (the live builds of 2026-11-24: rail 1968, lanes 2182,
# stations 2551, signs 1293, fra 190)
PROFILES = {"rail": 1500, "fra": 150, "lanes": 1500, "signs": 900, "stations": 2000, "drive": 300}


class Refused(Exception):
    pass


def refuse(msg):
    raise Refused(msg)


def minimum(name):
    if name not in PROFILES:
        refuse(f"unknown dataset {name!r}")
    return PROFILES[name]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def tree(root):
    """{relative path: size} of every regular file under root, [folders]; refuses links, special files and names
    that are not plain (lstat: nothing is followed)."""
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


def load_manifest(d):
    with open(os.path.join(d, "manifest.json"), "rb") as f:
        body = f.read(MAX_MANIFEST + 1)
    if len(body) > MAX_MANIFEST:
        refuse("manifest.json: too big")
    m = json.loads(body.decode("utf-8"))
    if not isinstance(m, dict):
        refuse("manifest.json: not an object")
    return m


def shard_ok(path):
    """A gzip of one JSON object, inflating to at most MAX_INFLATED."""
    with gzip.open(path, "rb") as f:
        body = f.read(MAX_INFLATED + 1)
    if len(body) > MAX_INFLATED:
        refuse(f"{path}: inflates past {MAX_INFLATED} bytes")
    if not isinstance(json.loads(body.decode("utf-8")), dict):
        refuse(f"{path}: not a JSON object")


def check_dataset(d, name, deep=True):
    """(manifest, {relative path: size} of the build's files but manifest.json); every check of the docstring but the
    seal's."""
    want_min = minimum(name)
    files, dirs = tree(d)
    if "manifest.json" not in files:
        refuse("no manifest.json")
    m = load_manifest(d)
    if not isinstance(m.get("built"), str) or not BUILT_RE.fullmatch(m["built"]):
        refuse(f"manifest built {m.get('built')!r}")
    if "data" in m:
        refuse("the build's manifest names a data folder of its own")
    n = m.get("shards")
    if not isinstance(n, int) or isinstance(n, bool):
        refuse(f"manifest shards {n!r}")
    shards = []
    for rel, size in files.items():
        if rel in ("manifest.json", "planet.txt"):
            continue
        g = SHARD_RE.fullmatch(rel)
        if g is None or not all(0 <= int(v) < 128 for v in g.groups()) or g.group(1) != str(int(g.group(1))) \
                or g.group(2) != str(int(g.group(2))):
            refuse(f"{rel}: not a z7 shard")
        if not 0 < size <= MAX_SHARD:
            refuse(f"{rel}: {size} bytes")
        shards.append(rel)
    for dd in dirs:
        if not re.fullmatch(r"\d{1,3}", dd) or not 0 <= int(dd) < 128:
            refuse(f"folder {dd!r}")
    if len(shards) != n:
        refuse(f"{len(shards)} shards, the manifest says {n}")
    if n < want_min:
        refuse(f"{n} shards: under {want_min} (a cut-off build)")
    if "planet.txt" in files and files["planet.txt"] > 4096:
        refuse("planet.txt: too big")
    if deep:
        for rel in shards:
            shard_ok(os.path.join(d, rel))
    return m, {k: v for k, v in files.items() if k != "manifest.json"}


def seal_path(art, name):
    return os.path.join(art, f"{name}.seal.json")


def cmd_seal(art, name):
    d = os.path.join(art, name)
    check_dataset(d, name)
    files, _ = tree(d)
    seal = {"name": name, "files": {rel: [size, sha256_file(os.path.join(d, rel))] for rel, size in sorted(files.items())}}
    with open(seal_path(art, name), "w", encoding="utf-8") as f:
        json.dump(seal, f, sort_keys=True, separators=(",", ":"))
    print(f"{name}: sealed {len(files)} files")


def checked(art, name):
    """(manifest, {rel: (size, sha256)} of the build's files, manifest.json included), the whole artifact checked."""
    top, _dirs = tree(art)
    # (the rail job's artifact holds the FRA crossings beside: another dataset's folder and seal, checked on their own)
    extra = sorted(r for r in top if not any(r.startswith(f"{n}/") or r == f"{n}.seal.json" for n in PROFILES))
    if extra:
        refuse(f"the artifact holds {extra[:5]}")
    with open(seal_path(art, name), "rb") as f:
        seal = json.loads(f.read(64 << 20).decode("utf-8"))
    if not isinstance(seal, dict) or seal.get("name") != name or not isinstance(seal.get("files"), dict):
        refuse("seal: not this dataset's")
    d = os.path.join(art, name)
    m, files = check_dataset(d, name)
    files = dict(files, **{"manifest.json": os.path.getsize(os.path.join(d, "manifest.json"))})
    if set(seal["files"]) != set(files):
        refuse("the seal names other files than the artifact holds")
    out = {}
    for rel, size in sorted(files.items()):
        s = seal["files"][rel]
        if not (isinstance(s, list) and len(s) == 2 and s[0] == size and isinstance(s[1], str)
                and re.fullmatch(r"[0-9a-f]{64}", s[1])):
            refuse(f"{rel}: not the seal's size")
        h = sha256_file(os.path.join(d, rel))
        if h != s[1]:
            refuse(f"{rel}: not the seal's SHA-256")
        out[rel] = (size, h)
    return m, out


def walk_order(rels):
    """rels in os.walk's top-down order with sorted folders and files (tools/mirror_data.py's files_of): a folder's
    own files before its subfolders'."""
    def k(rel):
        parts = rel.split("/")
        return [(1, p) for p in parts[:-1]] + [(0, parts[-1])]
    return sorted(rels, key=k)


def key_of(m, files):
    """tools/mirror_data.py's key_of over the same files (every file but manifest.json, in its walk's order)."""
    h = hashlib.sha256()
    for rel in walk_order(r for r in files if r != "manifest.json"):
        h.update(rel.encode() + b"\0" + bytes.fromhex(files[rel][1]))
    k = f"{re.sub(r'[^0-9A-Za-z]+', '_', str(m['built']))[:40]}-{h.hexdigest()[:12]}"
    if not KEY_RE.fullmatch(k):
        refuse(f"key {k!r}")
    return k


def remote_manifest(m, name, key):
    return dict(m, data=f"{CDN}/{PREFIX}/{name}/{key}")


def cmd_key(art, name):
    m, files = checked(art, name)
    print(key_of(m, files))


def cmd_todo(art, name, listing, complete):
    m, files = checked(art, name)
    key = key_of(m, files)
    prefix = f"{PREFIX}/{name}/{key}/"
    with open(listing, encoding="utf-8") as f:
        raw = f.read()
    got = json.loads(raw) if raw.strip() else {}
    there = {}
    for o in got.get("Contents") or ():
        k, size = o.get("Key"), o.get("Size")
        if not isinstance(k, str) or not k.startswith(prefix):
            refuse(f"listing: {k!r} outside {prefix}")
        rel = k[len(prefix):]
        if rel not in files:
            refuse(f"{k}: not one of the build's files (a published folder is never written over)")
        if rel == "manifest.json":
            continue        # (its size is the remote manifest's, with "data")
        if size != files[rel][0]:
            refuse(f"{k}: {size} bytes there, the build's are {files[rel][0]}")
        there[rel] = size
    missing = [rel for rel in sorted(files) if rel != "manifest.json" and rel not in there]
    marker = "manifest.json" in {o.get("Key", "")[len(prefix):] for o in got.get("Contents") or ()}
    if marker and missing:
        refuse("a whole-copy marker (manifest.json) over a part copy")
    if complete:
        if missing:
            refuse(f"{len(missing)} files not there, e.g. {missing[0]}")
        print("whole" if marker else "files")     # (whole: the marker there too, never uploaded again)
        return
    for rel in missing:
        print(f"{os.path.join(art, name, rel)} {prefix}{rel}")


def cmd_remote(art, name, out):
    m, files = checked(art, name)
    key = key_of(m, files)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(remote_manifest(m, name, key), f, separators=(",", ":"))
    print(f"{PREFIX}/{name}/{key}/manifest.json")


def cmd_verify(art, name):
    m, files = checked(art, name)
    key = key_of(m, files)
    probe = max((r for r in files if SHARD_RE.fullmatch(r)), key=lambda r: (files[r][0], r))
    url = f"{CDN}/{PREFIX}/{name}/{key}/{probe}"
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth-data"}),
                                        timeout=60) as r:
                if r.status == 200 and hashlib.sha256(r.read()).hexdigest() == files[probe][1]:
                    print(f"{url}: the build's bytes")
                    return
        except Exception as e:      # (a moment after the upload)
            print(f"  {url}: {type(e).__name__}: {e}", flush=True)
        time.sleep(5 * (i + 1))
    refuse(f"{url}: not the build's bytes on the public host")


def cmd_point(art, name, out):
    m, files = checked(art, name)
    key = key_of(m, files)
    dst = os.path.join(out, name)
    if os.path.exists(dst):
        refuse(f"{dst}: already there")
    for rel in sorted(files):
        if rel == "manifest.json":
            continue
        os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
        shutil.copyfile(os.path.join(art, name, rel), os.path.join(dst, rel))
    with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(remote_manifest(m, name, key), f, separators=(",", ":"))
    print(f"{name}: {len(files) - 1} files and the manifest naming {CDN}/{PREFIX}/{name}/{key}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("seal", "check", "key", "todo", "remote", "verify", "point"))
    ap.add_argument("art")
    ap.add_argument("name")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--listing")
    ap.add_argument("--complete", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "seal":
            cmd_seal(a.art, a.name)
        elif a.cmd == "check":
            m, files = checked(a.art, a.name)
            print(f"{a.name}: built {m['built']}, {m['shards']} shards, {sum(s for s, _ in files.values()) / 1e6:.1f} MB:"
                  " checked")
        elif a.cmd == "key":
            cmd_key(a.art, a.name)
        elif a.cmd == "todo":
            if not a.listing:
                refuse("todo needs --listing")
            cmd_todo(a.art, a.name, a.listing, a.complete)
        elif a.cmd in ("remote", "point"):
            if not a.out:
                refuse(f"{a.cmd} needs OUT")
            (cmd_remote if a.cmd == "remote" else cmd_point)(a.art, a.name, a.out)
        else:
            cmd_verify(a.art, a.name)
    except (Refused, OSError, ValueError, KeyError) as e:
        print(f"REFUSED: {type(e).__name__ + ': ' if not isinstance(e, Refused) else ''}{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
