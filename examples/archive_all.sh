#!/usr/bin/env bash
# Re-encode a trip's clips for keeping (GPS carried as a CAMM track) and keep going across USB
# drops: `novatek-mapillary archive` exits 75 when the source disk disappears, so remount it
# read-only and rerun until it finishes. Clips already in the manifest are skipped, so a rerun
# only does what's left. Then verify every archived clip once and print the summary.
#   ./archive_all.sh [SRC]      default: $STORE/raw
# The remount is read-only. If ARCHIVE_DST is on the same drive as SRC, run with MOUNT_OPTS=rw.
set -u
CONFIG=${NOVATEK_MAPILLARY_CONFIG:-$HOME/.config/novatek-mapillary/config.sh}
# shellcheck source-path=SCRIPTDIR source=config.example.sh
source "$CONFIG"
NM=${NM:-novatek-mapillary}
SRC=${1:-$STORE/raw}
DEV=/dev/disk/by-label/$DRIVE_LABEL
LOG=${LOG:-$HOME/.cache/novatek-mapillary-archive}
MOUNT_OPTS=${MOUNT_OPTS:-ro}
mkdir -p "$LOG" "$ARCHIVE_DST"
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG/archive.log"; }

mounted() { timeout 10 ls "$SRC" >/dev/null 2>&1; }
ensure_mounted() {
  local n=0
  until mounted; do
    n=$((n + 1)); [ $n -le 60 ] || { say "drive did not come back after 10 min, giving up"; exit 2; }
    udisksctl unmount -f -b "$DEV" >/dev/null 2>&1
    udisksctl mount -b "$DEV" -o "$MOUNT_OPTS" >/dev/null 2>&1 || sleep 10
  done
}

# Run archive (extra arguments appended) until it exits 0, rerunning after a USB drop (exit 75).
# Other failures (exit 1: a clip failed) are rerun at most $1 more times.
run_until_done() {
  local retries=$1 round rc failures=0
  shift
  for round in $(seq 1 50); do
    ensure_mounted
    say "round $round: $NM archive $SRC $ARCHIVE_DST ${ARCHIVE_ARGS[*]} $*"
    "$NM" archive "$SRC" "$ARCHIVE_DST" "${ARCHIVE_ARGS[@]}" "$@" 2>&1 | tee -a "$LOG/archive.log"
    rc=${PIPESTATUS[0]}
    case $rc in
      0) return 0 ;;
      75) say "round $round: source gone, remounting"; sleep 15 ;;
      130) say "interrupted"; exit 130 ;;
      *) failures=$((failures + 1))
         [ $failures -le "$retries" ] || { say "still failing after $failures passes (exit $rc); see $LOG/archive.log"; return "$rc"; }
         say "round $round: exit $rc, some clips failed; trying them again" ;;
    esac
  done
  say "gave up after 50 rounds"; return 1
}

run_until_done 2 || exit $?
say "--- verifying every archived clip ---"
run_until_done 0 --verify-only
rc=$?
say "summary: $(grep '^archive:' "$LOG/archive.log" | tail -1)"
if [ $rc = 0 ]; then
  say "ALL VERIFIED: freeing the sources is up to you"
else
  say "verify found problems (exit $rc); see $LOG/archive.log"
fi
exit $rc
