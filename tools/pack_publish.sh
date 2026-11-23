#!/usr/bin/env bash
# tools/pack_publish.sh NAME: the data workflow's (.github/workflows/rail.yml) upload of a PMTiles dataset's parts to
# the tile host (R2 bnge-tiles behind tiles.tgforever.net), run in the NAME_publish job, the only one holding the R2
# secrets (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, the endpoint in R2). It runs only this repo's standard-library
# checker (tools/pack_check.py, python3 -I) and aws-cli: the build ran in the NAME job, without secrets, and handed
# over art/ (NAME/ and NAME.seal.json, the artifact NAME-build).
#   1. art/ checked (tools/pack_check.py check: names, sizes, PMTiles, SHA-256 against the manifest and the seal)
#   2. NAME/<key>/ listed: a key there that is not one of the parts, or one with another size, refuses the upload
#      (a published folder is never overwritten or deleted); parts already there with their size are not sent again
#   3. the parts not there yet uploaded (immutable), the listing checked again: every part with its size
#   4. last, pub/NAME/manifest.json written with each part's URL (the artifact NAME, which the site job publishes on
#      Pages after the parts)
# CHUNK (optional): aws-cli's multipart chunk size (osmtags: 64MB, as its job set before).
set -euo pipefail
N=${1:-}
[[ "$N" =~ ^(osmtags|osmyears|osmcolours|tallbld|osmareas)$ ]] || { echo "::error::bad dataset: $N"; exit 1; }
: "${R2:?R2, the bucket endpoint, not set}"
BUCKET=bnge-tiles
chk() { python3 -I tools/pack_check.py "$@"; }

chk check art "$N" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
KEY=$(chk key art "$N")
[[ "$KEY" =~ ^[0-9A-Za-z_-]{4,80}$ ]] || { echo "::error::bad key: $KEY"; exit 1; }
aws --version
if [ -n "${CHUNK:-}" ]; then aws configure set default.s3.multipart_chunksize "$CHUNK"; fi
# (list-objects-v2 exits 0 on an empty prefix and non-zero on an auth / network error: no silent "not there")
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$N/$KEY/" --endpoint-url "$R2" --output json > before.json
chk todo art "$N" --listing before.json > todo.txt
while read -r f k; do
  # (the names come from the checked manifest, never from a listing of the artifact)
  [[ "$f" =~ ^art/$N/([0-9A-Za-z_-]+/)?$N-[0-9]{3}\.pmtiles$ && "$k" == "$N/$KEY/${f##*/}" ]] \
    || { echo "::error::bad upload line: $f $k"; exit 1; }
  for try in 1 2 3; do
    aws s3 cp "$f" "s3://$BUCKET/$k" --endpoint-url "$R2" --only-show-errors \
      --content-type application/octet-stream --cache-control "public, max-age=31536000, immutable" < /dev/null && break
    test "$try" -lt 3; sleep $((try * 20))
  done
done < todo.txt
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$N/$KEY/" --endpoint-url "$R2" --output json > after.json
chk todo art "$N" --listing after.json --complete
# (last: the manifest players read first, pointing at the parts)
chk point art "$N" "pub/$N/manifest.json"
head -c 1200 "pub/$N/manifest.json"; echo
