#!/usr/bin/env python3
"""The aerial workflow's own steps around tools/build_aerial_packs.py (.github/workflows/aerial.yml):

    aerial_ci.py plan --set us_T6 [--adm "MD DC"] --gen 1 --frags DIR --jobs 20 [--max-cells 40] [--dry-run]
        the cells still to build (a cell is done when a fragment of generation >= gen lists every pack of the set
        there, or says there are no open photos), in at most --jobs chunks of about equal work, each at most
        --max-cells cells (0: no cap; the job's time budget then cuts it). Prints {"chunks": [...], ...} (a matrix).
        Needs numpy (the set's npz); runs in a job WITHOUT secrets.
    aerial_ci.py check IN --gen 1 STAGE
        an artifact of a build job checked before upload: only regular files named as packs of this generation or as
        fragments, each pack's header naming its own path, each fragment well formed and every pack its mask lists
        on hand; the accepted files are moved under STAGE (packs/ and frags/). Standard library only (+ tilepack.py):
        runs in the upload job, which holds the R2 secrets.
    aerial_ci.py merge FRAGS OUT
        the coverage files OUT/cov/<rx>/<ry>.json from every fragment FRAGS/_cells/*.json (a cell's newest
        generation with packs wins); standard library only, as build_aerial_packs.py --merge.

The fragment and coverage formats are build_aerial_packs.py's (its docstring).
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tilepack  # noqa: E402

K = 3
Z_TOP, Z_LOW = 17, 14
CELL_Z = Z_LOW - K          # 11
REGION_Z = 6
MAX_PACK = 32 << 20         # bytes: a zoom-17 pack is ~0.3-1.5 MB
PACK_RE = re.compile(r"g(\d+)/(\d+)/(\d+)/(\d+)\.bngp")
FRAG_RE = re.compile(r"_cells/(\d+)_(\d+)\.json")
SET_RE = re.compile(r"[a-z]{2}(_[a-z]+)?_T[0-9a-z]+")
CLASS_RANK = {"core": 0, "urban": 1, "corr": 2}


def read_frag(path):
    """A fragment's (cell, g, mask) or None when it is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        cx, cy = (int(v) for v in d["cell"])
        g = int(d["g"])
        p = d.get("p")
        mask = int(p, 16) if p else 0
        if not (0 <= cx < 1 << CELL_Z and 0 <= cy < 1 << CELL_Z and g >= 0 and 0 <= mask < 1 << 64):
            return None
        if p is not None and not re.fullmatch(r"[0-9a-f]{16}", p):
            return None
        return (cx, cy), g, mask, d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def frags_in(d):
    out = {}
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if n.endswith(".json"):
            r = read_frag(os.path.join(d, n))
            if r is not None:
                out[r[0]] = r[1:]
    return out


# --- plan ---------------------------------------------------------------------------------------------------------
def plan(a):
    import numpy as np
    if not SET_RE.fullmatch(a.set):
        raise SystemExit(f"bad set: {a.set!r}")
    adm = set(a.adm.split())
    if not all(re.fullmatch(r"[A-Z]{2,3}", v) for v in adm):
        raise SystemExit(f"bad adm: {a.adm!r}")
    d = np.load(os.path.join(a.sets, f"{a.set}.npz"), allow_pickle=False)
    cls = d["cls"].tolist() if "cls" in d.files else ["urban"] * len(d["x"])
    want = {}                                   # (cx, cy) -> [mask, packs, best class rank]
    for x, y, ad, c in zip(d["x"].tolist(), d["y"].tolist(), d["adm"].tolist(), cls):
        if adm and ad not in adm:
            continue
        w = want.setdefault((x >> K, y >> K), [0, 0, 9])
        b = 1 << ((y & 7) * 8 + (x & 7))
        if not w[0] & b:
            w[0] |= b
            w[1] += 1
        w[2] = min(w[2], CLASS_RANK.get(c, 3))
    done = frags_in(os.path.join(a.frags, "_cells")) if a.frags else {}
    todo, n_done, grow = [], 0, []
    for c, (mask, n, rank) in want.items():
        f = done.get(c)
        if f is not None and (f[0] == 0 and not f[1] or f[0] >= a.gen and mask & ~f[1] == 0):
            n_done += 1                         # (no open photos there, or every pack built)
        elif f is not None and f[0] >= a.gen and f[1]:
            grow.append(c)                      # (built at this generation without some of the set's packs: the
            #                                      zoom 16..14 packs would change under the same keys; needs gen + 1)
        else:
            todo.append((rank, -n, c, n))
    todo.sort()                                 # (urban cores first, then the fuller cells)
    if a.dry_run:
        todo = sorted(todo, key=lambda t: (t[3], t[2]))[:2]     # (the two smallest cells: a quick trial)
        jobs = 1
    else:
        jobs = max(1, min(a.jobs, 256))
        if a.max_cells > 0:
            todo = todo[:jobs * a.max_cells]
    bins = [[0, []] for _ in range(min(jobs, len(todo)))]
    for _r, _n, c, n in todo:                 # (in priority order: a job cut by its time budget did the most wanted)
        cand = [b for b in bins if not a.max_cells or a.dry_run or len(b[1]) < a.max_cells]
        b = min(cand, key=lambda b: b[0])
        b[0] += n + 21 * n / 64                 # (+ its zooms 16..14: 21 packs a whole cell)
        b[1].append(c)
    chunks = [{"i": i, "cells": " ".join(f"{x},{y}" for x, y in b[1])} for i, b in enumerate(bins) if b[1]]
    out = {"chunks": chunks, "cells": len(want), "done": n_done, "this_run": len(todo),
           "left_after": len(want) - n_done - len(todo) - len(grow), "grow_skipped": len(grow),
           "packs_this_run": int(sum(t[3] for t in todo))}
    print(json.dumps(out, separators=(",", ":")))
    print(f"set {a.set}{' ' + a.adm if a.adm else ''} gen {a.gen}: {len(want)} cells, {n_done} done, {len(todo)} in "
          f"this run ({out['packs_this_run']} zoom-17 packs, {len(chunks)} jobs), {out['left_after']} left for later "
          f"runs" + (f", {len(grow)} cells built at this generation without some of the set's packs (skipped: build "
                     f"them with the next gen)" if grow else ""), file=sys.stderr)


# --- check (before upload) ----------------------------------------------------------------------------------------
def check(a):
    src = os.path.realpath(a.input)
    packs, frags, bad = {}, {}, []
    for root, dirs, files in os.walk(src):
        for n in dirs + files:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if os.path.islink(p):
                bad.append(f"{rel}: a link")
                continue
            if n in dirs:
                continue
            if not os.path.isfile(p):
                bad.append(f"{rel}: not a file")
                continue
            m = PACK_RE.fullmatch(rel)
            if m:
                g, z, px, py = (int(v) for v in m.groups())
                if g != a.gen or not Z_LOW <= z <= Z_TOP or os.path.getsize(p) > MAX_PACK:
                    bad.append(f"{rel}: generation, zoom or size")
                    continue
                with open(p, "rb") as f:
                    data = f.read()
                try:
                    hz, hpx, hpy, hk, tiles = parse_head(data)
                except ValueError as e:
                    bad.append(f"{rel}: {e}")
                    continue
                if (hz, hpx, hpy, hk) != (z, px, py, K) or not tiles:
                    bad.append(f"{rel}: its header says {hz}/{hpx}/{hpy} k {hk}, {tiles} tiles")
                    continue
                packs[(z, px, py)] = rel
                continue
            m = FRAG_RE.fullmatch(rel)
            if m:
                r = read_frag(p)
                if r is None or r[0] != tuple(int(v) for v in m.groups()) or r[1] not in (0, a.gen) or \
                        (r[1] == 0) != (r[2] == 0):
                    bad.append(f"{rel}: not a fragment of generation {a.gen}")
                    continue
                frags[r[0]] = (rel, r[2])
                continue
            bad.append(f"{rel}: not a pack or fragment")
    ok_frags, used = [], set()
    for (cx, cy), (rel, mask) in sorted(frags.items()):
        need = [(Z_TOP, (cx << K) + (b & 7), (cy << K) + (b >> 3)) for b in range(64) if mask >> b & 1]
        if mask:
            need.append((Z_LOW, cx, cy))
        miss = [k for k in need if k not in packs]
        if miss:
            bad.append(f"{rel}: {len(miss)} of its packs missing (first {miss[0]})")
            continue
        ok_frags.append(rel)
        # (the cell's packs: its zoom-17 ones and every zoom 16..14 pack under the cell)
        for (z, px, py), prel in packs.items():
            if (px >> (z - K - CELL_Z), py >> (z - K - CELL_Z)) == (cx, cy):
                used.add(prel)
    orphans = sorted(set(packs.values()) - used)
    for rel in sorted(used):
        dst = os.path.join(a.stage, "packs", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(src, rel), dst)
    for rel in ok_frags:
        dst = os.path.join(a.stage, "frags", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(src, rel), dst)
    rep = {"packs": len(used), "cells": len(ok_frags), "orphan_packs": len(orphans), "rejected": len(bad)}
    print(json.dumps(rep))
    for b in bad[:50]:
        print(f"::warning::rejected {b}", file=sys.stderr)
    if orphans:
        print(f"{len(orphans)} packs of cells without a fragment left out (a cell that failed: built again next run)",
              file=sys.stderr)
    return 1 if bad else 0


def parse_head(data):
    if len(data) < tilepack.HEAD.size:
        raise ValueError("short")
    magic, ver, k, z, _r, px, py = tilepack.HEAD.unpack_from(data)
    if magic != tilepack.MAGIC or ver != tilepack.VERSION:
        raise ValueError("not a BNGP pack")
    n = 1 << (2 * k)
    if len(data) < tilepack.HEAD.size + 8 * n:
        raise ValueError("short table")
    body = len(data) - tilepack.HEAD.size - 8 * n
    tiles = 0
    for i in range(n):
        off, ln = int.from_bytes(data[tilepack.HEAD.size + 8 * i:][:4], "little"), \
            int.from_bytes(data[tilepack.HEAD.size + 8 * i + 4:][:4], "little")
        if ln:
            if off + ln > body:
                raise ValueError("a tile past the end")
            tiles += 1
    return z, px, py, k, tiles


# --- merge (coverage files) ---------------------------------------------------------------------------------------
def merge(a):
    by_region = {}
    for (cx, cy), (g, mask, d) in frags_in(os.path.join(a.frags, "_cells")).items():
        r = (cx >> (CELL_Z - REGION_Z), cy >> (CELL_Z - REGION_Z))
        cells = by_region.setdefault(r, {})
        k = f"{cx},{cy}"
        e = {"g": g, "src": str(d.get("src", ""))[:16]}
        if mask:
            e["p"] = f"{mask:016x}"
            e["year"] = d.get("year") if isinstance(d.get("year"), int) else None
        cells[k] = e
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for (rx, ry), cells in sorted(by_region.items()):
        doc = {"format": 1, "region": [rx, ry], "cells": cells, "updated": now}
        p = os.path.join(a.out, "cov", str(rx), f"{ry}.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"), sort_keys=True)
        os.replace(p + ".tmp", p)
    print(json.dumps({"regions": len(by_region), "cells": sum(len(c) for c in by_region.values())}))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--set", required=True)
    p.add_argument("--sets", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aerial", "sets"))
    p.add_argument("--adm", default="")
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--frags", help="a folder with _cells/ (the bucket's fragments)")
    p.add_argument("--jobs", type=int, default=20)
    p.add_argument("--max-cells", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("input")
    c.add_argument("--gen", type=int, required=True)
    c.add_argument("stage")
    m = sub.add_parser("merge")
    m.add_argument("frags")
    m.add_argument("out")
    a = ap.parse_args()
    if a.cmd == "plan":
        return plan(a)
    if a.cmd == "check":
        return check(a)
    return merge(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
