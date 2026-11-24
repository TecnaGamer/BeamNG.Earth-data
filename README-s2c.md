# tiles-s2c: BeamNG.Earth's own 2025 Sentinel-2 composite on the tile host

The project's cloud-free Sentinel-2 imagery of 2025 (the user's go, 2026-11-23: "Go with the 2025"), built from the
Copernicus Sentinel-2 L2A scenes of 2024-2025 in Element84 Earth Search's `sentinel-2-c1-l2a` collection (AWS Open
Data, anonymous range reads). Licence: the Copernicus Sentinel data terms (free, full and open; any use, including
commercial; redistribution and changes allowed) with the credit **"Contains modified Copernicus Sentinel data
2024-2025"**. It replaces the WorldCover 2021 packs (`s2wc/v1`, `tiles.yml`) where its packs exist; EOX's 2018+
mosaics (CC BY-NC-SA) are never used.

```
s2c/v1/{z}/{px}/{py}.bngp                    WebP packs (8 x 8 tiles), zooms 3-14 (14 within 50 degrees, 13 beyond)
s2c/v1/_build/s2c/v1/shards/{x}_{y}.json     a zoom-8 shard's record: its packs' sizes and SHA-256, the scenes
s2c/v1/_build/s2c/v1/coarse/{x}_{y}.json     a zoom-5 region's zooms 10..8
s2c/v1/_build/s2c/v1/world/0_0.json          zooms 7..3
s2c/v1/index.json                            written last, once everything above is there
```

Players read it only once their server knows the source "bngs2c" (BeamNG.Earth branch claude-s2c) and `cdn.json`
turns it on (by hand, after the index is published):

```json
{"imagery": {"bngs2c": {"enabled": true, "max_zoom": 14, "max_lat": {"14": 50}, "min_zoom": 3}, ...}}
```

(`min_zoom` 3 only once the world stage is published; without it, 8.)

## Files

| File | What it is |
| --- | --- |
| `tools/build_s2c_packs.py` | The builder (a pinned copy of BeamNG.Earth's `tools/build_s2c_packs.py`; `server/tilepack.py` here writes the same pack bytes). Runs only in jobs without secrets, and on home machines. |
| `tools/s2c_ci.py` | Standard library only: the plans, the artifact check, the bucket listing check, the index, the home tarball unpacker. The only program the jobs with the R2 secrets run (with aws-cli). |
| `tools/s2c_pins.json` | numpy and Pillow versions, the tone table's hash and the builder's `--selftest` SHA-256 (the WebP encoder must be the pinned one: other versions give other bytes). |
| `tools/s2c_shards.txt` | The planet: 19,174 zoom-8 shards (every zoom-8 tile meeting a 1-degree cell of ESA WorldCover's 2021 composite, the 2021 packs' coverage; `build_s2c_packs.py tools/s2c_shards.txt --list-planet` makes it again). |
| `.github/workflows/tiles_s2c.yml` | By hand. Inputs: `stage` (shards, coarse, world, index, ingest), `only`, `region`, `jobs`, `workers`, `release`, `dry_run`. |
| `.github/workflows/tiles_s2c-shards.yml` | One matrix entry: build (no secrets) then upload (secrets: checker + aws-cli). |

## What each job checks

- **build** (no secrets): pip installs the pinned numpy / Pillow, runs `--selftest` and refuses to build when its
  SHA-256 is not the pin, builds the entry's shards (none started after 4 h 50 min) and uploads the output as an
  artifact. Kind `ingest` instead downloads one `s2c-home-*.tar` from this repo's release and unpacks it with
  `s2c_ci.py untar` (regular files with pack or record names only; no links, absolute paths or `..`).
- **upload** (R2 secrets, only from main, not on `dry_run`): `s2c_ci.py check`: only packs and records in the artifact,
  each record of this checkout's builder (SHA-256 of `tools/build_s2c_packs.py`), the pinned Pillow and tone table;
  each pack its record's size and SHA-256, a BNGP pack of that shard with WebP tiles. Anything else refuses the whole
  artifact (a cut-off shard's packs without a record are only reported). Then the packs' columns are listed: a pack in
  the bucket with another size refuses the upload (a published pack is never overwritten); with its size it is not
  sent again. The packs go up (`public, max-age=31536000, immutable`), the listing is checked again, the records go up
  last (`no-cache`), and the artifact is deleted.
- **index**: the whole prefix listed; once every planet shard, every region and the world have records,
  `index.json` is written (counts and bytes per zoom) and uploaded last.

## Running it

1. Copy the files above into this repo, commit, push to main.
2. Trial (one shard, the Alps round the Stelvio pass, measured on the PC: see below):
   `gh workflow run tiles_s2c.yml -R TecnaGamer/BeamNG.Earth-data -f stage=shards -f only="135,90" -f jobs=1`
   (`-f dry_run=true` first to see the build alone). Then look at a pack:
   `curl -s https://tiles.tgforever.net/s2c/v1/14/1083/724.bngp -o p.bngp` (or through BeamNG.Earth with
   `BNGE_S2C=on`).
3. The planet: `-f stage=shards` (200 jobs a run, 20 at once; run it again until the plan says 0 left), then
   `-f stage=coarse`, `-f stage=world`, `-f stage=index`.
4. Turn it on in `cdn.json` (by hand, last).

## Home machines

Any machine can build shards for the bucket, with exactly the runners' bytes:

```sh
python3.12 -m venv venv && venv/bin/pip install numpy==2.5.5 pillow==13.0.0   # (tools/s2c_pins.json)
venv/bin/python tools/build_s2c_packs.py x --selftest      # must print the pins' "selftest" SHA-256
venv/bin/python tools/build_s2c_packs.py out --shard-file mine.txt --workers 3 --threads 24 --cache stac
cd out && tar cf ../s2c-home-pc-001.tar [0-9]* _build && cd ..                 # packs and records only
gh release upload s2c-home s2c-home-pc-001.tar -R TecnaGamer/BeamNG.Earth-data # (gh release create s2c-home once)
gh workflow run tiles_s2c.yml -R TecnaGamer/BeamNG.Earth-data -f stage=ingest -f release=s2c-home
```

Use this repo's copy of the builder (the checker requires its SHA-256). A shard list is lines of `x,y` from
`tools/s2c_shards.txt`; split the file between machines and runners (`-f only=...` or a home list) so no shard is
built twice (a shard built twice gives the same bytes and is skipped at upload, but costs the reads). Windows works
the same (Python 3.12, the same pins; `--selftest` must match).

## Measured (one shard on the PC, 2026-11-23)

Shard 135,90 (46.1-47.0 N, 9.8-11.25 E: the Alps round the Stelvio pass, 11,561 km² all land; 4 MGRS tiles, 70
scenes, three orbits over the western half), 3 pack processes at idle priority on a PC busy with other builds (load
~36 on 16 threads):

| | |
|---|---|
| wall | 808 s (13.5 min) |
| read from AWS us-west-2 | 22.75 GB (355 MB a zoom-14 pack, 1.97 MB/km²), 13,876 range requests, 28 MB/s on average |
| CPU | ~3,550 s (55 s a zoom-14 pack on a contended core; 15-21 s wall for one pack alone) |
| memory | <= 2.5 GB per pack process (4 per runner fit its 16 GB) |
| output | 56.7 MB: zoom 14 37.0 MB (64 packs, 0.58 MB each), 13 14.4 MB (16, 0.90 MB; the 2021 packs: 0.85 MB), 12 4.3 MB, 11 1.0 MB |

The PC's link to us-west-2 (16-48 parallel range reads of whole COG blocks): 55-75 MB/s (450-600 Mbit/s), flat from 8
threads up. Determinism: three packs rebuilt alone (1 process, 8 threads, after refactors of the pipeline) gave the
shard's bytes exactly; `--selftest` the same on two numpy versions.

## Planet estimate

From the shard, scaled by area (the reads and CPU go with the ground the scenes cover; the shard is denser than most:
three orbits overlap there, deserts need ~7 scenes a tile, the tropics up to 10 an orbit), land without Antarctica
135 M km² to the WorldCover cells' 168 M km² (their coastal sea included), a density factor of 0.7-1.0:

| | estimate |
|---|---|
| read from AWS (free: open data) | ~190-330 TB |
| CPU | ~6,000-14,000 CPU-hours |
| GitHub runner-hours (4 vCPU, CPU-bound: ~17 min a shard-equivalent) | ~2,000-3,500, i.e. 4-7 days at 20 jobs at once (25-40 runs of 5 h) |
| R2 | ~220 GB (zoom 14 ~100, 13 ~88, 12 ~26, 11 ~6, 10..3 ~2), ~1.5 M uploads |

Home machines: they share one internet link (the PC alone measured 55-75 MB/s), so all of them together build at
most ~10-12 shard-equivalents an hour (22.75 GB each), and need ~12 cores for it (3,550 CPU-s a shard): the PC's 3
pack processes plus ~8 on the mini PC saturate the link; the Windows PC and the Deck add nothing while the link is
full. That is about 3 runners' worth: give home ~15 % of the shards (one contiguous list, e.g. a few zoom-5 regions)
and the runners the rest.
