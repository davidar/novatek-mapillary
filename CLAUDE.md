# novatek-mapillary

Geotags dashcam clips from Novatek-based cameras (Vantrue and others) for Mapillary. It reads the
GPS records embedded in each MP4 and writes `mapillary_tools` video description JSON, then prunes
out clips that would show where the car parks. Standard library only. `README.md` is the user doc
and holds the format facts and gotchas; read it first.

## Where this came from

The code was extracted from a private project: a 4-day, ~1,800 km road trip uploaded to Mapillary
in Sep/Oct 2026 with these scripts, about 1,100 clips. This repo was started fresh on purpose,
because the private project's history holds personal locations (home, overnight stops). Rules
that follow from that:

- **Nothing personal goes in this repo**: no real coordinates of homes or stays, no place names
  from the trip, no drive labels, local paths, usernames or account names. Personal settings
  belong in the private config (`examples/config.example.sh` shows the shape, with placeholders).
- **No video files at all**, not even synthetic ones (the owner's rule). Tests generate their
  clips at run time with `tests/synth_clip.py`. `.gitignore` blocks `*.mp4`/`*.mov`.
- No description JSON, frames or photos from real footage either.

## Layout

- `src/novatek_mapillary/`
  - `novatek.py`: MP4 box parsing, `gps ` index, `$GNRMC` records, per-clip start fit, clock-step re-stamping.
  - `dat.py`: the optional per-day `.dat` track, which supplies altitude, the fallback track and verify's cross-check.
  - `geo.py`: distance, bearing, timezones, filename times, and `infer_tz`.
  - `build.py`: clips → per-day descriptions; `fix_clock_steps` re-anchors whole clips.
  - `verify.py`: sanity checks.
  - `prune.py`: privacy pruning.
  - `cli.py`: the `parse`/`build`/`verify`/`prune` subcommands.
  - `__main__.py`: entry point for `python -m novatek_mapillary`.
- `tests/`
  - `synth_clip.py`: the synthetic clip and day generator. It also runs as a script and writes a demo card.
  - `test_novatek.py`, `test_pipeline.py`: 23 tests.
- `examples/`: `sync_card.sh`, `upload_all.sh`, `upload_status.sh` and `config.example.sh`. These
  are the scripts used on the trip, made generic and driven by the private config.
- `.github/workflows/tests.yml`: pytest on Python 3.10–3.14 plus `uv build` and shellcheck.

Run the tests with `uv run --group dev pytest` (about 2 s). Build with `uv build`; the wheel installs the
`novatek-mapillary` console script. The example scripts pass shellcheck (`shellcheck -x examples/*.sh`).

## How it was verified

- The algorithms are unchanged from the versions that produced the uploaded trip, apart from
  refactoring into functions that return results.
- On 21 real clips (including one recorded entirely inside a +14 s GPS clock step), `build`
  produced tracks byte-identical to the descriptions that were actually uploaded, and inferred
  the camera timezone correctly.
- The synthetic clips copy a real clip's layout: moov before mdat, one frame per chunk, and
  record k written after frame k·fps−2. Their frames are dummy bytes, so they aren't playable.
- It has only been tested on one Vantrue model (4K30 H.264, 60 s clips). Other Novatek cameras are
  expected to work if they write the same `gps ` box; `novatek-mapillary parse CLIP` shows quickly.
  `moov` before or after `mdat` and 32- or 64-bit `mvhd` are both handled (synthetic tests only).
- The start-time fit is good to about half a second, not better. Records are written once a
  second, so the phase within the second can't be recovered from one clip. The test
  `test_clip_start_is_fitted_within_the_phase_uncertainty` encodes the exact bound.

## Known rough edges

Fine for 0.1:
- `prune` reads each clip's duration from the file, and falls back to the last point.
- `prune.loc_at` is quadratic, which is fine at one day.
- `upload_all.sh`'s `pkill` pattern would also catch any other `mapillary_tools upload` the user
  is running.
