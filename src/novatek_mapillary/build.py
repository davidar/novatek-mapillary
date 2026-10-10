"""
Build mapillary_tools video description JSON from Novatek dashcam clips.

GPS source: the $GNRMC records embedded in each clip (see novatek.py), exact per second with UTC
time. The optional .dat track (see dat.py) supplies altitude, and is the fallback for clips whose
embedded records can't be read.

Output: one description JSON per day (camera-clock date) plus all.json and skipped.json, in the
format consumed by `mapillary_tools upload --desc_path`:
  MAPGPSTrack point = [ms_from_video_start, lon, lat, alt, angle|null, epoch_seconds]
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from pathlib import Path

from . import novatek
from .dat import default_dat_dir, load_track
from .geo import bearing, format_tz, infer_tz, name_time


def _files(root: Path, exclude: Path | None = None):
    """Every file under `root`, skipping dot-files and dot-folders, and everything under `exclude`
    when it is inside `root` (an output folder kept inside the card copy)."""
    base = root.resolve()
    ex = exclude.resolve() if exclude is not None else None
    if ex is not None and (ex == base or base not in ex.parents):
        ex = None  # not inside root: nothing to skip (a root inside `exclude` is fine)
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if ex is not None and (base / rel == ex or ex in (base / rel).parents):
            continue
        if p.is_file():
            yield p


def find_clips(root: Path, exclude: Path | None = None) -> list[Path]:
    """Every .mp4 under `root` (any case, any folder: Vantrue's Normal/ and Event/ interleave into
    one timeline), in filename-time order. `build` and `archive` agree on this list."""
    clips = [p for p in _files(root, exclude) if p.suffix.lower() == ".mp4"]
    return sorted(clips, key=lambda p: (name_time(p.name) or dt.datetime.min, p.name))


def find_other_files(root: Path, exclude: Path | None = None) -> list[Path]:
    """Every file under `root` that find_clips doesn't return (for `archive --copy-other`)."""
    return sorted(p for p in _files(root, exclude) if p.suffix.lower() != ".mp4")


def fix_clock_steps(descs: list[dict], min_step: float = 3.0, verbose: bool = True) -> int:
    """
    The GPS clock sometimes steps by whole seconds for about a minute and steps back. Records
    inside a clip are re-stamped by novatek.clip_track, but a clip whose records ALL fall inside
    the step gets a start time off by the step. Such a clip overlaps one neighbour and leaves a gap
    to the other, which prune reads as "camera off". Recording is continuous, so when the gap
    before a clip and the gap after it are equal and opposite whole seconds, shift that clip's
    epochs back into line. Positions (video ms) are untouched. `descs` need a "_dur" key.
    """
    def start(d):
        p = d["MAPGPSTrack"][0]
        return p[5] - p[0] / 1000.0

    n = 0
    for i in range(1, len(descs) - 1):
        prev, cur, nxt = descs[i - 1], descs[i], descs[i + 1]
        before = start(cur) - (start(prev) + prev["_dur"])
        after = start(nxt) - (start(cur) + cur["_dur"])
        if abs(before) >= min_step and abs(before + after) <= 1.5:
            step = round((before - after) / 2)
            for p in cur["MAPGPSTrack"]:
                p[5] -= step
            if verbose:
                print(f"  {Path(cur['filename']).name}: GPS clock step, epochs shifted {-step:+d} s")
            n += 1
    return n


def build(clips: list[Path], tz: dt.timezone | None = None, dat_dir: Path | None = None,
          make: str | None = None, model: str | None = None, lead: float = 0.0,
          min_points: int = 5, dat_offset: float = -1.0, verbose: bool = True,
          embedded: dict | None = None):
    """
    Returns (per_day, skipped, tz): per_day maps 'YYYYMMDD' to a list of descriptions sorted by
    filename. `tz` is the camera clock's UTC offset; None infers it from the embedded fixes.
    `embedded` may map clips to novatek.clip_track(clip, min_points=1) results already read
    (None for unreadable), so the clips aren't read again.
    """
    say = print if verbose else (lambda *a, **k: None)

    # Pass 1: the embedded records, which need no clock assumptions.
    given = embedded or {}
    embedded = {}
    for clip in clips:
        if clip in given:
            fit = given[clip]
            embedded[clip] = fit if fit is not None and len(fit[1]) >= min_points else None
            continue
        try:
            embedded[clip] = novatek.clip_track(clip, min_points=min_points)
        except Exception as ex:  # unreadable container: fall through to the .dat
            say(f"  {clip.name}: embedded GPS unreadable ({ex}); trying .dat")
            embedded[clip] = None

    if tz is None:
        tz = infer_tz((emb[0], name_time(c.name)) for c, emb in embedded.items()
                      if emb is not None and name_time(c.name) is not None)
        if tz is None:
            raise SystemExit("can't infer the camera clock's timezone (no clip has both embedded GPS and a "
                             "dated filename); pass --tz")
        say(f"camera clock: UTC{format_tz(tz)} (inferred from the embedded GPS)")

    track = load_track(dat_dir, tz, dat_offset, verbose=verbose)
    epochs = [p[0] for p in track]
    if track:
        say(f".dat track: {len(track)} points, "
            f"{dt.datetime.fromtimestamp(epochs[0], tz)} -> {dt.datetime.fromtimestamp(epochs[-1], tz)}")
    elif dat_dir is not None:
        say(f"warning: no .dat track under {dat_dir}; altitude will be null and no fallback")

    def dat_alt(epoch: float):
        i = bisect.bisect_left(epochs, epoch)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(track) and abs(track[j][0] - epoch) <= 2.0:
                if best is None or abs(track[j][0] - epoch) < abs(track[best][0] - epoch):
                    best = j
        return track[best][3] if best is not None else None

    per_day: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    source_counts = {"embedded": 0, "dat": 0}
    for clip in clips:
        named = name_time(clip.name)
        emb = embedded[clip]
        if emb is not None:
            s0, fixes, dur = emb
            gps_track = []
            for r in fixes:
                ms = (r.epoch - s0) * 1000.0
                if ms < -1500 or ms > dur * 1000 + 1500:
                    continue
                angle = round(r.course, 1) if (r.course is not None and r.speed > 1.0) else None
                gps_track.append([max(0.0, round(ms, 1)), r.lon, r.lat, dat_alt(r.epoch), angle, r.epoch])
            src = "embedded"
        else:
            # Fallback: the .dat track, by filename start time. Its per-row lag varies 0–2 s
            # between camera sessions, so this is second best.
            dur = novatek.mp4_duration(clip)
            if named is None or dur is None:
                skipped.append({"filename": str(clip), "reason": "no embedded fixes, and no dated filename or mvhd"})
                continue
            s0 = named.replace(tzinfo=tz).timestamp()
            lo = bisect.bisect_left(epochs, s0 - lead)
            hi = bisect.bisect_right(epochs, s0 + dur + lead)
            pts = track[lo:hi]
            if len(pts) < min_points:
                skipped.append({"filename": str(clip), "reason": f"no embedded fixes and only {len(pts)} .dat "
                                f"points in [{named.isoformat()} +{dur:.1f}s]"})
                continue
            gps_track = []
            for i, p in enumerate(pts):
                angle = None
                if p[4] > 1.0:
                    nxt = pts[i + 1] if i + 1 < len(pts) else None
                    prv = pts[i - 1] if i > 0 else None
                    if nxt is not None and nxt[0] - p[0] <= 5:
                        angle = round(bearing(p[1], p[2], nxt[1], nxt[2]), 1)
                    elif prv is not None and p[0] - prv[0] <= 5:
                        angle = round(bearing(prv[1], prv[2], p[1], p[2]), 1)
                gps_track.append([max(0.0, round((p[0] - s0) * 1000.0, 1)), p[2], p[1], p[3], angle, p[0]])
            src = "dat"
        if len(gps_track) < min_points:
            skipped.append({"filename": str(clip), "reason": f"only {len(gps_track)} usable points ({src})"})
            continue
        source_counts[src] += 1
        desc = {
            "filename": str(clip.resolve()),
            "filetype": "video",
            "filesize": clip.stat().st_size,
            "MAPGPSTrack": gps_track,
        }
        if make:
            desc["MAPDeviceMake"] = make
        if model:
            desc["MAPDeviceModel"] = model
        desc["_dur"] = dur
        day = (named.strftime("%Y%m%d") if named is not None
               else dt.datetime.fromtimestamp(gps_track[0][5] - gps_track[0][0] / 1000, tz).strftime("%Y%m%d"))
        per_day.setdefault(day, []).append(desc)

    fixed = 0
    for descs in per_day.values():
        descs.sort(key=lambda d: (name_time(Path(d["filename"]).name) or dt.datetime.min, Path(d["filename"]).name))
        fixed += fix_clock_steps(descs, verbose=verbose)
        for d in descs:
            d.pop("_dur")
    if fixed:
        say(f"re-anchored {fixed} clip(s) recorded entirely inside a GPS clock step")
    say(f"{sum(map(len, per_day.values()))} clips ({source_counts['embedded']} from embedded GPS, "
        f"{source_counts['dat']} from .dat), skipped {len(skipped)}")
    return per_day, skipped, tz


def write(per_day: dict[str, list[dict]], skipped: list[dict], out: Path, verbose: bool = True):
    out.mkdir(parents=True, exist_ok=True)
    everything = []
    for day, descs in sorted(per_day.items()):
        (out / f"{day}.json").write_text(json.dumps(descs, separators=(",", ":")))
        everything.extend(descs)
        if verbose:
            gb = sum(d["filesize"] for d in descs) / 1e9
            print(f"{day}: {len(descs):4d} clips, {gb:6.1f} GB -> {out / f'{day}.json'}")
    (out / "all.json").write_text(json.dumps(everything, separators=(",", ":")))
    (out / "skipped.json").write_text(json.dumps(skipped, indent=1))
    if verbose:
        for s in skipped[:10]:
            print("  skipped:", Path(s["filename"]).name, "-", s["reason"])


def run(root: Path, out: Path, tz=None, dat_dir: Path | None = None, **kw):
    clips = find_clips(root)
    if dat_dir is None:
        dat_dir = default_dat_dir([root, *{c.parent for c in clips}])
    per_day, skipped, tz = build(clips, tz, dat_dir, **kw)
    write(per_day, skipped, out, verbose=kw.get("verbose", True))
    return per_day, skipped, tz
