"""
Sanity-check a description JSON before upload. Reads only the JSON and the optional .dat track
(no clips), so it runs in a second or two for a whole day.

Per clip it checks: enough points, video offsets and epochs strictly increasing, no gap over
max_gap seconds, implied speed between points under max_speed m/s, and that the first point's
UTC time is within 30 s of the clip filename read in the camera's timezone (a clip an hour off
means someone changed the camera clock mid-day).

Independent cross-check: each point is compared with the .dat row nearest in time (best of
±2 s, because .dat rows lag the fix by 0–2 s depending on session). The .dat is written by a
different code path from the embedded records, so a systematic time/zone error shows up here as
tens of metres per clip, while a healthy day reads well under 5 m median.
"""
from __future__ import annotations

import bisect
import datetime as dt
from pathlib import Path

from .geo import format_tz, hav, infer_tz, name_time


def clip_start(d: dict) -> float:
    p = d["MAPGPSTrack"][0]
    return p[5] - p[0] / 1000.0


def verify(descs: list[dict], track: list = (), tz: dt.timezone | None = None,
           max_gap: float = 10.0, max_speed: float = 60.0, dat_tolerance: float = 5.0):
    """Returns (summary lines, problems) where problems is [(clip name, [issue, ...])]."""
    track = list(track)
    epochs = [p[0] for p in track]
    lines = []
    if tz is None:
        tz = infer_tz((clip_start(d), name_time(Path(d["filename"]).name)) for d in descs
                      if name_time(Path(d["filename"]).name) is not None)
        if tz is not None:
            lines.append(f"  camera clock: UTC{format_tz(tz)} (inferred)")

    problems = []
    name_deltas, dat_medians, all_dat = [], [], []
    for d in descs:
        name = Path(d["filename"]).name
        pts = d["MAPGPSTrack"]
        issues = []
        if len(pts) < 5:
            issues.append(f"only {len(pts)} points")
        for (ms0, lon0, lat0, *_r0, e0), (ms1, lon1, lat1, *_r1, e1) in zip(pts, pts[1:]):
            if ms1 <= ms0 or e1 <= e0:
                issues.append("non-increasing time"); break
            if e1 - e0 > max_gap:
                issues.append(f"gap {e1 - e0:.0f}s"); break
            v = hav(lat0, lon0, lat1, lon1) / (e1 - e0)
            if v > max_speed:
                issues.append(f"implied speed {v:.0f} m/s"); break
        named = name_time(name)
        if named is not None and tz is not None:
            delta = clip_start(d) - named.replace(tzinfo=tz).timestamp()
            name_deltas.append(delta)
            if abs(delta) > 30:
                issues.append(f"clip start {delta:+.0f}s from filename (camera clock changed?)")
        if track:
            errs = []
            for ms, lon, lat, alt, ang, e in pts:
                i = bisect.bisect_left(epochs, e - 2.0)
                best = None
                while i < len(track) and track[i][0] <= e + 2.0:
                    dd = hav(lat, lon, track[i][1], track[i][2])
                    best = dd if best is None or dd < best else best
                    i += 1
                if best is not None:
                    errs.append(best)
            if errs:
                errs.sort(); med = errs[len(errs) // 2]
                dat_medians.append(med); all_dat.extend(errs)
                if med > dat_tolerance:
                    issues.append(f".dat disagrees, median {med:.0f} m")
        if issues:
            problems.append((name, issues))

    if name_deltas:
        s = sorted(name_deltas)
        lines.append(f"  clip start vs filename: median {s[len(s)//2]:+.1f}s, range {s[0]:+.1f}..{s[-1]:+.1f}s")
    if all_dat:
        s = sorted(all_dat)
        lines.append(f"  vs .dat track: {len(s)} points, median {s[len(s)//2]:.1f} m, 95th pct {s[int(len(s)*.95)]:.1f} m; "
                     f"{sum(1 for m in dat_medians if m > dat_tolerance)} clips over {dat_tolerance:.0f} m median")
    else:
        lines.append("  no .dat track; skipped the cross-check")
    return lines, problems
