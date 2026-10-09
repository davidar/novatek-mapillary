#!/usr/bin/env bash
# Nightly: copy the dashcam SD card to the trip drive, check every file arrived, rebuild the
# descriptions, verify the newest day and prune it. Uploading is a separate step (upload_all.sh).
#   ./sync_card.sh [card-mount]   (looks for a card with Normal/ and GPS/ under /run/media/$USER if omitted)
set -euo pipefail
CONFIG=${NOVATEK_MAPILLARY_CONFIG:-$HOME/.config/novatek-mapillary/config.sh}
# shellcheck source-path=SCRIPTDIR source=config.example.sh
source "$CONFIG"
NM=${NM:-novatek-mapillary}

if [[ -n "${1:-}" ]]; then
  SD=$1
else
  SD=$(for d in /run/media/"$USER"/*; do if [[ -d "$d/Normal" && -d "$d/GPS" ]]; then echo "$d"; fi; done | head -1; true)
fi
[[ -d "${SD:-}/Normal" ]] || { echo "dashcam card not found (looked under /run/media/$USER/*)"; exit 1; }
mkdir -p "$STORE/raw"

echo "card: $SD   ->   $STORE/raw"
before=$(find "$STORE/raw" -type f | wc -l)
rsync -rt --no-perms --no-owner --no-group --info=progress2,stats1 \
  --log-file="$STORE/rsync-copy.log" "$SD"/ "$STORE"/raw/
after=$(find "$STORE/raw" -type f | wc -l)
echo "files on the drive: $before -> $after (+$((after - before)))"

# The card is usually wiped after this, so the drive becomes the only copy: check every file.
echo "--- comparing sizes, card vs drive ---"
if diff <(cd "$SD" && find . -type f -printf '%s %p\n' | sort -k2) \
        <(cd "$STORE/raw" && find . -type f -printf '%s %p\n' | sort -k2) | grep '^<'; then
  echo "MISMATCH: some card files differ or are missing on the drive"; exit 1
fi
echo "every file on the card is on the drive with the same size"

echo "--- building descriptions ---"
$NM build "$STORE/raw" --out "$STORE/desc" --tz "$CAMERA_TZ"

newest=$(printf "%s\n" "$STORE"/desc/2*.json | sort | tail -1)
echo "--- verifying $(basename "$newest") ---"
$NM verify "$newest" --tz "$CAMERA_TZ"

echo "--- privacy pruning ---"
$NM prune "$newest" --tz "$CAMERA_TZ" "${PRUNE_ARGS[@]}"

if [[ -n "${BACKUP_DIR:-}" ]]; then
  mkdir -p "$BACKUP_DIR" && rsync -rt "$STORE/desc/" "$BACKUP_DIR/desc/"
  echo "descriptions -> $BACKUP_DIR/desc"
fi
echo
echo "read $STORE/desc/upload/$(basename "$newest" .json).pruned.txt, then upload with ./upload_all.sh"
