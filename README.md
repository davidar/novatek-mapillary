# novatek-mapillary

Geotag dashcam clips from Novatek-based cameras (Vantrue and many others) for
[Mapillary](https://www.mapillary.com/), using the GPS records the camera embeds in every MP4, and
drop the clips that would show where you park.

`mapillary_tools` has no native parser for Novatek's GPS format. It can reach the records through
exiftool (`--video_geotag_source exiftool_runtime`), but that route anchors each clip's track at
its first fix instead of fitting the clip's start, and does nothing about the repeated and stepped
timestamps described below; its GPX route needs one GPX per clip rebased to the video. This writes
the video description JSON that `mapillary_tools upload --desc_path` takes directly: one exact
point per second, with UTC time and heading. Standard library only, no ffmpeg or exiftool.

```
novatek-mapillary build  /path/to/card-copy              # -> ../desc/<YYYYMMDD>.json per day
novatek-mapillary verify ../desc/20260927.json           # sanity checks, exit 1 on problems
novatek-mapillary prune  ../desc/20260927.json \
    --exclude-near=LAT,LON,2000                          # -> ../desc/upload/20260927.json
mapillary_tools upload --desc_path ../desc/upload/20260927.json /path/to/card-copy/Normal /path/to/card-copy/Event
```

Install with `uv tool install git+https://github.com/davidar/novatek-mapillary` or
`pipx install git+https://github.com/davidar/novatek-mapillary`, or run from a checkout with `python3 -m novatek_mapillary` (with `src/` on `PYTHONPATH`).

Tested on a Vantrue camera (4K30 H.264, 60 s loop clips) over about 1,800 km and 1,100 clips: the
tracks agree with the camera's separate `.dat` log to a median of 0.5 m. Other Novatek cameras
that write the same `gps ` box should work; `novatek-mapillary parse CLIP.MP4` shows quickly
whether yours does.

## Commands

- **`parse CLIP...`**: print each embedded GPS record (index, video time, fix time, position, speed,
  course) and the fitted start time of the video. `--json` for one object per clip.
- **`build ROOT`**: every `.mp4` under ROOT, in filename order, becomes one description; output is
  one JSON per camera-clock day plus `all.json` and `skipped.json`.
  - `--tz auto` (the default) works out the camera clock's UTC offset from the embedded fixes and
    the filenames. Give it explicitly (`--tz +10:00`) if the filenames carry no time.
  - `--dat-dir` points at the plain-text `.dat` tracks if they aren't in a `GPS/` folder next to
    the clips; they only supply altitude and a fallback (see below).
  - `--make` / `--model` set `MAPDeviceMake` / `MAPDeviceModel`; left out by default.
- **`verify DESC...`**: per clip: enough points, times strictly increasing, no gap over 10 s, no
  implied speed over 60 m/s, start within 30 s of the filename time, and a cross-check against the
  `.dat` track when there is one (a healthy day reads well under 5 m median). Reads only JSON.
- **`prune DESC...`**: privacy, see below. Writes `upload/<day>.json` and `upload/<day>.pruned.txt`
  (every window cut, why, where, and the clips dropped).

## Privacy pruning

Mapillary blurs faces and plates, but a sequence that starts in your driveway every morning tells
anyone where you live. `prune` works from the track alone and drops every clip that overlaps:

- the start and end of each day's recording,
- a gap in recording longer than `--gap` (5 s): the camera was off, so the car was parked,
- the car stationary for `--min-stop` (180 s) or more with the camera running,

each widened by `--buffer` (120 s) on both sides, so the approach and departure go too. On top of
that:

- `--exclude-near=LAT,LON,METRES` drops any clip that comes within METRES of a point, at any speed.
  Use it for home and for every place you stay overnight: a 2 km radius also takes out the
  queue at the entrance and the drive around the block.
- `--cut-after-entering=LAT,LON,METRES` drops everything from the first time the car enters the
  zone to the end of that day: for a trip that ends at a private address in a big city, put the
  zone where you join the city's motorway network and the whole approach goes.

Read the `.pruned.txt` report before uploading. Mapillary uploads can't be taken back easily.
The report gives the location of every stop, including home and the zones you passed in, so
keep it with your private config and out of anything you share.

## How the GPS is stored

Each clip's `moov` box contains a `gps ` box: a table of (offset, size) pairs, one per second of
video plus a header entry. Each points to a `freeGPS ` record inside `mdat`, written right after
the frames of that second, and holding (after some binary fields) the receiver's raw `$GNRMC`
sentence: UTC time, fix status, latitude, longitude, speed in knots, course. Looking the record's
byte offset up in the video track's chunk table gives the exact frame it was written after.

The sentence in a record is the most recent fix, written 0–1 s after the fix time (plus receiver
latency). So the start of the video is fitted per clip as the median of (fix time − video time)
over every fix's first appearance, plus 0.5 s, which is good to about half a second. Each
fix is then placed at `fix time − start` in the video.

## Things that took a while to find out

- **The camera clock is local time** and filenames follow it; the MP4s have no `creation_time`.
  The embedded RMC times are true UTC, which is what the descriptions use, so the camera clock only
  matters for the filename checks and the `.dat` lookup. `--tz auto` infers it; if you change the
  camera's clock (daylight saving) mid-trip, run each day with its own `--tz`.
- **The `.dat` track is not trustworthy at the one-second level.** Its rows lag the real fix by
  0–2 s, varying between camera sessions, and about 9% of rows repeat the previous fix with the
  first copy stamped a second early. Rows are stamped 1 s after the fix they hold (`--dat-offset
  -1`). It is used only for altitude, as a fallback for clips whose embedded records can't be read,
  and as `verify`'s independent cross-check.
- **Timestamps stall**: the same fix appears in two consecutive records. The first occurrence
  wins.
- **The GPS clock steps**: occasionally the receiver's UTC jumps forward by whole seconds (seen:
  14 s, probably GPS vs BeiDou time) for about a minute and then steps back, while the positions
  stay continuous. The camera clock and the filenames follow it. Records ≥ 3 s off a clip's median
  are re-stamped; a clip recorded entirely inside a step is re-anchored from its neighbours (the
  gaps before and after it are equal and opposite); the `.dat` rows are moved back. Unhandled, it
  shows up in `verify` as an implied speed of ~400 m/s and in `prune` as a phantom 0-minute
  "camera off".
- Clips recorded **before the GPS has a fix** at power-on, or after it loses the fix while parked,
  have no usable track and are skipped (see `skipped.json`).
- **Patchy GPS while parked** can leave a gap of over 10 s inside a clip; `verify` then exits 1.
  If the clip is inside a window `prune` cuts anyway, it doesn't matter.
- Vantrue's `Normal/` and `Event/` folders are one interleaved timeline (event clips are not
  duplicates), so give `mapillary_tools upload` both. `Photo/` JPEGs have no EXIF.

## Uploading with mapillary_tools

- Descriptions hold each clip's absolute path, so keep the card copy mounted where it was when
  you ran `build`, or run `build` again after moving it.
- Clips go up as video; `mapillary_tools` wraps the description's track into a CAMM stream on the
  fly. Uploads resume where they left off.
- **Never trust its exit code.** It exits 0 even when every clip failed (for example
  `FileNotFoundError` after a USB disk dropped out). Read the "Upload summary" at the end of the
  log.
- `--num_upload_workers` only parallelises image uploads; for video, run one `mapillary_tools
  upload` per day in parallel. A single stream can stall for minutes at a time.
- `upload --dry_run` still checks your token online. To test the packaging offline, call
  `VideoUploader.build_camm_stream` yourself.

`examples/` has the shell scripts used for a 4-day trip, driven by a private config file
(`examples/config.example.sh`): `sync_card.sh` (copy the card, check sizes, build, verify, prune),
`upload_all.sh` (one upload per day in parallel, remounting a dropped USB disk read-only and
rerunning until every day's summary is clean) and `upload_status.sh`.

## Tests

```
uv run --group dev pytest
```

The tests never use real footage, and no video file is committed: `tests/synth_clip.py` writes
small synthetic clips at test time with the same box layout as real ones (dummy frames, real
`gps ` index and `$GNRMC` records), including stalls, clock steps, clips with no fix, and a day
with stops and a camera-off gap. `python3 tests/synth_clip.py OUT_DIR` writes a demo card.

## Licence

MIT
