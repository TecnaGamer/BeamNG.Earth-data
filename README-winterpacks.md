# winterpacks: the leaf-type and water-share packs on the tile host

Two static raster packs for BeamNG.Earth's winter looks (the white ground, W2, and bare winter trees, W4), built by
`.github/workflows/winterpacks.yml` (manual only) and served from the project's tile host (Cloudflare R2 bnge-tiles
behind `https://tiles.tgforever.net`). Players' servers read them (BeamNG.Earth `server/winterpacks.py`) only once
`cdn.json` names a version:

```
cdn.json: {"leaftype": {"enabled": true, "version": "v1"}, "watershare": {"enabled": true, "version": "v1"}}
```

```
leaftype/<version>/leaftype.png        7200 x 3600 PNG, two grey bands (mode LA), from 180 W, 90 N, 0.05 degrees
leaftype/<version>/ATTRIBUTION.md
leaftype/<version>/manifest.json       {"format": 1, "name", "version", "built", "build", "grid", "files":
                                        [{"path", "size", "sha256"}], "source", "attribution"}   (uploaded last)
watershare/<version>/<x>/<y>.png       500 x 500 grey PNG per 2 x 2 degrees: x = floor((lon + 180) / 2),
                                        y = floor((90 - lat) / 2); row 0 the north edge
watershare/<version>/ATTRIBUTION.md
watershare/<version>/manifest.json     {"format": 1, ..., "shards": {"x/y": [size, sha256]}, "uniform": {"x/y": value}}
                                        (uploaded last; a shard in neither is all no data)
```

Values are 0..250 (share = value / 250); 255 in the water pack is "no data" (in GSW the open sea past the mapped
coast). A published version is immutable: nothing is overwritten or deleted, a change is a new version.

## leaftype: evergreen share and tree cover (ESA CCI Land Cover)

- Source: ESA Land Cover CCI v2.0.7, the 2015 map (300 m, 1/360 degree, LCCS classes), from CEDA's open archive
  (`https://dap.ceda.ac.uk/neodc/esacci/land_cover/data/land_cover_maps/v2.0.7/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2015-v2.0.7.tif`,
  312 MB; "Public data: access to these data is available to both registered and non-registered users"). The newer
  2016-2022 maps (v2.1.1) are on the Copernicus Climate Data Store, which needs an account: not used.
- Band 0, evergreen: (broadleaved evergreen + needleleaved evergreen tree cover) / all tree cover in the cell, by the
  classes' plant functional types in the CCI-LC Product User Guide v2.0, Table 7-2 (e.g. class 70 "needleleaved
  evergreen, closed to open": 70 % evergreen trees; 60 "broadleaved deciduous": 70 % deciduous trees; 90 "mixed": 30 %
  broadleaved deciduous, 20 % needleleaved evergreen, 10 % needleleaved deciduous). A cell with few trees leans to
  its 0.25, 1 and 5 degree blocks' mix (share = (E + K parent) / (T + K), K = 4 closed-forest pixels), so a field or a
  town carries its region's mix for OSM's small woods. 0 on cells without land.
- Band 1, trees: the cell's tree cover (share of its area) by the same table.
- Licence: the Land Cover CCI terms ("may be used by any user for any purpose"; acknowledge the ESA Climate Change
  Initiative and the Land Cover CCI project, cite the dataset): see `winterpacks/leaftype-ATTRIBUTION.md`.
- Build: one job, ~2 min of compute after the download (CEDA serves at ~0.3 MB/s: ~20 min).

## watershare: share of surface water (JRC Global Surface Water)

- Source: JRC Global Surface Water v1.5 (1984-2024), occurrence, 504 tiles of 10 x 10 degrees (30 m, 40000 px a side,
  16.5 GB), from the provider's open listing
  `https://s3.waw4-1.cloudferro.com/swift/v1/global-surface-water/download2024/Aggregated/VER1-5/occurrence/`
  (linked from `https://global-surface-water.appspot.com/download`; no account).
- Each 0.004 degree cell (16 x 16 pixels, ~445 m): the mean occurrence / 100, the expected share of water on a day of
  1984-2024 (a lake ~0.96-1, a seasonal marsh its wet share); a pixel with no data is counted as water (GSW's open sea
  beyond the coastal band); a cell whose pixels all have none is 255. Integer arithmetic. Rivers and lakes are not told
  apart: near the player OSM / Overture say which is which; the pack is for the far views.
- Licence: produced under the Copernicus Programme, "provided free of charge, without restriction of use"; credit
  "Source: EC JRC/Google" and Pekel et al. (2016), Nature 540, 418-422: see `winterpacks/watershare-ATTRIBUTION.md`.
- Build: 12 jobs of ~42 source tiles each (a tile: ~10 s of compute, its download), then the gather job (the manifest,
  every source tile done, the check). ~6,000 shards, ~150-200 MB.

## Files

| File | What it is |
| --- | --- |
| `tools/build_leaftype_pack.py` | The leaf-type builder (numpy, Pillow, rasterio). |
| `tools/build_water_pack.py` | The water-share builder (numpy, Pillow, rasterio; `tiles`, `manifest`, `stage`: standard library). |
| `tools/winterpack_ci.py` | Standard library only: the publish job's check (PNG chunks, sizes, SHA-256s, exact file set), the bucket listing check, the live check, the cdn.json edit. |
| `winterpacks/*-ATTRIBUTION.md` | The packs' notices, published beside them. |
| `.github/workflows/winterpacks.yml` | Runs by hand only. Inputs: `pack`, `version`, `tiles` (a watershare trial: never published), `dry_run` (default true). |
| `tests/test_winterpack_ci.py` | The checker's and the builders' cell arithmetic tests. |

## Steps

1. Dry run (build and check only; read the job summary):

   ```sh
   gh workflow run winterpacks.yml -R TecnaGamer/BeamNG.Earth-data --ref main -f pack=leaftype -f version=v1
   gh workflow run winterpacks.yml -R TecnaGamer/BeamNG.Earth-data --ref main -f pack=watershare -f version=v1
   ```

2. Publish (from main, `dry_run=false`; the live job byte-checks the CDN afterwards):

   ```sh
   gh workflow run winterpacks.yml -R TecnaGamer/BeamNG.Earth-data --ref main -f pack=leaftype -f version=v1 -f dry_run=false
   gh workflow run winterpacks.yml -R TecnaGamer/BeamNG.Earth-data --ref main -f pack=watershare -f version=v1 -f dry_run=false
   ```

3. Last, turn them on for players (a commit to main):

   ```sh
   python3 -I tools/winterpack_ci.py cdn-key cdn.json leaftype v1
   python3 -I tools/winterpack_ci.py cdn-key cdn.json watershare v1
   git commit -m "cdn.json: leaftype v1, watershare v1" cdn.json && git push
   ```

Rollback: remove the key from `cdn.json` (players' servers then read no pack at their next start; a run keeps the
version it decided).
