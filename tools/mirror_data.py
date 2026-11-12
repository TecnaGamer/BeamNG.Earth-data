"""mirror_data.py SITE: the bulk files of the Pages site's datasets copied to the project's tile host (R2 bucket
bnge-tiles behind Cloudflare's cache, https://tiles.tgforever.net), and each dataset's manifest given a "data" URL
naming that copy. BeamNG.Earth's server (railtags.data_url / fetch_data) reads a dataset's files there when its
manifest names one, and Pages' copy when it doesn't or the tile host fails.

Why (2026-11-11): players read ~1.15 TB a month of these files from GitHub Pages, whose soft limit is 100 GB a month;
R2 has no egress fees and Cloudflare's cache answers most reads.

Layout: data/<dataset>/<key>/<the dataset's files, the same paths as on Pages>, e.g.
data/rail/2026_11_11T18_58_20Z-1a2b3c4d5e6f/12/34.json.gz; <key> is the manifest's "built" and a hash of the files, so
a key's files never change (cached for a year, "immutable") and a dataset that did not change (the static releases:
lod2, regional, nycmassing) is not uploaded again. data/<dataset>/<key>/manifest.json, uploaded last, marks a whole
copy. A dataset: a folder of SITE with a manifest.json (rail, lanes, signs, stations, fra, lod2, lod2-ch, regional,
nycmassing/v1 ...); osmtags (its own archive parts, uploaded by its job) is left alone.

Pages keeps every file as before (clients from before the "data" field read them there). Run in the data workflow's
site job, with the R2 token in the environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) and its endpoint in R2;
the token is never printed. Only standard-library Python and the aws CLI: no code from elsewhere.

    python tools/mirror_data.py site            (R2=https://<account>.r2.cloudflarestorage.com)
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BUCKET = "bnge-tiles"
PREFIX = "data"
PUBLIC = "https://tiles.tgforever.net"
CACHE = "public, max-age=31536000, immutable"


def datasets(site):
    """The dataset folders of site (relative paths with a manifest.json), osmtags' kind (an archive) left out."""
    out = []
    for d, subs, files in os.walk(site):
        subs.sort()
        if "manifest.json" not in files or os.path.samefile(d, site):
            continue
        try:
            with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or "archive" in m or "parts" in m:
            continue
        out.append(os.path.relpath(d, site).replace(os.sep, "/"))
    # (a dataset inside another's folder: its files are its own, left out of the outer one's)
    return out


def files_of(site, ds, all_ds):
    root = os.path.join(site, ds)
    inner = [o[len(ds) + 1:] for o in all_ds if o.startswith(ds + "/")]
    out = []
    for d, subs, files in os.walk(root):
        subs.sort()
        rel_d = os.path.relpath(d, root).replace(os.sep, "/")
        if rel_d != "." and any(rel_d == i or rel_d.startswith(i + "/") for i in inner):
            continue
        for f in sorted(files):
            rel = f if rel_d == "." else f"{rel_d}/{f}"
            if rel != "manifest.json":
                out.append(rel)
    return out, inner


def key_of(site, ds, files, built):
    h = hashlib.sha256()
    for rel in files:
        fh = hashlib.sha256()
        with open(os.path.join(site, ds, rel), "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                fh.update(b)
        h.update(rel.encode() + b"\0" + fh.digest())
    return f"{re.sub(r'[^0-9A-Za-z]+', '_', str(built))[:40]}-{h.hexdigest()[:12]}"


def aws(*args, check=True):
    r = subprocess.run(["aws", *args, "--endpoint-url", os.environ["R2"]], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"aws {args[0]} {args[1]}: exit {r.returncode}: {r.stderr.strip()[-500:]}")
    return r


def there(key):
    return aws("s3api", "head-object", "--bucket", BUCKET, "--key", key, check=False).returncode == 0


def check_public(url, size):
    """The copy answers on the public host (one of its files, its size)."""
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth-data"}),
                                        timeout=60) as r:
                if r.status == 200 and len(r.read()) == size:
                    return True
        except Exception as e:      # (a moment after the upload)
            print(f"  {url}: {type(e).__name__}: {e}", flush=True)
        time.sleep(5 * (i + 1))
    return False


def mirror(site, ds, all_ds):
    path = os.path.join(site, ds, "manifest.json")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    files, inner = files_of(site, ds, all_ds)
    if not files:
        print(f"{ds}: no files, left on Pages", flush=True)
        return None
    key = key_of(site, ds, files, m.get("built", "x"))
    prefix = f"{PREFIX}/{ds}/{key}"
    url = f"{PUBLIC}/{prefix}"
    size = sum(os.path.getsize(os.path.join(site, ds, rel)) for rel in files)
    t0 = time.time()
    if there(f"{prefix}/manifest.json"):
        print(f"{ds}: {prefix} already there ({len(files)} files, {size / 1e6:.1f} MB)", flush=True)
    else:
        excl = []
        for i in ["manifest.json"] + [f"{i}/*" for i in inner]:
            excl += ["--exclude", i]
        aws("s3", "cp", os.path.join(site, ds), f"s3://{BUCKET}/{prefix}", "--recursive", *excl, "--only-show-errors",
            "--content-type", "application/octet-stream", "--cache-control", CACHE)
        m2 = dict(m, data=url)
        tmp = path + ".r2"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m2, f, separators=(",", ":"))
        aws("s3", "cp", tmp, f"s3://{BUCKET}/{prefix}/manifest.json", "--only-show-errors",
            "--content-type", "application/json", "--cache-control", "public, max-age=3600")
        os.remove(tmp)
        print(f"{ds}: {len(files)} files, {size / 1e6:.1f} MB to {prefix}, {time.time() - t0:.0f} s", flush=True)
    probe = max(files, key=lambda rel: (rel.count("/"), rel))       # (a shard, not a top-level note)
    if not check_public(f"{url}/{probe}", os.path.getsize(os.path.join(site, ds, probe))):
        print(f"{ds}: {url}/{probe} does not answer: the manifest keeps Pages", flush=True)
        return None
    m["data"] = url
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(m, f, separators=(",", ":"))
    os.replace(path + ".part", path)
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    a = ap.parse_args()
    if not os.environ.get("R2"):
        raise SystemExit("R2 (the bucket's endpoint) not set")
    all_ds = datasets(a.site)
    print(f"datasets: {' '.join(all_ds)}", flush=True)
    failed = []
    for ds in all_ds:
        try:
            if mirror(a.site, ds, all_ds) is None:
                failed.append(ds)
        except SystemExit as e:         # (one dataset failing: the others still go, it stays on Pages)
            print(f"{ds}: {e}", flush=True)
            failed.append(ds)
    print(f"on the tile host: {len(all_ds) - len(failed)} of {len(all_ds)}" + (f"; Pages only: {' '.join(failed)}"
                                                                                 if failed else ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
