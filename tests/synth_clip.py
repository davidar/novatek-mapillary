#!/usr/bin/env python3
"""
Write synthetic Novatek-style dashcam clips for the tests, so no real (or fake) video is ever
committed. The files mimic the layout of real Vantrue clips as far as the parser cares:

  ftyp | moov [mvhd, trak (video: mdhd, hdlr 'vide', stbl stts/stsc/stsz/stco), 'gps ' index] | mdat

with one video frame per chunk (dummy bytes, not decodable) and one `freeGPS ` record per second
written into mdat after frame k*fps-2, holding a $GNRMC sentence for the latest fix. Record 0 of
the index is a header and points nowhere useful, as in real files.

A "day" is a schedule of segments (drive / stop / off) turned into consecutive 60 s clips named
after the camera clock (Normal/YYYYMMDD_HHMMSS_NNNNN_N_A.MP4) plus a GPS/YYYYMMDD.dat track.

    python3 tests/synth_clip.py OUT_DIR            # writes a small demo card to OUT_DIR
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
import struct
import sys
from pathlib import Path


def box(typ: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), typ) + body


def full(version: int = 0, flags: int = 0) -> bytes:
    return struct.pack(">I", (version << 24) | flags)


def nmea_checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"{c:02X}"


def rmc(epoch: float, lat: float, lon: float, speed_mps: float, course: float | None, valid: bool = True) -> bytes:
    t = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc)
    la, lo = abs(lat), abs(lon)
    body = (f"GNRMC,{t:%H%M%S}.000,{'A' if valid else 'V'},"
            f"{int(la):02d}{(la % 1) * 60:07.4f},{'S' if lat < 0 else 'N'},"
            f"{int(lo):03d}{(lo % 1) * 60:07.4f},{'W' if lon < 0 else 'E'},"
            f"{speed_mps / 0.514444:.3f},{'' if course is None else f'{course:.2f}'},{t:%d%m%y},,,A")
    return f"${body}*{nmea_checksum(body)}\r\n".encode()


@dataclasses.dataclass
class Fix:
    lat: float
    lon: float
    speed: float          # m/s
    course: float | None  # degrees true


def gps_record(sentence: bytes, size: int = 256) -> bytes:
    # Real records are 16 KiB: size, 'freeGPS ', ~100 bytes of binary fields, the sentence, zeros.
    head = struct.pack(">I", size) + b"freeGPS " + bytes(0x34)
    return (head + sentence).ljust(size, b"\0")


def write_clip(path: Path, start: float, fix_at, seconds: int = 60, fps: int = 30,
               latency: float = 0.3, step: dict[int, int] | None = None, stall: set[int] = frozenset(),
               valid: bool = True, frame_bytes: int = 32, moov_last: bool = False,
               mvhd_version: int = 0) -> Path:
    """
    `start` is the true UTC epoch of video time 0; `fix_at(epoch)` gives the Fix at a whole second.
    Record k (1..seconds) is written after frame k*fps-2 and carries the latest fix at least
    `latency` s old. `step` maps record k to a GPS clock error in whole seconds; records in `stall`
    repeat the previous record's sentence. Vantrue writes `moov` before `mdat` with a version 0
    `mvhd`; `moov_last` and `mvhd_version=1` imitate cameras that don't.
    """
    step = step or {}
    n_frames = seconds * fps
    mdat_parts, frame_offs, gps_offs = [], [], []
    pos = 0
    sentence = None
    for i in range(n_frames):
        frame_offs.append(pos)
        mdat_parts.append(bytes([i & 0xFF]) * frame_bytes)
        pos += frame_bytes
        if (i + 2) % fps == 0:
            k = (i + 2) // fps
            if k not in stall or sentence is None:
                fix_epoch = math.floor(start + (i / fps) - latency)
                f = fix_at(fix_epoch)
                sentence = rmc(fix_epoch + step.get(k, 0), f.lat, f.lon, f.speed, f.course, valid)
            rec = gps_record(sentence)
            gps_offs.append((pos, len(rec)))
            mdat_parts.append(rec)
            pos += len(rec)

    def moov(base: int) -> bytes:
        ts = 1000
        if mvhd_version == 0:
            mvhd = box(b"mvhd", full(0), struct.pack(">4I", 0, 0, ts, seconds * ts), bytes(80))
        else:  # 64-bit creation/modification/duration
            mvhd = box(b"mvhd", full(1), struct.pack(">QQIQ", 0, 0, ts, seconds * ts), bytes(80))
        stbl = box(b"stbl",
                   box(b"stts", full(), struct.pack(">3I", 1, n_frames, 1000)),
                   box(b"stsc", full(), struct.pack(">4I", 1, 1, 1, 1)),
                   box(b"stsz", full(), struct.pack(">2I", frame_bytes, n_frames)),
                   box(b"stco", full(), struct.pack(f">{n_frames + 1}I", n_frames, *(base + o for o in frame_offs))))
        mdia = box(b"mdia",
                   box(b"mdhd", full(), struct.pack(">4I", 0, 0, fps * 1000, n_frames * 1000), bytes(4)),
                   box(b"hdlr", full(), struct.pack(">I4s", 0, b"vide"), bytes(12), b"synthetic\0"),
                   box(b"minf", stbl))
        trak = box(b"trak", box(b"tkhd", full(0, 3), bytes(80)), mdia)
        gps = box(b"gps ", struct.pack(">2I", 0, 0), *(struct.pack(">2I", base + o, s) for o, s in gps_offs))
        return box(b"moov", mvhd, trak, gps)

    ftyp = box(b"ftyp", b"mp42", struct.pack(">I", 0), b"isomavc1mp42")
    mdat = box(b"mdat", *mdat_parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if moov_last:
        base = len(ftyp) + 8
        path.write_bytes(ftyp + mdat + moov(base))
    else:
        base = len(ftyp) + len(moov(0)) + 8
        path.write_bytes(ftyp + moov(base) + mdat)
    return path


# --- a day of driving ------------------------------------------------------------------------

def offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


@dataclasses.dataclass
class Day:
    """Per-second ground truth for a synthetic day: fixes and when the camera was on."""
    fixes: dict[int, Fix]
    on: list[tuple[int, int]]  # [start, end) epochs of each powered-on stretch

    def fix_at(self, epoch: int) -> Fix:
        return self.fixes[min(max(epoch, min(self.fixes)), max(self.fixes))]


def simulate(start: int, lat: float, lon: float, schedule) -> Day:
    """
    `schedule`: list of (kind, seconds, speed_mps, course) with kind 'drive', 'stop' or 'off'
    ('stop' = parked with the camera running, 'off' = camera powered off, car parked).
    """
    fixes, on, t = {}, [], start
    cur_on = None
    for kind, secs, speed, course in schedule:
        if kind == "off":
            if cur_on is not None:
                on.append((cur_on, t)); cur_on = None
        elif cur_on is None:
            cur_on = t
        for _ in range(secs):
            v = speed if kind == "drive" else 0.0
            fixes[t] = Fix(lat, lon, v, course if v > 0 else None)
            if v:
                lat, lon = offset(lat, lon, v * math.cos(math.radians(course)), v * math.sin(math.radians(course)))
            t += 1
    if cur_on is not None:
        on.append((cur_on, t))
    fixes[t] = Fix(lat, lon, 0.0, None)
    return Day(fixes, on)


def write_card(root: Path, day: Day, tz: dt.timezone, clip_seconds: int = 60, latency: float = 0.3,
               start_frac: float = 0.4, dat: bool = True, clip_kwargs=None, first_index: int = 1):
    """
    Writes Normal/*.MP4 (one clip per clip_seconds while the camera is on) and GPS/YYYYMMDD.dat.
    Each clip starts `start_frac` s after a whole second of UTC. `clip_kwargs` maps a clip number
    (0-based) to extra write_clip arguments. Returns the clip paths.
    """
    clip_kwargs = clip_kwargs or {}
    clips, n = [], 0
    for a, b in day.on:
        t = a
        while t < b:
            secs = min(clip_seconds, b - t)
            if secs < 10:
                break
            local = dt.datetime.fromtimestamp(t, tz)
            name = f"{local:%Y%m%d_%H%M%S}_{first_index + n:05d}_N_A.MP4"
            clips.append(write_clip(root / "Normal" / name, t + start_frac, day.fix_at, seconds=secs,
                                    latency=latency, **clip_kwargs.get(n, {})))
            n += 1
            t += secs
    if dat:
        lines_by_day: dict[str, list[str]] = {}
        for a, b in day.on:
            for e in range(a, b):
                f = day.fixes[e]
                stamp = dt.datetime.fromtimestamp(e + 1, tz)  # rows are stamped 1 s after their fix
                lines_by_day.setdefault(f"{stamp:%Y%m%d}", []).append(
                    f"{stamp:%Y%m%d%H%M%S},{abs(f.lat):.6f},{'S' if f.lat < 0 else 'N'},"
                    f"{abs(f.lon):.6f},{'W' if f.lon < 0 else 'E'},{f.speed / 0.514444:.2f},{123.0 + e % 7:.1f}")
        for d, lines in lines_by_day.items():
            (root / "GPS").mkdir(parents=True, exist_ok=True)
            (root / "GPS" / f"{d}.dat").write_text("\n".join(lines) + "\n")
    return clips


DEMO_TZ = dt.timezone(dt.timedelta(hours=10))
DEMO_START = int(dt.datetime(2026, 1, 15, 8, 0, 0, tzinfo=DEMO_TZ).timestamp())


def demo_day() -> Day:
    """10 min of driving, 6 min stopped with the camera on, 5 min, 20 min off, 10 min."""
    return simulate(DEMO_START, -33.0, 147.0, [
        ("drive", 600, 25.0, 45.0),
        ("stop", 360, 0, 0),
        ("drive", 300, 20.0, 90.0),
        ("off", 1200, 0, 0),
        ("drive", 600, 27.0, 180.0),
    ])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    clips = write_card(out, demo_day(), DEMO_TZ)
    print(f"{len(clips)} synthetic clips + GPS/*.dat -> {out}")
