#!/usr/bin/env bash
# Where upload_all.sh is right now: one line per day from the live mapillary_tools output, then
# the supervisor's log (which only gets a line when a round starts, a day finishes or the drive drops).
LOG=${LOG:-$HOME/.cache/novatek-mapillary-upload}
done_=0; total=0
for f in "$LOG"/2*.last.log; do
  d=$(basename "$f" .last.log)
  line=$(tr '\r' '\n' < "$f" | grep "Uploading VIDEO" | tail -1)
  if grep -q "Upload summary" "$f"; then
    echo "$d  finished: $(grep -i "video uploaded\|failed\|Skipped" "$f" | sed 's/.*INFO *- //' | paste -sd';')"
  elif [ -n "$line" ]; then
    n=$(sed -E 's/.*\(([0-9]+)\/([0-9]+)\).*/\1/' <<< "$line"); t=$(sed -E 's/.*\(([0-9]+)\/([0-9]+)\).*/\2/' <<< "$line")
    done_=$((done_ + n - 1)); total=$((total + t))
    echo "$d  clip $n of $t  $(sed -E 's/.*\): *//' <<< "$line" | sed -E 's/\|[^|]*\|//')"
  else
    echo "$d  starting"
  fi
done
[ $total -gt 0 ] && echo "running days: $done_ of $total clips up ($((100 * done_ / total))%)"
pgrep -f "mapillary.tools.*upload" >/dev/null && echo "uploads running" || echo "NO upload process running"
echo "--- supervisor"; tail -5 "$LOG/upload.log"
