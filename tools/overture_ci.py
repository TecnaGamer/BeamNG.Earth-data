#!/usr/bin/env python3
"""The overture workflow's own steps (.github/workflows/overture.yml): the automatic Overture release update. This
script is this repo's own, standard library only: the jobs that can write to this repo (publish, flip) run nothing
else (and git / gh). BeamNG.Earth's own code runs only in the jobs without write access (tools/overture_appcode.py).

    overture_ci.py detect --cdn cdn.json --app-pick F --dev-pick F --app-sha S --dev-sha S [--candidate F] [--want R]
        whether there is a release to bring in: STAC's latest (or --want) in Overture's catalogue, of a strict id,
        newer than the release players are served (cdn.json's "overture" release, else the app's own RELEASE, read
        from its server/overturepick.py as text); never backwards. Prints key=value lines for $GITHUB_OUTPUT: state
        (none / new / published: a candidate for this release and app is on the candidates branch already), release,
        served, served_listed, app_release, published_at, codes (the app trees to validate: ["app", "dev"]).
    overture_ci.py corpus --corpus corpus.json --release R --out OUT --log LOG --counts C [--old-* ...]
        the corpus build (BeamNG.Earth tools/portcheck/build.py --online) and its counts (overture_appcode.py counts)
        checked: every place planned on R (its index's versions name R), no cell errors, every layer's count within
        bounds of the served release's (--old-*).
    overture_ci.py check-stac FILE --release R [--like PREV]
        the tall index's STAC file list (BeamNG.Earth tools/build_stac_index.py): gzip JSON of release R, every
        collection non-empty (and every collection PREV has), each file's name, bbox, size, and URLs on Overture's
        own hosts under release/R/ only (the tallbld job, which holds the R2 secrets, downloads them).
    overture_ci.py check-places FILE --release R [--like BUNDLED]
        the places snapshot (BeamNG.Earth tools/build_places.py): format 1, release R, places non-empty.
    overture_ci.py stage-stac FILE --release R --served S --tools DIR
        FILE checked (check-stac) and copied to DIR/overture_stac_R.json.gz; STAC files of releases older than S removed.
    overture_ci.py candidate --release R --app-tag T --app-sha S --served X --run-url U --out FILE
        the candidates branch's overture.json for the mini PC's decision builder.
    overture_ci.py gate --release R --served S --vkey V --dec-format F --bnwt-format F --bnwt-names J --schema-ok B
            [--published-at T]
        the flip gate: the tall index on the data site names R, the decision archives' manifest for V answers on the
        tile host, the BNWT indexes answer for R when they were built for S, the app's schema check said yes.
        Prints ready, pending, stuck (a candidate older than --stuck-days).
    overture_ci.py flip-cdn FILE --release R
        cdn.json's "overture" key set to {"release": R} by a minimal text edit (every other key byte-identical),
        checked by parsing both; never to a release older than the one it names. With --bnwt-format/--bnwt-names/
        --cip-format, the "bnwt" and "cip" keys follow in the same edit (see sync-cdn).
    overture_ci.py sync-cdn FILE --bnwt-format F --bnwt-names J --cip-format F [--release R]
        cdn.json's "bnwt" and "cip" keys moved to R (default: cdn.json's overture release) by the same minimal edit, each
        only when it exists with enabled true, its release is older, and that release's build is on the tile host
        (bnwt: every archive's bnwt<F>/R/<name>.index.json answers; cip: cip<F>/R/latest.json names a build whose
        cells.index.json answers). Otherwise the key stays (inert, safe) and the summary says so.
"""
import argparse
import datetime
import gzip
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

# (BeamNG.Earth server/overturepick.py RELEASE_RE: the strict form)
RELEASE_RE = re.compile(r"20[2-9]\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\.\d{1,2}")
TAG_RE = re.compile(r"v[0-9][0-9A-Za-z.+-]{0,63}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
VKEY_RE = re.compile(r"[0-9a-f]{32}")
RUN_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+")
STAC = "https://stac.overturemaps.org"
PAGES = "https://tecnagamer.github.io/BeamNG.Earth-data"
CDN = "https://tiles.tgforever.net"
SWITCH_MAX = 256 * 1024             # (BeamNG.Earth overturepick.SWITCH_MAX_BYTES: players read no more)
STAC_HOSTS = ("overturemaps-us-west-2.s3.us-west-2.amazonaws.com", "overturemapswestus2.blob.core.windows.net")
PART_RE = re.compile(r"part-\d{5}-[0-9a-f-]{36}-c000\.zstd\.parquet")
UA = "BeamNG.Earth-data overture workflow"
# count sanity (overture_ci.py corpus): a layer's features per corpus place may move this much between releases
# (2026-10-21.0 -> 2026-11-18.0, the 8 places on 16 cells each: worst drop -0.3 % roads Laramie, worst growth
# +3.9 % places Vienna), and always by SLACK features (small layers: Stelvio's 0 building parts)
MAX_DROP_PCT = 3.0
MAX_GROW_PCT = 10.0
SLACK = 25
SYNC_KEYS = ("bnwt", "cip")          # (cdn.json's keys the app reads only when their release is its Overture release)
BUILD_RE = re.compile(r"[0-9a-f]{16}")
BNWT_ARCHIVES = {"base", "transportation", "buildings", "places"}    # (BeamNG.Earth bnwt.ARCHIVES: all of them)


def valid(r):
    return isinstance(r, str) and RELEASE_RE.fullmatch(r) is not None


def order(r):
    day, _, n = r.partition(".")
    return day, int(n)


def say(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, max_bytes, tries=3):
    """(status, body) of a GET; status 0 when no answer after tries (body: the error). Never more than max_bytes."""
    err = ""
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read(max_bytes + 1)
                if len(body) > max_bytes:
                    return 0, f"more than {max_bytes} bytes"
                return r.status, body
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 403):
                return e.code, b""
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
        if i + 1 < tries:
            time.sleep(5 * (i + 1))
    return 0, err


def get_json(url, max_bytes=1 << 20):
    st, body = get(url, max_bytes)
    if st != 200:
        raise SystemExit(f"{url}: no answer ({st} {body!r})")
    return json.loads(body)


def outputs(d):
    for k, v in d.items():
        v = str(v)
        if "\n" in v or "\r" in v:
            raise SystemExit(f"output {k}: a line break")
        print(f"{k}={v}")


# --- detect ---------------------------------------------------------------------------------------------------------
def pick_release(path):
    """The RELEASE a tree's server/overturepick.py states (read as text, never run), or None (no such file: the tree
    does not read cdn.json's overture key)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(1 << 20)
    except OSError:
        return None
    m = re.search(r'^RELEASE = "([^"]+)"', text, re.M)
    if not m or not valid(m[1]):
        raise SystemExit(f"{path}: no RELEASE = \"<release id>\" line")
    return m[1]


def read_switch(path):
    with open(path, "rb") as f:
        body = f.read(SWITCH_MAX + 1)
    if len(body) > SWITCH_MAX:
        raise SystemExit(f"{path}: more than {SWITCH_MAX} bytes")
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: not an object")
    return doc, body


def pick_schemas(path):
    """SUPPORTED_SCHEMAS of a tree's server/overturepick.py, read as text."""
    with open(path, encoding="utf-8") as f:
        m = re.search(r"^SUPPORTED_SCHEMAS = \(([^)]*)\)", f.read(1 << 20), re.M)
    got = re.findall(r'"(\d+\.\d+\.\d+)"', m[1]) if m else []
    if not got:
        raise SystemExit(f"{path}: no SUPPORTED_SCHEMAS = (\"x.y.z\", ...) line")
    return got


def served_release(doc, app_release):
    """(release players are served, where from): cdn.json's overture release, else the app's own."""
    on = doc.get("overture")
    if on is None:
        return app_release, "the app's own (cdn.json names none)"
    rel = on.get("release") if isinstance(on, dict) else None
    if not valid(rel):
        raise SystemExit(f"cdn.json's overture key is not {{\"release\": <release id>}}: {on!r}")
    return rel, "cdn.json"


def stac_listing(stac=STAC):
    cat = get_json(f"{stac}/catalog.json")
    listed = set()
    for ln in cat.get("links", ()):
        if isinstance(ln, dict) and ln.get("rel") == "child":
            m = re.fullmatch(rf"(?:{re.escape(stac)}|\.)/([^/]+)/catalog\.json", str(ln.get("href")))
            if m and valid(m[1]):
                listed.add(m[1])
    return cat.get("latest"), listed


def read_candidate(path):
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            body = f.read(65537)
        doc = json.loads(body)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and len(body) <= 65536 else None


def detect(cdn_path, app_pick, dev_pick, app_sha, dev_sha, candidate=None, want="", stac=STAC, listing=None,
           floor_pick=None):
    """The detect step's outputs (a dict). floor_pick: the latest published release's overturepick.py or overture.py
    (its RELEASE line): never a release older than that one's own (an older app_ref must not move players back)."""
    out = {"state": "none", "release": "", "served": "", "served_listed": "false", "app_release": "",
           "published_at": "", "codes": "[]", "schemas": ""}
    for name, s in (("app sha", app_sha), ("dev sha", dev_sha)):
        if not SHA_RE.fullmatch(s or ""):
            raise SystemExit(f"{name}: not a full commit SHA: {s!r}")
    app_rel = pick_release(app_pick)
    if app_rel is None:
        say("the app release does not read cdn.json's overture key (no server/overturepick.py): nothing to do")
        return out
    if pick_release(dev_pick) is None:
        raise SystemExit("dev has no server/overturepick.py: the corpus cannot run there on another release")
    doc, _ = read_switch(cdn_path)
    served, where = served_release(doc, app_rel)
    floor = pick_release(floor_pick) if floor_pick else None
    if floor and order(floor) > order(served):
        served, where = floor, "the latest published release's own"
    out["schemas"] = ",".join(pick_schemas(app_pick))
    latest, listed = listing if listing is not None else stac_listing(stac)
    want = (want or "").strip()
    new = want or latest
    if not valid(new):
        raise SystemExit(f"not a release id: {new!r}")
    if new not in listed:
        raise SystemExit(f"{new} is not listed in Overture's STAC catalogue")
    out.update(served=served, served_listed="true" if served in listed else "false", app_release=app_rel)
    say(f"served: {served} ({where}); the app's own {app_rel}; STAC latest {latest}; asked {new}")
    if order(new) <= order(served):
        say(f"{new} is not newer than {served}: nothing to do")
        return out
    out["release"] = new
    out["codes"] = json.dumps(["app"] if app_sha == dev_sha else ["app", "dev"])
    cand = read_candidate(candidate)
    if cand and cand.get("release") == new and cand.get("app_sha") == app_sha:
        at = str(cand.get("published_at") or "")
        out.update(state="published", published_at=at if re.fullmatch(r"[0-9T:Z-]{20}", at) else "")
        say(f"{new}: published for this app on {at}: the flip gate only")
    else:
        out["state"] = "new"
        say(f"{new}: new (validate, build, publish)")
    return out


def cmd_detect(a):
    outputs(detect(a.cdn, a.app_pick, a.dev_pick, a.app_sha, a.dev_sha, a.candidate, a.want,
                   floor_pick=a.floor_pick or None))
    return 0


# --- corpus ---------------------------------------------------------------------------------------------------------
def corpus_run(names, release, out_dir, log_path, counts_path):
    """({place: {"asked", "planned", "complete"}}, counts doc, [problems]) of one corpus build on release."""
    problems = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError as e:
        return {}, None, [f"{log_path}: {e}"]
    runs = {}
    for name in names:
        m = None
        for m in re.finditer(rf"^{re.escape(name)}: (\d+) cells asked, (\d+) cells planned \((\d+) complete\)", log,
                             re.M):
            pass
        if m is None:
            problems.append(f"{release} {name}: no result line (a crash: see build-{name}.log)")
            continue
        asked, planned, complete = (int(v) for v in m.groups())
        runs[name] = {"asked": asked, "planned": planned, "complete": complete}
        if planned == 0 or planned < asked:
            problems.append(f"{release} {name}: {planned} cells planned of {asked} asked")
        try:
            with open(os.path.join(out_dir, f"build-{name}.log"), encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if re.match(rf"{re.escape(name)}: (cell |airports: )", ln):
                        problems.append(f"{release} {ln.strip()[:300]}")
        except OSError as e:
            problems.append(f"{release} {name}: {e}")
        try:
            with open(os.path.join(out_dir, "plans", name, "index.json"), encoding="utf-8") as f:
                idx = json.load(f)
            got = (idx.get("versions") or {}).get("overture")
            if got != release:
                problems.append(f"{release} {name}: planned on Overture {got}, not {release}")
        except (OSError, ValueError) as e:
            problems.append(f"{release} {name}: index.json: {e}")
    try:
        with open(counts_path, encoding="utf-8") as f:
            counts = json.load(f)
    except (OSError, ValueError) as e:
        return runs, None, problems + [f"{counts_path}: {e}"]
    if counts.get("release") != release:
        problems.append(f"{counts_path}: counted on {counts.get('release')}, not {release}")
    for name in names:
        p = (counts.get("places") or {}).get(name)
        if p is None:
            problems.append(f"{release} {name}: not counted")
            continue
        for e in p.get("errors", ())[:20]:
            problems.append(f"{release} {name}: {str(e)[:300]}")
    return runs, counts, problems


def compare_counts(names, old, new, max_drop=MAX_DROP_PCT, max_grow=MAX_GROW_PCT, slack=SLACK):
    """([table lines], [problems]) of the layer counts old -> new per place."""
    lines, problems = [], []
    for name in names:
        po, pn = (old.get("places") or {}).get(name), (new.get("places") or {}).get(name)
        if po is None or pn is None:
            continue
        if po.get("cells") != pn.get("cells"):
            problems.append(f"{name}: counted on other cells")
        for layer in sorted(set(po.get("layers", {})) | set(pn.get("layers", {}))):
            if layer not in po.get("layers", {}):
                # (a layer the baseline doesn't count: the app tree added it, e.g. the natural features' land and
                # bathymetry; no baseline to bound it by yet)
                lines.append(f"| {name} | {layer} | - | {pn['layers'].get(layer, '-')} | new |")
                continue
            if layer not in pn.get("layers", {}):
                # (counted before, not now: a layer the new tree lost or failed to read is a problem, never "new")
                problems.append(f"{name}: {layer} counted before, missing now")
                lines.append(f"| {name} | {layer} | {po['layers'][layer]} | - | missing |")
                continue
            a, b = int(po["layers"].get(layer, 0)), int(pn["layers"].get(layer, 0))
            pct = (b - a) / a * 100.0 if a else (0.0 if b == 0 else math.inf)
            bad = abs(b - a) > slack and (pct < -max_drop or pct > max_grow)
            lines.append(f"| {name} | {layer} | {a} | {b} | {pct:+.1f} % |{' **out of bounds**' if bad else ''}")
            if bad:
                problems.append(f"{name} {layer}: {a} -> {b} ({pct:+.1f} %; bounds -{max_drop:g} % / "
                                f"+{max_grow:g} %, at least {slack} features)")
    return lines, problems


def cmd_corpus(a):
    if not valid(a.release) or (a.old_release and not valid(a.old_release)):
        raise SystemExit("not a release id")
    with open(a.corpus, encoding="utf-8") as f:
        names = sorted(json.load(f)["places"])
    runs, counts, problems = corpus_run(names, a.release, a.out, a.log, a.counts)
    print(f"### Corpus on {a.release} ({len(runs)} of {len(names)} places planned)\n")
    for name in names:
        r = runs.get(name)
        print(f"- {name}: " + (f"{r['planned']} cells planned of {r['asked']} asked ({r['complete']} complete)"
                               if r else "no result"))
    if a.old_release:
        old_runs, old_counts, old_problems = corpus_run(names, a.old_release, a.old_out, a.old_log, a.old_counts)
        problems += old_problems
        for name in names:
            if name in runs and name in old_runs and runs[name]["planned"] < old_runs[name]["planned"]:
                print(f"- (note) {name}: {old_runs[name]['planned']} -> {runs[name]['planned']} cells planned")
        if counts is not None and old_counts is not None:
            lines, bad = compare_counts(names, old_counts, counts, a.max_drop_pct, a.max_grow_pct, a.slack)
            problems += bad
            print(f"\n| place | layer | {a.old_release} | {a.release} | change |\n|---|---|---|---|---|")
            print("\n".join(lines))
    else:
        print("\n(no baseline: the served release is not listed any more; absolute checks only)")
        if counts is not None:
            for name in names:
                p = (counts.get("places") or {}).get(name) or {}
                if not any(int(v) for v in (p.get("layers") or {}).values()):
                    problems.append(f"{name}: no features at all")
    if problems:
        print("\n### Problems\n")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    print("\nAll checks passed.")
    return 0


# --- products -------------------------------------------------------------------------------------------------------
def read_gz_json(path, max_bytes=256 << 20):
    with gzip.open(path, "rb") as f:
        body = f.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise SystemExit(f"{path}: more than {max_bytes} bytes unpacked")
    return json.loads(body)


def upstream_counts(release, keys, stac=STAC):
    """{"theme/type": file count} from Overture's own STAC collections (partition:file_count, and as many item links)."""
    out = {}
    for key in keys:
        c = get_json(f"{stac}/{release}/{key}/collection.json", 8 << 20)
        n = c.get("partition:file_count")
        items = sum(1 for ln in c.get("links", ()) if isinstance(ln, dict) and ln.get("rel") == "item")
        if not isinstance(n, int) or n != items:
            raise SystemExit(f"Overture's STAC {key} of {release}: file count {n!r}, {items} items")
        out[key] = n
    return out


def check_stac(path, release, like=None, upstream=None):
    """The STAC file's collections (dict) when it passes; SystemExit with the reason otherwise. upstream: {"theme/type":
    file count} of Overture's own STAC (upstream_counts) each collection must have exactly, with unique names."""
    if not valid(release):
        raise SystemExit(f"not a release id: {release!r}")
    if os.path.basename(path) != f"overture_stac_{release}.json.gz":
        raise SystemExit(f"{path}: not named overture_stac_{release}.json.gz")
    d = read_gz_json(path)
    if not isinstance(d, dict) or set(d) != {"release", "collections"} or d["release"] != release:
        raise SystemExit(f"{path}: not {{release: {release}, collections}} (release {d.get('release')!r})")
    cols = d["collections"]
    if not isinstance(cols, dict) or not cols:
        raise SystemExit(f"{path}: no collections")
    for key, c in cols.items():
        m = re.fullmatch(r"([a-z_]+)/([a-z_]+)", key)
        if not m or not isinstance(c, dict) or not isinstance(c.get("files"), list) or not c["files"]:
            raise SystemExit(f"{path}: collection {key!r} is empty or not a collection")
        theme, typ = m.groups()
        prefix = f"/release/{release}/theme={theme}/type={typ}/"
        names = [r[0] if isinstance(r, list) and r else None for r in c["files"]]
        if len(set(map(str, names))) != len(names):
            raise SystemExit(f"{path}: {key}: a file listed twice")
        if upstream is not None and upstream.get(key) != len(names):
            raise SystemExit(f"{path}: {key}: {len(names)} files, Overture's STAC lists {upstream.get(key)}")
        for row in c["files"]:
            if not (isinstance(row, list) and len(row) == 4):
                raise SystemExit(f"{path}: {key}: a file row {str(row)[:200]}")
            name, bbox, urls, size = row
            if not (isinstance(name, str) and PART_RE.fullmatch(name)):
                raise SystemExit(f"{path}: {key}: file name {name!r}")
            if not (isinstance(bbox, list) and len(bbox) == 4 and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox)
                    and -180 <= bbox[0] <= bbox[2] <= 180 and -90 <= bbox[1] <= bbox[3] <= 90):
                raise SystemExit(f"{path}: {key}/{name}: bbox {bbox!r}")
            if not (isinstance(size, int) and not isinstance(size, bool) and 0 < size < 1 << 40):
                raise SystemExit(f"{path}: {key}/{name}: size {size!r}")
            if not (isinstance(urls, list) and 1 <= len(urls) <= len(STAC_HOSTS)):
                raise SystemExit(f"{path}: {key}/{name}: urls {urls!r}")
            for u in urls:
                ok = isinstance(u, str) and any(u == f"https://{h}{prefix}{name}" for h in STAC_HOSTS)
                if not ok:
                    raise SystemExit(f"{path}: {key}/{name}: url {str(u)[:200]!r} is not Overture's own of {release}")
    if like:
        prev = read_gz_json(like)
        pcols = prev.get("collections") or {}
        missing = sorted(set(pcols) - set(cols))
        if missing:
            raise SystemExit(f"{path}: collections missing that {os.path.basename(like)} has: {missing}")
        for key, c in pcols.items():
            was = sum(int(r[3]) for r in c.get("files", ()))
            now = sum(int(r[3]) for r in cols[key]["files"])
            if was and now < was / 2:
                raise SystemExit(f"{path}: {key}: {now} bytes of files, under half of {was}")
    return cols


def stac_keys(path):
    return sorted(k for k in (read_gz_json(path).get("collections") or {}) if re.fullmatch(r"[a-z_]+/[a-z_]+", k))


def cmd_check_stac(a):
    up = upstream_counts(a.release, stac_keys(a.file)) if a.upstream else None
    cols = check_stac(a.file, a.release, a.like, up)
    print(f"{a.file}: {a.release}, {len(cols)} collections, {sum(len(c['files']) for c in cols.values())} files: ok")
    return 0


def check_places(path, release, like=None):
    d = read_gz_json(path, 64 << 20)
    if not isinstance(d, dict) or d.get("format") != 1 or d.get("release") != release:
        raise SystemExit(f"{path}: not a format 1 places snapshot of {release}")
    places = d.get("places")
    if not isinstance(places, list) or not places:
        raise SystemExit(f"{path}: no places")
    for p in places[:1000]:
        if not (isinstance(p, dict) and isinstance(p.get("cc"), str) and isinstance(p.get("lat"), (int, float))
                and isinstance(p.get("lon"), (int, float))):
            raise SystemExit(f"{path}: a place {str(p)[:200]}")
    if like:
        was = len(read_gz_json(like, 64 << 20).get("places") or ())
        if was and len(places) < 0.9 * was:
            raise SystemExit(f"{path}: {len(places)} places, under 90 % of {os.path.basename(like)}'s {was}")
    return len(places)


def cmd_check_places(a):
    n = check_places(a.file, a.release, a.like)
    print(f"{a.file}: {a.release}, {n} places: ok")
    return 0


def stac_files(tools):
    out = {}
    for f in os.listdir(tools):
        m = re.fullmatch(r"overture_stac_(.+)\.json\.gz", f)
        if m and valid(m[1]):
            out[m[1]] = os.path.join(tools, f)
    return out


def cmd_stage_stac(a):
    if not (valid(a.release) and valid(a.served)):
        raise SystemExit("not a release id")
    have = stac_files(a.tools)
    prev = have.get(a.served) or (have[max(have, key=order)] if have else None)
    check_stac(a.file, a.release, prev if prev and os.path.basename(prev) != os.path.basename(a.file) else None,
               upstream_counts(a.release, stac_keys(a.file)))
    dst = os.path.join(a.tools, f"overture_stac_{a.release}.json.gz")
    shutil.copyfile(a.file, dst)
    print(f"staged {dst}")
    for rel, path in sorted(have.items()):
        if order(rel) < order(a.served):
            os.remove(path)
            print(f"removed {path} (older than the served {a.served})")
    return 0


def cmd_candidate(a):
    for what, v, rx in (("release", a.release, RELEASE_RE), ("served", a.served, RELEASE_RE),
                        ("app tag", a.app_tag, TAG_RE), ("app sha", a.app_sha, SHA_RE), ("run url", a.run_url, RUN_RE)):
        if not rx.fullmatch(v or ""):
            raise SystemExit(f"{what}: {v!r}")
    doc = {"format": 1, "release": a.release, "served": a.served, "app_tag": a.app_tag, "app_sha": a.app_sha,
           "published_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "run": a.run_url}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    print(json.dumps(doc, sort_keys=True))
    return 0


# --- gate -----------------------------------------------------------------------------------------------------------
def stac_schema(release, fetch=get, stac=STAC):
    """The schema:version Overture's STAC states for release when its root catalogue lists it, else None."""
    try:
        st, body = fetch(f"{stac}/catalog.json", 1 << 20)
        root = json.loads(body) if st == 200 else {}
        hrefs = {str(ln.get("href")) for ln in root.get("links", ()) if isinstance(ln, dict) and ln.get("rel") == "child"}
        if f"{stac}/{release}/catalog.json" not in hrefs and f"./{release}/catalog.json" not in hrefs:
            return None
        st, body = fetch(f"{stac}/{release}/catalog.json", 1 << 20)
        cat = json.loads(body) if st == 200 else {}
        return str(cat.get("schema:version")) if cat.get("release:version") == release else None
    except (ValueError, AttributeError, TypeError):
        return None


def gate(release, served, vkey, dec_format, bnwt_format, bnwt_names, schema_ok, published_at="", stuck_days=10.0,
         pages=PAGES, cdn=CDN, now=None, fetch=get, schemas=None):
    """(ready, [pending], stuck, [report lines]). schemas: the app's SUPPORTED_SCHEMAS (read as text from its
    overturepick.py by detect, never from a job that ran app code): the live STAC must state one of them."""
    if not (valid(release) and valid(served) and VKEY_RE.fullmatch(vkey or "")):
        raise SystemExit("gate: a bad release or vkey")
    if not (re.fullmatch(r"\d{1,3}", dec_format) and re.fullmatch(r"\d{1,3}", bnwt_format)):
        raise SystemExit("gate: a bad format")
    names = json.loads(bnwt_names)
    if not (isinstance(names, dict) and names and all(
            re.fullmatch(r"[a-z]+", k) and isinstance(v, str) and re.fullmatch(r"[a-z]+\.c\d+", v)
            for k, v in names.items())):
        raise SystemExit(f"gate: bnwt names {bnwt_names!r}")
    if set(names) != BNWT_ARCHIVES or any(not v.startswith(k + ".c") for k, v in names.items()):
        raise SystemExit(f"gate: bnwt names {bnwt_names!r} are not one name for each of {sorted(BNWT_ARCHIVES)}")
    if not schemas or not all(re.fullmatch(r"\d+\.\d+\.\d+", x) for x in schemas):
        raise SystemExit(f"gate: supported schemas {schemas!r}")
    pending, report = [], []

    def note(ok, gate_name, text):
        report.append(f"- {'ready' if ok else 'WAITING'}: {gate_name}: {text}")
        if not ok:
            pending.append(gate_name)

    note(schema_ok == "true", "schema", f"the app's overturepick.check_stac on {release}: {schema_ok}")
    sv = stac_schema(release, fetch)
    note(sv in schemas, "stac", f"Overture's STAC lists {release}: schema {sv} (the app reads {', '.join(schemas)})")
    st, body = fetch(f"{pages}/tallbld/manifest.json", 1 << 20)
    got = None
    if st == 200:
        try:
            m = json.loads(body)
            got = m.get("release") if isinstance(m, dict) and m.get("parts") else None
        except ValueError:
            got = None
    note(got == release, "tallbld", f"{pages}/tallbld/manifest.json names {got!r} (HTTP {st})")
    url = f"{cdn}/dec/d{dec_format}/{vkey}/manifest.bngm"
    st, body = fetch(url, 64 << 20)
    note(st == 200 and isinstance(body, bytes) and body[:4] == b"BNGM", "decisions", f"{url}: HTTP {st}")
    built = []
    for arch, name in sorted(names.items()):
        st, _ = fetch(f"{cdn}/bnwt{bnwt_format}/{served}/{name}.index.json", 1 << 20)
        if st == 200:
            built.append((arch, name))
        elif st not in (404, 410):      # (no answer, or a denial: not known whether BNWT is built)
            note(False, "bnwt", f"{cdn}/bnwt{bnwt_format}/{served}/{name}.index.json: HTTP {st}")
            break
    else:
        if not built:
            report.append(f"- skipped: bnwt: no archive index of {served} on the tile host (BNWT not built)")
        for arch, name in built:
            st, _ = fetch(f"{cdn}/bnwt{bnwt_format}/{release}/{name}.index.json", 1 << 20)
            note(st == 200, "bnwt", f"{name} of {release}: HTTP {st} ({served}'s is built)")
    stuck = False
    if published_at:
        try:
            t = datetime.datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            age = ((now or datetime.datetime.now(datetime.timezone.utc)) - t).total_seconds() / 86400.0
            stuck = bool(pending) and age > stuck_days
            report.append(f"- the candidate was published {age:.1f} days ago")
        except ValueError:
            pass
    return not pending, sorted(set(pending)), stuck, report


def cmd_gate(a):
    ready, pending, stuck, report = gate(a.release, a.served, a.vkey, a.dec_format, a.bnwt_format, a.bnwt_names,
                                         a.schema_ok, a.published_at, a.stuck_days, a.pages, a.cdn,
                                         schemas=[x for x in a.schemas.split(",") if x])
    say(f"### Flip gate for {a.release}\n")
    say("\n".join(report))
    outputs({"ready": "true" if ready else "false", "pending": " ".join(pending), "stuck": "true" if stuck else "false"})
    return 0


# --- flip -----------------------------------------------------------------------------------------------------------
def _top_spans(text):
    """{key: (start, end)} of each top-level value's text span in a JSON object's text, and the closing brace's index."""
    dec = json.JSONDecoder()
    ws = re.compile(r"[ \t\n\r]*")
    i = ws.match(text, 0).end()
    if text[i:i + 1] != "{":
        raise ValueError("not an object")
    i = ws.match(text, i + 1).end()
    spans = {}
    if text[i:i + 1] == "}":
        return spans, i
    while True:
        if text[i:i + 1] != '"':
            raise ValueError("a key expected")
        key, i = json.decoder.scanstring(text, i + 1)
        i = ws.match(text, i).end()
        if text[i:i + 1] != ":":
            raise ValueError("':' expected")
        i = ws.match(text, i + 1).end()
        _, end = dec.raw_decode(text, i)
        if key in spans:
            raise ValueError(f"key {key!r} twice")
        spans[key] = (i, end)
        i = ws.match(text, end).end()
        if text[i:i + 1] == ",":
            i = ws.match(text, i + 1).end()
            continue
        if text[i:i + 1] == "}":
            if text[i + 1:].strip():
                raise ValueError("text after the object")
            return spans, i
        raise ValueError("',' or '}' expected")


def flip_text(text, release):
    """cdn.json's text with its overture key naming release (minimal edit), or the same text when it does already."""
    if not valid(release):
        raise SystemExit(f"not a release id: {release!r}")
    before = json.loads(text)
    if not isinstance(before, dict):
        raise SystemExit("cdn.json: not an object")
    spans, close = _top_spans(text)
    on = before.get("overture")
    if on is not None:
        old = on.get("release") if isinstance(on, dict) else None
        if not valid(old):
            raise SystemExit(f"cdn.json's overture key is not {{\"release\": <release id>}}: {on!r}")
        if old == release:
            return text
        if order(old) > order(release):
            raise SystemExit(f"cdn.json names {old}, newer than {release}: never backwards")
        value = json.dumps(dict(on, release=release), separators=(", ", ": "))
        s, e = spans["overture"]
        new = text[:s] + value + text[e:]
    else:
        value = json.dumps({"release": release}, separators=(", ", ": "))
        head = text[:close].rstrip()
        sep = ", " if spans else ""
        new = head + f'{sep}"overture": ' + value + text[len(head):close] + text[close:]
    after = json.loads(new)
    want = dict(before, overture=dict(on or {}, release=release))
    if after != want:
        raise SystemExit("cdn.json: the edit changed more than the overture key")
    ns, _ = _top_spans(new)
    for k, (s, e) in spans.items():
        if k != "overture" and text[s:e] != new[ns[k][0]:ns[k][1]]:
            raise SystemExit(f"cdn.json: key {k!r} not byte-identical after the edit")
    if len(new.encode("utf-8")) > SWITCH_MAX:
        raise SystemExit(f"cdn.json: more than {SWITCH_MAX} bytes")
    return new


# --- bnwt / cip keys follow the overture key -------------------------------------------------------------------------
def _check_names(bnwt_names):
    try:
        names = json.loads(bnwt_names)
    except (TypeError, ValueError):
        raise SystemExit(f"bnwt names {bnwt_names!r}")
    if not (isinstance(names, dict) and set(names) == BNWT_ARCHIVES and all(
            isinstance(v, str) and re.fullmatch(r"[a-z]+\.c\d+", v) and v.startswith(k + ".c")
            for k, v in names.items())):
        raise SystemExit(f"bnwt names {bnwt_names!r} are not one name for each of {sorted(BNWT_ARCHIVES)}")
    return names


def probe_bnwt(release, bnwt_format, bnwt_names, cdn=CDN, fetch=get):
    """(published, why): every BNWT archive's index of release answers on the tile host."""
    if not (valid(release) and re.fullmatch(r"\d{1,3}", bnwt_format or "")):
        raise SystemExit("probe_bnwt: a bad release or format")
    for arch, name in sorted(_check_names(bnwt_names).items()):
        st, _ = fetch(f"{cdn}/bnwt{bnwt_format}/{release}/{name}.index.json", 1 << 20)
        if st != 200:
            return False, f"{name}.index.json of {release}: HTTP {st}"
    return True, f"every archive index of {release} answers"


def probe_cip(release, cip_format, cdn=CDN, fetch=get):
    """(published, why): latest.json of release names a build (16 hex) whose cells.index.json answers and is that build's."""
    if not (valid(release) and re.fullmatch(r"\d{1,3}", cip_format or "")):
        raise SystemExit("probe_cip: a bad release or format")
    base = f"{cdn}/cip{cip_format}/{release}"
    st, body = fetch(f"{base}/latest.json", 4096)
    if st != 200:
        return False, f"latest.json of {release}: HTTP {st}"
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return False, "latest.json: not JSON"
    build = doc.get("build") if isinstance(doc, dict) else None
    if not (isinstance(build, str) and BUILD_RE.fullmatch(build)):
        return False, "latest.json: no build id"
    st, body = fetch(f"{base}/{build}/cells.index.json", 256 << 20)
    if st != 200:
        return False, f"{build}/cells.index.json: HTTP {st}"
    try:
        idx = json.loads(body)
    except (TypeError, ValueError):
        return False, f"{build}/cells.index.json: not JSON"
    if not (isinstance(idx, dict) and idx.get("release") == release and idx.get("build") == build):
        return False, f"{build}/cells.index.json: another build"
    return True, f"build {build} of {release} has its index"


def lagging(text, release):
    """The SYNC_KEYS whose value exists, is enabled and names a release older than release (the ones to move)."""
    if not valid(release):
        raise SystemExit(f"not a release id: {release!r}")
    doc = json.loads(text)
    out = []
    for k in SYNC_KEYS:
        v = doc.get(k) if isinstance(doc, dict) else None
        if isinstance(v, dict) and v.get("enabled") is True and valid(v.get("release")) \
                and order(v["release"]) < order(release):
            out.append(k)
    return out


def sync_text(text, release, published):
    """(new text, [report lines]): each SYNC_KEYS key that lags and is in published (a set of keys whose build of
    release is on the tile host) set to release by a minimal edit; other keys byte-identical. A key that is absent or
    not enabled is never touched; one whose build is not out stays (inert, safe)."""
    if not valid(release):
        raise SystemExit(f"not a release id: {release!r}")
    before = json.loads(text)
    if not isinstance(before, dict):
        raise SystemExit("cdn.json: not an object")
    spans, _ = _top_spans(text)
    todo, report = [], []
    for k in SYNC_KEYS:
        v = before.get(k)
        if v is None:
            report.append(f"- {k}: no such key in cdn.json: left alone")
        elif not (isinstance(v, dict) and v.get("enabled") is True):
            report.append(f"- {k}: not enabled: left alone")
        elif not valid(v.get("release")):
            report.append(f"- {k}: no release id: left alone")
        elif v["release"] == release:
            report.append(f"- {k}: names {release} already")
        elif order(v["release"]) > order(release):
            report.append(f"- {k}: names {v['release']}, newer than {release}: left alone")
        elif k in published:
            todo.append(k)
            report.append(f"- {k}: {v['release']} -> {release}")
        else:
            report.append(f"- WAITING: {k}: the build of {release} is not published on the tile host; "
                          f"stays at {v['release']} (inert until then)")
    if not todo:
        return text, report
    new, want = text, dict(before)
    for k in sorted(todo, key=lambda x: -spans[x][0]):          # (last first: earlier offsets stay valid)
        s, e = spans[k]
        value = dict(before[k], release=release)
        new = new[:s] + json.dumps(value, separators=(", ", ": ")) + new[e:]
        want[k] = value
    if json.loads(new) != want:
        raise SystemExit("cdn.json: the edit changed more than the bnwt/cip keys")
    ns, _ = _top_spans(new)
    for k, (s, e) in spans.items():
        if k not in todo and text[s:e] != new[ns[k][0]:ns[k][1]]:
            raise SystemExit(f"cdn.json: key {k!r} not byte-identical after the edit")
    if len(new.encode("utf-8")) > SWITCH_MAX:
        raise SystemExit(f"cdn.json: more than {SWITCH_MAX} bytes")
    return new, report


def sync_keys(text, release, bnwt_format, bnwt_names, cip_format, cdn=CDN, fetch=get):
    """sync_text with the tile host asked (only for the keys that lag)."""
    lag = lagging(text, release)
    got, why = set(), {}
    if "bnwt" in lag:
        ok, why["bnwt"] = probe_bnwt(release, bnwt_format, bnwt_names, cdn, fetch)
        if ok:
            got.add("bnwt")
    if "cip" in lag:
        ok, why["cip"] = probe_cip(release, cip_format, cdn, fetch)
        if ok:
            got.add("cip")
    new, report = sync_text(text, release, got)
    report += [f"  - {k}: {w}" for k, w in sorted(why.items()) if k not in got]
    return new, report


def _write(path, new):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)


def _sync_args(a):
    have = [bool(a.bnwt_format), bool(a.bnwt_names), bool(a.cip_format)]
    if any(have) and not all(have):
        raise SystemExit("--bnwt-format, --bnwt-names and --cip-format go together")
    return all(have)


def cmd_flip_cdn(a):
    sync = _sync_args(a)
    with open(a.file, encoding="utf-8") as f:
        text = f.read(SWITCH_MAX + 1)
    if a.expect_served:         # (cdn.json changed since the gate was checked against the served release: not flipped)
        on = json.loads(text).get("overture")
        cur = on.get("release") if isinstance(on, dict) else None
        if on is not None and cur not in (a.expect_served, a.release):
            raise SystemExit(f"cdn.json names {cur!r} now, not the {a.expect_served} the gate was checked against")
    new = flip_text(text, a.release)
    if new == text:
        print(f"{a.file}: names {a.release} already")
    else:
        print(f"{a.file}: overture release {a.release}")
    if sync:
        new, report = sync_keys(new, a.release, a.bnwt_format, a.bnwt_names, a.cip_format)
        print(f"bnwt / cip keys for {a.release}:\n" + "\n".join(report))
    if new != text:
        _write(a.file, new)
    return 0


def cmd_sync_cdn(a):
    if not _sync_args(a):
        raise SystemExit("sync-cdn: --bnwt-format, --bnwt-names and --cip-format are required")
    with open(a.file, encoding="utf-8") as f:
        text = f.read(SWITCH_MAX + 1)
    rel = a.release
    if not rel:
        on = json.loads(text).get("overture")
        rel = on.get("release") if isinstance(on, dict) else None
        if not valid(rel):
            print(f"{a.file}: no overture release: nothing to follow")
            return 0
    new, report = sync_keys(text, rel, a.bnwt_format, a.bnwt_names, a.cip_format)
    print(f"bnwt / cip keys for {rel}:\n" + "\n".join(report))
    if new != text:
        _write(a.file, new)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("detect")
    p.add_argument("--cdn", required=True)
    p.add_argument("--app-pick", required=True)
    p.add_argument("--dev-pick", required=True)
    p.add_argument("--app-sha", required=True)
    p.add_argument("--dev-sha", required=True)
    p.add_argument("--candidate", default="")
    p.add_argument("--want", default="")
    p.add_argument("--floor-pick", default="", help="the latest published release's overturepick.py / overture.py")
    p = sub.add_parser("corpus")
    p.add_argument("--corpus", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--counts", required=True)
    p.add_argument("--old-release", default="")
    p.add_argument("--old-out", default="")
    p.add_argument("--old-log", default="")
    p.add_argument("--old-counts", default="")
    p.add_argument("--max-drop-pct", type=float, default=MAX_DROP_PCT)
    p.add_argument("--max-grow-pct", type=float, default=MAX_GROW_PCT)
    p.add_argument("--slack", type=int, default=SLACK)
    for name in ("check-stac", "check-places"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--release", required=True)
        p.add_argument("--like", default=None)
        if name == "check-stac":
            p.add_argument("--upstream", action="store_true", help="file counts against Overture's own STAC")
    p = sub.add_parser("stage-stac")
    p.add_argument("file")
    p.add_argument("--release", required=True)
    p.add_argument("--served", required=True)
    p.add_argument("--tools", required=True)
    p = sub.add_parser("candidate")
    for k in ("--release", "--served", "--app-tag", "--app-sha", "--run-url", "--out"):
        p.add_argument(k, required=True)
    p = sub.add_parser("gate")
    for k in ("--release", "--served", "--vkey", "--dec-format", "--bnwt-format", "--bnwt-names", "--schema-ok"):
        p.add_argument(k, required=True)
    p.add_argument("--published-at", default="")
    p.add_argument("--schemas", required=True, help="the app's SUPPORTED_SCHEMAS, comma-separated (detect's)")
    p.add_argument("--stuck-days", type=float, default=10.0)
    p.add_argument("--pages", default=PAGES)
    p.add_argument("--cdn", default=CDN)
    p = sub.add_parser("flip-cdn")
    p.add_argument("file")
    p.add_argument("--release", required=True)
    p.add_argument("--expect-served", default="", help="refuse when cdn.json names another release than this")
    p = sub.add_parser("sync-cdn")
    p.add_argument("file")
    p.add_argument("--release", default="", help="default: cdn.json's overture release")
    for pp in (p, sub.choices["flip-cdn"]):
        pp.add_argument("--bnwt-format", default="")
        pp.add_argument("--bnwt-names", default="")
        pp.add_argument("--cip-format", default="")
    a = ap.parse_args()
    return {"detect": cmd_detect, "corpus": cmd_corpus, "check-stac": cmd_check_stac, "check-places": cmd_check_places,
            "stage-stac": cmd_stage_stac, "candidate": cmd_candidate, "gate": cmd_gate,
            "flip-cdn": cmd_flip_cdn, "sync-cdn": cmd_sync_cdn}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
