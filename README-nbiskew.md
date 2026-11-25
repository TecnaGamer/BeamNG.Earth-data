# nbiskew: the US bridge inventory's skews

One static table on the project's tile host for BeamNG.Earth's bridge deck ends (the main repo's `server/nbiskew.py`,
read by `server/roadshape.py`'s deck-end skew): the US National Bridge Inventory's skew angle and structure length at
every structure's coordinates.

    nbiskew/<version>/nbiskew.npz       lat, lon (float32, rows sorted by lat), skew (uint8: NBI item 34, degrees;
                                        99 "major variation"), length (uint16: item 49 in dm; 0 unknown); ~4-5 MB
    nbiskew/<version>/ATTRIBUTION.md
    nbiskew/<version>/manifest.json     format 1, name, version, build (16 hex of the npz's SHA-256), records,
                                        columns, varies, the file's size and SHA-256; uploaded last

Built by `tools/build_nbiskew.py` from FHWA's delimited files of every state in one archive
(`https://www.fhwa.dot.gov/bridge/nbi/<year>hwybronefiledel.zip`, fetched only from www.fhwa.dot.gov), only the records
of structures carrying a route (item 5A type 1) with coordinates and a skew. Deterministic: two builds of one archive
are byte-identical (the workflow builds twice and compares).

## Workflow

Actions -> nbiskew -> Run workflow (manual only): version (v1, v2, ...), year (the NBI file year), dry_run (default:
build and check only). The publish job (R2 secrets, main only) runs only `tools/winterpack_ci.py` (standard library)
and aws-cli: the table and its attribution uploaded once (immutable; a published version is never rewritten), the
manifest last; the live job compares the CDN's copy byte for byte.

Players' servers read the table only once `cdn.json` has `{"nbiskew": {"enabled": true, "version": "v1"}}`, a commit
by hand after the live check (last):

    python3 -I tools/winterpack_ci.py cdn-key cdn.json nbiskew v1

`BNGE_NBISKEW=v1` / `off` overrides the switch on a server; `BNGE_NBISKEW_DIR=<dir>` reads a local build
(`<dir>/nbiskew/manifest.json`, `<dir>/nbiskew/nbiskew.npz`).
