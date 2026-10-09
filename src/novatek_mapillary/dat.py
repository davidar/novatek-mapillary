"""
The plain-text track some Novatek cameras also write per day (Vantrue: GPS/YYYYMMDD.dat):

    YYYYMMDDHHMMSS,lat,N|S,lon,E|W,speed_knots,altitude_m

stamped with the camera clock (local time). It is NOT trustworthy at the 1 s level: its lag
behind the real fix varies 0–2 s between camera sessions and about 9% of rows repeat the previous
fix. The embedded records are the source of truth; the .dat only supplies altitude, a fallback
track for clips whose embedded records are unreadable, and an independent cross-check.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

Row = tuple  # (epoch, lat, lon, alt, speed_mps)


def undo_clock_steps(rows: list[Row], name: str | None = None, min_step: float = 3.0, window: int = 900) -> list[Row]:
    """
    Rows in file order are one per second. The GPS clock sometimes jumps forward by several whole
    seconds and back again a minute later. Pair each backward jump with a forward jump of about
    the same size within `window` rows before it and move the rows in between back. Unpaired jumps
    (clock already stepped at power-on) are left alone.
    """
    rows = list(rows)
    for j in range(1, len(rows)):
        back = rows[j - 1][0] + 1 - rows[j][0]
        if back < min_step:
            continue
        for i in range(j - 1, max(0, j - window), -1):
            fwd = rows[i][0] - rows[i - 1][0] - 1
            if fwd >= min_step and abs(fwd - back) <= 3:
                step = round((fwd + back) / 2)
                rows[i:j] = [(r[0] - step,) + tuple(r[1:]) for r in rows[i:j]]
                if name:
                    print(f"{name}: GPS clock stepped +{step} s for {j - i} rows; moved back")
                break
    return rows


def parse_dat(path: Path, tz: dt.timezone, offset: float) -> list[Row]:
    rows: list[Row] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            t = dt.datetime.strptime(p[0], "%Y%m%d%H%M%S").replace(tzinfo=tz)
            lat = float(p[1]) * (-1 if p[2] == "S" else 1)
            lon = float(p[3]) * (-1 if p[4] == "W" else 1)
            spd = float(p[5]) * 0.514444  # knots -> m/s
            alt = float(p[6])
        except ValueError:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        rows.append((t.timestamp() + offset, lat, lon, alt, spd))
    return rows


def load_track(dat_dir: Path | None, tz: dt.timezone, offset: float = -1.0, verbose: bool = True) -> list[Row]:
    """
    Sorted, de-duplicated (epoch, lat, lon, alt, speed_mps) from every *.dat in `dat_dir`.
    `offset` seconds is added to each row time: rows are stamped 1 s after the fix they contain
    (checked against the embedded records), hence the default -1.
    """
    if dat_dir is None or not dat_dir.is_dir():
        return []
    by_epoch: dict[float, Row] = {}
    for dat in sorted(dat_dir.glob("*.dat")):
        for r in undo_clock_steps(parse_dat(dat, tz, offset), dat.name if verbose else None):
            by_epoch.setdefault(r[0], r)
    rows = [by_epoch[k] for k in sorted(by_epoch)]
    # The camera sometimes writes the same fix in two consecutive rows; the first copy is stamped
    # one second early (checked against the embedded $GNRMC records), so keep only the second.
    dedup = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is not None and nxt[0] - r[0] <= 1.5 and nxt[1] == r[1] and nxt[2] == r[2] and r[4] > 0.5:
            continue
        dedup.append(r)
    if verbose and len(dedup) != len(rows):
        print(f".dat track: dropped {len(rows) - len(dedup)} duplicated-fix rows")
    return dedup


def default_dat_dir(clip_dirs) -> Path | None:
    """A GPS/ folder next to the clip folders (the card layout), if there is one."""
    for d in clip_dirs:
        for cand in (d / "GPS", d.parent / "GPS"):
            if cand.is_dir() and any(cand.glob("*.dat")):
                return cand
    return None
