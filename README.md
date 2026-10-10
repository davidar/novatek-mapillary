# novatek-mapillary

Geotag dashcam clips from Novatek-based cameras (Vantrue and many others) for
[Mapillary](https://www.mapillary.com/), using the GPS records the camera embeds in every MP4, and
drop the clips that would show where you park.

`mapillary_tools` has no native parser for Novatek's GPS format. It can reach the records through
exiftool (`--video_geotag_source exiftool_runtime`), but that route anchors each clip's track at
its first fix instead of fitting the clip's start, and does nothing about the repeated and stepped
timestamps described below; its GPX route needs one GPX per clip rebased to the video. This writes
the video description JSON that `mapillary_tools upload --desc_path` takes directly: one exact
point per second, with UTC time and heading. Standard library only, no ffmpeg or exiftool
(except `archive`, which re-encodes the clips with ffmpeg).

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
- **`archive SRC DST`**: re-encode the clips for keeping, with the GPS track inside each one as a
  CAMM track. See "Archiving the footage" below.

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
rerunning until every day's summary is clean) and `upload_status.sh`. `archive_all.sh` does the
same remount-and-rerun for `archive`, then verifies everything once.

## Archiving the footage

A Novatek camera writes about 32 Mbps of 4K H.264, so a day of driving is on the order of 100 GB. A plain
ffmpeg re-encode loses the GPS: no tool copies the `gps ` box and its records. `archive`
re-encodes every clip and puts the track that `build` fits back in as a **CAMM** track (Google's Camera Motion Metadata), which Mapillary, Google Street View and
`mapillary_tools` read. It needs `ffmpeg` and `ffprobe` on the `PATH`.

```
novatek-mapillary archive /path/to/card-copy /path/to/archive              # 1440p 10-bit AV1
novatek-mapillary archive /path/to/card-copy /path/to/archive --verify-only  # before freeing space
```

- Every `.mp4` under SRC becomes a clip at the same relative path under DST. The frames and their
  timestamps are kept exactly, with the same count, so tools that address frames by number still
  work. The audio is copied. The camera's burned-in overlay stays, since it's part of the picture.
- `--codec av1|hevc|copy`, `--quality N` (CRF, or CQ on NVENC) or `--bitrate RATE`, `--encoder
  auto|nvenc|software`, `--preset P` (software only), `--height N` (default 1440, never
  upscales, `0` keeps the source size) and `--ten-bit`/`--no-ten-bit` (default on).
  `--codec copy` only remuxes: nothing is lost, and the clip still gets CAMM, tags and a manifest
  line. `--codec h264` exists for tests and old players.
- `creation_time` is the fitted clip start in UTC. The container holds whole seconds; the
  manifest keeps milliseconds. A clip with no usable track still gets one, from its filename in
  the camera's timezone. Such a clip is archived without CAMM, and the
  manifest says why.
- The GPS comes from `build` run in memory over whole days, or from `--desc DIR`, the per-day JSON
  of an earlier `build`. Use the full descriptions, not the pruned ones: the archive is private.
- `DST/<day>.gps.json` keeps the raw embedded records (`parse --json`) in case the fitting is ever
  improved. CAMM holds only the fitted result.
- `--copy-other` also copies `.dat` tracks, photos and anything else unchanged.
- SRC is only read. Nothing is ever deleted there. Free the space by hand once `--verify-only`
  passes on every clip.
- SRC and DST may be nested either way, for example on one disk. With SRC inside DST, the cleanup
  of stray `.part` files never looks inside SRC. With DST inside SRC, the archived clips there are
  not taken for sources. `build` and `archive` agree on what counts as a clip.

### What to encode with

- **The default is 1440p, 10-bit, AV1**, aiming at four to five times smaller than the source.
  At that size 4K blocks visibly while 1440p reads as a clean, slightly soft picture, and 10-bit
  costs nothing. Software SVT-AV1 (`--encoder software`, preset 6) gives the best picture per
  byte but runs well below real time, so a whole trip is a multi-day run. `av1_nvenc` is the fast
  option; it always runs with lookahead and spatial and temporal adaptive quantisation, which save
  bits for nothing. Denoising first doesn't help: it saves nothing and softens the picture.
- **Check `--quality` on your own footage.** The defaults were set on one camera's 4K30 clips.
  Encode two or three whole hard clips, read `mbps` in the manifest, and adjust. With SVT-AV1 stay
  with constant quality rather than `--bitrate`, which undershoots badly on short clips.
- **Small text doesn't survive** at that size: small sign text, distant route numbers and number
  plates are lost at any resolution, 4K included. Large roundels survive. Keeping the small text
  takes roughly twice the bitrate, or `--codec copy`.
- **`--jobs`** defaults to 1 for NVENC, whose consumer cards allow few sessions and are busy with
  one. For software it defaults to one job per 8 CPUs, from 1 to 4, because a single SVT-AV1 encode
  already spreads over many threads. The chosen value is printed at the start.
- Near-transparent encodes save little. Encoding 4K dashcam footage to look identical costs about
  the source's bitrate, because the picture is mostly fine texture and sensor noise that the
  camera's own encoder was already starved on. Re-encoding a lossy source also adds generation loss.

### The CAMM track

The CAMM track follows what `mapillary_tools` 0.14 writes. The choices worth knowing:

- **`time_gps_epoch` holds Unix seconds**, not GPS time since 1980 as the spec's name suggests.
  That is what `mapillary_tools` writes and reads, so a Mapillary upload gets the right times.
- **The last GPS sample runs to the end of the video**, so the track has the video's duration in
  ffprobe and players. `mapillary_tools` gives it zero length. Nothing reads that back.
- Make and model go in `udta` in both spellings: `@mak`/`@mod` (what `mapillary_tools` writes)
  and `©mak`/`©mod` (what ffmpeg, exiftool and Apple's apps read). `©xyz` holds the first fix so
  photo apps can place the clip.
- Each GPS sample also carries velocity east and north, from the RMC speed and course.

**Resuming.** `DST/archive-manifest.jsonl` gets one line per finished clip: the source's size,
mtime and SHA-256, the encoder settings, frame and packet counts, GPS point count, SSIM and the
verify result. A rerun skips clips whose size and mtime match. If they don't, the source is hashed,
so a renamed folder, or a copy that reset mtimes, moves the archives instead of re-encoding them.
A crash leaves at most a `.part` file, which the next run deletes. A clip whose encode fails is
retried on every run. A clip whose archive fails verification is encoded once more from scratch on
the next run. If that fails too, the manifest marks it `gave-up` and later runs skip it, with a
"GAVE UP" line in the summary. Delete its archive file to try again. If the source disk disappears
mid-run, `archive` exits 75 so a wrapper can remount and rerun. `examples/archive_all.sh` does
that. At software speeds a whole trip is a multi-day run: stop it whenever you like, and the next
run picks up where the manifest left off without redoing any clip.

**Verification**, after every clip and with `--verify-only`: the same video and audio packet
counts and first and last timestamps, durations within a frame, and every GPS point matching its
description to 1e-7° and 1 ms. With `mapillary_tools` installed, its own parser checks the points
too. A full decode must print no errors (`--quick` skips that). SSIM is spot-checked on three
frames, as a tripwire for a broken encode, not as a quality score. A downscaled archive is scaled
back up to the source size for it, and the warning threshold is 0.80, which good archives at the
default settings clear comfortably. A remux skips the check.

**Uploading from the archive bypasses `prune`.** `mapillary_tools`' parser reads the archived
CAMM track (the tests check this), so an archived clip could go to `mapillary_tools upload`
without a description. That hasn't been tried as a real upload. More to the point, the track is
the full one, home and overnight stops included. No command prunes an archived clip's track yet, so upload from the card
copy with the pruned descriptions, as above.

Syncing the archive to cloud storage is up to you. Neither the tool nor the scripts assume a
particular service.

## Tests

```
uv run --group dev pytest
```

The tests never use real footage, and no video file is committed: `tests/synth_clip.py` writes
small synthetic clips at test time with the same box layout as real ones (dummy frames, real
`gps ` index and `$GNRMC` records), including stalls, clock steps, clips with no fix, and a day
with stops and a camera-off gap. `python3 tests/synth_clip.py OUT_DIR` writes a demo card. The
`archive` tests use decodable clips made with ffmpeg's test sources, rewritten into the same
layout, and skip when ffmpeg isn't installed. The `mapillary_tools` cross-check runs only when it
is importable: `uv run --group dev --with mapillary_tools pytest tests/test_mp4write.py`.

## Licence

MIT
