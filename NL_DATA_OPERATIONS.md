# Dutch roadside datasets: maintainer runbook

This is the publication/operations guide for application PR
[#1214](https://github.com/TecnaGamer/BeamNG.Earth/pull/1214) and data PR
[#5](https://github.com/TecnaGamer/BeamNG.Earth-data/pull/5).
Publishing is a maintainer action; those PRs do not themselves deploy data.

## What is published

| Dataset | Contents | Runtime layout | Licence |
| --- | --- | --- | --- |
| `nl-rvv` | Supported NDW sign attributes and complete supported text qualifiers | z12 `x/y.json.gz`, format 2 | NDW attributes CC0-1.0 |
| `nl-signal-context` | Motor-road OSM signal/turn-lane context and WKD built-up-area sections | z14 `x/y.json.gz`, format 1 | Derived database ODbL 1.0; retain OSM and RWS/NDW attribution |

Neither dataset contains photographs, vendor sign artwork, Groenpol meshes,
surveyed stop lines or measured traffic-signal programmes. Model licensing is
separate: Sven's original Groenpol assets are in the application, CC BY-SA 4.0.
Keep source hashes, query notes, attribution and licence fields with each build.
The contributor's local NDW test sample is not a national deployment artifact.

## First publication

1. Merge application #1214 into `dev` first, including its API 302 source and
   platform-native builds. Confirm the NDW producer exists there. The `nl-rvv`
   data job checks out that producer from application `dev`.
2. Merge data #5. Use this repository's existing **data** workflow
   (`.github/workflows/rail.yml`, Actions → data → Run workflow) on `main`.
   There is no separate Dutch-only Pages deployment and no new enable input.
3. Check both optional jobs, `nl-rvv` and `nl-signal-context`, individually.
   A green overall site/deploy is insufficient: Dutch jobs may fail without
   blocking rail/lanes. The workflow builds the national sources, validates the
   artifacts, assembles the existing site, mirrors datasets and deploys Pages.
4. Check the existing R2/Pages configuration. R2 credentials remain only in the
   existing trusted site/mirror job; the Dutch source/build jobs need no secrets.
   Do not copy tokens into source, artifacts, manifests or the mod.
5. Keep a copy of each successful Dutch artifact and its manifest/build ID before
   the next scheduled run. Current Dutch artifact retention is **three days**.

The source jobs download NDW current-state attributes and pinned WKD plus bounded
Overpass results. An interrupted Overpass response or changed WKD archive hash
must fail validation; do not bypass a checksum to make the job green. To update
WKD deliberately, review the new official distribution, CRS and licence, update
the pinned URL/hash in `tools/fetch_nl_signal_sources.py`, and rebuild.

## Validate before and after publication

The same artifact checkers used by the workflow can be run locally:

```sh
python tools/test_nl_roadside_ci.py
python tools/test_nl_context_ci.py
python tools/nl_roadside_ci.py out/nl-rvv
python tools/nl_context_ci.py out/nl-signal-context
```

Use an empty output directory for each producer run. The context producer needs
`pyproj` and `shapely` offline; the application reader does not need those GIS
packages. The workflow installs its build dependencies.

After `deploy`, verify these JSON endpoints (not an HTML 404 response):

- https://tecnagamer.github.io/BeamNG.Earth-data/nl-rvv/manifest.json
- https://tecnagamer.github.io/BeamNG.Earth-data/nl-signal-context/manifest.json

Check `built`, `format`, `zoom`, licence/attribution, source provenance and a
plausible record/tile count against the validated artifact. Fetch at least one
listed tile near Rotterdam and one in another Dutch region; gzip-decode and
parse them. `nl-rvv.tiles` lists `x/y` keys; context `tiles` maps
`x/y.json.gz` to checksums/sizes. Do not assume the two indexes have one schema.

If a manifest contains `data`, use that exact URL as the bulk-data base and check
listed tile paths there too. Do not manufacture the R2 key from `built`:
`mirror_data.py` constructs it using the build and file content. The mirror
uploads shards first and its completion manifest last, probes the public copy,
then updates the Pages manifest's `data` URL. A mirror failure can leave a valid
Pages-only dataset; investigate its log rather than inventing a `data` URL.

Never edit content inside a published immutable build prefix. Publish a newly
built, validated dataset instead. Do not replace the whole site with just these
two folders: that would remove other countries' datasets.

## Application smoke test

Use an application build containing #1214 and compatible native API 302 or its
successor. Leave Dutch signals off first: existing signals must remain unchanged.
Then enable `dutchSignals`, load Capelseweg/Hoofdweg and Kralingseplein, and wait
for committed terrain/roads and complete assemblies. Check both directions,
feet, head orientation, phase changes, tile reload/rebase and an off/on cycle.
The recorded city checks are in the application's
`docs/NL_SIGNALS_CITY_VALIDATION.md`.

For signs, the server needs `BNE_ROADSIGNS=on` and `BNE_NL_ROADSIGNS=on`.
The global road-sign setting is hidden on the current application basis; a
client switch alone does not enable server sign generation. Validate the public
NDW build on a clean test cache as well as a warm cache, rather than depending on
the contributor's seeded local files. Check equivalent-sign suppression locally,
not national removal of existing signs.

Groenpol does **not** promise a replacement at every intersection: source-backed
matching and safe complete placement are required. Unsupported signs, missing
context, unsafe geometry and ambiguous matches keep stock fallbacks. Stop
stations are inferred, and arrows use the existing approach phases, not surveyed
independent protected-turn programmes.

## Failure, recovery and rollback

- Inspect the Dutch job and checker errors first. Do not publish an unchecked or
  partly uploaded artifact, or an empty manifest as a successful replacement.
- **Current workflow limitation:** unlike some existing optional datasets, the
  two Dutch jobs are not in the site's automatic last-good-artifact recovery
  loop. If either job fails, its dataset may be omitted from the next Pages
  deployment. Do not assume it is automatically preserved. Monitor both jobs;
  retain successful artifacts, or add reviewed last-good recovery before relying
  on unattended weekly continuity.
- To restore a known-good build, validate the saved artifact again, assemble it
  into the complete existing site, and use the ordinary mirror/Pages deployment.
  Restore only the affected dataset's manifest/reference; preserve all other
  datasets and old immutable prefixes still referenced by running servers.
- For a runtime problem, disable the affected optional feature/server flags
  first; stock objects remain the fallback. Preserve the application version,
  dataset `built` ID, location, relevant logs and a failing tile for diagnosis.
- The application caches manifests/shards and retries failures with backoff.
  A repaired publication may not appear immediately. Check the public endpoints
  first, then retry in a fresh test server/profile if needed. Do not tell every
  player to delete their entire Earth cache.

Public source updates, dataset publication and application releases are distinct
steps. Merging code alone does not publish the national data or guarantee that
every player has enabled the feature.
