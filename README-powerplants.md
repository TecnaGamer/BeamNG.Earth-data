# powerplants: the world's thermal power plants, rebuilt every week

One static index on the project's tile host for BeamNG.Earth's power plants (the main repo's `server/powerplants.py`):
the map's Power plants layer and the plant stream (steam plumes, aviation lights, far stand-ins of the mapped towers,
the generic plant where nothing of a plant is mapped).

    powerplants/<version>/power_plants.npz    plants (point, fuel, size, state, closing year, reach, OSM element, name)
                                              and their mapped cooling towers and chimneys (point, kind, height,
                                              diameter, OSM element, plant); ~1 MB; ODbL
    powerplants/<version>/ATTRIBUTION.md
    powerplants/<version>/manifest.json       format 1, name, version, build (16 hex of the npz's SHA-256), built,
                                              attribution, plants, structures, the file's size and SHA-256; uploaded last

Built by `tools/build_powerplants.py` (a pinned copy of the main repo's; copy it here again when it changes there) and
its hand-checked list `tools/data/power_plants_manual.json`:

- OpenStreetMap's power=plant through GMWID's latest build (only its latest is published: its hash and version are in
  the run's provenance artifact), with EIA-860M's US plants OSM lacks;
- OpenStreetMap's mapped chimneys and cooling towers from the Overpass API (15-degree longitude bands, three public
  instances in turn, retried);
- GPPD v1.3.0, GEM's coal tracker, GeoNuclearData and powerplantmatching at pinned commits, their SHA-256 checked.

Deterministic: the workflow stages the index twice from the same inputs and compares the npz byte for byte.

## Workflow

`.github/workflows/powerplants.yml`: Saturdays 05:41 UTC, and by hand (Actions -> powerplants -> Run workflow;
`dry_run` defaults to true: plan and build only).

1. **plan**: the version `cdn.json` serves and the next one (v1, v2, ...), the served manifest from the CDN.
2. **build** (no secrets): inputs, Overpass bands, the index twice (byte-compared), `validate`: the server reader's
   rules, the anchor plants (Drax, Gavin, Cattenom, Niederaussem, Irsching) with their mapped cooling towers, and the
   plant and structure counts within 0.9-1.15x of the served build. A build identical to the served one stops here.
3. **publish** (R2 secrets; only `tools/winterpack_ci.py`, standard library, and aws-cli): the files uploaded once
   (immutable, a published version is never rewritten), the manifest last.
4. **live**: the CDN's copy compared byte for byte.
5. **flip** (contents write; the checker and git only): `cdn.json`'s `{"powerplants": {"enabled": true, "version":
   "vN"}}` by a minimal edit, only if it still names the version the plan read. Players' servers pick it up at their
   next start.
6. **notify**: one issue when a scheduled run failed (nothing flipped).

Rollback: commit `cdn.json` back to the previous version (every version stays on the tile host), or remove the
`powerplants` key: the servers then read the index bundled with the app. On a server: `BNGE_POWERPLANTS=off` (the
bundled index), `BNGE_POWERPLANTS=vN` (that version), `BNGE_POWERPLANTS_DIR=<dir>` (a local build:
`<dir>/powerplants/manifest.json`, `<dir>/powerplants/power_plants.npz`).

The building tiles (cooling tower shells, chimneys) always use the index bundled with the app, so a weekly flip never
makes a cached tile stale; a tower mapped this week gets its plume at once and its shell with the next app release.
