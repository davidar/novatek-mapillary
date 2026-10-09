"""
Privacy pruning: drop the clips recorded around every place the car was parked.

Parking is detected from the description JSON itself (no video is decoded):
  * a gap in the recording timeline (camera powered off, e.g. engine off at a servo or motel),
  * the car stationary for at least min_stop seconds with the camera running,
  * the start and end of the day's recording (leaving home, arriving at the motel).
Each such window is widened by `buffer` seconds on both sides and every clip overlapping it is
removed, so the approach to and departure from the spot go too. Exclusion zones remove anything
within a radius of a point (home, a friend's place, every overnight stop) regardless of speed. A
cut-after-entering zone drops everything from the first moment the car comes within a radius of a
point until the end of the day (arriving in a city where the destination is private: the whole
approach goes).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .geo import hav
from .novatek import mp4_duration

Zone = tuple  # (lat, lon, metres)


def parse_zone(s: str) -> Zone:
    lat, lon, r = (float(x) for x in s.split(","))
    return lat, lon, r


def prune(descs: list[dict], buffer: float = 120.0, min_stop: float = 180.0, gap: float = 5.0,
          exclude_near: list[Zone] = (), cut_after_entering: list[Zone] = (),
          tz: dt.timezone = dt.timezone.utc, label: str = "day"):
    """
    Returns (kept descriptions, report lines). The report gives every window cut and why, with
    times in `tz`, and lists the dropped clips on its last line.
    """
    clips = []
    for d in descs:
        pts = d["MAPGPSTrack"]
        start = pts[0][5] - pts[0][0] / 1000.0
        dur = mp4_duration(Path(d["filename"])) or (pts[-1][0] / 1000.0)
        clips.append({"d": d, "start": start, "end": start + dur, "pts": pts})
    clips.sort(key=lambda c: c["start"])
    if not clips:
        return [], [f"{label}: no clips"]

    def loc_at(t):
        best = None
        for c in clips:
            for p in c["pts"]:
                if best is None or abs(p[5] - t) < abs(best[5] - t):
                    best = p
        return best[2], best[1]

    windows = []  # (t0, t1, reason)
    windows.append((clips[0]["start"], clips[0]["start"], "day start"))
    windows.append((clips[-1]["end"], clips[-1]["end"], "day end"))
    for p, n in zip(clips, clips[1:]):
        g = n["start"] - p["end"]
        if g > gap:
            windows.append((p["end"], n["start"], f"camera off {g / 60:.0f} min"))
    # stationary runs over the merged point stream
    stream = sorted((p[5], p[2], p[1]) for c in clips for p in c["pts"])
    run_start = None
    for (t0, la0, lo0), (t1, la1, lo1) in zip(stream, stream[1:]):
        still = (t1 - t0) <= 5 and hav(la0, lo0, la1, lo1) / max(t1 - t0, 1e-6) < 0.7
        if still and run_start is None:
            run_start = t0
        if not still and run_start is not None:
            if t0 - run_start >= min_stop:
                windows.append((run_start, t0, f"stationary {(t0 - run_start) / 60:.0f} min"))
            run_start = None
    if run_start is not None and stream[-1][0] - run_start >= min_stop:
        windows.append((run_start, stream[-1][0], f"stationary {(stream[-1][0] - run_start) / 60:.0f} min"))
    for lat, lon, r in cut_after_entering:
        inside = [p for p in stream if hav(p[1], p[2], lat, lon) <= r]
        if inside:
            windows.append((inside[0][0], clips[-1]["end"], f"entered {r / 1000:g} km zone"))
    windows.sort()

    dropped = {}
    for t0, t1, why in windows:
        lo, hi = t0 - buffer, t1 + buffer
        for c in clips:
            if c["start"] < hi and c["end"] > lo:
                dropped.setdefault(Path(c["d"]["filename"]).name, why)
    for c in clips:
        for lat, lon, r in exclude_near:
            if any(hav(p[2], p[1], lat, lon) <= r for p in c["pts"]):
                dropped.setdefault(Path(c["d"]["filename"]).name, f"within {r:.0f} m of {lat:.4f},{lon:.4f}")

    kept = [c["d"] for c in clips if Path(c["d"]["filename"]).name not in dropped]
    fmt = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")
    gb = lambda ds: sum(d.get("filesize", 0) for d in ds) / 1e9
    lines = [f"{label}: kept {len(kept)} of {len(clips)} clips ({gb(kept):.1f} of {gb([c['d'] for c in clips]):.1f} GB); "
             f"buffer {buffer:.0f}s, min stop {min_stop:.0f}s"]
    for t0, t1, why in windows:
        n = sum(1 for c in clips if c["start"] < t1 + buffer and c["end"] > t0 - buffer)
        lat, lon = loc_at(t0 if why.startswith("entered") else (t0 + t1) / 2)
        span = fmt(t0) if t1 - t0 < 1 else f"{fmt(t0)}–{fmt(t1)}"
        lines.append(f"  {span:<18} {why:<20} at {lat:.5f},{lon:.5f}  -> {n} clips cut ({fmt(t0 - buffer)}–{fmt(t1 + buffer)})")
    zone_hits = [k for k, v in dropped.items() if v.startswith("within")]
    if zone_hits:
        lines.append(f"  exclusion zones: {len(zone_hits)} clips")
    lines.append("  dropped clips: " + ", ".join(sorted(dropped)) if dropped else "  nothing dropped")
    return kept, lines
