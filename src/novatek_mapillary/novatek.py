"""
Read the GPS records a Novatek-based dashcam (sold as Vantrue and others) embeds in its MP4 clips.

Each clip has a `gps ` box in `moov` listing (offset, size) of one `freeGPS ` record per
second of video, written into mdat alongside the frames. Each record holds the raw $GNRMC
sentence from the receiver: UTC time, fix status, lat/lon, speed (knots), course. Record 0 is a
header, records 1..N sit right after the frames of video second k, so a record's byte offset,
looked up in the video track's chunk table, gives the exact video time it was written.

The RMC timestamp is the fix's true time; the record it first appears in was written up to a
second later (fix arrives, camera copies the latest fix at the next record write). So a fix's
video time is `epoch - clip_start`, and `clip_start` is fitted per clip from all first-seen fixes.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import re
import struct
from pathlib import Path

RMC = re.compile(
    rb"\$G[NP]RMC,(\d{6})(?:\.\d+)?,([AV]),(\d{2})(\d+\.\d+),([NS]),(\d{3})(\d+\.\d+),([EW]),([\d.]*),([\d.]*),(\d{6})"
)


@dataclasses.dataclass
class Record:
    k: int             # record index (1-based; 0 is the header)
    video_t: float     # seconds into the video where the record was written
    epoch: float       # UTC epoch of the fix (RMC time)
    valid: bool        # RMC status 'A'
    lat: float
    lon: float
    speed: float       # m/s
    course: float | None  # degrees true, None if empty


@dataclasses.dataclass
class ClipInfo:
    duration: float
    gps_index: list[tuple[int, int]]
    chunk_offsets: list[int]
    chunk_first_sample: list[int]
    sample_delta: float  # seconds per video sample (assumes constant frame rate)
    timescale: int = 0   # the video track's media timescale (mdhd)
    width: int = 0       # the video track's size (tkhd)
    height: int = 0


def _boxes(data, off, end):
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", data[off:off + 8])
        h = 8
        if size == 1:
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            h = 16
        if size == 0:
            size = end - off
        if size < h:
            return
        yield typ, off, size, h
        off += size


def _find(data, types, off, end):
    for typ, o, s, h in _boxes(data, off, end):
        if typ == types[0]:
            if len(types) == 1:
                yield o, s, h
            else:
                yield from _find(data, types[1:], o + h, o + s)


def _locate(f, typ: bytes):
    """(offset, size, header length) of the first top-level box of type `typ`, reading only box
    headers, so a `moov` after a multi-gigabyte `mdat` costs nothing to find. None if absent."""
    f.seek(0, 2)
    end = f.tell()
    off = 0
    while off + 8 <= end:
        f.seek(off)
        hdr = f.read(16)
        if len(hdr) < 8:
            return None
        size, t = struct.unpack(">I4s", hdr[:8])
        h = 8
        if size == 1:
            if len(hdr) < 16:
                return None
            size = struct.unpack(">Q", hdr[8:16])[0]
            h = 16
        if size == 0:
            size = end - off
        if size < h:
            return None
        if t == typ:
            return off, size, h
        off += size
    return None


def parse_clip(path: Path) -> ClipInfo | None:
    """Container facts needed to place the GPS records in time. None if the file has no `moov`
    box (or is truncated before its end). `moov` may come before or after `mdat`."""
    with path.open("rb") as f:
        loc = _locate(f, b"moov")
        if loc is None:
            return None
        mo, ms, mh = loc
        f.seek(mo)
        data = f.read(ms)
    if len(data) < ms:
        return None
    mo = 0  # `data` now holds just the moov box
    # duration (mvhd version 1 has 64-bit times: creation, modification, 32-bit timescale, duration)
    o, s, h = next(_find(data, [b"mvhd"], mo + mh, mo + ms))
    if data[o + h] == 0:
        _, _, ts, dur = struct.unpack(">4I", data[o + h + 4:o + h + 20])
    else:
        ts, dur = struct.unpack(">IQ", data[o + h + 20:o + h + 32])
    duration = dur / ts if ts else 0.0
    # gps index
    gps = []
    for o, s, h in _find(data, [b"gps "], mo + mh, mo + ms):
        n = (s - h) // 8
        gps = [struct.unpack(">2I", data[o + h + 8 * i:o + h + 8 * i + 8]) for i in range(n)]
    # video track sample tables
    chunks, first_sample, delta = [], [], 1 / 30
    vts = width = height = 0
    for to, tsz, th in _find(data, [b"trak"], mo + mh, mo + ms):
        ho, hs, hh = next(_find(data, [b"mdia", b"hdlr"], to + th, to + tsz))
        if data[ho + hh + 8:ho + hh + 12] != b"vide":
            continue
        o, s, h = next(_find(data, [b"mdia", b"mdhd"], to + th, to + tsz))
        timescale = struct.unpack(">I", data[o + h + 12:o + h + 16])[0] if data[o + h] == 0 \
            else struct.unpack(">I", data[o + h + 20:o + h + 24])[0]
        vts = timescale
        ko, ks, kh = next(_find(data, [b"tkhd"], to + th, to + tsz))
        if ks - kh >= 84:
            width, height = (v >> 16 for v in struct.unpack(">2I", data[ko + ks - 8:ko + ks]))
        so, ss, sh = next(_find(data, [b"mdia", b"minf", b"stbl"], to + th, to + tsz))
        tab = {typ: (o, s, h) for typ, o, s, h in _boxes(data, so + sh, so + ss)}
        if b"stco" in tab:
            o, s, h = tab[b"stco"]; n = struct.unpack(">I", data[o + h + 4:o + h + 8])[0]
            chunks = list(struct.unpack(f">{n}I", data[o + h + 8:o + h + 8 + 4 * n]))
        else:
            o, s, h = tab[b"co64"]; n = struct.unpack(">I", data[o + h + 4:o + h + 8])[0]
            chunks = list(struct.unpack(f">{n}Q", data[o + h + 8:o + h + 8 + 8 * n]))
        o, s, h = tab[b"stsc"]; n = struct.unpack(">I", data[o + h + 4:o + h + 8])[0]
        stsc = [struct.unpack(">3I", data[o + h + 8 + 12 * i:o + h + 20 + 12 * i]) for i in range(n)]
        o, s, h = tab[b"stts"]
        delta = struct.unpack(">I", data[o + h + 12:o + h + 16])[0] / timescale
        idx = 0
        for ci in range(len(chunks)):
            spc = 0
            for fc, spc_, _ in stsc:
                if fc - 1 <= ci:
                    spc = spc_
            first_sample.append(idx)
            idx += spc
        break
    return ClipInfo(duration, gps, chunks, first_sample, delta, vts, width, height)


def mp4_duration(path: Path) -> float | None:
    """Duration in seconds from moov/mvhd, None if the file can't be read."""
    try:
        info = parse_clip(path)
    except (OSError, StopIteration, struct.error):
        return None
    return info.duration if info and info.duration else None


def read_records(path: Path, info: ClipInfo | None = None) -> list[Record]:
    info = info or parse_clip(path)
    if info is None or not info.gps_index:
        return []
    recs = []
    with path.open("rb") as f:
        for k, (off, size) in enumerate(info.gps_index):
            if k == 0 or off <= 0 or size <= 0:
                continue
            f.seek(off)
            blk = f.read(min(size, 512))
            m = RMC.search(blk)
            ci = bisect.bisect_right(info.chunk_offsets, off) - 1
            if ci >= 0 and info.chunk_first_sample:
                video_t = info.chunk_first_sample[ci] * info.sample_delta
            else:
                video_t = float(k)
            if not m:
                continue
            hms, status, latd, latm, ns, lond, lonm, ew, spd, crs, dmy = m.groups()
            t = dt.datetime.strptime((dmy + hms).decode(), "%d%m%y%H%M%S").replace(tzinfo=dt.timezone.utc)
            lat = (int(latd) + float(latm) / 60) * (-1 if ns == b"S" else 1)
            lon = (int(lond) + float(lonm) / 60) * (-1 if ew == b"W" else 1)
            recs.append(Record(k, video_t, t.timestamp(), status == b"A", lat, lon,
                               float(spd or 0) * 0.514444, float(crs) if crs else None))
    return recs


CLOCK_STEP_S = 3.0


def clip_track(path: Path, min_points: int = 5, info: ClipInfo | None = None, recs: list[Record] | None = None):
    """
    Returns (start_epoch, fixes, duration) where fixes is a list of unique valid Records sorted
    by epoch, and start_epoch is the fitted UTC epoch of video time 0. None if too few fixes.

    The camera copies the most recent fix into each record, so the first record a fix appears in
    was written 0–1 s (plus receiver latency) after the fix time. Taking the median of
    (epoch - video_t) over all first-seen fixes and adding 0.5 s undoes that on average. Records
    are written exactly once a second, so where in that second the writes fall can't be recovered
    from one clip: the start is good to about half a second (plus receiver latency).

    The receiver's UTC clock occasionally steps by a whole number of seconds (4–14 s seen, 14 s
    being GPS vs BeiDou time) and steps back a minute later, while the positions stay continuous.
    Records whose (epoch - video_t) is >= CLOCK_STEP_S off the clip's median are re-stamped by
    the rounded difference before de-duplication; otherwise a stepped-back record would win the
    "first occurrence" and put its position 10+ s away in the video.
    Stalls (the same fix repeated) only ever read 1–2 s behind, so they are left alone.
    `info` and `recs` (from parse_clip / read_records) save reading the clip again.
    """
    info = info or parse_clip(path)
    recs = [r for r in (recs if recs is not None else read_records(path, info)) if r.valid]
    if recs:
        offs = sorted(r.epoch - r.video_t for r in recs)
        med = offs[len(offs) // 2]
        for i, r in enumerate(recs):
            dev = (r.epoch - r.video_t) - med
            if abs(dev) >= CLOCK_STEP_S:
                recs[i] = dataclasses.replace(r, epoch=r.epoch - round(dev))
    seen: dict[float, Record] = {}
    for r in recs:  # in record order, so the first occurrence wins
        if r.epoch not in seen:
            seen[r.epoch] = r
    fixes = sorted(seen.values(), key=lambda r: r.epoch)
    if len(fixes) < min_points:
        return None
    diffs = sorted(r.epoch - r.video_t for r in fixes)
    start = diffs[len(diffs) // 2] + 0.5
    return start, fixes, info.duration
