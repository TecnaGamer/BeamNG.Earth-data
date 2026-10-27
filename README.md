# BeamNG.Earth data

Data files that [BeamNG.Earth](https://github.com/TecnaGamer/BeamNG.Earth) reads at runtime, rebuilt automatically
by this repository's GitHub Actions and served by its GitHub Pages
(`https://tecnagamer.github.io/BeamNG.Earth-data/`).

## rail/: OpenStreetMap's railways (weekly)

`rail/<x>/<y>.json.gz` per zoom-7 tile with railways: OpenStreetMap's railway tags by way id (railway, usage,
service, gauge, tracks, electrified, voltage, frequency, maxspeed, bridge, tunnel, ...) and its level crossing and
catenary mast nodes; `rail/manifest.json` says when it was built. BeamNG.Earth joins them to Overture Maps' rail
segments (`server/railtags.py`) to leave out lines not built yet, show disused and abandoned lines worn, use the real
gauge, and give level crossings their gates and lights.

Built every week from the OpenStreetMap planet by `.github/workflows/rail.yml`, with
`tools/build_rail_tags.py` from the BeamNG.Earth repository.

**License:** the data is © OpenStreetMap contributors, available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). See
https://www.openstreetmap.org/copyright.

## fra/: US level crossings (weekly)

`fra/<x>/<y>.json.gz` per zoom-7 tile: the US Federal Railroad Administration's Highway-Rail Crossing Inventory, its
open at-grade crossings as `[lat, lon, kit]` (kit: `gate`, `flash` or `cross`), built by `tools/build_fra_shards.py`.
Public domain (a work of the US government).
