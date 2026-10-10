"""
Byte-level MP4 box writing: just enough to add a CAMM track and some `udta` tags to a file
ffmpeg wrote, to move `moov` to the front (faststart), and to rewrite chunk offsets when bytes
are inserted into `mdat`. Standard library only; `novatek.py` stays the reader.

The CAMM track follows Google's Camera Motion Metadata spec and what `mapillary_tools` 0.14
writes (see docs/archive-design.md, "The CAMM track"):
  - type 6 (GPS) samples, little-endian: float64 time_gps_epoch (Unix seconds, as
    mapillary_tools does), int32 fix type (3 with altitude, 2 without), float64 lat, lon,
    float32 alt, then float32 horizontal/vertical accuracy, velocity east/north/up and speed
    accuracy (0 = unknown, except velocity east/north when known);
  - media timescale max(1000, movie timescale), `mdhd` version 1, `tkhd` duration all ones,
    `hdlr` 'camm' / CameraMetadataMotionHandler, a self-referencing `dref`;
  - the last sample's `stts` delta runs to the end of the video;
  - when the first fix is after 0, an edit list: an empty edit up to the first fix, then a real
    entry covering video end - first fix, so every sample is inside it.
"""
from __future__ import annotations

import dataclasses
import math
import os
import struct
from pathlib import Path

from .novatek import _boxes, _locate

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta", b"mvex"}
UINT32_MAX = 0xFFFFFFFF
CAMM_GPS_SAMPLE = struct.Struct("<2xHdiddfffffff")  # 60 bytes
TEXT_LANG_UND = 0x55C4  # ISO 639-2 'und', packed


@dataclasses.dataclass
class Box:
    type: bytes
    data: bytes = b""                 # payload after the header (leaves)
    children: list[Box] | None = None  # containers

    def to_bytes(self) -> bytes:
        body = self.data if self.children is None else b"".join(c.to_bytes() for c in self.children)
        if 8 + len(body) > UINT32_MAX:
            return struct.pack(">I4sQ", 1, self.type, 16 + len(body)) + body
        return struct.pack(">I4s", 8 + len(body), self.type) + body

    def find(self, *path: bytes) -> Box | None:
        return next(self.find_all(*path), None)

    def find_all(self, *path: bytes):
        for c in self.children or ():
            if c.type == path[0]:
                if len(path) == 1:
                    yield c
                else:
                    yield from c.find_all(*path[1:])


def parse_boxes(data: bytes, off: int = 0, end: int | None = None, containers=CONTAINERS) -> list[Box]:
    end = len(data) if end is None else end
    out = []
    for typ, o, s, h in _boxes(data, off, end):
        if typ in containers:
            out.append(Box(typ, children=parse_boxes(data, o + h, o + s, containers)))
        else:
            out.append(Box(typ, data[o + h:o + s]))
    return out


def full(version: int = 0, flags: int = 0) -> bytes:
    return struct.pack(">I", (version << 24) | flags)


# --- reading moov --------------------------------------------------------------------------

def top_level(path: Path) -> list[tuple[bytes, int, int, int]]:
    """(type, offset, size, header length) of every top-level box, reading only headers."""
    out = []
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        off = 0
        while off + 8 <= end:
            f.seek(off)
            hdr = f.read(16)
            size, typ = struct.unpack(">I4s", hdr[:8])
            h = 8
            if size == 1:
                size = struct.unpack(">Q", hdr[8:16])[0]
                h = 16
            elif size == 0:
                size = end - off
            if size < h or off + size > end:
                raise ValueError(f"{path}: truncated or corrupt box {typ!r} at {off}")
            out.append((typ, off, size, h))
            off += size
    return out


def read_moov(path: Path) -> tuple[int, int, Box]:
    """(offset, size, parsed moov) of the file's moov box."""
    with open(path, "rb") as f:
        loc = _locate(f, b"moov")
        if loc is None:
            raise ValueError(f"{path}: no moov box")
        mo, ms, mh = loc
        f.seek(mo)
        data = f.read(ms)
    if len(data) < ms:
        raise ValueError(f"{path}: moov truncated")
    return mo, ms, Box(b"moov", children=parse_boxes(data, mh, ms))


def read_moov_bytes(data: bytes) -> tuple[int, int, Box]:
    """read_moov for a whole file already in memory."""
    for typ, o, s, h in _boxes(data, 0, len(data)):
        if typ == b"moov":
            return o, s, Box(b"moov", children=parse_boxes(data, o + h, o + s))
    raise ValueError("no moov box")


def mvhd_timescale(moov: Box) -> int:
    d = moov.find(b"mvhd").data
    return struct.unpack(">I", d[12:16] if d[0] == 0 else d[20:24])[0]


def mvhd_times(moov: Box) -> tuple[int, int]:
    d = moov.find(b"mvhd").data
    return struct.unpack(">2I", d[4:12]) if d[0] == 0 else struct.unpack(">2Q", d[4:20])


def handler(trak: Box) -> bytes:
    h = trak.find(b"mdia", b"hdlr")
    return h.data[8:12] if h else b""


def mdhd_timescale(trak: Box) -> int:
    d = trak.find(b"mdia", b"mdhd").data
    return struct.unpack(">I", d[12:16] if d[0] == 0 else d[20:24])[0]


def video_timescale(path: Path) -> int | None:
    """Media timescale of the first video track (from its mdhd), None if there is none."""
    _, _, moov = read_moov(path)
    for trak in moov.find_all(b"trak"):
        if handler(trak) == b"vide":
            return mdhd_timescale(trak)
    return None


def video_size(path: Path) -> tuple[int | None, int | None]:
    """(width, height) of the first video track, from its tkhd (16.16 fixed point)."""
    _, _, moov = read_moov(path)
    for trak in moov.find_all(b"trak"):
        if handler(trak) == b"vide":
            d = trak.find(b"tkhd").data
            w, h = struct.unpack(">2I", d[-8:])
            return w >> 16, h >> 16
    return None, None


# --- chunk offsets -------------------------------------------------------------------------

def chunk_offsets(stbl: Box) -> list[int]:
    b = stbl.find(b"stco") or stbl.find(b"co64")
    n = struct.unpack(">I", b.data[4:8])[0]
    return list(struct.unpack(f">{n}{'I' if b.type == b'stco' else 'Q'}", b.data[8:8 + n * (4 if b.type == b'stco' else 8)]))


def set_chunk_offsets(stbl: Box, offsets: list[int]) -> None:
    """Replace the stbl's stco/co64 with `offsets`, as stco if they fit in 32 bits, else co64."""
    wide = any(o > UINT32_MAX for o in offsets)
    new = Box(b"co64" if wide else b"stco",
              full() + struct.pack(f">I{len(offsets)}{'Q' if wide else 'I'}", len(offsets), *offsets))
    for i, c in enumerate(stbl.children):
        if c.type in (b"stco", b"co64"):
            stbl.children[i] = new
            return
    stbl.children.append(new)


def shift_chunk_offsets(moov: Box, fn) -> None:
    """Map every chunk offset of every track through `fn(old) -> new`. A track whose offsets
    pass 2**32 gets its stco promoted to co64 (which changes moov's size: callers placing moov
    before the data must recompute)."""
    for stbl in moov.find_all(b"trak", b"mdia", b"minf", b"stbl"):
        set_chunk_offsets(stbl, [fn(o) for o in chunk_offsets(stbl)])


def _rewrite_moov_at_end(path: Path, edit) -> None:
    """For files whose moov is the last box: truncate at moov, let `edit(moov, f, pos)` append
    any new data at `pos` (returning nothing) and change `moov`, then write moov back."""
    boxes = top_level(path)
    if not boxes or boxes[-1][0] != b"moov":
        raise ValueError(f"{path}: moov is not the last box; refusing to append")
    mo, ms, moov = read_moov(path)
    with open(path, "r+b") as f:
        f.truncate(mo)
        f.seek(mo)
        edit(moov, f, mo)
        f.write(moov.to_bytes())


# --- faststart -----------------------------------------------------------------------------

def faststart(path: Path) -> bool:
    """Move moov in front of the first mdat and shift every chunk offset by what moved. False if
    it already is. Writes a temporary file next to `path` and renames it over."""
    boxes = top_level(path)
    types = [b[0] for b in boxes]
    if b"moov" not in types or b"mdat" not in types:
        raise ValueError(f"{path}: needs both moov and mdat")
    if types.index(b"moov") < types.index(b"mdat"):
        return False
    _, _, moov = read_moov(path)
    first_mdat = types.index(b"mdat")
    order = [b for b in boxes[:first_mdat] if b[0] != b"moov"]
    rest = [b for b in boxes[first_mdat:] if b[0] != b"moov"]
    orig = chunk_offsets_all(moov)

    moov_len = len(moov.to_bytes())
    for _ in range(4):  # promoting stco -> co64 changes the size; settle it
        starts, pos = {}, sum(b[2] for b in order) + moov_len
        for b in rest:
            starts[b[1]] = pos
            pos += b[2]
        spans = sorted((b[1], b[1] + b[2], starts[b[1]]) for b in rest)

        def fn(o, spans=spans):
            for a, e, new in spans:
                if a <= o < e:
                    return new + (o - a)
            raise ValueError(f"chunk offset {o} is outside every box")
        restore_chunk_offsets(moov, orig)
        shift_chunk_offsets(moov, fn)
        new_len = len(moov.to_bytes())
        if new_len == moov_len:
            break
        moov_len = new_len
    tmp = path.with_name(path.name + ".faststart")
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            for _, off, size, _ in order:
                _copy(src, dst, off, size)
            dst.write(moov.to_bytes())
            for _, off, size, _ in rest:
                _copy(src, dst, off, size)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def chunk_offsets_all(moov: Box) -> list[list[int]]:
    return [chunk_offsets(s) for s in moov.find_all(b"trak", b"mdia", b"minf", b"stbl")]


def restore_chunk_offsets(moov: Box, offsets: list[list[int]]) -> None:
    for stbl, offs in zip(moov.find_all(b"trak", b"mdia", b"minf", b"stbl"), offsets):
        set_chunk_offsets(stbl, offs)


def _copy(src, dst, off, size, bufsize=1 << 22):
    src.seek(off)
    left = size
    while left:
        buf = src.read(min(bufsize, left))
        if not buf:
            raise ValueError("unexpected end of file")
        dst.write(buf)
        left -= len(buf)


# --- udta ----------------------------------------------------------------------------------

def text_atom(text: str) -> bytes:
    """QuickTime user-data text: uint16 size, uint16 language, text (what ©mak etc. hold)."""
    b = text.encode("utf-8")
    return struct.pack(">HH", len(b), TEXT_LANG_UND) + b


def iso6709(lat: float, lon: float, alt: float | None = None) -> str:
    s = f"{lat:+08.4f}{lon:+09.4f}"
    if alt is not None:
        s += f"{alt:+.3f}"
    return s + "/"


def udta_boxes(make: str | None = None, model: str | None = None, xyz: str | None = None) -> list[Box]:
    """make/model as both a bare string (@mak/@mod: mapillary_tools, Ricoh) and a text atom
    (©mak/©mod: QuickTime, ffmpeg, exiftool, Insta360), plus ©xyz (ISO 6709)."""
    out = []
    if make:
        out += [Box(b"@mak", make.encode()), Box(b"\xa9mak", text_atom(make))]
    if model:
        out += [Box(b"@mod", model.encode()), Box(b"\xa9mod", text_atom(model))]
    if xyz:
        out.append(Box(b"\xa9xyz", text_atom(xyz)))
    return out


def merge_udta(moov: Box, boxes: list[Box]) -> None:
    """Add `boxes` as direct children of moov/udta, replacing children of the same type and
    creating udta if ffmpeg didn't write one."""
    if not boxes:
        return
    udta = moov.find(b"udta")
    if udta is None:
        udta = Box(b"udta", children=[])
        moov.children.append(udta)
    new_types = {b.type for b in boxes}
    udta.children = [c for c in udta.children if c.type not in new_types] + boxes


def set_udta(path: Path, make=None, model=None, xyz=None) -> None:
    """Write udta tags into a file whose moov is last (see `append_camm_track` to do both)."""
    _rewrite_moov_at_end(path, lambda moov, f, pos: merge_udta(moov, udta_boxes(make, model, xyz)))


# --- CAMM ----------------------------------------------------------------------------------

def camm_gps_sample(epoch: float, lat: float, lon: float, alt: float | None,
                    ve: float = 0.0, vn: float = 0.0) -> bytes:
    return CAMM_GPS_SAMPLE.pack(6, epoch, 3 if alt is not None else 2, lat, lon,
                                -1.0 if alt is None else alt, 0.0, 0.0, ve, vn, 0.0, 0.0)


def velocity(speed: float | None, course: float | None) -> tuple[float, float]:
    """(east, north) m/s from a speed and a course in degrees true; zeros when unknown."""
    if not speed or course is None:
        return 0.0, 0.0
    r = math.radians(course)
    return speed * math.sin(r), speed * math.cos(r)


def _stts(deltas: list[int]) -> bytes:
    runs: list[list[int]] = []
    for d in deltas:
        if runs and runs[-1][1] == d:
            runs[-1][0] += 1
        else:
            runs.append([1, d])
    return full() + struct.pack(">I", len(runs)) + b"".join(struct.pack(">2I", n, d) for n, d in runs)


def build_camm_trak(times: list[float], sizes: list[int], chunk_offset: int, track_id: int,
                    movie_ts: int, video_end: float, creation: int = 0, modification: int = 0) -> Box:
    """A CAMM trak for samples at presentation `times` (seconds, non-decreasing, >= 0) stored
    contiguously at `chunk_offset`."""
    media_ts = max(1000, movie_ts)
    end = max(video_end, times[-1])
    empty = round(times[0] * movie_ts) if times[0] > 0 else 0
    base = empty / movie_ts
    media = [max(0, round((t - base) * media_ts)) for t in times]
    media[0] = 0
    for i in range(1, len(media)):
        media[i] = max(media[i], media[i - 1])
    media_end = max(media[-1], math.ceil((end - base) * media_ts))
    deltas = [b - a for a, b in zip(media, media[1:])] + [media_end - media[-1]]

    stsd = full() + struct.pack(">I", 1) + struct.pack(">I4s6xH", 16, b"camm", 1)
    if len(set(sizes)) == 1:
        stsz = full() + struct.pack(">2I", sizes[0], len(sizes))
    else:
        stsz = full() + struct.pack(f">2I{len(sizes)}I", 0, len(sizes), *sizes)
    stbl = Box(b"stbl", children=[
        Box(b"stsd", stsd),
        Box(b"stts", _stts(deltas)),
        Box(b"stsc", full() + struct.pack(">4I", 1, 1, len(sizes), 1)),
        Box(b"stsz", stsz),
    ])
    set_chunk_offsets(stbl, [chunk_offset])
    dinf = Box(b"dinf", children=[Box(b"dref", full() + struct.pack(">I", 1) + Box(b"url ", full(0, 1)).to_bytes())])
    minf = Box(b"minf", children=[Box(b"nmhd", full()), dinf, stbl])
    mdhd = Box(b"mdhd", full(1) + struct.pack(">QQIQHH", creation, modification, media_ts, media_end,
                                              TEXT_LANG_UND, 0))
    hdlr = Box(b"hdlr", full() + struct.pack(">I4s12x", 0, b"camm") + b"CameraMetadataMotionHandler\0")
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    # flags 3: track_enabled | track_in_movie (ISO 14496-12), as the video track has
    tkhd = Box(b"tkhd", full(0, 3) + struct.pack(">5I", creation, modification, track_id, 0, UINT32_MAX)
               + bytes(8) + struct.pack(">hhhH", 0, 0, 0, 0) + matrix + struct.pack(">2I", 0, 0))
    children = [tkhd]
    if empty:
        seg = math.ceil((end - base) * movie_ts)
        entries = [(empty, -1), (seg, 0)]
        wide = any(d > 0x7FFFFFFF for d, _ in entries)
        fmt = ">qq" if wide else ">ii"
        elst = full(1 if wide else 0) + struct.pack(">I", len(entries)) + b"".join(
            struct.pack(fmt, d, m) + struct.pack(">hh", 1, 0) for d, m in entries)
        children.append(Box(b"edts", children=[Box(b"elst", elst)]))
    children.append(Box(b"mdia", children=[mdhd, hdlr, minf]))
    return Box(b"trak", children=children)


def _next_track_id(moov: Box, bump: bool) -> int:
    mvhd = moov.find(b"mvhd")
    d = bytearray(mvhd.data)
    at = 96 if d[0] == 0 else 108
    nid = struct.unpack(">I", d[at:at + 4])[0]
    used = [struct.unpack(">I", (t.data[12:16] if t.data[0] == 0 else t.data[20:24]))[0]
            for t in moov.find_all(b"trak", b"tkhd")]
    nid = max([nid, *(u + 1 for u in used)])
    if bump:
        d[at:at + 4] = struct.pack(">I", nid + 1)
        mvhd.data = bytes(d)
    return nid


def video_end(moov: Box) -> float:
    """End of the first video track in seconds (its tkhd duration, edits included), falling
    back to the movie duration in mvhd."""
    movie_ts = mvhd_timescale(moov)
    for trak in moov.find_all(b"trak"):
        if handler(trak) == b"vide" and movie_ts:
            d = trak.find(b"tkhd").data
            dur = struct.unpack(">I", d[20:24])[0] if d[0] == 0 else struct.unpack(">Q", d[28:36])[0]
            if dur and dur != UINT32_MAX:
                return dur / movie_ts
    d = moov.find(b"mvhd").data
    if d[0] == 0:
        ts, dur = struct.unpack(">2I", d[12:20])
    else:
        ts, dur = struct.unpack(">IQ", d[20:32])
    return dur / ts if ts else 0.0


def append_camm_track(path: Path, points: list, velocities: list | None = None,
                      make: str | None = None, model: str | None = None, xyz: str | None = None) -> int:
    """
    Append a CAMM GPS track to an MP4 whose moov is the last box (ffmpeg without faststart):
    truncate at moov, write an mdat of samples, then moov with the new trak, next_track_ID
    bumped and udta tags merged. `points` are description points
    [ms_into_video, lon, lat, alt, angle, epoch]; `velocities` optional (east, north) m/s per
    point. Nothing before the old moov moves, so the other tracks' offsets stay valid.
    Returns the number of samples written (0 writes only the udta tags).
    """
    pts = sorted(points, key=lambda p: p[0])
    vel = velocities or [(0.0, 0.0)] * len(pts)
    if velocities is not None and len(velocities) != len(points):
        raise ValueError("one velocity per point")
    if velocities is not None:
        order = sorted(range(len(points)), key=lambda i: points[i][0])
        vel = [velocities[i] for i in order]
    samples = [camm_gps_sample(p[5], p[2], p[1], p[3], *v) for p, v in zip(pts, vel)]

    def edit(moov: Box, f, pos: int):
        if samples:
            body = b"".join(samples)
            hdr = struct.pack(">I4s", 8 + len(body), b"mdat") if 8 + len(body) <= UINT32_MAX \
                else struct.pack(">I4sQ", 1, b"mdat", 16 + len(body))
            f.write(hdr + body)
            created, modified = mvhd_times(moov)
            trak = build_camm_trak([max(0.0, p[0] / 1000.0) for p in pts], [len(s) for s in samples],
                                   pos + len(hdr), _next_track_id(moov, bump=True), mvhd_timescale(moov),
                                   video_end(moov), created, modified)
            udta = [i for i, c in enumerate(moov.children) if c.type == b"udta"]
            moov.children.insert(udta[0] if udta else len(moov.children), trak)
        merge_udta(moov, udta_boxes(make, model, xyz))

    _rewrite_moov_at_end(path, edit)
    return len(samples)


# --- reading CAMM back ---------------------------------------------------------------------

def sample_table(stbl: Box):
    """[(offset, size, media_time)] for every sample of a track."""
    offs = chunk_offsets(stbl)
    d = stbl.find(b"stsc").data
    n = struct.unpack(">I", d[4:8])[0]
    stsc = [struct.unpack(">3I", d[8 + 12 * i:20 + 12 * i]) for i in range(n)]
    d = stbl.find(b"stsz").data
    size, count = struct.unpack(">2I", d[4:12])
    sizes = [size] * count if size else list(struct.unpack(f">{count}I", d[12:12 + 4 * count]))
    d = stbl.find(b"stts").data
    n = struct.unpack(">I", d[4:8])[0]
    times, t = [], 0
    for i in range(n):
        c, delta = struct.unpack(">2I", d[8 + 8 * i:16 + 8 * i])
        for _ in range(c):
            times.append(t)
            t += delta
    out, si = [], 0
    for ci, off in enumerate(offs):
        spc = 0
        for first, per, _ in stsc:
            if first - 1 <= ci:
                spc = per
        for _ in range(spc):
            if si >= len(sizes):
                break
            out.append((off, sizes[si], times[si] if si < len(times) else t))
            off += sizes[si]
            si += 1
    return out


def read_camm(path: Path) -> list[dict] | None:
    """
    The GPS samples (types 5 and 6) of the file's CAMM track, as dicts with `ms` (presentation
    time in the video, edit list applied), lat, lon, alt (None for fix type 2 / type 5 without
    altitude), epoch (time_gps_epoch; None for type 5), fix, ve, vn. None if there is no CAMM
    track.
    """
    _, _, moov = read_moov(path)
    movie_ts = mvhd_timescale(moov)
    for trak in moov.find_all(b"trak"):
        stsd = trak.find(b"mdia", b"minf", b"stbl", b"stsd")
        if stsd is None or stsd.data[12:16] != b"camm":
            continue
        media_ts = mdhd_timescale(trak)
        offset, media_start = 0.0, 0
        elst = trak.find(b"edts", b"elst")
        if elst is not None:
            d = elst.data
            n = struct.unpack(">I", d[4:8])[0]
            w = 16 if d[0] else 8
            fmt = ">qq" if d[0] else ">ii"
            for i in range(n):
                seg, mt = struct.unpack(fmt, d[8 + (w + 4) * i:8 + (w + 4) * i + w])
                if mt == -1:
                    offset += seg / movie_ts
                else:
                    media_start = mt
                    break
        out = []
        with open(path, "rb") as f:
            for off, size, mt in sample_table(trak.find(b"mdia", b"minf", b"stbl")):
                f.seek(off)
                blk = f.read(size)
                typ = struct.unpack("<H", blk[2:4])[0]
                t = offset + (mt - media_start) / media_ts
                if typ == 6 and len(blk) >= CAMM_GPS_SAMPLE.size:
                    _, epoch, fix, lat, lon, alt, _ha, _va, ve, vn, _vu, _sa = CAMM_GPS_SAMPLE.unpack(blk[:60])
                    out.append({"ms": t * 1000, "lat": lat, "lon": lon, "alt": alt if fix >= 3 else None,
                                "epoch": epoch, "fix": fix, "ve": ve, "vn": vn})
                elif typ == 5 and len(blk) >= 28:
                    lat, lon, alt = struct.unpack("<3d", blk[4:28])
                    out.append({"ms": t * 1000, "lat": lat, "lon": lon, "alt": alt, "epoch": None,
                                "fix": None, "ve": 0.0, "vn": 0.0})
        return out
    return None
