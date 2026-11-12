# BeamNG.Earth data

Data files that [BeamNG.Earth](https://github.com/TecnaGamer/BeamNG.Earth) reads at runtime, rebuilt automatically
by this repository's GitHub Actions and served by its GitHub Pages
(`https://tecnagamer.github.io/BeamNG.Earth-data/`).

Each dataset's files are also copied to the project's tile host (Cloudflare R2 behind
`https://tiles.tgforever.net`, `data/<dataset>/<build key>/`, the same paths; `tools/mirror_data.py`), and the
dataset's `manifest.json` on Pages names that copy in its `"data"` field: BeamNG.Earth reads the files there and
falls back to Pages. The OSM road tag archive (`osmtags/`) is on the tile host only, in parts of at most 400 MB that
the manifest's `"parts"` list.

## rail/: OpenStreetMap's railways (weekly)

`rail/<x>/<y>.json.gz` per zoom-7 tile with railways: OpenStreetMap's railway tags by way id (railway, usage,
service, gauge, tracks, electrified, voltage, frequency, maxspeed, bridge, tunnel, ...) and its level crossing and
catenary mast nodes; `rail/manifest.json` says when it was built. BeamNG.Earth joins them to Overture Maps' rail
segments (`server/railtags.py`) to leave out lines not built yet, show disused and abandoned lines worn, use the real
gauge, and give level crossings their gates and lights.

Built every week from the OpenStreetMap planet by `.github/workflows/rail.yml`, with
`tools/build_rail_tags.py` from the BeamNG.Earth repository.

## lanes/: OpenStreetMap's lane-level road tags (weekly)

`lanes/<x>/<y>.json.gz` per zoom-7 tile: the lane tags of every OpenStreetMap highway that has any (turn:lanes,
change:lanes, placement, lanes:forward / backward / both_ways, overtaking, divider, lane_markings, width:lanes, bus /
HOV lanes, ...) by way id. BeamNG.Earth joins them to Overture Maps' road segments (`server/railtags.py` LaneTags) to
paint the road markings lane by lane and give the traffic the same lanes. Built by the same workflow with
`tools/build_lane_tags.py`.

**License:** the data is © OpenStreetMap contributors, available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). See
https://www.openstreetmap.org/copyright.

## stations/: OpenStreetMap's fuel and charging stations (weekly)

`stations/<x>/<y>.json.gz` per zoom-7 tile: every `amenity=fuel` and `amenity=charging_station` (a node, or a way's
centre) with its name, brand (or operator / network) and, for chargers, `capacity`, and every `man_made=charge_point`
node. BeamNG.Earth's station finder lists them on its world map where no map data is downloaded, and its EV charger
sites take their real charger count and spots from them (`server/railtags.py` StationTags). Built by the same workflow
with `tools/build_station_tags.py`.

**License:** the data is © OpenStreetMap contributors, available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). See
https://www.openstreetmap.org/copyright.

## fra/: US level crossings (weekly)

`fra/<x>/<y>.json.gz` per zoom-7 tile: the US Federal Railroad Administration's Highway-Rail Crossing Inventory, its
open at-grade crossings as `[lat, lon, kit]` (kit: `gate`, `flash` or `cross`), built by `tools/build_fra_shards.py`.
Public domain (a work of the US government).

## lod2/: Germany's measured roofs (static, release "lod2-data")

`lod2/<x>/<y>.npz` per zoom-10 tile: one record per building part from the German states' open LoD2 building models
(`lat`, `lon` int32 in 1e-6 degrees, `code` int16 AdV roof type, `height` and `roof` uint16 decimetres), built by
BeamNG.Earth's `tools/build_lod2_roofs.py` (PR #872, Henner4746). 13 states, 65.3 million records; changed data under
dl-de/by-2-0, dl-de/zero-2.0 and CC BY 4.0, with each state's attribution in `lod2/ATTRIBUTION.md`. The data
workflow unpacks the release's `lod2.tar` into the site.

## deroads/: Germany's surveyed roads (static, release "deroads-data")

`deroads/<x>/<y>.npz` per zoom-10 tile: the German states' official road axes (ATKIS Basis-DLM, from basemap.de's
GeoPackages) with their surveyed width, lane count and surface, built by BeamNG.Earth's `tools/build_de_roads.py`
(PR #1036, Henner4746). All 16 states, 684 shards, 216 MB; under dl-de/by-2-0, dl-de/zero-2.0 and CC BY 4.0, with
each state's attribution in `deroads/ATTRIBUTION.md`. The data workflow unpacks this repo's release `deroads.tar`, or,
until there is one, the release on Henner4746's fork whose SHA-256 the workflow pins.

## dem/mapterhorn/: Mapterhorn's elevation tiles z0-12 (tile host only, workflow "dem")

[Mapterhorn](https://mapterhorn.com/attribution)'s `planet.pmtiles` (z0-12, terrarium WebP; data from many open
sources, see that page) repacked as it is into packs of 8 x 8 tiles (`server/tilepack.py`) by
`tools/build_mt_packs.py`, under `dem/mapterhorn/<release>/{z}/{px}/{py}.bngp` with `index.bin` (which packs exist)
and `release.json`. Mapterhorn allows storing and redistributing its tiles with attribution; BeamNG.Earth reads them
for zooms 0-12 when `cdn.json` turns `"dem"` -> `"bngmt"` on, and asks tiles.mapterhorn.com only for deeper zooms.

## addons.json: the in-game Add-ons page's list (edited by hand)
The add-ons BeamNG.Earth's Add-ons page offers and lets players install (server/addons.py, REGISTRY_URL; read through
raw.githubusercontent.com, not the Pages site, so editing it needs no data build). Every edit reaches players within a
few hours, with no release in between: treat a change here like a release. Only list a repo whose author agreed and
whose files were checked first. A file that can't be read, or whose entries are all invalid, leaves players on the list
bundled with their version; an empty `addons` list takes every add-on off. The format is in BeamNG.Earth's
docs/ADDONS.md.
