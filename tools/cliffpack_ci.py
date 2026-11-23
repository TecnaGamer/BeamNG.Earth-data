#!/usr/bin/env python3
"""The cliffpack workflow's own steps (.github/workflows/cliffpack.yml): BeamNG.Earth's optional 4K cliff texture pack
(PR #1412, clifftextures.py) checked and staged for the tile host (Cloudflare R2 bnge-tiles behind
tiles.tgforever.net) under cliff/4k/<version>/. Standard library only: the job holding the R2 secrets runs nothing else
(and aws-cli). Nothing here executes, imports or decodes the pack beyond its WebP container headers.

What players read (clifftextures.py at PR #1412 head 40b40d56): cdn.json {"cliffs": {"enabled": true, "base": B}} ->
B/manifest.json, at most 64 KiB, {"format": 1, "files": [{"path", "size", "sha256"}]}; every path must be one of the
27 allowlisted <set>/<name>.webp names (ANY other path rejects the WHOLE manifest, so nothing else is listed in it),
0 < size <= 64 MiB, total <= 512 MiB, sha256 64 lowercase hex; each file B/<path> is checked against its size and
sha256, then opened as a single-frame WebP with power-of-two sides <= 4096. Anything wrong: the bundled textures.

    cliffpack_ci.py names
        the 27 allowlisted paths, one per line (the upload loop's list: never a listing of untrusted files).
    cliffpack_ci.py pin --pins tools/cliffpack_pins.json --version v1
        prints "<asset> <zip sha256>" for the version; refuses an unpinned version.
    cliffpack_ci.py unzip ZIP DEST --sha256 HEX
        the zip's SHA-256 checked, then unpacked into a new folder DEST: only allowlisted names (the 27 WebPs, the
        handoff's inventory.json, manifest.json, README.md, notices/*.NOTICE.md), stored or deflated, not encrypted,
        no symlinks, absolute paths, backslashes, "..", duplicates or oversize entries; files created exclusively.
    cliffpack_ci.py check IN OUT --version v1 [--pins P]
        an unpacked handoff checked and staged into a new folder OUT: exactly the allowed files (no symlinks, no
        special files, no other names), each WebP a well-formed RIFF/WEBP (chunk sizes adding up to the file, one
        still image, power-of-two sides <= 4096) within the size bounds, all 27 present, every required notice
        present; the handoff's inventory.json / manifest.json (when present) agreeing with the files exactly.
        OUT gets the 27 WebPs, manifest.json (generated here) and ATTRIBUTION.md (the notices bundled).
    cliffpack_ci.py recheck STAGE --version v1 [--pins P]
        a staged folder (the artifact, in the job with the secrets) checked again: exactly the 27 WebPs,
        manifest.json and ATTRIBUTION.md; every WebP's header, size and SHA-256; manifest.json byte-identical to
        the one generated from the files, and its SHA-256 the pinned one.
    cliffpack_ci.py remote STAGE --version v1 --listing L.json [--complete]
        `aws s3api list-objects-v2` output for cliff/4k/<version>/: refuses keys outside the pack's names; prints
        state=new (no manifest.json yet) or state=published. --complete: every WebP and ATTRIBUTION.md is there with
        its staged size (checked before manifest.json, which goes last).
    cliffpack_ci.py live STAGE --base https://tiles.tgforever.net/cliff/4k/v1/
        what players will read: manifest.json and every file through the CDN, byte-checked against the stage.
    cliffpack_ci.py cdn-key FILE --base URL
        cdn.json's "cliffs" key set to {"enabled": true, "base": URL} by a minimal text edit (every other key
        byte-identical), checked by parsing both.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile

# (clifftextures.py: MAX_MANIFEST_BYTES, MAX_SOURCE_BYTES, MAX_PACK_BYTES; Pillow check: power of two, <= 4096)
MAX_MANIFEST = 64 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_PACK = 512 * 1024 * 1024
MAX_SIDE = 4096
MIN_FILE = 26                   # (the smallest well-formed WebP: RIFF header + one chunk header + a VP8L header)
MAX_META = 1024 * 1024          # inventory / manifest / README / one notice
MAX_ENTRIES = 64
VERSION_RE = re.compile(r"v[1-9][0-9]{0,3}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BASE_RE = re.compile(r"https://tiles\.tgforever\.net/cliff/4k/v[1-9][0-9]{0,3}/")
UA = "BeamNG.Earth-data cliffpack workflow"

# The pack's names: clifftextures.pack_path() of every '/cliff/' source in BeamNG.Earth's mod/texture_sources/
# textures.json (PR #1412 head 40b40d56: 27 entries, 27 distinct paths). A path the app does not list makes it reject
# the whole manifest, so a new name needs the app first, then a new version here.
EXPECTED = frozenset("""
arid/arid_far.normal.webp
arid/arid_far_ao.data.webp
arid/arid_far_base.color.webp
arid/arid_far_roughness.data.webp
arid/arid_near.normal.webp
arid/arid_near_ao.data.webp
arid/arid_near_base.color.webp
arid/arid_near_roughness.data.webp
canyon/canyon_far.normal.webp
canyon/canyon_far_base.color.webp
canyon/canyon_far_roughness.data.webp
canyon/canyon_near.normal.webp
canyon/canyon_near_base.color.webp
canyon/canyon_near_roughness.data.webp
canyon/canyon_source_ao.data.webp
limestone/limestone_cut.normal.webp
limestone/limestone_cut_ao.data.webp
limestone/limestone_cut_base.color.webp
limestone/limestone_cut_near_roughness.data.webp
natural/natural_ao.data.webp
natural/natural_base.color.webp
natural/natural_far.normal.webp
natural/natural_far_roughness.data.webp
natural/natural_near.normal.webp
natural/natural_near_roughness.data.webp
shared/rock_grain.normal.webp
shared/terrain_rim.data.webp
""".split())
SETS = ("natural", "limestone", "arid", "canyon", "shared")
# (every notice is required: hosting needs each set's tool, version and terms; the four sets' must state CC0)
NOTICES = {
    "notices/cliff_natural.NOTICE.md": "CC0",       # natural_* and shared/rock_grain (Poly Haven CC0 scans)
    "notices/cliff_limestone.NOTICE.md": "CC0",
    "notices/cliff_arid.NOTICE.md": "CC0",
    "notices/cliff_canyon.NOTICE.md": "CC0",
    "notices/cliff_references.NOTICE.md": "",       # shared/terrain_rim (MIT) and the reference data
}
HANDOFF_META = {"inventory.json", "manifest.json", "README.md"}     # optional in the handoff; never published
ALLOWED_IN = EXPECTED | set(NOTICES) | HANDOFF_META
ALLOWED_DIRS = set(SETS) | {"notices"}
STAGED = EXPECTED | {"manifest.json", "ATTRIBUTION.md"}


class Refused(Exception):
    pass


def refuse(msg):
    raise Refused(msg)


# -- paths -------------------------------------------------------------------------------------------------------------

def safe_rel(name):
    """name as a '/'-separated relative path of plain components, or refused."""
    if not isinstance(name, str) or not name or len(name) > 256:
        refuse(f"bad name {name!r}")
    if "\\" in name or name.startswith("/") or ":" in name or "\x00" in name:
        refuse(f"bad name {name!r}")
    parts = name.split("/")
    for p in parts:
        if p in ("", ".", "..") or not PART_RE.fullmatch(p):
            refuse(f"bad name {name!r}")
    return name


def walk(root):
    """{relative path: size} of every regular file under root; refuses symlinks, special files, unknown folders."""
    out = {}
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        refuse(f"{root}: not a folder")

    def rec(path, rel):
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            safe_rel(r)
            st = os.lstat(e.path)
            if stat.S_ISLNK(st.st_mode):
                refuse(f"{r}: symlink")
            if stat.S_ISDIR(st.st_mode):
                if rel or e.name not in ALLOWED_DIRS:
                    refuse(f"{r}/: unexpected folder")
                rec(e.path, r)
            elif stat.S_ISREG(st.st_mode):
                out[r] = st.st_size
            else:
                refuse(f"{r}: not a regular file")

    rec(root, "")
    return out


def read_small(path, limit=MAX_META):
    with open(path, "rb") as f:
        body = f.read(limit + 1)
    if len(body) > limit:
        refuse(f"{path}: over {limit} bytes")
    return body


# -- WebP --------------------------------------------------------------------------------------------------------------

def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def _u24(b, i):
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16)


def _image_dims(fourcc, body, name):
    if fourcc == b"VP8 ":
        if len(body) < 10 or body[0] & 1 or body[3:6] != b"\x9d\x01\x2a":
            refuse(f"{name}: VP8 chunk is not a key frame")
        return _u16(body, 6) & 0x3FFF, _u16(body, 8) & 0x3FFF
    if fourcc == b"VP8L":
        if len(body) < 5 or body[0] != 0x2F:
            refuse(f"{name}: VP8L signature missing")
        bits = int.from_bytes(body[1:5], "little")
        if bits >> 29:
            refuse(f"{name}: VP8L version {bits >> 29}")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    refuse(f"{name}: no image chunk")


def webp_info(path, name):
    """(width, height) of a well-formed single-image WebP, or refused. Walks every RIFF chunk: their sizes (padded to
    even) must add up to the file exactly; one VP8/VP8L image (VP8X: no animation, canvas = image)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
            refuse(f"{name}: not a RIFF/WEBP file")
        if int.from_bytes(head[4:8], "little") + 8 != size:
            refuse(f"{name}: RIFF size {int.from_bytes(head[4:8], 'little') + 8} is not the file's {size}")
        pos, chunks = 12, []
        while pos < size:
            if size - pos < 8:
                refuse(f"{name}: truncated chunk header at {pos}")
            f.seek(pos)
            h = f.read(8)
            fourcc, n = h[:4], int.from_bytes(h[4:8], "little")
            if not all(0x20 <= c < 0x7F for c in fourcc):
                refuse(f"{name}: bad chunk id at {pos}")
            end = pos + 8 + n + (n & 1)
            if end > size:
                refuse(f"{name}: chunk {fourcc!r} runs past the end")
            chunks.append((fourcc, f.read(min(n, 32))))
            pos = end
    ids = [c for c, _ in chunks]
    if not ids:
        refuse(f"{name}: no chunks")
    if ids[0] in (b"VP8 ", b"VP8L"):
        if len(ids) != 1:
            refuse(f"{name}: simple WebP with extra chunks {ids[1:]}")
        w, h = _image_dims(ids[0], chunks[0][1], name)
    elif ids[0] == b"VP8X":
        x = chunks[0][1]
        if len(x) < 10:
            refuse(f"{name}: short VP8X chunk")
        if x[0] & 0x02 or b"ANIM" in ids or b"ANMF" in ids:
            refuse(f"{name}: animated")
        imgs = [(c, b) for c, b in chunks[1:] if c in (b"VP8 ", b"VP8L")]
        if len(imgs) != 1 or not set(ids[1:]) <= {b"ICCP", b"ALPH", b"VP8 ", b"VP8L", b"EXIF", b"XMP "}:
            refuse(f"{name}: unexpected VP8X chunks {ids[1:]}")
        w, h = _u24(x, 4) + 1, _u24(x, 7) + 1
        if (w, h) != _image_dims(*imgs[0], name):
            refuse(f"{name}: canvas {w}x{h} is not the image's")
    else:
        refuse(f"{name}: first chunk {ids[0]!r}")
    for n in (w, h):
        if not 1 <= n <= MAX_SIDE or n & (n - 1):
            refuse(f"{name}: {w}x{h} is not power-of-two sides <= {MAX_SIDE}")
    return w, h


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- manifest, attribution, pins ---------------------------------------------------------------------------------------

def manifest_bytes(records):
    """The players' manifest: format 1, the files by path, nothing else (clifftextures.resolve rejects other paths)."""
    files = [{"path": p, "size": records[p][0], "sha256": records[p][1]} for p in sorted(records)]
    if set(records) != EXPECTED:
        refuse("manifest: not exactly the 27 pack files")
    total = sum(r[0] for r in records.values())
    if total > MAX_PACK:
        refuse(f"pack is {total} bytes, over {MAX_PACK}")
    body = (json.dumps({"format": 1, "files": files}, indent=2) + "\n").encode()
    if len(body) > MAX_MANIFEST:
        refuse(f"manifest.json is {len(body)} bytes, over {MAX_MANIFEST}")
    return body


def attribution_bytes(version, records, notices):
    lines = [
        f"# BeamNG.Earth optional 4K cliff texture pack, {version}",
        "",
        f"Served at https://tiles.tgforever.net/cliff/4k/{version}/ for BeamNG.Earth "
        "(https://github.com/TecnaGamer/BeamNG.Earth), an opt-in download; the game falls back to its bundled cliff "
        "textures. Every file's size and SHA-256 are in manifest.json next to this file.",
        "",
        "The notices below are the contributor's, verbatim. In short: `natural/*` and `shared/rock_grain` are derived",
        "from Poly Haven CC0 scans; `limestone/*`, `arid/*` and `canyon/*` are generated images dedicated to CC0 by",
        "their contributor; `shared/terrain_rim` is numerical data under MIT. See each notice for the exact terms.",
        "",
        "## Files",
        "",
    ]
    for s in SETS:
        names = sorted(p for p in records if p.startswith(s + "/"))
        lines.append(f"- {s}: " + ", ".join(f"`{p.split('/', 1)[1]}`" for p in names))
    for n in sorted(notices):
        lines += ["", "---", "", f"<!-- {n} -->", "", notices[n].strip("\n")]
    return ("\n".join(lines) + "\n").encode()


def load_pins(path, version):
    if not VERSION_RE.fullmatch(version or ""):
        refuse(f"bad version {version!r}")
    with open(path, encoding="utf-8") as f:
        pins = json.load(f)
    pin = pins.get(version)
    if not isinstance(pin, dict):
        refuse(f"{version} is not pinned in {path}: add its asset and SHA-256 (a reviewed commit) first")
    if not ASSET_RE.fullmatch(str(pin.get("asset", ""))) or not SHA_RE.fullmatch(str(pin.get("zip_sha256", ""))):
        refuse(f"{path}: {version}'s asset / zip_sha256 malformed")
    ms = pin.get("manifest_sha256")
    if ms is not None and not SHA_RE.fullmatch(str(ms)):
        refuse(f"{path}: {version}'s manifest_sha256 malformed")
    return pin


# -- commands ----------------------------------------------------------------------------------------------------------

def cmd_names(a):
    print("\n".join(sorted(EXPECTED)))


def cmd_pin(a):
    pin = load_pins(a.pins, a.version)
    print(pin["asset"], pin["zip_sha256"])


def cmd_unzip(a):
    want = a.sha256.strip().lower()
    if not SHA_RE.fullmatch(want):
        refuse("--sha256: not 64 hex digits")
    got = sha256_file(a.zip)
    if got != want:
        refuse(f"{a.zip}: SHA-256 {got}, not the pinned {want}")
    if os.path.lexists(a.dest):
        refuse(f"{a.dest}: exists (unpack into a new folder)")
    with zipfile.ZipFile(a.zip) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            refuse(f"{len(infos)} entries, over {MAX_ENTRIES}")
        seen, total, todo = set(), 0, []
        for i in infos:
            name = i.filename
            mode = (i.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                refuse(f"{name!r}: symlink entry")
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                refuse(f"{name!r}: special file entry")
            if i.flag_bits & 0x1:
                refuse(f"{name!r}: encrypted")
            if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                refuse(f"{name!r}: compression {i.compress_type}")
            if name.endswith("/"):
                d = safe_rel(name[:-1])
                if d not in ALLOWED_DIRS:
                    refuse(f"{name!r}: unexpected folder")
                continue
            safe_rel(name)
            if name not in ALLOWED_IN:
                refuse(f"{name!r}: not an allowlisted name")
            if name.lower() in seen:
                refuse(f"{name!r}: duplicate entry")
            seen.add(name.lower())
            limit = MAX_FILE if name in EXPECTED else MAX_META
            if i.file_size > limit:
                refuse(f"{name!r}: {i.file_size} bytes, over {limit}")
            total += i.file_size
            todo.append(i)
        if total > MAX_PACK + 16 * MAX_META:
            refuse(f"entries total {total} bytes")
        os.mkdir(a.dest)
        for i in todo:
            parts = i.filename.split("/")
            d = a.dest
            for p in parts[:-1]:
                d = os.path.join(d, p)
                if not os.path.isdir(d):
                    os.mkdir(d)
            target = os.path.join(d, parts[-1])
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            n = 0
            with os.fdopen(fd, "wb") as out, z.open(i) as src:      # (zipfile checks the CRC and size at the end)
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    n += len(chunk)
                    if n > i.file_size:
                        refuse(f"{i.filename!r}: more data than its declared size")
                    out.write(chunk)
            if n != i.file_size:
                refuse(f"{i.filename!r}: {n} bytes, declared {i.file_size}")
    print(f"{a.zip}: SHA-256 ok, {len(todo)} files unpacked into {a.dest}")


def _check_handoff_listing(name, body, records, with_dims):
    """The handoff's own inventory.json / manifest.json must describe exactly the files measured here."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        refuse(f"{name}: not JSON ({e})")
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
        refuse(f"{name}: no files list")
    if name == "manifest.json" and doc.get("format") != 1:
        refuse(f"{name}: format {doc.get('format')!r}, not 1")
    theirs = {}
    for e in doc["files"]:
        if not isinstance(e, dict) or e.get("path") in theirs:
            refuse(f"{name}: bad or duplicate entry {e!r}")
        theirs[e.get("path")] = e
    if set(theirs) != set(records):
        refuse(f"{name}: lists {sorted(set(theirs) ^ set(records))} differently from the files")
    for p, (size, digest, dims) in records.items():
        e = theirs[p]
        if e.get("size") != size or e.get("sha256") != digest:
            refuse(f"{name}: {p} size/SHA-256 differ from the file")
        if with_dims and list(e.get("dimensions") or []) != list(dims):
            refuse(f"{name}: {p} dimensions {e.get('dimensions')} differ from the file's {list(dims)}")


def cmd_check(a):
    if not VERSION_RE.fullmatch(a.version):
        refuse(f"bad version {a.version!r}")
    pin = load_pins(a.pins, a.version) if a.pins else None
    files = walk(a.src)
    extra = set(files) - ALLOWED_IN
    if extra:
        refuse(f"unexpected files: {sorted(extra)}")
    missing = (EXPECTED | set(NOTICES)) - set(files)
    if missing:
        refuse(f"missing files: {sorted(missing)}")
    if os.path.lexists(a.out):
        refuse(f"{a.out}: exists (stage into a new folder)")
    os.mkdir(a.out)
    records, total = {}, 0
    for p in sorted(EXPECTED):
        size = files[p]
        if not MIN_FILE <= size <= MAX_FILE:
            refuse(f"{p}: {size} bytes, outside {MIN_FILE}..{MAX_FILE}")
        total += size
        if total > MAX_PACK:
            refuse(f"pack over {MAX_PACK} bytes")
        # (copied and hashed in one read: the staged bytes are the hashed bytes; then parsed from the copy)
        d = os.path.join(a.out, p.split("/")[0])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(a.out, *p.split("/"))
        h, n = hashlib.sha256(), 0
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with open(os.path.join(a.src, *p.split("/")), "rb") as src, os.fdopen(fd, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += len(chunk)
                if n > size:
                    refuse(f"{p}: grew while read")
                h.update(chunk)
                out.write(chunk)
        if n != size:
            refuse(f"{p}: changed while read")
        dims = webp_info(dst, p)
        records[p] = (size, h.hexdigest(), dims)
    notices = {}
    for n, needle in NOTICES.items():
        body = read_small(os.path.join(a.src, *n.split("/")), 64 * 1024)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            refuse(f"{n}: not UTF-8")
        if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            refuse(f"{n}: empty or control characters")
        if needle and needle not in text:
            refuse(f"{n}: does not state {needle}")
        notices[n] = text.replace("\r\n", "\n")
    for meta, dims in (("inventory.json", True), ("manifest.json", False)):
        if meta in files:
            _check_handoff_listing(meta, read_small(os.path.join(a.src, meta)), records, dims)
    man = manifest_bytes({p: r[:2] for p, r in records.items()})
    att = attribution_bytes(a.version, records, notices)
    for name, body in (("ATTRIBUTION.md", att), ("manifest.json", man)):
        with open(os.path.join(a.out, name), "xb") as f:
            f.write(body)
    msha = hashlib.sha256(man).hexdigest()
    if pin and pin.get("manifest_sha256") and pin["manifest_sha256"] != msha:
        refuse(f"generated manifest.json SHA-256 {msha}, not the pinned {pin['manifest_sha256']}")
    sides = {}
    for p, r in records.items():
        sides.setdefault(f"{r[2][0]}x{r[2][1]}", []).append(p)
    print(f"{a.src}: ok, {len(records)} WebPs, {total} bytes ({total / 2**20:.1f} MiB), "
          + ", ".join(f"{k}: {len(v)}" for k, v in sorted(sides.items())))
    print(f"staged into {a.out}: manifest.json sha256 {msha} ({len(man)} bytes), ATTRIBUTION.md ({len(att)} bytes)")


def _stage_records(stage):
    files = walk(stage)
    if set(files) != STAGED:
        refuse(f"staged files differ: {sorted(set(files) ^ STAGED)}")
    records = {}
    for p in sorted(EXPECTED):
        size = files[p]
        if not MIN_FILE <= size <= MAX_FILE:
            refuse(f"{p}: {size} bytes, outside {MIN_FILE}..{MAX_FILE}")
        path = os.path.join(stage, *p.split("/"))
        webp_info(path, p)
        records[p] = (size, sha256_file(path))
    return files, records


def cmd_recheck(a):
    pin = load_pins(a.pins, a.version) if a.pins else None
    files, records = _stage_records(a.stage)
    man = manifest_bytes(records)
    staged = read_small(os.path.join(a.stage, "manifest.json"), MAX_MANIFEST)
    if staged != man:
        refuse("manifest.json is not the one generated from the staged files")
    msha = hashlib.sha256(man).hexdigest()
    if pin is not None:
        if not pin.get("manifest_sha256"):
            refuse(f"{a.version}: no manifest_sha256 pinned (required before upload)")
        if pin["manifest_sha256"] != msha:
            refuse(f"manifest.json SHA-256 {msha}, not the pinned {pin['manifest_sha256']}")
    att = read_small(os.path.join(a.stage, "ATTRIBUTION.md"))
    att.decode("utf-8")
    if f"pack, {a.version}\n".encode() not in att:
        refuse("ATTRIBUTION.md is not this version's")
    print(f"{a.stage}: ok, {len(records)} WebPs, {sum(r[0] for r in records.values())} bytes, manifest {msha}")


def _listing(path, version):
    pre = f"cliff/4k/{version}/"
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    doc = json.loads(text) if text else {}
    out = {}
    for c in doc.get("Contents") or []:
        k = c.get("Key", "")
        if not k.startswith(pre):
            refuse(f"listing key {k!r} outside {pre}")
        out[k[len(pre):]] = int(c.get("Size", -1))
    return out


def cmd_remote(a):
    if not VERSION_RE.fullmatch(a.version):
        refuse(f"bad version {a.version!r}")
    keys = _listing(a.listing, a.version)
    stray = set(keys) - STAGED
    if stray:
        refuse(f"cliff/4k/{a.version}/ holds keys outside the pack: {sorted(stray)}")
    if a.complete:
        files = walk(a.stage)
        for p in sorted(EXPECTED | {"ATTRIBUTION.md"}):
            if keys.get(p) != files[p]:
                refuse(f"{p}: in the bucket {keys.get(p)} bytes, staged {files[p]}")
        print(f"cliff/4k/{a.version}/: every file uploaded with its size")
        return
    print("state=" + ("published" if "manifest.json" in keys else "new"))
    print(f"keys={len(keys)}")


def _get(url, limit, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read(limit + 1)
                if len(body) > limit:
                    refuse(f"{url}: over {limit} bytes")
                return body, r.headers
        except urllib.error.HTTPError as e:
            if e.code not in (404, 429, 500, 502, 503, 504) or i == tries - 1:
                raise
        except urllib.error.URLError:
            if i == tries - 1:
                raise
        time.sleep(20 * (i + 1))


def cmd_live(a):
    if not BASE_RE.fullmatch(a.base):
        refuse(f"bad base {a.base!r}")
    files, records = _stage_records(a.stage)
    staged = read_small(os.path.join(a.stage, "manifest.json"), MAX_MANIFEST)
    body, _ = _get(a.base + "manifest.json", MAX_MANIFEST)
    if body != staged:
        refuse(f"{a.base}manifest.json differs from the staged one")
    for p in sorted(EXPECTED):
        size, digest = records[p]
        body, headers = _get(a.base + p, size)
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            refuse(f"{a.base}{p}: bytes differ from the manifest")
        ct = (headers.get("Content-Type") or "").split(";")[0].strip()
        if ct != "image/webp":
            print(f"::warning::{p}: Content-Type {ct!r}")
    print(f"{a.base}: manifest.json and all {len(records)} files served as staged")


def _top_spans(text):
    """{key: (value start, value end)} of a JSON object's top-level members, and the closing brace's index."""
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


def cdn_key_text(text, base):
    if not BASE_RE.fullmatch(base):
        refuse(f"bad base {base!r}")
    before = json.loads(text)
    if not isinstance(before, dict):
        refuse("cdn.json: not an object")
    value = {"enabled": True, "base": base}
    if before.get("cliffs") == value:
        return text
    spans, close = _top_spans(text)
    v = json.dumps(value, separators=(", ", ": "))
    if "cliffs" in spans:
        s, e = spans["cliffs"]
        new = text[:s] + v + text[e:]
    else:
        head = text[:close].rstrip()
        new = head + (", " if spans else "") + '"cliffs": ' + v + text[len(head):]
    if json.loads(new) != dict(before, cliffs=value):
        refuse("cdn.json: the edit changed more than the cliffs key")
    ns, _ = _top_spans(new)
    for k, (s, e) in spans.items():
        if k != "cliffs" and text[s:e] != new[ns[k][0]:ns[k][1]]:
            refuse(f"cdn.json: {k} changed")
    return new


def cmd_cdn_key(a):
    with open(a.file, encoding="utf-8", newline="") as f:
        text = f.read(256 * 1024 + 1)
    if len(text) > 256 * 1024:
        refuse("cdn.json over 256 KiB (players read no more)")
    new = cdn_key_text(text, a.base)
    if new == text:
        print(f"{a.file}: cliffs names {a.base} already")
        return
    tmp = a.file + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, a.file)
    print(f"{a.file}: cliffs {a.base}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("names")
    p = sub.add_parser("pin")
    p.add_argument("--pins", required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("unzip")
    p.add_argument("zip")
    p.add_argument("dest")
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("check")
    p.add_argument("src")
    p.add_argument("out")
    p.add_argument("--version", required=True)
    p.add_argument("--pins", default="")
    p = sub.add_parser("recheck")
    p.add_argument("stage")
    p.add_argument("--version", required=True)
    p.add_argument("--pins", default="")
    p = sub.add_parser("remote")
    p.add_argument("stage")
    p.add_argument("--version", required=True)
    p.add_argument("--listing", required=True)
    p.add_argument("--complete", action="store_true")
    p = sub.add_parser("live")
    p.add_argument("stage")
    p.add_argument("--base", required=True)
    p = sub.add_parser("cdn-key")
    p.add_argument("file")
    p.add_argument("--base", required=True)
    a = ap.parse_args()
    cmds = {"names": cmd_names, "pin": cmd_pin, "unzip": cmd_unzip, "check": cmd_check, "recheck": cmd_recheck,
            "remote": cmd_remote, "live": cmd_live, "cdn-key": cmd_cdn_key}
    try:
        cmds[a.cmd](a)
    except (Refused, zipfile.BadZipFile, OSError, ValueError, IndexError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
