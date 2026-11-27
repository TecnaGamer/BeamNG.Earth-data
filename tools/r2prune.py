#!/usr/bin/env python3
"""tools/r2prune.py: the weekly pruning of the tile host's bucket (R2 bnge-tiles behind tiles.tgforever.net). The user's
go, 2026-11-27. Standard-library Python plus boto3 (when installed) or the aws CLI; no code from elsewhere.

What it may delete (and nothing else, never a whole top-level prefix, never a key outside these patterns):

  A. Timestamped snapshots, one folder per build, never overwritten by their pipelines:
       osmtags/<ts>/  osmyears/<ts>-<hash>/  osmareas/  osmcolours/  tallbld/   (parts NAME-NNN.pmtiles)
       data/<dataset>/<ts>-<hash>/                                             (manifest.json uploaded last)
     Kept: the newest 3 COMPLETE ones; everything the live manifest on Pages points to; everything newer than the
     newest complete one (it may still be uploading). A folder is complete when its parts run NAME-000.. without a gap,
     weigh at least 90 % of the live one (osmtags: parts uploaded in order, no end marker on R2), and nothing in it was
     written in the last 6 hours; a data folder when its manifest.json exists. Incomplete folders older than the
     oldest kept complete one go only after 7 days.
  B. Versioned releases: bnwt<N>/<release>, cip<N>/<release> (marker: an *.index.json / latest.json), cliff/4k/vN,
     watershare/vN, leaftype/vN, nbiskew/vN, dem/mapterhorn/X.Y.Z (marker: manifest.json / release.json).
     Kept: what cdn.json references, every version at or above the latest released app's Overture RELEASE
     (--release-floor), the newest two complete versions. A version older than those goes only when a newer complete
     version's marker is at least 14 days old.
  Everything else (s2wc, s2c, aerial, dec, unmarked or oddly named folders, _build scratch) is kept and reported.

Dry run is the default; a real run deletes at most --max-delete-gb (500) and refuses otherwise.
    python3 tools/r2prune.py [--delete] [--cdn cdn.json] [--summary FILE] [--max-delete-gb N] [--release-floor ID]
The bucket's endpoint: R2 (https://<account>.r2.cloudflarestorage.com) or R2_ACCOUNT_ID; the keys: AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY (or R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY). Never printed.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

BUCKET = "bnge-tiles"
PUBLIC = "https://tiles.tgforever.net/"
PAGES = "https://tecnagamer.github.io/BeamNG.Earth-data/"
KEEP_COMPLETE = 3
QUIET_HOURS = 6
INCOMPLETE_DAYS = 7
MIN_AGE_DAYS = 14
GB = 1e9

PACKS = ("osmtags", "osmyears", "osmareas", "osmcolours", "tallbld")
# timestamped snapshot folder names: 2026_11_24T19_54_34Z, 20261124T195124Z-f643054a, 2026_11_23-3a413272bdb5
TS_RE = re.compile(r"^(\d{4})_?(\d\d)_?(\d\d)(?:T(\d\d)_?(\d\d)_?(\d\d)Z)?(?:-[0-9a-f]{6,40})?$")
RELEASE_RE = re.compile(r"^\d{4}-\d\d-\d\d\.\d+$")
VN_RE = re.compile(r"^v\d+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# (root regexp for the top-level/second-level prefix, version regexp, marker file regexp, cdn.json key names)
VERSIONED = [
    (r"bnwt\d+/", RELEASE_RE, re.compile(r"^[^/]+\.index\.json$"), {"overture"}),
    (r"cip\d+/", RELEASE_RE, re.compile(r"^latest\.json$"), {"overture"}),
    (r"cliff/4k/", VN_RE, re.compile(r"^manifest\.json$"), {"cliffs"}),
    (r"watershare/", VN_RE, re.compile(r"^manifest\.json$"), {"watershare"}),
    (r"leaftype/", VN_RE, re.compile(r"^manifest\.json$"), {"leaftype"}),
    (r"nbiskew/", VN_RE, re.compile(r"^manifest\.json$"), {"nbiskew"}),
    (r"dem/mapterhorn/", SEMVER_RE, re.compile(r"^release\.json$"), {"dem"}),
]
# kept without a look (no completeness marker the pipelines write last, or a different kind of key)
KEPT_ROOTS = ("s2wc/", "s2c/", "aerial/", "dec/")


class Obj:
    def __init__(self, key, size, mtime):
        self.key, self.size, self.mtime = key, size, mtime


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


# ---- the bucket ----------------------------------------------------------------------------------------------------

class S3:
    """list(prefix, delimiter) -> (objects, common prefixes); delete(keys)."""

    def __init__(self):
        ep = os.environ.get("R2") or ("https://%s.r2.cloudflarestorage.com" % os.environ["R2_ACCOUNT_ID"])
        self.ep = ep
        self.key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY_ID")
        self.secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_ACCESS_KEY")
        try:
            import boto3
            self.c = boto3.client("s3", endpoint_url=ep, aws_access_key_id=self.key,
                                  aws_secret_access_key=self.secret, region_name="auto")
        except ImportError:
            self.c = None

    def _aws(self, *args):
        env = dict(os.environ, AWS_ACCESS_KEY_ID=self.key, AWS_SECRET_ACCESS_KEY=self.secret, AWS_DEFAULT_REGION="auto")
        r = subprocess.run(["aws", "s3api", *args, "--endpoint-url", self.ep, "--output", "json"], env=env,
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def list(self, prefix, delimiter=None):
        objs, prefs = [], []
        if self.c:
            kw = {"Bucket": BUCKET, "Prefix": prefix}
            if delimiter:
                kw["Delimiter"] = delimiter
            while True:
                r = self.c.list_objects_v2(**kw)
                objs += [Obj(x["Key"], x["Size"], x["LastModified"]) for x in r.get("Contents", [])]
                prefs += [x["Prefix"] for x in r.get("CommonPrefixes", [])]
                if not r.get("IsTruncated"):
                    break
                kw["ContinuationToken"] = r["NextContinuationToken"]
        else:
            a = ["list-objects-v2", "--bucket", BUCKET, "--prefix", prefix]
            if delimiter:
                a += ["--delimiter", delimiter]
            r = self._aws(*a)
            for x in r.get("Contents") or []:
                objs.append(Obj(x["Key"], x["Size"], datetime.datetime.fromisoformat(x["LastModified"].replace("Z", "+00:00"))))
            prefs = [x["Prefix"] for x in r.get("CommonPrefixes") or []]
        return objs, prefs

    def delete(self, keys):
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            if self.c:
                r = self.c.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
                if r.get("Errors"):
                    raise RuntimeError("delete errors: %r" % r["Errors"][:3])
            else:
                with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                    json.dump({"Objects": [{"Key": k} for k in chunk], "Quiet": True}, f)
                    f.flush()
                    r = self._aws("delete-objects", "--bucket", BUCKET, "--delete", "file://" + f.name)
                if r.get("Errors"):
                    raise RuntimeError("delete errors: %r" % r["Errors"][:3])


# ---- pointers ------------------------------------------------------------------------------------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bnge-r2prune"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def walk(o, path=()):
    """Every (key path, scalar) of a JSON value."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, path + (str(k),))
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, path)
    else:
        yield path, o


def public_paths(manifest):
    """The bucket paths of every tile-host URL a Pages manifest names."""
    out = set()
    for _, v in walk(manifest):
        if isinstance(v, str) and v.startswith(PUBLIC):
            out.add(urllib.parse.unquote(v[len(PUBLIC):]).split("?")[0])
    return out


def ts_key(name):
    """Sort key of a snapshot folder name (no slash), or None."""
    m = TS_RE.match(name)
    if not m:
        return None
    g = [int(x) if x else 0 for x in m.groups()[:6]]
    return (*g, name)


def ver_key(name):
    return tuple(int(x) for x in re.findall(r"\d+", name))


# ---- the plan ------------------------------------------------------------------------------------------------------

class Plan:
    def __init__(self):
        self.rows = []      # (family, folder, action, reason, bytes) action: keep / delete / report
        self.delete_keys = []

    def add(self, family, folder, action, reason, size=0, keys=()):
        self.rows.append((family, folder, action, reason, size))
        if action == "delete":
            self.delete_keys += list(keys)


def pointed(folder, ptr_paths):
    return any((p.rstrip("/") + "/").startswith(folder) for p in ptr_paths)


def full_list(s3, folder):
    return s3.list(folder)[0]


def pack_complete(name, objs, ref_size, now):
    """osmtags-000.pmtiles .. contiguous, non-empty, at least 90 % of the live one, quiet for QUIET_HOURS."""
    if not objs:
        return False, "empty"
    idx = []
    for o in objs:
        base = o.key.rsplit("/", 1)[-1]
        m = re.fullmatch(re.escape(name) + r"-(\d{3})\.pmtiles", base)
        if m:
            idx.append(int(m.group(1)))
        elif base == name + ".pmtiles":
            idx.append(0)
        else:
            return False, "stray file " + base
    if sorted(idx) != list(range(len(idx))):
        return False, "parts have a gap"
    if any(o.size <= 0 for o in objs):
        return False, "an empty part"
    total = sum(o.size for o in objs)
    if ref_size and total < ref_size * 0.9:
        return False, "only %.1f GB of %.1f GB" % (total / GB, ref_size / GB)
    if now - max(o.mtime for o in objs) < datetime.timedelta(hours=QUIET_HOURS):
        return False, "written in the last %d h" % QUIET_HOURS
    return True, "complete"


def plan_snapshots(s3, plan, root, ptr_paths, ptr_known, kind, now):
    """root: 'osmtags/' or 'data/rail/'; kind: 'pack' (parts) or 'data' (manifest.json last)."""
    _, children = s3.list(root, "/")
    fam = root.rstrip("/")
    if not children:
        return
    name = root.strip("/").split("/")[-1]
    snaps, odd = [], []
    for c in children:
        k = ts_key(c[len(root):].rstrip("/"))
        (snaps if k else odd).append((k, c) if k else c)
    for c in odd:
        plan.add(fam, c, "report", "name is not a timestamped snapshot: kept")
    snaps.sort()
    if not ptr_known:
        for _, c in snaps:
            plan.add(fam, c, "report", "live pointer unknown (Pages manifest unreadable or names no tile-host copy): kept")
        return
    info = {}
    live_size = 0
    if kind == "pack":
        for _, c in snaps:
            info[c] = full_list(s3, c)
            if pointed(c, ptr_paths):
                live_size = max(live_size, sum(o.size for o in info[c]))
    for _, c in snaps:
        if kind == "pack":
            ok, why = pack_complete(name, info[c], live_size, now)
        else:
            mk = [o for o in s3.list(c + "manifest.json")[0] if o.key == c + "manifest.json"]
            ok, why = (True, "complete") if mk else (False, "no manifest.json yet")
        info[c] = (info[c] if kind == "pack" else None, ok, why)
    complete = [c for _, c in snaps if info[c][1]]
    if not complete:
        for _, c in snaps:
            plan.add(fam, c, "keep", "no complete snapshot yet (%s)" % info[c][2])
        return
    keep_complete = set(complete[-KEEP_COMPLETE:])
    newest_c = complete[-1]
    oldest_kept = complete[-KEEP_COMPLETE:][0]
    names = [c for _, c in snaps]
    for c in names:
        objs = info[c][0] if kind == "pack" else None
        ok = info[c][1]

        def sized():
            return objs if objs is not None else full_list(s3, c)

        if pointed(c, ptr_paths):
            plan.add(fam, c, "keep", "live: the Pages manifest points to it")
        elif c in keep_complete:
            plan.add(fam, c, "keep", "one of the newest %d complete" % KEEP_COMPLETE)
        elif names.index(c) > names.index(newest_c):
            plan.add(fam, c, "keep", "newer than the newest complete one (%s)" % info[c][2])
        elif ok:
            o = sized()
            plan.add(fam, c, "delete", "complete, older than the newest %d" % KEEP_COMPLETE, sum(x.size for x in o),
                     [x.key for x in o])
        else:
            o = sized()
            age = now - max((x.mtime for x in o), default=now)
            if age >= datetime.timedelta(days=INCOMPLETE_DAYS) and names.index(c) < names.index(oldest_kept):
                plan.add(fam, c, "delete", "incomplete (%s), %d days old, older than the kept ones" % (info[c][2], age.days),
                         sum(x.size for x in o), [x.key for x in o])
            else:
                plan.add(fam, c, "keep", "incomplete (%s), not yet 7 days old: kept" % info[c][2])


def cdn_protects(cdn, family_keys, folder, ver):
    """cdn.json names this version: a URL under the folder, or the version as a value under one of the family's keys."""
    f = folder.rstrip("/")
    for path, v in walk(cdn):
        if isinstance(v, str):
            if ("/" + f + "/") in v or v.rstrip("/").endswith("/" + f) or ("/" + f + "?") in v:
                return True
            if v == ver and family_keys & set(path):
                return True
    return False


def plan_versions(s3, plan, root, ver_re, marker_re, family_keys, cdn, floor, now):
    _, children = s3.list(root, "/")
    fam = root.rstrip("/")
    vers, odd = [], []
    for c in children:
        v = c[len(root):].rstrip("/")
        (vers if ver_re.match(v) else odd).append(c)
    for c in odd:
        plan.add(fam, c, "report", "not a version folder (scratch or unknown): kept")
    vers.sort(key=lambda c: ver_key(c[len(root):]))
    marks = {}
    for c in vers:
        m = [o for o in s3.list(c, "/")[0] if marker_re.match(o.key[len(c):])]
        marks[c] = max((o.mtime for o in m), default=None)
    complete = [c for c in vers if marks[c]]
    keep2 = set(complete[-2:])
    for c in vers:
        v = c[len(root):].rstrip("/")
        if cdn_protects(cdn, family_keys, c, v):
            plan.add(fam, c, "keep", "cdn.json references it")
        elif floor and RELEASE_RE.match(v) and ver_key(v) >= ver_key(floor):
            plan.add(fam, c, "keep", "at or above the latest app's Overture RELEASE %s" % floor)
        elif c in keep2:
            plan.add(fam, c, "keep", "one of the newest 2 complete versions")
        elif not marks[c]:
            plan.add(fam, c, "report", "no completion marker: kept")
        else:
            newer = [d for d in complete if ver_key(d[len(root):]) > ver_key(v) and now - marks[d] >= datetime.timedelta(days=MIN_AGE_DAYS)]
            if not newer:
                plan.add(fam, c, "keep", "no newer complete version is 14 days old yet")
            else:
                o = full_list(s3, c)
                plan.add(fam, c, "delete", "complete; %s complete for 14+ days" % newer[-1][len(root):].rstrip("/"),
                         sum(x.size for x in o), [x.key for x in o])


def pages_pointers(ds, fetch):
    """(set of bucket paths, known?) for a dataset's manifest on Pages."""
    try:
        m = fetch(PAGES + ds + "/manifest.json")
    except Exception as e:  # noqa: BLE001
        print("pointer %s: %s" % (ds, e), file=sys.stderr)
        return set(), False
    p = public_paths(m)
    return p, bool(p)


def build_plan(s3, cdn, floor=None, fetch=fetch_json, now=None):
    now = now or now_utc()
    plan = Plan()
    _, tops = s3.list("", "/")
    roots = set(tops)
    for t in tops:
        if t in ("cliff/", "dem/"):             # (one level down: cliff/4k/vN, dem/mapterhorn/X.Y.Z)
            roots |= set(s3.list(t, "/")[1])
    versioned = set()
    for t in sorted(roots):
        top = t.rstrip("/")
        if top in PACKS:
            p, known = pages_pointers(top, fetch)
            plan_snapshots(s3, plan, t, p, known, "pack", now)
        elif top == "data":
            for d in s3.list("data/", "/")[1]:
                p, known = pages_pointers(d[len("data/"):].rstrip("/"), fetch)
                plan_snapshots(s3, plan, d, p, known, "data", now)
        for root_re, ver_re, marker_re, keys in VERSIONED:
            if re.fullmatch(root_re, t):
                versioned.add(t.split("/")[0])
                plan_versions(s3, plan, t, ver_re, marker_re, keys, cdn, floor, now)
        if t in KEPT_ROOTS:
            plan.add(top, t, "report", "no completeness marker the pipeline writes last: kept")
    known_tops = set(PACKS) | {"data"} | versioned | {r.rstrip("/") for r in KEPT_ROOTS} | {"cliff", "dem"}
    for t in tops:
        if t.rstrip("/") not in known_tops:
            plan.add(t.rstrip("/"), t, "report", "unknown top-level prefix: untouched")
    return plan


def safe_key(k):
    """Only keys of the patterns above may ever be deleted, and never a top-level prefix itself."""
    parts = k.split("/")
    if len(parts) < 3 or not all(parts):
        return False
    top = parts[0]
    if top in PACKS:
        return ts_key(parts[1]) is not None
    if top == "data":
        return len(parts) >= 4 and ts_key(parts[2]) is not None
    for root_re, ver_re, _, _ in VERSIONED:
        n = root_re.count("/")
        if re.fullmatch(root_re, "/".join(parts[:n]) + "/") and len(parts) > n + 1 and ver_re.match(parts[n]):
            return True
    return False


# ---- report --------------------------------------------------------------------------------------------------------

def render(plan, dry, cap_gb, note=""):
    out = ["## R2 prune: %s" % ("DRY RUN, nothing deleted" if dry else "real run"), ""]
    if note:
        out += [note, ""]
    dele = [r for r in plan.rows if r[2] == "delete"]
    total = sum(r[4] for r in dele)
    out.append("%s %d folders, %.1f GB (cap %.0f GB)" % ("Would delete" if dry else "Delete", len(dele), total / GB, cap_gb))
    out.append("")
    out += ["| family | folder | action | reason | GB |", "|---|---|---|---|---|"]
    for fam, folder, act, why, size in sorted(plan.rows, key=lambda r: (r[0], r[1])):
        out.append("| %s | %s | %s | %s | %s |" % (fam, folder, act, why, "%.2f" % (size / GB) if size else ""))
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--delete", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="(the default)")
    ap.add_argument("--cdn", default="cdn.json")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    ap.add_argument("--max-delete-gb", type=float, default=500)
    ap.add_argument("--release-floor", default="", help="the latest released app's Overture RELEASE (e.g. 2026-10-21.0)")
    a = ap.parse_args(argv)
    dry = not a.delete or a.dry_run
    if a.release_floor and not RELEASE_RE.match(a.release_floor):
        sys.exit("bad --release-floor")
    with open(a.cdn, encoding="utf-8") as f:
        cdn = json.load(f)
    s3 = S3()
    plan = build_plan(s3, cdn, a.release_floor or None)
    keys = plan.delete_keys
    bad = [k for k in keys if not safe_key(k)]
    if bad:
        sys.exit("refusing: keys outside the prune patterns: %r" % bad[:3])
    total = sum(r[4] for r in plan.rows if r[2] == "delete")
    note = ""
    if not dry and total > a.max_delete_gb * GB:
        note = "REFUSED: %.1f GB to delete is over the cap of %.0f GB; nothing deleted (raise max_delete_gb by hand)." % (total / GB, a.max_delete_gb)
    text = render(plan, dry or bool(note), a.max_delete_gb, note)
    print(text)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(text)
    if note:
        return 1
    if not dry:
        # (the markers first: a half-deleted folder never looks complete)
        keys.sort(key=lambda k: (not k.endswith(("manifest.json", "latest.json", ".index.json", "release.json")), k))
        s3.delete(keys)
        print("deleted %d objects, %.1f GB" % (len(keys), total / GB))
    return 0


if __name__ == "__main__":
    sys.exit(main())
