#!/usr/bin/env python3
"""The winterpacks workflow's checker (.github/workflows/winterpacks.yml): BeamNG.Earth's two static raster packs on the
tile host (Cloudflare R2 bnge-tiles behind tiles.tgforever.net), built in jobs WITHOUT secrets (numpy, Pillow,
rasterio and the builders tools/build_leaftype_pack.py / tools/build_water_pack.py) and handed over as an artifact.
The job holding the R2 secrets runs only this script (standard library only, python3 -I) and aws-cli. Nothing here
decodes an image beyond its PNG chunk structure.

  leaftype/<version>/leaftype.png       ESA CCI Land Cover -> evergreen share + tree cover, 0.05 degrees (one file)
  leaftype/<version>/ATTRIBUTION.md
  leaftype/<version>/manifest.json      uploaded LAST
  watershare/<version>/<x>/<y>.png      JRC Global Surface Water -> water share, 0.004 degrees, 2-degree shards
  watershare/<version>/ATTRIBUTION.md
  watershare/<version>/manifest.json    uploaded LAST

Players' servers read a pack only once cdn.json has {"<pack>": {"enabled": true, "version": "<version>"}} (a hand
commit after the live check, last). A published version is never overwritten: a version whose manifest.json is in the
bucket is only compared (state=published); a change needs a new version. Nothing here deletes anything.

  check ART PACK          ART/PACK holds exactly manifest.json, ATTRIBUTION.md and the files the manifest names (no
                          links, no other files or folders, plain names); every file a well-formed PNG (signature,
                          chunk CRCs, IHDR the pack's size / bit depth / colour type, IEND last) with the manifest's size
                          and SHA-256; the manifest's fields as the pack's format says; watershare: at least --min shards
  todo ART PACK --listing L [--complete]
                          L: aws s3api list-objects-v2 of PACK/<version>/. Refuses a key there that is not one of the
                          pack's files, or one with another size (a published folder is never overwritten); prints
                          "<local path> <bucket key>" for each file not there yet, manifest.json never (the caller sends
                          it last, after --complete). --complete: every file but the manifest there with its size;
                          prints "published" when the manifest is there too (byte size equal), else "files".
  live ART PACK --base URL [--sample N]
                          manifest.json and the files (watershare: N of them, spread evenly, and the largest) fetched
                          through the CDN and compared byte for byte with the artifact
  cdn-key FILE PACK VERSION
                          cdn.json's PACK key set to {"enabled": true, "version": VERSION} by a minimal text edit (every
                          other key byte-identical), checked by parsing both

Exit 1 (REFUSED: ...) on anything unexpected.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

PACKS = {
    "leaftype": {"width": 7200, "height": 3600, "colour": 4, "single": "leaftype.png", "min": 1},
    "watershare": {"width": 500, "height": 500, "colour": 0, "single": None, "min": 3000},
}
VERSION_RE = re.compile(r"v[1-9][0-9]{0,3}")
SHARD_RE = re.compile(r"(\d{1,3})/(\d{1,2})\.png")
HEX64 = re.compile(r"[0-9a-f]{64}")
BUILT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_FILE = 64 << 20
MAX_MANIFEST = 4 << 20
UA = "BeamNG.Earth-data winterpack check"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


class Refused(Exception):
    pass


def refuse(msg):
    raise Refused(msg)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def png_check(path, prof):
    """A PNG's chunk structure: signature, IHDR first with the pack's sides, 8-bit depth and colour type, every chunk's
    CRC, at least one IDAT, IEND last and the file's end."""
    size = os.path.getsize(path)
    if not 0 < size <= MAX_FILE:
        refuse(f"{path}: {size} bytes")
    with open(path, "rb") as f:
        body = f.read()
    if body[:8] != PNG_SIG:
        refuse(f"{path}: not a PNG")
    i, first, idat, end = 8, True, 0, False
    while i < len(body):
        if end:
            refuse(f"{path}: bytes after IEND")
        if i + 12 > len(body):
            refuse(f"{path}: cut short")
        n, = struct.unpack(">I", body[i:i + 4])
        typ = body[i + 4:i + 8]
        if i + 12 + n > len(body):
            refuse(f"{path}: chunk {typ!r} past the end")
        data = body[i + 8:i + 8 + n]
        crc, = struct.unpack(">I", body[i + 8 + n:i + 12 + n])
        if zlib.crc32(typ + data) & 0xFFFFFFFF != crc:
            refuse(f"{path}: chunk {typ!r} CRC")
        if first:
            if typ != b"IHDR" or n != 13:
                refuse(f"{path}: IHDR not first")
            w, h, depth, colour, comp, filt, lace = struct.unpack(">IIBBBBB", data)
            if (w, h, depth, colour, comp, filt, lace) != (prof["width"], prof["height"], 8, prof["colour"], 0, 0, 0):
                refuse(f"{path}: IHDR {w}x{h} depth {depth} colour {colour} ({comp}, {filt}, {lace})")
            first = False
        elif typ == b"IDAT":
            idat += 1
        elif typ == b"IEND":
            end = True
        elif typ[0:1].isupper() and typ not in (b"PLTE",):
            refuse(f"{path}: critical chunk {typ!r}")
        i += 12 + n
    if not end or not idat:
        refuse(f"{path}: no IDAT or IEND")


def walk(root):
    """{relative path: size} of every regular file under root; refuses links and special files."""
    out = {}

    def rec(path, rel):
        for name in sorted(os.listdir(path)):
            p = os.path.join(path, name)
            r = f"{rel}{name}"
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                refuse(f"{r}: a link")
            if stat.S_ISDIR(st.st_mode):
                rec(p, r + "/")
            elif stat.S_ISREG(st.st_mode):
                out[r] = st.st_size
            else:
                refuse(f"{r}: not a regular file")
    rec(root, "")
    return out


def load_manifest(root, pack):
    path = os.path.join(root, "manifest.json")
    if os.path.getsize(path) > MAX_MANIFEST:
        refuse("manifest.json too large")
    with open(path, "rb") as f:
        raw = f.read()
    m = json.loads(raw.decode("utf-8"))
    if not isinstance(m, dict) or m.get("format") != 1 or m.get("name") != pack:
        refuse(f"manifest: format / name {m.get('format')!r} {m.get('name')!r}")
    if not VERSION_RE.fullmatch(str(m.get("version"))):
        refuse(f"manifest: version {m.get('version')!r}")
    if not BUILT_RE.fullmatch(str(m.get("built"))) or not re.fullmatch(r"[0-9a-f]{16}", str(m.get("build"))):
        refuse("manifest: built / build")
    if not isinstance(m.get("attribution"), str) or len(m["attribution"]) < 40:
        refuse("manifest: no attribution")
    return m, raw


def files_of(m, pack):
    """{relative path: (size, sha256)} the manifest names."""
    out = {}
    if pack == "leaftype":
        fl = m.get("files")
        if not isinstance(fl, list) or len(fl) != 1 or not isinstance(fl[0], dict) or \
                fl[0].get("path") != PACKS[pack]["single"]:
            refuse("manifest: files")
        out[fl[0]["path"]] = (fl[0].get("size"), fl[0].get("sha256"))
        g = m.get("grid") or {}
        if (g.get("width"), g.get("height"), g.get("res_deg"), g.get("scale"), g.get("bands")) != \
                (7200, 3600, 0.05, 250, ["evergreen", "trees"]):
            refuse(f"manifest: grid {g}")
    else:
        sh, un = m.get("shards"), m.get("uniform")
        if not isinstance(sh, dict) or not isinstance(un, dict):
            refuse("manifest: shards / uniform")
        g = m.get("grid") or {}
        if (g.get("res_deg"), g.get("cells_per_shard"), g.get("shard_deg"), g.get("scale"), g.get("nodata")) != \
                (0.004, 500, 2, 250, 255):
            refuse(f"manifest: grid {g}")
        for k, v in sh.items():
            mm = re.fullmatch(r"(\d{1,3})/(\d{1,2})", k)
            if not mm or int(mm.group(1)) >= 180 or int(mm.group(2)) >= 90 or not isinstance(v, list) or len(v) != 2:
                refuse(f"manifest: shard {k!r}")
            out[f"{k}.png"] = (v[0], v[1])
        for k, v in un.items():
            mm = re.fullmatch(r"(\d{1,3})/(\d{1,2})", k)
            if not mm or int(mm.group(1)) >= 180 or int(mm.group(2)) >= 90 or k in sh or \
                    not isinstance(v, int) or not 0 <= v <= 250:
                refuse(f"manifest: uniform {k!r}: {v!r}")
    for p, (size, sha) in out.items():
        if not isinstance(size, int) or not 0 < size <= MAX_FILE or not isinstance(sha, str) or \
                not HEX64.fullmatch(sha):
            refuse(f"manifest: {p}: size / sha256")
    return out


def cmd_check(a):
    pack = a.pack
    prof = PACKS[pack]
    root = os.path.join(a.art, pack)
    have = walk(root)
    m, _ = load_manifest(root, pack)
    want = files_of(m, pack)
    extra = set(have) - set(want) - {"manifest.json", "ATTRIBUTION.md"}
    missing = (set(want) | {"manifest.json", "ATTRIBUTION.md"}) - set(have)
    if extra or missing:
        refuse(f"{pack}: extra {sorted(extra)[:5]}, missing {sorted(missing)[:5]}")
    with open(os.path.join(root, "ATTRIBUTION.md"), encoding="utf-8") as f:
        att = f.read()
    if len(att) < 200 or ("Copernicus" not in att and "Land Cover CCI" not in att):
        refuse("ATTRIBUTION.md: too short or without the source's credit")
    if pack == "watershare" and len(want) < a.min:
        refuse(f"watershare: {len(want)} shards, fewer than {a.min} (a cut-off build?)")
    total = 0
    for p in sorted(want):
        size, sha = want[p]
        if have[p] != size:
            refuse(f"{p}: {have[p]} bytes, the manifest says {size}")
        png_check(os.path.join(root, p), prof)
        if sha256_file(os.path.join(root, p)) != sha:
            refuse(f"{p}: SHA-256")
        total += size
    print(f"{pack} {m['version']} build {m['build']} ({m['built']}): {len(want)} files, {total / 1e6:.1f} MB, checked")


def key_of(a):
    root = os.path.join(a.art, a.pack)
    m, _ = load_manifest(root, a.pack)
    return m["version"]


def _listing(path, prefix):
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    d = json.loads(txt) if txt.strip() else {}
    out = {}
    for o in d.get("Contents") or []:
        k = o.get("Key", "")
        if not k.startswith(prefix):
            refuse(f"listing: key {k!r} outside {prefix}")
        out[k[len(prefix):]] = int(o.get("Size", -1))
    return out


def cmd_todo(a):
    root = os.path.join(a.art, a.pack)
    m, raw = load_manifest(root, a.pack)
    if a.version and a.version != m["version"]:
        refuse(f"the artifact is version {m['version']}, not {a.version}")
    want = files_of(m, a.pack)
    want["ATTRIBUTION.md"] = (os.path.getsize(os.path.join(root, "ATTRIBUTION.md")), None)
    prefix = f"{a.pack}/{m['version']}/"
    keys = _listing(a.listing, prefix)
    stray = set(keys) - set(want) - {"manifest.json"}
    if stray:
        refuse(f"{prefix} holds keys outside the pack: {sorted(stray)[:5]}")
    for k, size in keys.items():
        exp = len(raw) if k == "manifest.json" else want[k][0]
        if size != exp:
            refuse(f"{prefix}{k}: {size} bytes in the bucket, {exp} here (a published version is never rewritten)")
    if a.complete:
        miss = [p for p in want if p not in keys]
        if miss:
            refuse(f"{prefix}: {len(miss)} files not there, e.g. {miss[:3]}")
        print("published" if "manifest.json" in keys else "files")
        return
    for p in sorted(want, key=lambda s: (s == "ATTRIBUTION.md", s)):
        if p not in keys:
            print(f"{a.art}/{a.pack}/{p} {prefix}{p}")


def _get(url, limit, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read(limit + 1)
                if len(body) > limit:
                    refuse(f"{url}: over {limit} bytes")
                return body
        except urllib.error.HTTPError as e:
            if e.code not in (404, 429, 500, 502, 503, 504) or i == tries - 1:
                raise
        except urllib.error.URLError:
            if i == tries - 1:
                raise
        time.sleep(15 * (i + 1))


def cmd_live(a):
    root = os.path.join(a.art, a.pack)
    m, raw = load_manifest(root, a.pack)
    base = a.base.rstrip("/")
    if not re.fullmatch(rf"https://[a-z0-9.-]+/{a.pack}/{m['version']}", base):
        refuse(f"bad base {a.base!r}")
    if _get(f"{base}/manifest.json", MAX_MANIFEST) != raw:
        refuse("manifest.json differs on the CDN")
    want = sorted(files_of(m, a.pack))
    pick = want
    if len(want) > a.sample:
        step = len(want) / a.sample
        pick = sorted({want[int(i * step)] for i in range(a.sample)} |
                      {max(want, key=lambda p: os.path.getsize(os.path.join(root, p)))})
    for p in pick:
        with open(os.path.join(root, p), "rb") as f:
            if _get(f"{base}/{p}", MAX_FILE) != f.read():
                refuse(f"{p} differs on the CDN")
    print(f"{base}: manifest and {len(pick)} of {len(want)} files byte-identical")


def _top_spans(text):
    """{key: (value start, value end)} of a JSON object's top-level members, and the closing brace's index (as
    tools/cliffpack_ci.py)."""
    dec = json.JSONDecoder()
    ws = " \t\r\n"
    i = 0
    while text[i] in ws:
        i += 1
    if text[i] != "{":
        refuse("cdn.json: not an object")
    i += 1
    spans = {}
    while True:
        while text[i] in ws:
            i += 1
        if text[i] == "}":
            return spans, i
        key, i = dec.raw_decode(text, i)
        while text[i] in ws:
            i += 1
        if text[i] != ":":
            refuse("cdn.json: malformed")
        i += 1
        while text[i] in ws:
            i += 1
        _, e = dec.raw_decode(text, i)
        spans[key] = (i, e)
        i = e
        while text[i] in ws:
            i += 1
        if text[i] == ",":
            i += 1
        elif text[i] != "}":
            refuse("cdn.json: malformed")


def cdn_key_text(text, pack, version):
    if pack not in PACKS or not VERSION_RE.fullmatch(version):
        refuse(f"bad pack / version {pack!r} {version!r}")
    before = json.loads(text)
    if not isinstance(before, dict):
        refuse("cdn.json: not an object")
    value = {"enabled": True, "version": version}
    if before.get(pack) == value:
        return text
    spans, close = _top_spans(text)
    v = json.dumps(value, separators=(", ", ": "))
    if pack in spans:
        s, e = spans[pack]
        new = text[:s] + v + text[e:]
    else:
        head = text[:close].rstrip()
        new = head + (", " if spans else "") + json.dumps(pack) + ": " + v + text[len(head):]
    if json.loads(new) != dict(before, **{pack: value}):
        refuse(f"cdn.json: the edit changed more than the {pack} key")
    ns, _ = _top_spans(new)
    for k, (s, e) in spans.items():
        if k != pack and text[s:e] != new[ns[k][0]:ns[k][1]]:
            refuse(f"cdn.json: {k} changed")
    return new


def cmd_cdn_key(a):
    with open(a.file, encoding="utf-8", newline="") as f:
        text = f.read(256 * 1024 + 1)
    if len(text) > 256 * 1024:
        refuse("cdn.json over 256 KiB")
    new = cdn_key_text(text, a.pack, a.version)
    if new == text:
        print(f"{a.file}: {a.pack} names {a.version} already")
        return
    tmp = a.file + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, a.file)
    print(f"{a.file}: {a.pack} {a.version}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("--min", type=int, default=None)
    p = sub.add_parser("key")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p = sub.add_parser("todo")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("--listing", required=True)
    p.add_argument("--version", default="")
    p.add_argument("--complete", action="store_true")
    p = sub.add_parser("live")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("--base", required=True)
    p.add_argument("--sample", type=int, default=40)
    p = sub.add_parser("cdn-key")
    p.add_argument("file")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("version")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            if a.min is None:
                a.min = PACKS[a.pack]["min"]
            cmd_check(a)
        elif a.cmd == "key":
            print(key_of(a))
        elif a.cmd == "todo":
            cmd_todo(a)
        elif a.cmd == "live":
            cmd_live(a)
        else:
            cmd_cdn_key(a)
    except (Refused, OSError, ValueError, KeyError, TypeError) as e:
        print(f"REFUSED: {type(e).__name__ + ': ' if not isinstance(e, Refused) else ''}{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
