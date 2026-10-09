"""Small shared helpers: distances, bearings, timezones, clip filename times."""
from __future__ import annotations

import datetime as dt
import math
import re

# Novatek cameras name clips after the camera clock: 20260927_120016_00229_N_A.MP4 (Vantrue),
# 2026_0927_120016_001.MP4 and similar.
_NAME_TIME = re.compile(r"(\d{4})_?(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")


def hav(a_lat, a_lon, b_lat, b_lon) -> float:
    """Great-circle distance in metres."""
    R = 6371000
    la1, lo1, la2, lo2 = map(math.radians, (a_lat, a_lon, b_lat, b_lon))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def bearing(a_lat, a_lon, b_lat, b_lon) -> float:
    """Initial bearing from a to b, degrees true."""
    la1, lo1, la2, lo2 = map(math.radians, (a_lat, a_lon, b_lat, b_lon))
    x = math.sin(lo2 - lo1) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def parse_tz(s: str) -> dt.timezone:
    """'+10:00', '-03:30', '+11' -> timezone."""
    sign = -1 if s.startswith("-") else 1
    hh, _, mm = s.lstrip("+-").partition(":")
    return dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm or 0)))


def format_tz(tz: dt.timezone) -> str:
    secs = int(tz.utcoffset(None).total_seconds())
    sign = "-" if secs < 0 else "+"
    secs = abs(secs)
    return f"{sign}{secs // 3600:02d}:{secs % 3600 // 60:02d}"


def name_time(name: str) -> dt.datetime | None:
    """Camera-clock time from a clip filename, naive (no timezone). None if it has none."""
    m = _NAME_TIME.search(name)
    if not m:
        return None
    try:
        return dt.datetime(*map(int, m.groups()))
    except ValueError:
        return None


def infer_tz(pairs) -> dt.timezone | None:
    """
    The camera clock's UTC offset from (utc_epoch, naive camera-clock datetime) pairs, rounded to
    15 minutes; the most common value wins, so a few odd clips don't matter. None if no pairs.
    """
    votes: dict[int, int] = {}
    for epoch, naive in pairs:
        off = naive.replace(tzinfo=dt.timezone.utc).timestamp() - epoch
        q = round(off / 900) * 900
        votes[q] = votes.get(q, 0) + 1
    if not votes:
        return None
    best = max(votes, key=votes.get)
    return dt.timezone(dt.timedelta(seconds=best))
