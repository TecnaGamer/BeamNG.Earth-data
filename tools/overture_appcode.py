#!/usr/bin/env python3
"""The overture workflow's steps that run BeamNG.Earth's own code (.github/workflows/overture.yml). This script runs
ONLY in jobs WITHOUT secrets and with a read-only token (validate, products, gate): it imports the app tree given by
--tree (a checkout of BeamNG.Earth at a pinned commit) and asks the app's own functions. The jobs that can write to
this repo run tools/overture_ci.py (standard library only) instead.

    overture_appcode.py schema --tree CODE --release R
        overturepick.check_stac (the check every player's app makes before it takes cdn.json's release): Overture's
        STAC lists R and its catalogue states a schema this build reads. Exit 0 when taken, 1 otherwise.
    overture_appcode.py counts --tree CODE --cache C --plans OUT --out counts.json
        per corpus place (tools/portcheck/build.py's OUT/plans/<place>/index.json), the features per layer the app's
        MapData gives for the place's cells and their eight neighbours (the cell's own features only), with every
        error. The release is the process's (BNGE_OVERTURE_RELEASE, as the corpus build had it).
    overture_appcode.py gate-info --tree CODE --release R --cache C
        what the flip gate needs from the app (printed as key=value lines for $GITHUB_OUTPUT): schema_ok (check_stac
        again), vkey (decisions.vkey of the map data's versions in a decision builder's environment with
        BNGE_OVERTURE_RELEASE=R: tools/build_decisions.py's player_env, the switch's DEM release, a block worker set up
        as its _worker sets one up), dec_format (decisions.FORMAT), bnwt_format and bnwt_names (bnwt.FORMAT, the
        archives' index names, as bnwt.yml computes them).
"""
import argparse
import json
import os
import re
import sys
import time

RELEASE_RE = re.compile(r"20[2-9]\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\.\d{1,2}")
LAYERS = ("roads", "connectors", "buildings", "building_parts", "places", "water", "land_use", "infrastructure")
# blockworker.init's parameters as tools/build_decisions.py's _worker passes them (gate-info): a tree whose init takes
# others stops the gate (the vkey would not be the builder's) until this script follows it
INIT_PARAMS = ("cache_dir", "offline", "roads", "grading", "road_widths", "surface", "warm_q", "log_level", "airports",
               "sea", "nice", "roads_in_ground", "gas_stations")


def _tree(tree):
    tree = os.path.abspath(tree)
    for sub in ("tools", "server"):         # (server first on the path: the app's own modules win)
        sys.path.insert(0, os.path.join(tree, sub))
    import npyguard         # (as earthd and the builders: numpy's AVX-512 kernels held back before numpy is imported)
    npyguard.apply()
    return tree


def _release(r):
    if not RELEASE_RE.fullmatch(r or ""):
        raise SystemExit(f"not a release id: {r!r}")
    return r


def check_schema(release, tries=3):
    """(ok, why) of the app's overturepick.check_stac; no answer after tries: (False, why)."""
    import netfetch
    import overturepick
    fetcher = netfetch.Fetcher()
    why = ""
    for i in range(tries):
        try:
            return overturepick.check_stac(release, lambda url: fetcher.get(url, kind="demand").body)
        except overturepick.Unreachable as e:
            why = f"no answer: {e}"
            time.sleep(10 * (i + 1))
    return False, why


def cmd_schema(a):
    _tree(a.tree)
    ok, why = check_schema(_release(a.release))
    print(f"{a.release}: {'taken' if ok else 'not taken'} ({why})")
    return 0 if ok else 1


def cmd_counts(a):
    _tree(a.tree)
    import numpy as np
    import mapcells
    import mapdata
    md = mapdata.MapData(os.path.abspath(a.cache))
    res = {"release": md.release, "places": {}}
    root = os.path.join(a.plans, "plans")
    for place in sorted(os.listdir(root)):
        try:
            with open(os.path.join(root, place, "index.json"), encoding="utf-8") as f:
                idx = json.load(f)
        except (OSError, ValueError) as e:
            res["places"][place] = {"cells": [], "layers": {}, "errors": [f"index.json: {type(e).__name__}: {e}"]}
            continue
        cells = set()
        for c in idx.get("cells", ()):
            z, x, y = (int(v) for v in str(c).split("/"))
            cells |= {(z, x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        r = {"cells": [f"{z}/{x}/{y}" for z, x, y in sorted(cells)], "layers": {k: 0 for k in LAYERS}, "errors": []}
        t0 = time.time()
        for z, x, y in sorted(cells):
            try:
                cd = md.cell(mapcells.CellKey(z, x, y), layers=LAYERS, kind="demand")
            except Exception as e:      # (reported: the check fails on any)
                r["errors"].append(f"{z}/{x}/{y}: {type(e).__name__}: {e}")
                continue
            for name in LAYERS:
                f = cd.layers.get(name)
                if f is None or not len(f):
                    continue
                own = np.asarray(f.own, bool) if getattr(f, "own", None) is not None else np.ones(len(f), bool)
                r["layers"][name] += int(own.sum())
        r["seconds"] = round(time.time() - t0, 1)
        res["places"][place] = r
        print(place, r["seconds"], r["layers"], len(r["errors"]), "errors", flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, sort_keys=True)
    return 0


def vkey_info(release, cache):
    """(vkey hex, decisions.FORMAT, the map data's versions) a decision builder of this tree has for release."""
    os.environ["BNGE_OVERTURE_RELEASE"] = release
    os.makedirs(cache, exist_ok=True)
    import build_decisions
    build_decisions.player_env()            # (the build switches as every player's server has them)
    import cdn
    import netfetch
    if "BNGE_DEM_CDN" not in os.environ:    # (build_decisions.main: the DEM packs' release from the switch file)
        os.environ["BNGE_DEM_CDN"] = cdn.dem_release(netfetch.Fetcher(), cache) or "off"
    import overturepick
    got = overturepick.choose_for_process(cache)
    if got != release:
        raise SystemExit(f"the tree picks {got}, not {release}")
    import inspect
    import logging
    import blockworker as bw
    params = tuple(inspect.signature(bw.init).parameters)
    if params != INIT_PARAMS:
        raise SystemExit(f"blockworker.init takes {params}: follow build_decisions._worker in overture_appcode.py")
    # (as build_decisions._worker: roads, grading, measured widths, surface, airports, sea, gas stations)
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    import decisions
    v = bw._s.md.versions()
    vv = decisions.vkey(v)
    doc = decisions.vkey_doc(v)
    if vv is None or not doc or doc.get("versions", {}).get("overture") != release:
        raise SystemExit(f"the version key's document does not hold {release}: {doc}")
    return vv[0], decisions.FORMAT, v


def cmd_gate_info(a):
    _tree(a.tree)
    rel = _release(a.release)
    ok, why = check_schema(rel)
    print(f"schema: {rel} {'taken' if ok else 'not taken'} ({why})", file=sys.stderr)
    vk, fmt, versions = vkey_info(rel, os.path.abspath(a.cache))
    print(f"versions: {json.dumps(versions, sort_keys=True)}", file=sys.stderr)
    import bnwt
    names = {k: f"{k}.c{bnwt.CONTENT[k]}" for k in bnwt.ARCHIVES}
    out = {"schema_ok": "true" if ok else "false", "vkey": vk, "dec_format": str(int(fmt)),
           "bnwt_format": str(int(bnwt.FORMAT)), "bnwt_names": json.dumps(names, separators=(",", ":"), sort_keys=True)}
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("schema")
    p.add_argument("--tree", required=True)
    p.add_argument("--release", required=True)
    p = sub.add_parser("counts")
    p.add_argument("--tree", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--plans", required=True, help="tools/portcheck/build.py's --out")
    p.add_argument("--out", required=True)
    p = sub.add_parser("gate-info")
    p.add_argument("--tree", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--cache", required=True)
    a = ap.parse_args()
    return {"schema": cmd_schema, "counts": cmd_counts, "gate-info": cmd_gate_info}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
