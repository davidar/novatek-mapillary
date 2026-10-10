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

import bisect
import dataclasses
import datetime as dt
import math
import shutil
import struct
import subprocess
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


# --- decodable clips (ffmpeg) ---------------------------------------------------------------

def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_clip(path: Path, seconds: float = 5, fps: int = 30, audio: bool = True,
                size: str = "320x180") -> Path:
    """A small decodable H.264 (+ AAC) clip from ffmpeg's test sources, moov last (no faststart)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    subprocess.run(cmd + ["-f", "mp4", str(path)], check=True)
    return path


def write_decodable_clip(path: Path, start: float, fix_at, seconds: int = 5, fps: int = 30,
                         latency: float = 0.3, step: dict[int, int] | None = None,
                         stall: set[int] = frozenset(), valid: bool = True, audio: bool = True,
                         moov_last: bool = False) -> Path:
    """
    Like `write_clip`, but the frames are real (ffmpeg's testsrc2, libx264, plus a sine tone
    in AAC), so the clip can be re-encoded. ffmpeg's file is rewritten into the Novatek layout:
    the video track gets one frame per chunk, record k (same schedule and arguments as
    `write_clip`) is inserted into mdat right after frame k*fps-2, every track's chunk offsets
    are shifted with `mp4write.shift_chunk_offsets`, and a `gps ` index box is added to moov,
    which goes before mdat as on a Vantrue unless `moov_last`.
    """
    from novatek_mapillary import mp4write as w

    step = step or {}
    tmp = ffmpeg_clip(path.with_name(path.name + ".ffmpeg"), seconds, fps, audio)
    boxes = w.top_level(tmp)
    (_, mdat_off, mdat_size, mdat_h), = [b for b in boxes if b[0] == b"mdat"]
    data = tmp.read_bytes()
    tmp.unlink()
    ftyp = next(data[o:o + s] for t, o, s, _ in boxes if t == b"ftyp")
    payload_start = mdat_off + mdat_h
    _, _, moov = w.read_moov_bytes(data)

    # one video frame per chunk, as real clips have
    vtrak = next(t for t in moov.find_all(b"trak") if w.handler(t) == b"vide")
    vstbl = vtrak.find(b"mdia", b"minf", b"stbl")
    frames = w.sample_table(vstbl)
    for i, c in enumerate(vstbl.children):
        if c.type == b"stsc":
            desc = struct.unpack(">I", c.data[16:20])[0]
            vstbl.children[i] = w.Box(b"stsc", w.full() + struct.pack(">4I", 1, 1, 1, desc))
    w.set_chunk_offsets(vstbl, [o for o, _, _ in frames])

    # the records, inserted after frame k*fps-2 (old absolute offsets)
    inserts: list[tuple[int, bytes]] = []
    sentence = None
    for i in range(len(frames)):
        if (i + 2) % fps == 0:
            k = (i + 2) // fps
            if k not in stall or sentence is None:
                fix_epoch = math.floor(start + (i / fps) - latency)
                f = fix_at(fix_epoch)
                sentence = rmc(fix_epoch + step.get(k, 0), f.lat, f.lon, f.speed, f.course, valid)
            inserts.append((frames[i][0] + frames[i][1], gps_record(sentence)))
    positions = [p for p, _ in inserts]
    cum = [0]
    for _, rec in inserts:
        cum.append(cum[-1] + len(rec))

    parts, last = [], payload_start
    for pos, rec in inserts:
        parts += [data[last:pos], rec]
        last = pos
    parts.append(data[last:mdat_off + mdat_size])
    new_payload = b"".join(parts)

    gps_len = 8 + 8 * (len(inserts) + 1)
    moov.children.append(w.Box(b"gps ", bytes(gps_len - 8)))
    moov_len = len(moov.to_bytes())
    new_start = len(ftyp) + 8 + (0 if moov_last else moov_len)

    def fn(o):  # old absolute offset -> new
        return o - payload_start + new_start + cum[bisect.bisect_right(positions, o)]
    w.shift_chunk_offsets(moov, fn)
    index = [(0, 0)] + [(pos - payload_start + new_start + cum[j], len(rec))
                        for j, (pos, rec) in enumerate(inserts)]
    moov.children[-1] = w.Box(b"gps ", b"".join(struct.pack(">2I", *e) for e in index))
    mdat = struct.pack(">I4s", 8 + len(new_payload), b"mdat") + new_payload
    path.write_bytes(ftyp + mdat + moov.to_bytes() if moov_last else ftyp + moov.to_bytes() + mdat)
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
               start_frac: float = 0.4, dat: bool = True, clip_kwargs=None, first_index: int = 1,
               writer=write_clip, min_seconds: int = 10):
    """
    Writes Normal/*.MP4 (one clip per clip_seconds while the camera is on) and GPS/YYYYMMDD.dat.
    Each clip starts `start_frac` s after a whole second of UTC. `clip_kwargs` maps a clip number
    (0-based) to extra `writer` arguments (`write_clip`, or `write_decodable_clip` for clips
    that ffmpeg can decode). Returns the clip paths.
    """
    clip_kwargs = clip_kwargs or {}
    clips, n = [], 0
    for a, b in day.on:
        t = a
        while t < b:
            secs = min(clip_seconds, b - t)
            if secs < min_seconds:
                break
            local = dt.datetime.fromtimestamp(t, tz)
            name = f"{local:%Y%m%d_%H%M%S}_{first_index + n:05d}_N_A.MP4"
            clips.append(writer(root / "Normal" / name, t + start_frac, day.fix_at, seconds=secs,
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
