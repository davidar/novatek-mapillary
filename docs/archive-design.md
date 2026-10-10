# Design: `novatek-mapillary archive`

Status: implemented 2026-10-10 (`archive.py`, `mp4write.py`); kept as the design record, with
the measured encode results folded into "Why" and "Decisions". Read `README.md` and
`CLAUDE.md` first (format facts, privacy rules, no video files in the repo). Reviewed on
2026-10-10 against the source of `mapillary_tools` 0.14.7; the facts below that cite it were
checked there, not remembered.

## Why

A Novatek camera writes 4K30 H.264 at about 32 Mbps, so a day of driving is on the order of
100 GB. Once the clips are uploaded (or edited), the owner wants to keep the footage but not at
that size.

A re-encode that looks the same as the source saves little (see "Decisions"). The camera's 4K is
mostly fine texture and sensor noise its own encoder was already starved on, so encoding it to
look identical costs about the source's bitrate, and re-encoding a lossy source also pays
generation loss. The settled choice gives up the full 4K detail instead: **1440p 10-bit AV1**,
about 4–5× smaller, a clean but slightly soft picture. `--codec copy` remuxes with no loss at
all, for when the point is the GPS rather than the size.

Whatever the codec, a plain `ffmpeg` re-encode **throws away the GPS**: the `gps ` index box and
the `freeGPS ` records are camera-specific and no tool copies them. So the archive should carry the GPS in a standard form instead: a **CAMM** track,
which Mapillary, Google Street View and `mapillary_tools` all read. The fixes this package already
makes (stalled timestamps, clock steps, the half-second start fit) then come along for free, and an
archived clip can be uploaded to Mapillary later without its description JSON.

## What it does

```
novatek-mapillary archive SRC DST [--desc DIR] [--codec av1|hevc|copy] [--quality N | --bitrate RATE]
                                  [--encoder auto|nvenc|software] [--ten-bit] [--height N] [--no-faststart]
                                  [--copy-other] [--make M] [--model M] [--jobs N]
                                  [--verify-only] [--quick] [--dry-run]
```

For every `.mp4` under `SRC` (any folder layout, case-insensitive extension), write one archived
clip at the same relative path under `DST`:

- **Video** re-encoded with the same frames: same count, same timestamps, same frame rate, no
  frames dropped or duplicated (`-fps_mode passthrough`, and `-video_track_timescale` set to the
  source video track's timescale, read from its `mdhd`; without that, ffmpeg picks its own
  timescale and may add an edit list, so the first PTS can shift even though the frame count
  matches). Downstream tools that address frames by number (an editing pipeline, keyframe maps)
  keep working against the archive.
- **Audio** copied as is (`-c:a copy`; Vantrue writes 64 kbps AAC, not worth re-encoding). A clip
  with no audio track is fine; ffmpeg just writes none.
- **GPS** as a CAMM track (below).
- **Metadata**, all written by the Python step except `creation_time`:
  - `creation_time` set to the fitted clip start in UTC, passed to ffmpeg as
    `-metadata creation_time=…`, which sets `mvhd`, `tkhd` and `mdhd` in one go. The originals
    have none, and their filenames are in camera-local time. A clip **without** a GPS track still
    gets one: the filename time in the camera timezone (`--tz`, or the one `build` inferred). The
    manifest records which of the two it was.
  - Make and model in `udta`, in **both** spellings: `@mak`/`@mod` (a bare string, what
    `mapillary_tools` writes and what Ricoh cameras use) and `©mak`/`©mod` (the QuickTime
    user-data text atom: `uint16 size, uint16 language, text`, what Insta360 writes and what
    ffmpeg, exiftool and Apple's apps read). `mapillary_tools` reads both, parsing the `©` form
    with the 4-byte header, so the header must be there. Don't ask ffmpeg to write them: its mp4
    muxer puts make under `udta/meta/ilst`, which `mapillary_tools` doesn't look at, and drops
    model altogether.
  - `©xyz` (ISO 6709, first fix, same text-atom header), so photo apps place the clip.
- **The camera's burned-in overlay** (date, time, position, speed) stays; it's part of the picture.

With `--copy-other`, every non-MP4 file under `SRC` is copied unchanged: `.dat` tracks, photos,
checksum lists. Without it, only clips are written.

`archive` **never deletes or modifies anything under `SRC`**, and it opens `SRC` read-only.
Freeing the space is a separate, manual step, taken once `--verify-only` passes on every clip.

## Where the GPS comes from

The archive should hold the same track that would be uploaded, with every fix applied, so the
source of truth is `build`'s output:

1. With `--desc DIR`: the per-day description JSON from a previous `build` (`DIR/*.json`), matched
   to clips by resolved path, falling back to the filename. `filesize` must match the source; if
   it doesn't, warn and rebuild that clip's track.
2. Without `--desc`: run `build.build()` on the clips in memory (same options: `--tz`,
   `--dat-dir`). That includes `fix_clock_steps`, which needs whole days, so group by day first;
   don't build clip by clip.

Description points are `[ms_into_video, lon, lat, alt, angle, epoch]`. Clips `build` skipped (no
fix at power-on, fix lost while parked) are still archived, **without** a CAMM track, and the
manifest says so. Their footage is as worth keeping as any other.

The pruned descriptions (`desc/upload/`) are for uploading, not archiving. Use the full ones: the
archive is private, and the owner decides later what to share. Note that this means an archived
clip uploaded straight from its CAMM track bypasses `prune`; see "Out of scope".

**The raw records are kept too.** Next to the clips, one `DST/<day>.gps.json` per day holds
`parse --json` output for every clip of that day (a few KB per clip). It preserves the raw
`$GNRMC` records if the fitting is ever improved; CAMM holds only the fitted result.

## The CAMM track

Spec: Google's Camera Motion Metadata
(<https://developers.google.com/streetview/publish/camm-spec>). Match what `mapillary_tools`
0.14 writes, since Mapillary is the main consumer. Its builder is
`mapillary_tools/camm/camm_builder.py` and its parser `camm_parser.py`; read both. Facts from them:

- One `trak` with `hdlr` handler `camm` (name `CameraMetadataMotionHandler`), sample entry
  format `camm`, `mdhd` version 1, `tkhd` version 0 with duration all ones, and a
  self-referencing `dref`. Media timescale `max(1000, movie_timescale)`, as it does, so sample
  times are exact to 1 ms; the description's times are rounded to 0.1 ms, so nothing is lost
  that the verify tolerance (1 ms) would notice.
- Each sample is `2 bytes reserved, uint16le type, payload`, little-endian throughout.
- **Type 6 (GPS)**, one sample per fix:
  - `float64 time_gps_epoch`, `int32 gps_fix_type`, `float64 lat`, `float64 lon`, `float32 alt`
  - then `float32` horizontal and vertical accuracy, `velocity_east`, `velocity_north`,
    `velocity_up` and `speed_accuracy`

  `mapillary_tools` puts **Unix** epoch seconds in `time_gps_epoch`, even though the spec's name
  suggests GPS time since 1980: `uploader.prepare_camm_info` copies the description's sixth
  element straight into it. Do the same, so a Mapillary upload gets the right times. Check it
  against Mapillary's parser in a test, and note the choice in the README.
- Fix type is 3 with altitude and 2 without. Velocity east/north comes from the RMC speed and
  course; `read_records` has both, so it's better than the zeros `mapillary_tools` writes. Set
  `velocity_up` and the accuracies to 0 (unknown), as it does.
- Sample times are the description's video times (`ms_into_video / 1000`). Each sample's `stts`
  delta runs to the next fix, and **the last one runs to the end of the video**. This deviates
  from `mapillary_tools`, which writes 0 for the last sample: its `convert_telemetry_to_raw_samples`
  never sees the video duration (it multiplexes GPS, accelerometer and gyro from any source), and
  nothing reads the value back (its parser takes each sample's time from the cumulative `stts`).
  Running to the end gives the track the video's duration, which is what ffprobe and players show.
  The verify step must not compare the track's duration against what an upload would carry.
- **Edit list.** If the first fix is after 0, add an empty edit (`elst` `media_time = -1`) followed
  by the real one, as `_create_edit_list_from_points` does. The real entry's duration **must cover
  every sample time**: `mapillary_tools`' parser keeps only samples that fall inside a non-empty
  `elst` entry (`_filter_telemetry_by_elst_segments`) and drops the rest silently. Its own builder
  sets that duration to `last fix − first fix`; since our last sample runs to the end of the
  video, set ours to `video end − first fix`. A round-trip test needs a clip whose first fix is
  after 0, so the two-entry list is exercised.
- Optionally also write type 5 (minimal GPS: lat, lon, alt as `float64`) if some reader turns out
  to need it. Not by default.

### Getting the track into the file (standard library only)

`ffmpeg` can't make a CAMM track from data, so do it in two steps:

1. `ffmpeg` writes video and audio to `DST/…/clip.mp4.part`, **without** `+faststart`, so the
   layout is `ftyp | (free) | mdat | moov`, `moov` last.
2. Python appends the CAMM track:
   - read `moov` (it is the last box; refuse the file if it isn't)
   - truncate the file where `moov` starts
   - write a new `mdat` holding the CAMM samples
   - write the new `moov` (old tracks unchanged, plus the CAMM `trak`, `mvhd.next_track_ID`
     bumped, `udta` with make/model/xyz in both forms)
   - set the CAMM `stco` to point into the new `mdat`

   Nothing before the new `mdat` moves, so the other tracks' chunk offsets stay valid. Use `co64`
   if an offset passes 2³²; a 60 s clip won't, but a merged file could.
3. **Faststart, on by default** (`--no-faststart` to skip): move `moov` to the front and add its
   size to every `stco`/`co64` entry in every track. It helps streaming previews (Dropbox, phones)
   and costs a rewrite of the file. The offset-shifting function is shared with the test clip
   generator (see "Tests"), so write it first and in isolation.
4. `fsync`, rename `.part` → `.mp4`, and **only then** append the manifest line. A crash before the
   rename leaves only a `.part`, which the next run deletes and redoes; a crash after the rename
   but before the manifest line leaves a finished clip the next run re-verifies and records.

`novatek.py` already walks boxes (`_boxes`, `_find`). Put the writer in a new `mp4write.py`, so
`novatek.py` stays a reader. Do it with plain byte-level box building; no `construct` dependency.

## Encoding

The defaults are 1440p, 10-bit, AV1 (see "Decisions"). Software SVT-AV1 is the primary path; NVENC
is the fast option. Both codecs stay, plus `--codec copy` (remux only: no quality, encoder,
`--preset` or `--height` options apply, SSIM is not checked). `--height N` (default 1440, `0`
keeps the source size) applies `-vf scale=-2:N` before encoding and never upscales; SSIM then
compares the archive scaled back up to the source size.

| | `--encoder nvenc` | `--encoder software` |
|---|---|---|
| AV1 | `av1_nvenc -preset p6 -tune hq -multipass fullres -rc vbr -rc-lookahead 32 -spatial-aq 1 -temporal-aq 1 -cq 44 -b:v 0` | `libsvtav1 -preset 6 -crf 38` |
| HEVC | `hevc_nvenc` with the same options, `-cq 37` | `libx265 -preset slow -crf 24` |

`--preset P` passes through to the software encoders (an SVT-AV1 number, a libx265 name) and is
rejected for NVENC, which always runs p6.

- `--encoder auto` probes `ffmpeg -encoders` and does a 1-frame test encode, picking NVENC if it
  works. Decode with `-hwaccel cuda` when NVENC is used.
- **Constant quality, not constant bitrate**, by default: motorway footage at speed is far
  harder to compress than a parked car, so a fixed bitrate either wastes bytes or smears. Keep
  `--bitrate` for a hard size budget; then use `-maxrate` at about 1.5× the target.
- 10-bit (`p010` / `yuv420p10le`) by default; `--no-ten-bit` turns it off. The source is 8-bit,
  but 10-bit costs nothing with these encoders and bands less.
- GOP: a keyframe every 2 s (`-g` = 2 × fps), so archived clips still seek fast and an editing
  pipeline can pull keyframes cheaply.
- `--jobs N`: clips in parallel. Default 1 for NVENC (consumer cards allow few sessions, and one
  job keeps the encoder busy) and for a remux; for software, `max(1, min(4, cpus // 8))`, because
  one SVT-AV1 encode at 4K already uses many threads. The chosen value is printed at the start.
- Speed: SVT-AV1 at preset 6 runs well below real time, so a whole trip is a multi-day run on
  one machine; resuming makes that safe.

## Verification (`--verify-only`, and after every clip)

For each clip, compare source and archive, and record the result in the manifest:

- **Frames:** video packet count and the first/last PTS are identical (`ffprobe -count_packets`).
  Duration is within one frame. The PTS check depends on `-video_track_timescale` above.
- **Audio:** packet count is identical.
- **GPS:** parse the CAMM track back with our own reader, and with `mapillary_tools`'
  `camm_parser` if it is installed. Every fix must match the description: lat/lon to 1e-7°,
  time to 1 ms, the same point count. The `mapillary_tools` pass is the one that catches an edit
  list that doesn't cover the samples.
- **Decodes cleanly:** `ffmpeg -v error -i out -f null -` prints nothing. This is a full decode,
  so `--quick` skips it.
- **Quality spot check:** SSIM (ffmpeg's `ssim` filter) on a few frames
  per clip, say at 10%, 50% and 90%. Record the number; warn below a threshold set from the test
  encodes. It is a tripwire for a broken encode, not a quality metric to optimise.
- **Size:** record source and archive bytes.

## Resuming and the manifest

`DST/archive-manifest.jsonl` has one line per finished clip:

- relative path, source size, mtime and **SHA-256**
- archive size
- encoder settings
- `creation_time` and where it came from (fitted start or filename)
- frames, audio packets, CAMM points (or `"camm": null` and the reason)
- SSIM, the verify result and the time taken
- `status` and `attempts` (encodes of this source so far)

Statuses: `ok`; `failed` (the encode itself failed: retried on every run); `verify-failed` (an
archive exists but didn't verify: the next run encodes it once more from scratch); `gave-up` (the
retry didn't verify either: skipped from then on, and the summary says so; deleting the archive
file starts over). A `--verify-only` failure on an `ok` clip makes it `verify-failed`, so it gets
its one retry. Only `ok` and `gave-up` count as done, and only their hashes are used for moves.

The source hash is computed **once**, when the clip is first archived, and doubles as a checksum
of the original. It is not recomputed on every rerun: hashing hundreds of gigabytes just to
decide what to skip takes many minutes. On start, for each clip under `SRC`:

1. Find the manifest line with the same relative path. If it is done (`ok` or `gave-up`), its size
   and mtime match the source and the archive file exists with the recorded size, skip the clip.
2. Otherwise hash the source. If a line with that hash exists (same path or another: the folder
   was renamed, or the copy reset mtimes), and its archive file exists, move the archive to the
   new relative path if needed, append a fresh line, and skip the encode.
3. Otherwise encode.

Delete stray `.part` files first, never looking inside `SRC` (which may sit inside `DST`; a `DST`
inside `SRC` is likewise left out of the source list). `Ctrl-C` finishes or abandons the current
clip cleanly. Each source clip's container is parsed once per run, and the result is shared by
the sidecar, `build` and the encode, which matters on a USB disk with thousands of clips.

**Removable disks.** Sources often live on a USB disk that drops out now and then: on the trip, a
USB SSD disappeared for a few seconds several times a day. A read error alone doesn't say whether
the disk went or the clip is bad, so after any `OSError` on a source file check whether `SRC` is
still a mount point (or, if it isn't one, whether the clip's parent directory still exists). If
the source is gone, stop with exit code 75 (`EX_TEMPFAIL`), so a wrapper can wait for the disk,
remount it and rerun. If it's still there, record the clip as failed in the manifest and go on.
The `upload_all.sh` example already does the wait-and-rerun for uploads; `examples/archive_all.sh`
can follow it.

## Example script

`examples/archive_all.sh`, driven by the private config (new variables, placeholders only in
`config.example.sh`):

- `ARCHIVE_DST`, `ARCHIVE_ARGS`. Where `DST` lives (another disk, free space on the source disk
  mounted read-write, a per-day batch on the laptop) is the user's choice per run; the tool and
  the script don't care, they only need the path.
- remount the source read-only if it dropped, rerun until the manifest covers every clip
- then run `--verify-only` once over everything and print a summary: clips, GB before/after,
  ratio, clips without GPS, the worst SSIM

Syncing the result to cloud storage stays the user's business: say so in the README, and don't
script a particular service.

## Tests

No video in the repo, so the tests make their own:

- **A decodable synthetic Novatek clip.** `ffmpeg -f lavfi -i testsrc2=size=320x180:rate=30`
  plus a `sine` audio source, 3–5 s, encoded with a software encoder. Then a helper in
  `tests/synth_clip.py` adds a `gps ` index box to `moov` and `freeGPS ` records. The records
  **must be interleaved with the frames**, not put in a trailing box: `read_records` maps each
  record to the chunk whose offset precedes it, so records after the last chunk all get the last
  chunk's video time and the start fit collapses. So the helper rewrites ffmpeg's `mdat`, inserting
  record k after the chunk holding frame k·fps−2, and shifts every `stco`/`co64` entry in every
  track by the bytes inserted before it. That is the same offset-shifting function faststart
  needs, used with per-chunk deltas instead of one constant, so `mp4write.py` provides it and
  the test helper calls it. Skip these tests (`pytest.importorskip`-style) when `ffmpeg` is
  absent; add an `apt-get install ffmpeg` step to the CI workflow so they run there.
- `archive` on it with `--encoder software` (`libsvtav1`, or `libx264` as a stand-in where SVT
  isn't built in):
  - frame and audio packet counts and first/last PTS match the source
  - the CAMM track round-trips through our reader
  - and through `mapillary_tools`' parser when installed (a separate optional test), on a clip
    whose first fix is after 0, so the edit list is checked
  - `creation_time` is right, for a clip with GPS and for one without
  - `@mak`, `©mak` and `©xyz` are present and `ffprobe` reports make and model
- **The box writer in isolation:**
  - append a CAMM track to a tiny ffmpeg-made MP4; `ffprobe` lists a data stream with handler
    `camm`, and the other tracks still decode
  - faststart moves `moov` and the file still decodes with identical packets; `--no-faststart`
    leaves it where it was
- **Resuming:** kill after the first of three clips (simulate with a failing encoder for clip 2),
  rerun, check that clip 1 isn't re-encoded and nothing stale is left. Then rename the source
  folder and rerun: nothing is re-encoded, the archives move, the manifest gains fresh lines.
- **A clip with no fixes** is archived without CAMM and flagged.
- **A clock-step day** (the existing synthetic day schedule): the CAMM times equal the fixed
  description times, not the raw ones.

## Decisions

Settled (2026-10-10):

- **Faststart** on by default, `--no-faststart` to skip.
- **Raw GPS sidecar**: yes, one `<day>.gps.json` per day next to the clips.
- **Manifest identity**: full SHA-256 once, cached by path + size + mtime (above).
- **Last CAMM sample** runs to the end of the video; **make/model** in both spellings.
- **Where the output goes** is a per-run choice; nothing in the tool or script assumes one.

**Codec, size and quality** (settled 2026-10-10, from short 4K motorway and town probes scored
with VMAF and SSIM against the source):

- **1440p, 10-bit, AV1** by default, aiming at 4–5× smaller than the source. At that size 4K
  blocks visibly while 1440p reads as a clean, slightly soft picture. 10-bit is free and harmless.
- **Software SVT-AV1 preset 6 is the quality option**: the best picture per byte, well below real
  time. **CRF, not bitrate**: SVT's `-b:v` VBR undershoots badly on short clips. Users check the
  manifest's `mbps` on their own clips and adjust `--quality`.
- **NVENC is the fast option**: `av1_nvenc`, always with `-rc-lookahead 32 -spatial-aq 1
  -temporal-aq 1`, which save bits at the same SSIM for nothing; `hevc_nvenc` gets the same flags.
- Not worth it: denoising before encoding (`hqdn3d`: no saving at fixed quality, and softer);
  preset p7 and tune uhq on NVENC; tuning the keyframe interval (it barely matters; 2 s stays);
  near-transparent 4K, which costs about the source's bitrate because the picture is mostly
  texture and sensor noise.
- **Legibility**: at this size small sign text, distant route numbers and number plates are lost
  in every variant, 4K included; large roundels survive. Keeping them needs roughly twice the
  bitrate, or `--codec copy`.
- **SSIM warning at 0.80**: a tripwire, well below what acceptable encodes score.
- **`--jobs`** defaults as in "Encoding".

Not probed: dusk and rain footage; whether HEVC's wider playback (cloud previews, phones) is
worth more than AV1's size; libx265's settings, untuned.

## Out of scope

- Concatenating clips into per-drive files. Possible later; per-clip keeps the names, the
  resuming and the frame addressing simple.
- Deleting sources.
- Uploading. An archived clip with CAMM can be passed to `mapillary_tools upload` directly, which
  is worth a sentence in the README once tested. But the archive holds the **full** track, so
  uploading from it needs a privacy step first: either a `prune` that reads CAMM, or descriptions
  rebuilt from the manifest and the sidecar and pruned as today. Say so in the README.
