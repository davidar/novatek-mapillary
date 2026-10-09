#!/usr/bin/env bash
# Upload pruned days to Mapillary, one mapillary_tools process per day in parallel, and keep going
# across USB drops: a USB drive can come back within seconds under a new device name but
# unmounted, and mapillary_tools then fails every remaining clip with FileNotFoundError while
# still exiting 0. So: watch the mount, kill + remount (read-only) + rerun until every day ends
# with no failed uploads. Clips already up are skipped by mapillary_tools itself.
#   ./upload_all.sh [YYYYMMDD ...]      default: every day in $STORE/desc/upload/
set -u
CONFIG=${NOVATEK_MAPILLARY_CONFIG:-$HOME/.config/novatek-mapillary/config.sh}
# shellcheck source-path=SCRIPTDIR source=config.example.sh
source "$CONFIG"
DEV=/dev/disk/by-label/$DRIVE_LABEL
LOG=${LOG:-$HOME/.cache/novatek-mapillary-upload}
DAYS=("$@")
if [ ${#DAYS[@]} -eq 0 ]; then
  for f in "$STORE"/desc/upload/2*.json; do DAYS+=("$(basename "$f" .json)"); done
fi
mkdir -p "$LOG"
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG/upload.log"; }

mounted() { timeout 10 ls "$STORE/desc/upload" >/dev/null 2>&1; }
ensure_mounted() {
  local n=0
  until mounted; do
    n=$((n + 1)); [ $n -le 60 ] || { say "drive did not come back after 10 min, giving up"; exit 2; }
    udisksctl unmount -f -b "$DEV" >/dev/null 2>&1
    udisksctl mount -b "$DEV" -o ro >/dev/null 2>&1 || sleep 10
  done
}

# Clip folders to hand mapillary_tools: wherever the descriptions' files live.
DESCS=(); for d in "${DAYS[@]}"; do DESCS+=("$STORE/desc/upload/$d.json"); done
mapfile -t DIRS < <(python3 -c 'import json, os, sys
print("\n".join(sorted({os.path.dirname(d["filename"]) for f in sys.argv[1:] for d in json.load(open(f))})))' "${DESCS[@]}")
[ ${#DIRS[@]} -gt 0 ] || { say "no clips in ${DESCS[*]}"; exit 1; }

for round in $(seq 1 50); do
  ensure_mounted
  say "round $round: starting ${DAYS[*]}"
  pids=()
  for d in "${DAYS[@]}"; do
    : > "$LOG/$d.last.log"
    mapillary_tools upload --user_name "$MAPILLARY_USER" --desc_path "$STORE/desc/upload/$d.json" \
      "${DIRS[@]}" 2>&1 | tee -a "$LOG/$d.log" > "$LOG/$d.last.log" &
    pids+=($!)
  done
  dropped=0
  while pgrep -f "mapillary.tools.*upload" >/dev/null; do
    sleep 20
    if ! mounted; then
      dropped=1; say "round $round: drive gone, stopping uploads"
      pkill -INT -f "mapillary.tools.*upload"; sleep 5
      pkill -KILL -f "mapillary.tools.*upload"
    fi
  done
  wait "${pids[@]}" 2>/dev/null
  todo=()
  for d in "${DAYS[@]}"; do
    # Don't trust the exit code: a day is done only if its log ends in a clean summary.
    if [ $dropped = 0 ] && grep -q "Upload summary\|Nothing uploaded" "$LOG/$d.last.log" \
       && ! grep -q "uploads\? failed\|Traceback" "$LOG/$d.last.log"; then
      say "$d done: $(tr '\r' '\n' < "$LOG/$d.last.log" | grep -i "video uploaded\|Skipped\|Nothing uploaded" | sed 's/.*INFO *- //' | paste -sd';')"
    else
      todo+=("$d")
    fi
  done
  [ ${#todo[@]} -gt 0 ] || { say "ALL DONE"; exit 0; }
  DAYS=("${todo[@]}")
  say "round $round: still to do: ${DAYS[*]}"
  sleep 15
done
say "gave up after 50 rounds: ${DAYS[*]}"; exit 1
