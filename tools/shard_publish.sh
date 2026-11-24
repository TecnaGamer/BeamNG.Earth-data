#!/usr/bin/env bash
# tools/shard_publish.sh NAME: the data workflow's (.github/workflows/rail.yml) upload of a weekly z7 shard dataset
# (rail, fra, lanes, signs, stations, drive) to the tile host (R2 bnge-tiles behind tiles.tgforever.net), run in a
# publish job, the only one holding the R2 secrets (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, the endpoint in R2). It
# runs only this repo's standard-library checker (tools/shard_check.py, python3 -I) and aws-cli: the build ran in its
# own job, without secrets, and handed over art/ (NAME/ and NAME.seal.json, the artifact <job>-build).
#   1. art/ checked (names, sizes, gzip JSON shards, the shard count and its minimum, SHA-256 against the seal)
#   2. data/NAME/<key>/ listed: a key there that is not one of the build's files, or one with another size, refuses the
#      upload (a published folder is never overwritten or deleted); files already there are not sent again
#   3. the files not there yet uploaded (immutable), the listing checked again: every file with its size
#   4. data/NAME/<key>/manifest.json (the marker of a whole copy, naming its "data") uploaded last, unless it is there
#   5. one shard read back from the public host (its SHA-256 the seal's)
#   6. pub/NAME/: the files and the manifest naming the folder (the artifact the site job puts on Pages). Players'
#      servers (BeamNG.Earth pagesdata.Manifest) read a build's files only from its folder there, or from Pages while
#      Pages serves that same build.
set -euo pipefail
N=${1:-}
[[ "$N" =~ ^(rail|fra|lanes|signs|stations|drive)$ ]] || { echo "::error::bad dataset: $N"; exit 1; }
: "${R2:?R2, the bucket endpoint, not set}"
BUCKET=bnge-tiles
chk() { python3 -I tools/shard_check.py "$@"; }

chk check art "$N" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
KEY=$(chk key art "$N")
[[ "$KEY" =~ ^[0-9A-Za-z_]{1,40}-[0-9a-f]{12}$ ]] || { echo "::error::bad key: $KEY"; exit 1; }
P="data/$N/$KEY"
aws --version
# (list-objects-v2 exits 0 on an empty prefix and non-zero on an auth / network error: no silent "not there")
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$P/" --endpoint-url "$R2" --output json > "before-$N.json"
chk todo art "$N" --listing "before-$N.json" > "todo-$N.txt"
echo "$N: $(wc -l < "todo-$N.txt") files to upload to $P"
while read -r f k; do
  # (the names come from the checked tree, never from a listing of the artifact)
  [[ "$f" =~ ^art/$N/([0-9]{1,3}/[0-9]{1,3}\.json\.gz|planet\.txt)$ && "$k" == "$P/${f#art/$N/}" ]] \
    || { echo "::error::bad upload line: $f $k"; exit 1; }
  for try in 1 2 3; do
    aws s3 cp "$f" "s3://$BUCKET/$k" --endpoint-url "$R2" --only-show-errors \
      --content-type application/octet-stream --cache-control "public, max-age=31536000, immutable" < /dev/null && break
    test "$try" -lt 3; sleep $((try * 20))
  done
done < "todo-$N.txt"
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$P/" --endpoint-url "$R2" --output json > "after-$N.json"
STATE=$(chk todo art "$N" --listing "after-$N.json" --complete)
if [ "$STATE" != "whole" ]; then
  # (last: the marker of a whole copy, naming its folder)
  chk remote art "$N" "remote-$N.json"
  aws s3 cp "remote-$N.json" "s3://$BUCKET/$P/manifest.json" --endpoint-url "$R2" --only-show-errors \
    --content-type application/json --cache-control "public, max-age=3600" < /dev/null
fi
chk verify art "$N"
chk point art "$N" pub
head -c 600 "pub/$N/manifest.json"; echo
