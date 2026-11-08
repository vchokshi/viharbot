#!/usr/bin/env bash
# Upload the website images to a DigitalOcean Space. A DRY RUN unless APPLY=yes.
#
#   SPACES_BUCKET=viharbot-assets SPACES_REGION=nyc3 \
#   SPACES_ACCESS_KEY=... SPACES_SECRET_KEY=... \
#   scripts/upload_images_to_spaces.sh /path/to/images            # lists what would upload
#   APPLY=yes ... scripts/upload_images_to_spaces.sh /path/to/images
#
# Only the files in TEAM_IMAGES (src/viharbot/web_app.py) are uploaded, to the "images/" prefix,
# public-read, with a one-day cache. Spaces keys are not the DigitalOcean API token; create one in
# the control panel (Spaces Object Storage > Access Keys). Keys come from the environment only.
# Objects in the bucket are public: never upload anything that is not meant to be public.
set -euo pipefail

SRC="${1:?usage: $0 <images directory>}"
BUCKET="${SPACES_BUCKET:?set SPACES_BUCKET}"
REGION="${SPACES_REGION:?set SPACES_REGION (for example nyc3)}"
ENDPOINT="https://${REGION}.digitaloceanspaces.com"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# Spaces keys use their own names so they never clash with real AWS credentials in your shell.
# They are mapped to the names the aws CLI expects for this script's process only.
export AWS_ACCESS_KEY_ID="${SPACES_ACCESS_KEY:?set SPACES_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${SPACES_SECRET_KEY:?set SPACES_SECRET_KEY}"
unset AWS_PROFILE AWS_SESSION_TOKEN

command -v aws >/dev/null || { echo "The aws CLI is required (it speaks the S3 API Spaces uses)." >&2; exit 2; }

# The allowlist is the single list of publishable images.
FILES=()
while IFS= read -r line; do FILES+=("$line"); done < <(python3 -I -c '
import ast, pathlib, re
src = pathlib.Path(__import__("sys").argv[1]).read_text()
block = re.search(r"TEAM_IMAGES = \{(.*?)\n\}", src, re.S).group(1)
print("\n".join(ast.literal_eval("[" + block + "]")))
' "$HERE/src/viharbot/web_app.py")

missing=0
for name in "${FILES[@]}"; do
  [[ -f "$SRC/$name" ]] || { echo "MISSING: $SRC/$name" >&2; missing=1; }
done
[[ $missing -eq 0 ]] || exit 1

for name in "${FILES[@]}"; do
  case "$name" in *.svg) type="image/svg+xml" ;; *) type="image/png" ;; esac
  if [[ "${APPLY:-}" == "yes" ]]; then
    aws s3 cp "$SRC/$name" "s3://${BUCKET}/images/${name}" --endpoint-url "$ENDPOINT" \
      --acl public-read --content-type "$type" --cache-control "public, max-age=86400"
  else
    echo "would upload: $name -> s3://${BUCKET}/images/${name} (${type})"
  fi
done
[[ "${APPLY:-}" == "yes" ]] || echo "Dry run. Re-run with APPLY=yes to upload ${#FILES[@]} files."
