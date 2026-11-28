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
  nbiskew/<version>/nbiskew.npz         the US National Bridge Inventory's skews (items 16, 17, 34, 49), one npz
  nbiskew/<version>/ATTRIBUTION.md      (tools/build_nbiskew.py: lat, lon float32, skew uint8, length uint16)
  nbiskew/<version>/manifest.json       uploaded LAST
  powerplants/<version>/power_plants.npz  BeamNG.Earth's thermal power plants with their mapped cooling towers and
  powerplants/<version>/ATTRIBUTION.md    chimneys (BeamNG.Earth tools/build_powerplants.py, a pinned copy here; the
  powerplants/<version>/manifest.json     weekly powerplants workflow), uploaded LAST

Players' servers read a pack only once cdn.json has {"<pack>": {"enabled": true, "version": "<version>"}} (a hand
commit after the live check, last). A published version is never overwritten: a version whose manifest.json is in the
bucket is only compared (state=published); a change needs a new version. Nothing here deletes anything.

  check ART PACK          ART/PACK holds exactly manifest.json, ATTRIBUTION.md and the files the manifest names (no
                          links, no other files or folders, plain names); every file a well-formed PNG (signature,
                          chunk CRCs, IHDR the pack's size / bit depth / colour type, IEND last) with the manifest's size
                          and SHA-256; the manifest's fields as the pack's format says; watershare: at least --min shards;
                          nbiskew: the npz a plain zip of exactly lat / lon / skew / length .npy members (no pickles,
                          their headers the columns' dtypes, each the manifest's record count, sizes that add up)
  todo ART PACK --listing L [--complete]
                          L: aws s3api list-objects-v2 of PACK/<version>/. Refuses a key there that is not one of the
                          pack's files, or one with another size (a published folder is never overwritten); prints
                          "<local path> <bucket key>" for each file not there yet, manifest.json never (the caller sends
                          it last, after --complete). --complete: every file but the manifest there with its size;
                          prints "published" when the manifest is there too (byte size equal), else "files".
  live ART PACK --base URL [--sample N]
                          manifest.json and the files (watershare: N of them, spread evenly, and the largest) fetched
                          through the CDN and compared byte for byte with the artifact
  next-version FILE PACK  the version after the one cdn.json's PACK key names (v1 when it names none)
  candidates ART PACK --listing L
                          the versions (oldest first) holding this build's file (size and MD5 ETag) and a manifest: the
                          caller fetches each one's manifest.json into a folder as vN.json
  allocate ART PACK --listing L [--served V] [--manifests DIR]
                          L: aws s3api list-objects-v2 of the whole PACK/ prefix (every version, partial ones too).
                          Each version holding this build's file, oldest first, is checked completely: no other object,
                          its ATTRIBUTION.md the artifact's (size, MD5), and with a manifest (DIR/vN.json, its listed
                          size) one that checks, names vN, this file and these counts: "reuse vN" (activated again as
                          published); without a manifest: "resume vN" (finished there). A version with anything that
                          conflicts is skipped (stderr). Else "new vM" past every version the bucket or cdn.json (V)
                          has used: a published or half-published version is never written with another build. A
                          listed manifest not fetched refuses (a transient failure: the run is tried again)
  restamp ART PACK VERSION
                          the artifact's manifest's version set to VERSION (nothing else changes), checked again
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
    "nbiskew": {"single": "nbiskew.npz", "min": 1},
    "powerplants": {"single": "power_plants.npz", "min": 1},
}
# the power plants' index (BeamNG.Earth server/powerplants.py validate): each column's dtype and which count its
# length is (p: plants, s: structures, p1: plants + 1, any: the names' bytes, one: 1)
PP_COLUMNS = {"schema": ("<i4", "one"), "p_lat": ("<i4", "p"), "p_lon": ("<i4", "p"), "p_fuel": ("|u1", "p"),
              "p_state": ("|u1", "p"), "p_flags": ("|u1", "p"), "p_mw": ("<f4", "p"), "p_closed": ("<i2", "p"),
              "p_radius": ("<f4", "p"), "p_osm": ("<i8", "p"), "p_osm_type": ("|u1", "p"), "p_name_off": ("<i4", "p1"),
              "names": ("|u1", "any"), "s_lat": ("<i4", "s"), "s_lon": ("<i4", "s"), "s_kind": ("|u1", "s"),
              "s_height": ("<f4", "s"), "s_diam": ("<f4", "s"), "s_osm": ("<i8", "s"), "s_osm_type": ("|u1", "s"),
              "s_building": ("|u1", "s"), "s_plant": ("<i4", "s")}
PP_PLANTS = (5000, 200_000)         # a build's plant count (fewer: a cut-off build)
PP_STRUCTS = (10_000, 2_000_000)
NBI_COLUMNS = {"lat": "<f4", "lon": "<f4", "skew": "|u1", "length": "<u2"}
NBI_MIN_RECORDS = 100000
CREDIT = {"leaftype": ("Copernicus", "Land Cover CCI"), "watershare": ("Copernicus", "Land Cover CCI"),
          "nbiskew": ("National Bridge Inventory",), "powerplants": ("OpenStreetMap contributors",)}
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


def npz_check(path, records):
    """A table npz (nbiskew): a plain zip of exactly the NBI_COLUMNS' .npy members, stored or deflated, each a
    version 1.0 .npy whose header is the column's dtype, C order, shape (records,), its uncompressed size the header
    plus records x the item size. Standard library only: the zip's own directory, the headers parsed as literals."""
    import ast
    import zipfile
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = sorted(i.filename for i in infos)
        if names != sorted(k + ".npy" for k in NBI_COLUMNS):
            refuse(f"{path}: members {names}")
        for i in infos:
            if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or i.flag_bits & 0x1:
                refuse(f"{i.filename}: compression / encryption")
            col = i.filename[:-4]
            itemsize = int(NBI_COLUMNS[col][2:])
            with z.open(i) as f:
                head = f.read(10)
                if head[:8] != b"\x93NUMPY\x01\x00":
                    refuse(f"{i.filename}: not a .npy 1.0")
                hl = struct.unpack("<H", head[8:10])[0]
                if hl > 4096:
                    refuse(f"{i.filename}: header too long")
                d = ast.literal_eval(f.read(hl).decode("latin-1"))
            if not isinstance(d, dict) or d.get("descr") != NBI_COLUMNS[col] or d.get("fortran_order") is not False \
                    or d.get("shape") != (records,):
                refuse(f"{i.filename}: header {d!r}, not {NBI_COLUMNS[col]} ({records},)")
            if i.file_size != 10 + hl + records * itemsize:
                refuse(f"{i.filename}: {i.file_size} bytes uncompressed")


def pp_npz_check(path, plants, structures):
    """The power plants' npz: a plain zip of exactly PP_COLUMNS' .npy members (stored or deflated, no pickles), each
    header its column's dtype, C order, one dimension of its count's length, the uncompressed size adding up."""
    import ast
    import zipfile
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if sorted(i.filename for i in infos) != sorted(k + ".npy" for k in PP_COLUMNS):
            refuse(f"{path}: members {sorted(i.filename for i in infos)[:30]}")
        for i in infos:
            if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or i.flag_bits & 0x1:
                refuse(f"{i.filename}: compression / encryption")
            descr, n_of = PP_COLUMNS[i.filename[:-4]]
            with z.open(i) as f:
                head = f.read(10)
                if head[:8] != b"\x93NUMPY\x01\x00":
                    refuse(f"{i.filename}: not a .npy 1.0")
                hl = struct.unpack("<H", head[8:10])[0]
                if hl > 4096:
                    refuse(f"{i.filename}: header too long")
                d = ast.literal_eval(f.read(hl).decode("latin-1"))
            want = {"p": plants, "s": structures, "p1": plants + 1, "one": 1}.get(n_of)
            shape = d.get("shape") if isinstance(d, dict) else None
            if not isinstance(d, dict) or d.get("descr") != descr or d.get("fortran_order") is not False \
                    or not isinstance(shape, tuple) or len(shape) != 1 or not isinstance(shape[0], int) \
                    or shape[0] < 0 or (want is not None and shape[0] != want):
                refuse(f"{i.filename}: header {d!r}, not {descr} of {n_of}")
            if i.file_size != 10 + hl + shape[0] * int(descr[2:]):
                refuse(f"{i.filename}: {i.file_size} bytes uncompressed")


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
    if pack == "powerplants":
        fl = m.get("files")
        if not isinstance(fl, list) or len(fl) != 1 or not isinstance(fl[0], dict) or \
                fl[0].get("path") != PACKS[pack]["single"]:
            refuse("manifest: files")
        out[fl[0]["path"]] = (fl[0].get("size"), fl[0].get("sha256"))
        n, k = m.get("plants"), m.get("structures")
        for v, (lo, hi) in ((n, PP_PLANTS), (k, PP_STRUCTS)):
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                refuse(f"manifest: plants / structures {n!r} {k!r}")
    elif pack == "nbiskew":
        fl = m.get("files")
        if not isinstance(fl, list) or len(fl) != 1 or not isinstance(fl[0], dict) or \
                fl[0].get("path") != PACKS[pack]["single"]:
            refuse("manifest: files")
        out[fl[0]["path"]] = (fl[0].get("size"), fl[0].get("sha256"))
        n = m.get("records")
        if not isinstance(n, int) or isinstance(n, bool) or not NBI_MIN_RECORDS <= n <= 2_000_000 or \
                m.get("columns") != NBI_COLUMNS or m.get("varies") != 99:
            refuse(f"manifest: records / columns {n!r}")
    elif pack == "leaftype":
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
    if len(att) < 200 or not any(c in att for c in CREDIT[pack]):
        refuse("ATTRIBUTION.md: too short or without the source's credit")
    if pack == "watershare" and len(want) < a.min:
        refuse(f"watershare: {len(want)} shards, fewer than {a.min} (a cut-off build?)")
    total = 0
    for p in sorted(want):
        size, sha = want[p]
        if have[p] != size:
            refuse(f"{p}: {have[p]} bytes, the manifest says {size}")
        if pack == "nbiskew":
            npz_check(os.path.join(root, p), m["records"])
        elif pack == "powerplants":
            pp_npz_check(os.path.join(root, p), m["plants"], m["structures"])
        else:
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


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _versions(listing, pack):
    """{version number: {file name: (size, etag)}} of a listing of the whole PACK/ prefix (refuses any other key)."""
    with open(listing, encoding="utf-8") as f:
        txt = f.read()
    d = json.loads(txt) if txt.strip() else {}
    seen = {}
    for o in d.get("Contents") or []:
        k = str(o.get("Key", ""))
        mm = re.fullmatch(re.escape(pack) + r"/(v[1-9][0-9]{0,3})/([A-Za-z0-9_.-]+)", k)
        if not mm:
            refuse(f"listing: key {k!r} outside {pack}/<version>/")
        seen.setdefault(int(mm.group(1)[1:]), {})[mm.group(2)] = (int(o.get("Size", -1)),
                                                                  str(o.get("ETag", "")).strip('"').lower())
    return seen


def _matching(a):
    """(the artifact's manifest, its file name, size, sha256, MD5, the attribution's (size, MD5), the versions, the
    version numbers holding this build's file: same size and MD5 ETag, oldest first)."""
    root = os.path.join(a.art, a.pack)
    m, _ = load_manifest(root, a.pack)
    want = files_of(m, a.pack)
    if len(want) != 1:
        refuse("allocate: single-file packs only")
    (fname, (size, sha)), = want.items()
    md5 = _md5_file(os.path.join(root, fname))
    att = os.path.join(root, "ATTRIBUTION.md")
    att_id = (os.path.getsize(att), _md5_file(att))
    seen = _versions(a.listing, a.pack)
    hits = [n for n in sorted(seen) if seen[n].get(fname) == (size, md5)]
    return m, fname, size, sha, md5, att_id, seen, hits


def cmd_candidates(a):
    """The versions holding this build's file AND a manifest, oldest first: the manifests the publish job fetches
    (into --manifests DIR as vN.json) before it allocates."""
    _m, _f, _s, _h, _md5, _att, seen, hits = _matching(a)
    for n in hits:
        if "manifest.json" in seen[n]:
            print(f"v{n}")


def _candidate(a, n, files, m, fname, size, sha, att_id):
    """('reuse' | 'resume', None) when version n can take this build, else (None, the conflict)."""
    extra = set(files) - {fname, "ATTRIBUTION.md", "manifest.json"}
    if extra:
        return None, f"other objects {sorted(extra)[:3]}"
    if "ATTRIBUTION.md" in files and files["ATTRIBUTION.md"] != att_id:
        return None, "another ATTRIBUTION.md"
    if "manifest.json" not in files:
        return "resume", None
    if "ATTRIBUTION.md" not in files:
        return None, "a manifest without its ATTRIBUTION.md"
    path = os.path.join(a.manifests or "", f"v{n}.json")
    if not a.manifests or not os.path.isfile(path):
        refuse(f"v{n}: its manifest was not fetched (candidates lists it): run again")    # (transient: not a conflict)
    if os.path.getsize(path) != files["manifest.json"][0]:
        return None, "the fetched manifest is not the listed one's size"
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, a.pack))
        shutil.copyfile(path, os.path.join(t, a.pack, "manifest.json"))
        try:
            pm, _ = load_manifest(os.path.join(t, a.pack), a.pack)
            pf = files_of(pm, a.pack)
        except (Refused, ValueError, KeyError, TypeError) as e:
            return None, f"its manifest does not check ({e})"
    if pm.get("version") != f"v{n}":
        return None, f"its manifest names {pm.get('version')!r}"
    if pf != {fname: (size, sha)}:
        return None, "its manifest names another file"
    for k in ("plants", "structures", "records"):
        if k in m and pm.get(k) != m[k]:
            return None, f"its manifest's {k} differ"
    return "reuse", None


def cmd_allocate(a):
    """The version this build goes to (see the doc): the oldest version holding this build that checks completely
    (reuse / resume); versions holding it with anything that conflicts are skipped (said on stderr); else a new one
    past every version used."""
    m, fname, size, sha, _md5, att_id, seen, hits = _matching(a)
    for n in hits:
        mode, why = _candidate(a, n, seen[n], m, fname, size, sha, att_id)
        if mode:
            print(f"{mode} v{n}")
            return
        print(f"conflict v{n}: {why}: skipped", file=sys.stderr)
    top = max(list(seen) + [0])
    if a.served:
        if not VERSION_RE.fullmatch(a.served):
            refuse(f"served version {a.served!r}")
        top = max(top, int(a.served[1:]))
    if top + 1 > 9999:
        refuse("no version left")
    print(f"new v{top + 1}")


def cmd_restamp(a):
    """The artifact's manifest's version set to a.version, nothing else."""
    if not VERSION_RE.fullmatch(a.version):
        refuse(f"version {a.version!r}")
    root = os.path.join(a.art, a.pack)
    m, raw = load_manifest(root, a.pack)
    m["version"] = a.version
    path = os.path.join(root, "manifest.json")
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(path + ".tmp", path)
    a2 = argparse.Namespace(art=a.art, pack=a.pack, min=PACKS[a.pack]["min"])
    cmd_check(a2)


def cmd_next_version(a):
    """The version after the one cdn.json's PACK key names (v1 when none)."""
    with open(a.file, encoding="utf-8") as f:
        d = json.loads(f.read(256 * 1024 + 1))
    on = d.get(a.pack) if isinstance(d, dict) else None
    v = str(on.get("version") or "") if isinstance(on, dict) else ""
    if not v:
        print("v1")
        return
    if not VERSION_RE.fullmatch(v):
        refuse(f"cdn.json: {a.pack} names {v!r}")
    n = int(v[1:]) + 1
    if n > 9999:
        refuse("no version left")
    print(f"v{n}")


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
    p = sub.add_parser("allocate")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("--listing", required=True)
    p.add_argument("--served", default="")
    p.add_argument("--manifests", default="")
    p = sub.add_parser("candidates")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("--listing", required=True)
    p = sub.add_parser("restamp")
    p.add_argument("art")
    p.add_argument("pack", choices=sorted(PACKS))
    p.add_argument("version")
    p = sub.add_parser("next-version")
    p.add_argument("file")
    p.add_argument("pack", choices=sorted(PACKS))
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
        elif a.cmd == "next-version":
            cmd_next_version(a)
        elif a.cmd == "allocate":
            cmd_allocate(a)
        elif a.cmd == "candidates":
            cmd_candidates(a)
        elif a.cmd == "restamp":
            cmd_restamp(a)
        else:
            cmd_cdn_key(a)
    except (Refused, OSError, ValueError, KeyError, TypeError) as e:
        print(f"REFUSED: {type(e).__name__ + ': ' if not isinstance(e, Refused) else ''}{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
