"""
Re-encode Novatek dashcam clips for keeping, with the GPS carried along as a CAMM track.

For every .mp4 under SRC, write one clip at the same relative path under DST: the same frames
and timestamps re-encoded (AV1 or HEVC), the audio copied, the fitted GPS track from `build` as
a CAMM track (which Mapillary, Google Street View and mapillary_tools read), creation_time set
to the fitted clip start in UTC (the filename time when a clip has no track), and make/model in
udta. Per day, DST/<day>.gps.json keeps the raw embedded records. DST/archive-manifest.jsonl
has one line per finished clip (source SHA-256, sizes, settings, verify result), so a rerun
skips what's done, and a renamed source folder costs no re-encode. SRC is never written to.

Exit codes: 0 all good, 1 some clip failed or didn't verify, 75 the source disappeared (USB
drop: remount and rerun), 130 interrupted. See docs/archive-design.md.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import build as build_mod
from . import mp4write, novatek
from .dat import default_dat_dir
from .geo import format_tz, infer_tz, name_time, parse_tz
from .verify import clip_start

EX_TEMPFAIL = 75
MANIFEST = "archive-manifest.jsonl"
# A tripwire for a broken encode, not a quality target: archives at the default settings score
# well above it, garbage well below.
SSIM_WARN = 0.80
DEFAULT_HEIGHT = 1440   # at a 4-5x reduction 4K blocks visibly; 1440p reads clean, slightly soft
DEFAULT_PRESET = {"libsvtav1": "6", "libx265": "slow", "libx264": "medium"}

ENCODERS = {
    "av1": {"nvenc": "av1_nvenc", "software": "libsvtav1"},
    "hevc": {"nvenc": "hevc_nvenc", "software": "libx265"},
    "h264": {"nvenc": "h264_nvenc", "software": "libx264"},  # for tests and old players
}
# Defaults, chosen on 4K30 dashcam footage for about a 4-5x reduction at 1440p 10-bit
# (docs/archive-design.md, "Decisions").
# - NVENC: always with lookahead + spatial and temporal AQ, which save bits at the same quality.
# - libsvtav1: CRF, not -b:v; SVT's VBR undershoots badly on short clips.
# - libx265 / libx264: untuned (libx264 is the test stand-in).
DEFAULT_QUALITY = {
    ("av1", "nvenc"): 44, ("av1", "software"): 38,
    ("hevc", "nvenc"): 37, ("hevc", "software"): 24,
    ("h264", "nvenc"): 23, ("h264", "software"): 23,
}


# Manifest statuses. "ok": archived and verified. "verify-failed": archived but didn't verify;
# the next run re-encodes it once ("attempts" counts encodes of the same source). "gave-up": the
# retry didn't verify either; skipped from then on (delete the archive file to try again).
# "failed": the encode itself failed; retried on every run.
DONE = ("ok", "gave-up")
ARCHIVED = ("ok", "verify-failed", "gave-up")
MAX_ATTEMPTS = 2


class SourceGone(Exception):
    """The source disk went away (exit 75, so a wrapper can remount and rerun)."""


class EncodeError(Exception):
    pass


@dataclasses.dataclass
class Options:
    codec: str = "av1"
    encoder: str = "auto"          # auto | nvenc | software
    quality: int | None = None
    bitrate: str | None = None
    ten_bit: bool | None = None    # None: 10-bit for av1/hevc, 8-bit for h264 and copy
    height: int | None = None      # None: DEFAULT_HEIGHT; 0: keep the source size. Never upscales.
    preset: str | None = None      # software encoders only; None: DEFAULT_PRESET
    faststart: bool = True
    copy_other: bool = False
    make: str | None = None
    model: str | None = None
    jobs: int | None = None        # None: default_jobs() for the chosen encoder
    verify_only: bool = False
    quick: bool = False
    dry_run: bool = False
    desc_dir: Path | None = None
    tz: dt.timezone | None = None
    dat_dir: Path | None = None
    ssim_warn: float = SSIM_WARN
    verbose: bool = True


# --- small helpers ---------------------------------------------------------------------------

def _say(opts: Options, *a):
    if opts.verbose:
        print(*a, flush=True)


def sha256(path: Path, bufsize: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def parse_rate(s: str) -> int:
    """'8M', '6000k', '6500000' -> bits per second."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", s)
    if not m:
        raise ValueError(f"bad bitrate {s!r}")
    return int(float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()])


def iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fsync(path: Path):
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def day_of(clip: Path) -> str:
    t = name_time(clip.name)
    return t.strftime("%Y%m%d") if t else "undated"


# --- the source going away -------------------------------------------------------------------

class SourceWatch:
    """After an OSError on a source file: is the source gone (USB drop), or just this clip bad?
    If SRC is a mount point it must still be one; otherwise the clip's folder must still exist."""

    def __init__(self, src: Path):
        self.src = src
        self.was_mount = os.path.ismount(src)

    def gone(self, clip: Path | None = None) -> bool:
        if self.was_mount:
            return not os.path.ismount(self.src)
        return not (clip.parent if clip is not None else self.src).is_dir()

    def check(self, clip: Path | None = None):
        if self.gone(clip):
            raise SourceGone(f"{self.src} is gone")


# --- the manifest ----------------------------------------------------------------------------

class Manifest:
    def __init__(self, dst: Path):
        self.path = dst / MANIFEST
        self.lines: list[dict] = []
        self.lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line cut off by a crash
                if isinstance(rec, dict) and "path" in rec:
                    self.lines.append(rec)

    def latest(self) -> dict[str, dict]:
        out = {}
        for rec in self.lines:
            out[rec["path"]] = rec
        return out

    def by_hash(self) -> dict[str, dict]:
        out = {}
        for rec in self.lines:
            if rec.get("sha256") and rec.get("status") in DONE:
                out[rec["sha256"]] = rec
        return out

    def last_tz(self) -> dt.timezone | None:
        for rec in reversed(self.lines):
            if rec.get("tz"):
                return parse_tz(rec["tz"])
        return None

    def append(self, rec: dict):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.lines.append(rec)


# --- encoding --------------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _ffmpeg_encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@functools.lru_cache(maxsize=None)
def encoder_works(name: str) -> bool:
    """Listed by ffmpeg, and a one-frame test encode succeeds (NVENC is listed even without a
    usable GPU)."""
    if not re.search(rf"\s{re.escape(name)}\s", _ffmpeg_encoders()):
        return False
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=256x256:r=30",
                        "-frames:v", "1", "-c:v", name, "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


def choose_encoder(opts: Options) -> tuple[str, str]:
    """(kind, ffmpeg encoder name) for opts.codec and opts.encoder; ('copy', 'copy') for a remux."""
    if opts.codec == "copy":
        return "copy", "copy"
    kinds = ["nvenc", "software"] if opts.encoder == "auto" else [opts.encoder]
    for kind in kinds:
        name = ENCODERS[opts.codec][kind]
        if encoder_works(name):
            return kind, name
    raise SystemExit(f"no working {opts.codec} encoder ({', '.join(ENCODERS[opts.codec][k] for k in kinds)}); "
                     "check `ffmpeg -encoders`")


def check_options(opts: Options) -> None:
    """Reject combinations that make no sense (SystemExit with the reason)."""
    if opts.codec == "copy":
        given = [f for f, v in (("--quality", opts.quality is not None), ("--bitrate", opts.bitrate),
                                ("--ten-bit", opts.ten_bit), ("--height", opts.height),
                                ("--preset", opts.preset)) if v]
        if given:
            raise SystemExit(f"--codec copy only remuxes: {', '.join(given)} can't apply")
    if opts.quality is not None and opts.bitrate:
        raise SystemExit("--quality and --bitrate are exclusive")
    if opts.height and (opts.height < 16 or opts.height % 2):
        raise SystemExit("--height must be 0 (keep the source size) or an even number of at least 16")
    if opts.preset is not None and opts.encoder == "nvenc":
        raise SystemExit("--preset is for the software encoders (NVENC always runs p6)")
    if opts.jobs is not None and opts.jobs < 1:
        raise SystemExit("--jobs must be at least 1")


def check_encoder(opts: Options, kind: str, name: str) -> None:
    """Checks that need the chosen encoder (with --encoder auto it is only known now)."""
    if opts.preset is not None:
        if kind == "nvenc":
            raise SystemExit("--preset is for the software encoders, but --encoder auto picked "
                             f"{name}; add --encoder software, or drop --preset")
        if name == "libsvtav1" and not re.fullmatch(r"-?\d+", opts.preset):
            raise SystemExit("libsvtav1 --preset is a number (0 slowest .. 13 fastest)")
    if kind == "nvenc" and opts.codec == "h264" and ten_bit(opts):
        raise SystemExit("h264_nvenc can't encode 10-bit; add --no-ten-bit")


def ten_bit(opts: Options) -> bool:
    if opts.codec == "copy":
        return False
    return opts.ten_bit if opts.ten_bit is not None else opts.codec in ("av1", "hevc")


def target_height(opts: Options) -> int | None:
    """The height to scale down to, or None to keep the source size."""
    if opts.codec == "copy":
        return None
    h = opts.height if opts.height is not None else DEFAULT_HEIGHT
    return h or None


def preset(opts: Options, name: str) -> str | None:
    return opts.preset if opts.preset is not None else DEFAULT_PRESET.get(name)


def default_jobs(kind: str, cpus: int | None = None) -> int:
    """Clips encoded in parallel. NVENC: 1 (consumer cards allow few sessions, and one already
    keeps the encoder busy). A remux is disk-bound: 1. Software: SVT-AV1 at 4K already spreads one
    encode over many threads, so only a big machine gains from more: one job per 8 CPUs, 1 to 4."""
    if kind != "software":
        return 1
    cpus = cpus if cpus is not None else (os.cpu_count() or 1)
    return max(1, min(4, cpus // 8))


def video_args(codec: str, kind: str, name: str, quality: int | None, bitrate: str | None,
               ten_bit: bool, gop: int, height: int | None = None, preset: str | None = None) -> list[str]:
    """ffmpeg video options. `height` scales down (keeping the aspect, even width)."""
    if kind == "copy":
        return ["-c:v", "copy"]
    q = quality if quality is not None else DEFAULT_QUALITY[(codec, kind)]
    rate = parse_rate(bitrate) if bitrate else None
    a = ["-c:v", name]
    if kind == "nvenc":
        a += ["-preset", "p6", "-tune", "hq", "-multipass", "fullres", "-rc", "vbr",
              "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1"]
        a += (["-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)] if rate
              else ["-cq", str(q), "-b:v", "0"])
        a += ["-pix_fmt", "p010le" if ten_bit else "yuv420p"]
    else:
        if name == "libsvtav1":
            a += ["-preset", preset or DEFAULT_PRESET[name]] + (["-b:v", str(rate)] if rate else ["-crf", str(q)])
        elif name == "libx265":
            a += ["-preset", preset or DEFAULT_PRESET[name], "-tag:v", "hvc1", "-x265-params", "log-level=error"]
            a += (["-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)] if rate
                  else ["-crf", str(q)])
        else:  # libx264
            a += ["-preset", preset or DEFAULT_PRESET[name]]
            a += (["-b:v", str(rate), "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2)] if rate
                  else ["-crf", str(q)])
        a += ["-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
    if height:
        a += ["-vf", f"scale=-2:{height}"]
    return a + ["-g", str(gop)]


def encode(src: Path, out: Path, vargs: list[str], kind: str, timescale: int, creation: str | None):
    """ffmpeg: same frames and timestamps (passthrough, the source's video timescale), audio
    copied, moov last (no +faststart: the CAMM track is appended after it)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if kind == "nvenc":
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-dn", *vargs, "-c:a", "copy"]
    if kind != "copy":
        cmd += ["-fps_mode", "passthrough"]
    cmd += ["-video_track_timescale", str(timescale)]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd += ["-f", "mp4", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-15:])
        raise EncodeError(f"ffmpeg exited {r.returncode}: {tail}")


# --- verification ----------------------------------------------------------------------------

def probe(path: Path) -> dict:
    """Video packet count, first/last PTS (seconds), frame duration and stream duration; audio
    packet count."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=index,codec_type,time_base,duration,avg_frame_rate,width,height:packet=stream_index,pts",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise EncodeError(f"ffprobe failed on {path.name}: {r.stderr.strip()[-300:]}")
    j = json.loads(r.stdout)
    streams = j.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    out = {"frames": 0, "audio_packets": 0, "first_pts": None, "last_pts": None, "duration": None, "frame": None,
           "width": None, "height": None}
    if v is not None:
        out["width"], out["height"] = v.get("width"), v.get("height")
        num, den = map(int, v["time_base"].split("/"))
        pts = [int(p["pts"]) for p in j.get("packets", []) if p["stream_index"] == v["index"] and "pts" in p]
        out["frames"] = len(pts)
        if pts:
            out["first_pts"], out["last_pts"] = min(pts) * num / den, max(pts) * num / den
        out["duration"] = float(v["duration"]) if v.get("duration") else None
        fn, fd = map(int, v.get("avg_frame_rate", "0/1").split("/"))
        out["frame"] = fd / fn if fn else None
    if a is not None:
        out["audio_packets"] = sum(1 for p in j.get("packets", []) if p["stream_index"] == a["index"])
    return out


def ssim_spots(src: Path, out: Path, times: list[float],
               size: tuple[int, int] | None = None) -> tuple[list[float], str | None]:
    """SSIM of the frame at each time in `times`, source vs archive, in one ffmpeg run (each
    input seeks on its own, so only a GOP per spot is decoded). With `size` (the source's width
    and height), the archive is scaled back up to it first, for a downscaled archive. Returns
    (values, None), or (whatever was read, the error) if the comparison didn't complete."""
    cmd, graph, outs = ["ffmpeg", "-nostdin", "-hide_banner"], [], []
    up = f"scale={size[0]}:{size[1]}:flags=bicubic," if size else ""
    for i, t in enumerate(times):
        cmd += ["-ss", f"{t:.3f}", "-i", str(src), "-ss", f"{t:.3f}", "-i", str(out)]
        graph.append(f"[{2 * i}:v]format=yuv420p[a{i}];[{2 * i + 1}:v]{up}format=yuv420p[b{i}];[a{i}][b{i}]ssim[s{i}]")
        outs += ["-map", f"[s{i}]", "-frames:v", "1", "-f", "null", "-"]
    r = subprocess.run(cmd + ["-filter_complex", ";".join(graph)] + outs, capture_output=True, text=True)
    vals = [float(v) for v in re.findall(r"SSIM .*?All:([\d.]+)", r.stderr)]
    err = None if r.returncode == 0 and len(vals) == len(times) else \
        f"ffmpeg exited {r.returncode}: " + " ".join(r.stderr.strip().splitlines()[-3:])[-200:]
    return vals, err


def compare_gps(points: list | None, camm: list[dict] | None) -> list[str]:
    """Every fix must match the description: same count, lat/lon to 1e-7, time to 1 ms."""
    if not points:
        return [] if not camm else [f"unexpected CAMM track with {len(camm)} points"]
    if camm is None:
        return ["no CAMM track"]
    pts = sorted(points, key=lambda p: p[0])
    if len(camm) != len(pts):
        return [f"CAMM has {len(camm)} points, description {len(pts)}"]
    for p, c in zip(pts, camm):
        if abs(c["lat"] - p[2]) > 1e-7 or abs(c["lon"] - p[1]) > 1e-7:
            return [f"CAMM position differs at {p[0]:.0f} ms"]
        if abs(c["ms"] - p[0]) > 1.0:
            return [f"CAMM time {c['ms']:.1f} ms vs {p[0]:.1f} ms"]
        if c["epoch"] is not None and abs(c["epoch"] - p[5]) > 1e-6:
            return [f"CAMM epoch differs at {p[0]:.0f} ms"]
    return []


def mapillary_camm(path: Path) -> list[dict] | None | str:
    """The CAMM points as mapillary_tools' parser sees them (it drops samples outside the edit
    list), or 'unavailable' when mapillary_tools isn't installed."""
    try:
        from mapillary_tools.camm import camm_parser  # type: ignore
    except Exception:
        return "unavailable"
    with open(path, "rb") as f:
        info = camm_parser.extract_camm_info(f)
    if info is None:
        return None
    # 0.14 files type-6 points under mini_gps (CAMMGPSPoint subclasses Point): take both
    got = sorted([*(info.gps or []), *(info.mini_gps or [])], key=lambda q: q.time)
    return [{"ms": g.time * 1000, "lat": g.lat, "lon": g.lon,
             "epoch": getattr(g, "time_gps_epoch", None)} for g in got]


def verify_clip(src: Path, out: Path, points: list | None, quick: bool = False,
                ssim_warn: float = SSIM_WARN, ssim: bool = True) -> tuple[list[str], dict]:
    """(issues, facts) comparing an archived clip with its source and description points.
    `ssim=False` (a remux: the frames are the source's) records SSIM as null; with `ssim=True` a
    comparison that fails is an issue, never a silent null."""
    issues, facts = [], {}
    a, b = probe(src), probe(out)
    facts.update(frames=b["frames"], audio_packets=b["audio_packets"])
    if a["frames"] != b["frames"]:
        issues.append(f"{b['frames']} video packets, source has {a['frames']}")
    for k in ("first_pts", "last_pts"):
        if a[k] is None or b[k] is None or abs(a[k] - b[k]) > 1e-6:
            issues.append(f"{k} {b[k]} vs source {a[k]}")
    if a["duration"] is not None and b["duration"] is not None:
        if abs(a["duration"] - b["duration"]) > (a["frame"] or 0.04) + 1e-6:
            issues.append(f"duration {b['duration']:.3f} s vs source {a['duration']:.3f} s")
    if a["audio_packets"] != b["audio_packets"]:
        issues.append(f"{b['audio_packets']} audio packets, source has {a['audio_packets']}")

    camm = mp4write.read_camm(out)
    facts["camm"] = len(camm) if camm is not None else None
    issues += compare_gps(points, camm)
    mt = mapillary_camm(out)
    facts["mapillary_tools"] = "unavailable" if mt == "unavailable" else "checked"
    if mt != "unavailable":
        issues += [f"mapillary_tools: {i}" for i in compare_gps(points, mt)]

    if not quick:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
        if r.returncode != 0 or r.stderr.strip():
            issues.append("decode errors: " + r.stderr.strip()[-200:])
    facts["height"] = b["height"]
    dur = b["duration"] or 0.0
    size = (a["width"], a["height"]) if a["height"] and (a["width"], a["height"]) != (b["width"], b["height"]) else None
    vals, err = ssim_spots(src, out, [dur * f for f in (0.1, 0.5, 0.9)], size) if ssim else ([], None)
    if ssim and not dur:
        err = "no video duration to pick frames from"
    if err:  # the quality tripwire must not switch itself off quietly
        issues.append(f"SSIM comparison failed: {err}")
    facts["ssim"] = round(min(vals), 4) if vals and not err else None
    if facts["ssim"] is not None and facts["ssim"] < ssim_warn:
        issues.append(f"SSIM {facts['ssim']:.3f} below {ssim_warn}")
    facts["src_bytes"], facts["archive_bytes"] = src.stat().st_size, out.stat().st_size
    if dur:
        facts["src_mbps"] = round(facts["src_bytes"] * 8 / dur / 1e6, 2)
        facts["mbps"] = round(facts["archive_bytes"] * 8 / dur / 1e6, 2)
    return issues, facts


# --- GPS for each clip -----------------------------------------------------------------------

@dataclasses.dataclass
class Track:
    desc: dict | None            # the description (None: no usable track)
    reason: str | None = None    # why not
    fixes: list | None = None    # novatek.Record list from clip_track, for velocities
    info: novatek.ClipInfo | None = None  # the parsed container, read once per clip


def velocities(points: list, fixes: list | None) -> list[tuple[float, float]]:
    """(east, north) per point from the RMC speed and course of the fix it was built from
    (matched by position, in order; zeros for points from the .dat fallback)."""
    out, j = [], 0
    fixes = fixes or []
    for p in points:
        v = (0.0, 0.0)
        for jj in range(j, len(fixes)):
            f = fixes[jj]
            if f.lat == p[2] and f.lon == p[1]:
                v = mp4write.velocity(f.speed, f.course)
                j = jj + 1
                break
        out.append(v)
    return out


def _load_desc_dir(d: Path):
    """The descriptions `build` wrote: the per-day YYYYMMDD.json files and skipped.json, not
    all.json (which repeats every clip and would make each filename ambiguous)."""
    by_path, by_name, skipped = {}, {}, {}
    files = [f for f in d.glob("*.json") if re.fullmatch(r"\d{8}\.json", f.name) or f.name == "skipped.json"]
    for f in sorted(files):
        try:
            items = json.loads(f.read_text())
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict) or "filename" not in it:
                continue
            fn = Path(it["filename"])
            if "MAPGPSTrack" in it:
                by_path[str(fn)] = it
                by_name.setdefault(fn.name, []).append(it)
            elif "reason" in it:
                skipped[str(fn)] = it["reason"]
                skipped[fn.name] = it["reason"]
    return by_path, by_name, skipped


def tracks_for(clips: list[Path], opts: Options, src: Path, watch: SourceWatch,
               fallback_tz: dt.timezone | None) -> tuple[dict[Path, Track], dt.timezone | None, dict]:
    """
    The description for each clip, from --desc or from build() run on whole days (so
    fix_clock_steps sees each clip's neighbours), plus the raw records per clip for the sidecar.
    Returns (tracks, tz, raw) where raw maps clip -> parse --json style dict.
    """
    out: dict[Path, Track] = {}
    raw: dict[Path, dict] = {}
    fixes_of: dict[Path, list] = {}
    info_of: dict[Path, novatek.ClipInfo | None] = {}
    fits: dict[Path, tuple | None] = {}
    sizes: dict[Path, int] = {}
    for c in clips:  # each clip is read once here; build() and the encode reuse it
        try:
            sizes[c] = c.stat().st_size
            info = novatek.parse_clip(c)
            recs = novatek.read_records(c, info) if info else []
            fit = novatek.clip_track(c, min_points=1, info=info, recs=recs) if info else None
        except OSError:
            watch.check(c)
            info, recs, fit = None, [], None
        except Exception:
            info, recs, fit = None, [], None
        raw[c] = {"clip": str(c.relative_to(src)), "duration": info.duration if info else None,
                  "start_epoch": fit[0] if fit else None, "records": [r.__dict__ for r in recs]}
        fixes_of[c] = fit[1] if fit else []
        info_of[c], fits[c] = info, fit

    tz = opts.tz
    need_build = list(clips)
    if opts.desc_dir is not None:
        by_path, by_name, skipped = _load_desc_dir(opts.desc_dir)
        need_build = []
        for c in clips:
            d = by_path.get(str(c.resolve()))
            if d is None and len(by_name.get(c.name, [])) == 1:
                d = by_name[c.name][0]
            size = sizes.get(c)
            if size is None:  # its read above failed and the source is still there
                try:
                    size = c.stat().st_size
                except OSError:
                    watch.check(c)
                    raise
            if d is not None and d.get("filesize") == size:
                out[c] = Track(d, fixes=fixes_of[c], info=info_of[c])
            elif d is not None:
                _say(opts, f"  {c.name}: description is for a file of {d.get('filesize')} bytes, "
                           f"this one is {size}; rebuilding its day")
                need_build.append(c)
            elif str(c.resolve()) in skipped or c.name in skipped:
                out[c] = Track(None, skipped.get(str(c.resolve())) or skipped.get(c.name), info=info_of[c])
            else:
                need_build.append(c)
        if need_build:  # whole days, for fix_clock_steps
            days = {day_of(c) for c in need_build}
            need_build = [c for c in clips if day_of(c) in days]

    if need_build:
        dat_dir = opts.dat_dir or default_dat_dir([src, *{c.parent for c in need_build}])
        try:
            per_day, skipped, tz = build_mod.build(need_build, tz, dat_dir, verbose=opts.verbose,
                                                   embedded={c: fits[c] for c in need_build})
        except SystemExit as e:  # no clip has embedded GPS to infer the camera clock from
            _say(opts, f"  {e}")
            per_day, skipped = {}, [{"filename": str(c), "reason": "no embedded fixes"} for c in need_build]
            tz = tz or fallback_tz
        watch.check()  # build swallows read errors per clip: make sure that wasn't a USB drop
        built = {d["filename"]: d for descs in per_day.values() for d in descs}
        reasons = {s["filename"]: s["reason"] for s in skipped}
        for c in need_build:
            if c in out and opts.desc_dir is not None and out[c].desc is not None:
                continue  # an up-to-date description from --desc wins
            d = built.get(str(c.resolve()))
            out[c] = Track(d, fixes=fixes_of[c], info=info_of[c]) if d is not None else \
                Track(None, reasons.get(str(c), "no usable track"), info=info_of[c])
    if tz is None:  # e.g. everything came from --desc: infer it as build would
        tz = infer_tz((clip_start(t.desc), name_time(c.name)) for c, t in out.items()
                      if t.desc is not None and name_time(c.name) is not None) or fallback_tz
    return out, tz, raw


def creation_of(clip: Path, track: Track, tz: dt.timezone | None) -> tuple[float | None, str | None]:
    """(UTC epoch of video time 0, 'gps' | 'filename'), or (None, None)."""
    if track.desc is not None:
        return clip_start(track.desc), "gps"
    named = name_time(clip.name)
    if named is not None and tz is not None:
        return named.replace(tzinfo=tz).timestamp(), "filename"
    return None, None


# --- one clip --------------------------------------------------------------------------------

def _part(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def _settings(opts: Options, kind: str, name: str) -> dict:
    """The encoder settings as recorded in the manifest."""
    if kind == "copy":
        return {"codec": "copy", "encoder": "copy", "kind": "copy", "faststart": opts.faststart}
    return {"codec": opts.codec, "encoder": name, "kind": kind,
            **({"bitrate": opts.bitrate} if opts.bitrate else
               {"quality": opts.quality if opts.quality is not None else DEFAULT_QUALITY[(opts.codec, kind)]}),
            **({"preset": preset(opts, name)} if kind == "software" else {}),
            "ten_bit": ten_bit(opts), "height": target_height(opts), "faststart": opts.faststart}


def _is_copy(rec: dict | None) -> bool:
    return bool(rec) and (rec.get("encoder") or {}).get("kind") == "copy"


def archive_clip(clip: Path, out: Path, track: Track, tz, opts: Options, kind: str, name: str,
                 watch: SourceWatch) -> dict:
    """Encode, add CAMM + udta, faststart, fsync, verify, rename. Returns the manifest facts."""
    t0 = time.monotonic()
    part = _part(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = track.info
        if info is None:  # not read with the others (e.g. it failed then): read it now
            try:
                info = novatek.parse_clip(clip)
            except OSError:
                watch.check(clip)
                raise
        if info is None or not info.timescale:
            raise EncodeError("no video track")
        ts = info.timescale
        fps = 1 / info.sample_delta if info.sample_delta else 30
        creation, creation_src = creation_of(clip, track, tz)
        want_h = target_height(opts)
        scale = want_h if want_h and info.height and info.height > want_h else None   # never upscale
        vargs = video_args(opts.codec, kind, name, opts.quality, opts.bitrate, ten_bit(opts), max(1, round(2 * fps)),
                           scale, preset(opts, name))
        try:
            encode(clip, part, vargs, kind, ts, iso_utc(creation) if creation is not None else None)
        except EncodeError:
            watch.check(clip)
            raise
        points = track.desc["MAPGPSTrack"] if track.desc is not None else []
        make = opts.make or (track.desc or {}).get("MAPDeviceMake")       # an explicit flag wins
        model = opts.model or (track.desc or {}).get("MAPDeviceModel")
        xyz = mp4write.iso6709(points[0][2], points[0][1], points[0][3]) if points else None
        mp4write.append_camm_track(part, points, velocities(points, track.fixes), make, model, xyz)
        if opts.faststart:
            mp4write.faststart(part)
        _fsync(part)
        issues, facts = verify_clip(clip, part, points, opts.quick, opts.ssim_warn, ssim=kind != "copy")
        os.replace(part, out)
        _fsync_dir(out.parent)
    except BaseException:
        if part.exists():
            part.unlink()
        raise
    return {
        "encoder": _settings(opts, kind, name),
        "creation_time": iso_utc(creation) if creation is not None else None,
        "creation_source": creation_src,
        "tz": format_tz(tz) if tz is not None else None,
        "camm_reason": None if points else (track.reason or "no usable track"),
        "verify": issues or "ok",
        "status": "ok" if not issues else "verify-failed",
        "seconds": round(time.monotonic() - t0, 1),
        **facts,
    }


# --- the run ---------------------------------------------------------------------------------

def _clean_parts(dst: Path, src: Path, opts: Options):
    """Delete the .part files a crash left under DST. Never descends into SRC, which may sit
    inside DST (archive never deletes anything under SRC)."""
    src_res = src.resolve()
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if (Path(root) / d).resolve() != src_res]
        for f in files:
            if f.endswith(".part") or f.endswith(".part.faststart"):
                p = Path(root) / f
                _say(opts, f"  removing stray {p.relative_to(dst)}")
                p.unlink()


def _base_record(rel: str, st: os.stat_result, sha: str | None) -> dict:
    return {"path": rel, "archive": rel, "src_size": st.st_size, "src_mtime_ns": st.st_mtime_ns,
            "sha256": sha, "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def _archive_ok(dst: Path, rec: dict) -> bool:
    p = dst / rec.get("archive", rec["path"])
    try:
        return p.is_file() and p.stat().st_size == rec.get("archive_bytes")
    except OSError:
        return False


def _copy_other(files: list[Path], src: Path, dst: Path, opts: Options, watch: SourceWatch) -> int:
    n = 0
    for f in files:
        out = dst / f.relative_to(src)
        try:
            st = f.stat()
            if out.exists() and out.stat().st_size == st.st_size and int(out.stat().st_mtime) == int(st.st_mtime):
                continue
            if opts.dry_run:
                _say(opts, f"  would copy {f.relative_to(src)}")
                n += 1
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            part = _part(out)
            shutil.copy2(f, part)
            os.replace(part, out)
            n += 1
        except OSError:
            watch.check(f)
            raise
    return n


def run(src: Path, dst: Path, opts: Options) -> int:
    src, dst = Path(src), Path(dst)
    check_options(opts)
    if not src.is_dir():
        print(f"{src}: not a directory", file=sys.stderr)
        return EX_TEMPFAIL
    if dst.resolve() == src.resolve():
        raise SystemExit("DST must differ from SRC")
    watch = SourceWatch(src)
    try:
        clips = build_mod.find_clips(src, exclude=dst)
        others = build_mod.find_other_files(src, exclude=dst) if opts.copy_other else []
    except OSError:
        watch.check()
        raise
    if not opts.dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        _clean_parts(dst, src, opts)
    manifest = Manifest(dst)
    try:
        if opts.verify_only:
            return _verify_all(clips, src, dst, opts, manifest, watch)
        return _archive_all(clips, others, src, dst, opts, manifest, watch)
    except SourceGone as e:
        print(f"source gone: {e}; remount it and rerun", file=sys.stderr)
        return EX_TEMPFAIL


def _plan(clips, src, dst, manifest, watch) -> list[Path]:
    """The clips that need work: every clip unless the manifest's line for its path is done (ok, or
    gave up after a retry), matches its size and mtime, and the archive is there with the recorded
    size. A verify-failed clip is work: it gets one more encode. Nothing is hashed here."""
    latest = manifest.latest()
    todo = []
    for c in clips:
        rel = str(c.relative_to(src))
        try:
            st = c.stat()
        except OSError:
            watch.check(c)
            raise
        rec = latest.get(rel)
        if not (rec and rec.get("status") in DONE and rec.get("src_size") == st.st_size
                and rec.get("src_mtime_ns") == st.st_mtime_ns and _archive_ok(dst, rec)):
            todo.append(c)
    return todo


def _archive_all(clips, others, src, dst, opts, manifest, watch) -> int:
    todo = _plan(clips, src, dst, manifest, watch)
    _say(opts, f"{len(clips)} clips under {src}: {len(clips) - len(todo)} done, {len(todo)} to check")
    if opts.dry_run:
        for c in todo:
            _say(opts, f"  would encode {c.relative_to(src)} (or move its archive, if the manifest has its hash)")
        if opts.copy_other:
            _copy_other(others, src, dst, opts, watch)
        return 0

    days = {day_of(c) for c in todo} | {day_of(c) for c in clips if not (dst / f"{day_of(c)}.gps.json").exists()}
    day_clips = [c for c in clips if day_of(c) in days]
    tracks, tz, raw = tracks_for(day_clips, opts, src, watch, manifest.last_tz()) if day_clips else ({}, opts.tz, {})
    for day in sorted(days):
        side = dst / f"{day}.gps.json"
        part = side.with_name(side.name + ".part")
        part.write_text(json.dumps([raw[c] for c in day_clips if day_of(c) == day], separators=(",", ":")))
        os.replace(part, side)

    by_hash = manifest.by_hash()
    latest = manifest.latest()
    present = {str(c.relative_to(src)) for c in clips}
    gave_up_now: list[str] = []
    claimed: set[str] = set()
    failed = 0
    kind = name = None
    jobs = opts.jobs or 1
    if todo:
        kind, name = choose_encoder(opts)
        check_encoder(opts, kind, name)
        jobs = opts.jobs or default_jobs(kind)
        h = target_height(opts)
        _say(opts, f"encoder: {name}" + (f" preset {preset(opts, name)}" if kind == "software" else "")
                   + ("" if kind == "copy" else f", {'10' if ten_bit(opts) else '8'}-bit, "
                      + (f"{h}p (no upscaling)" if h else "source size"))
                   + f", jobs {jobs}" + ("" if opts.jobs else " (default)")
                   + (f", camera clock UTC{format_tz(tz)}" if tz else ""))

    def encoder() -> tuple[str, str]:
        return kind, name

    def decide(c: Path) -> tuple[str | None, str]:
        """Hash the source (once: the manifest keeps it), then move a matching archive into
        place, or say whether to re-verify an unrecorded archive or encode."""
        rel = str(c.relative_to(src))
        try:
            sha = sha256(c)
            st = c.stat()
        except OSError:
            watch.check(c)
            raise
        prev = latest.get(rel)
        if prev and prev.get("status") == "verify-failed" and prev.get("sha256") == sha:
            return "retry", sha  # the archive there didn't verify: encode it again from scratch
        cand = by_hash.get(sha)
        if (cand and _archive_ok(dst, cand) and cand["archive"] not in claimed
                and (cand["archive"] == rel or cand["path"] not in present)):
            claimed.add(cand["archive"])
            new = {**cand, **_base_record(rel, st, sha)}
            if cand["archive"] != rel:
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst / cand["archive"], dst / rel)
                new["moved_from"] = cand["archive"]
                _say(opts, f"  moved {cand['archive']} -> {rel}")
            manifest.append(new)
            return None, sha
        return ("recover" if (dst / rel).is_file() else "encode"), sha

    def one(c: Path, what: str, sha: str) -> dict:
        rel = str(c.relative_to(src))
        base = _base_record(rel, c.stat(), sha)
        prev = latest.get(rel)
        attempts = (prev.get("attempts", 1) + 1) if what == "retry" and prev else 1
        if what == "recover":
            # a finished clip whose manifest line was lost (crash after the rename): re-verify
            pts = tracks[c].desc["MAPGPSTrack"] if tracks[c].desc else []
            issues, facts = verify_clip(c, dst / rel, pts, opts.quick, opts.ssim_warn, ssim=opts.codec != "copy")
            if not issues:
                creation, csrc = creation_of(c, tracks[c], tz)
                return {**base, "recovered": True, "encoder": _settings(opts, kind, name), "attempts": 1,
                        "creation_time": iso_utc(creation) if creation else None,
                        "creation_source": csrc, "tz": format_tz(tz) if tz else None,
                        "camm_reason": None if pts else tracks[c].reason, "verify": "ok", "status": "ok", **facts}
            _say(opts, f"  {rel}: existing archive doesn't verify ({'; '.join(issues)}); re-encoding")
        if what == "retry":
            _say(opts, f"  {rel}: its archive didn't verify last time; encoding it again (attempt {attempts})")
        k, n = encoder()
        rec = {**base, **archive_clip(c, dst / rel, tracks[c], tz, opts, k, n, watch), "attempts": attempts}
        if rec["status"] == "verify-failed" and attempts >= MAX_ATTEMPTS:
            rec["status"] = "gave-up"
        return rec

    def record(c: Path, sha: str | None, rec: dict | None, err: BaseException | None):
        nonlocal failed
        rel = str(c.relative_to(src))
        if err is not None:
            if isinstance(err, SourceGone):
                raise err
            watch.check(c)
            failed += 1
            try:
                base = _base_record(rel, c.stat(), sha)
            except OSError:  # the clip itself went away, the disk didn't
                base = {"path": rel, "archive": rel, "sha256": sha}
            rec = {**base, "status": "failed", "error": f"{type(err).__name__}: {err}"[-500:]}
            _say(opts, f"  {rel}: FAILED: {err}")
        else:
            if rec["status"] != "ok":
                failed += 1
            if rec["status"] == "gave-up":
                gave_up_now.append(rel)
            gps = f"{rec['camm']} GPS points" if rec.get("camm") else f"no GPS ({rec.get('camm_reason')})"
            ratio = rec["src_bytes"] / rec["archive_bytes"] if rec.get("archive_bytes") else 0
            _say(opts, f"  {rel}: {rec['status']}, {gps}, {rec.get('mbps')} Mbps, {ratio:.1f}x smaller, "
                       f"SSIM {rec.get('ssim')}"
                       + ("" if rec["verify"] == "ok" else f"; {'; '.join(rec['verify'])}"))
        manifest.append(rec)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        running: dict = {}

        def collect(block: bool):
            if not running:
                return
            done, _ = wait(running, timeout=None if block else 0, return_when=FIRST_COMPLETED)
            for fut in done:
                c, sha = running.pop(fut)
                try:
                    rec, err = fut.result(), None
                except Exception as e:  # this clip failed; the next may not
                    rec, err = None, e
                record(c, sha, rec, err)

        try:
            for c in todo:
                sha = None
                try:
                    what, sha = decide(c)
                    if what is None:
                        continue
                    if jobs <= 1:
                        record(c, sha, one(c, what, sha), None)
                        continue
                except SourceGone:
                    raise
                except Exception as e:
                    record(c, sha, None, e)
                    continue
                while len(running) >= jobs:
                    collect(block=True)
                running[ex.submit(one, c, what, sha)] = (c, sha)
                collect(block=False)
            while running:
                collect(block=True)
        except BaseException:
            ex.shutdown(wait=True, cancel_futures=True)
            raise

    if opts.copy_other:
        n = _copy_other(others, src, dst, opts, watch)
        _say(opts, f"copied {n} other file(s)")
    _summary(clips, src, dst, manifest, opts)
    return 1 if failed else 0


def _summary(clips, src, dst, manifest, opts):
    latest = manifest.latest()
    recs = [latest.get(str(c.relative_to(src))) for c in clips]
    done = [r for r in recs if r and r.get("status") in ARCHIVED]
    a = sum(r.get("src_bytes") or r.get("src_size", 0) for r in done)
    b = sum(r.get("archive_bytes") or 0 for r in done)
    nogps = sum(1 for r in done if not r.get("camm"))
    ssims = [r["ssim"] for r in done if r.get("ssim") is not None]
    _say(opts, f"archive: {len(done)}/{len(clips)} clips, {a / 1e9:.2f} GB -> {b / 1e9:.2f} GB"
               + (f" ({a / b:.1f}x)" if b else "") + f", {nogps} without GPS"
               + (f", worst SSIM {min(ssims):.3f}" if ssims else ""))
    gave_up = [r["path"] for r in done if r.get("status") == "gave-up"]
    if gave_up:
        _say(opts, f"GAVE UP on {len(gave_up)} clip(s) whose archive failed verification twice; they are "
                   f"skipped from now on (their manifest lines say why; delete the archive file to try "
                   f"again): " + ", ".join(gave_up[:10]) + (" ..." if len(gave_up) > 10 else ""))


def _verify_all(clips, src, dst, opts, manifest, watch) -> int:
    latest = manifest.latest()
    tracks, tz, _ = tracks_for(clips, opts, src, watch, manifest.last_tz()) if clips else ({}, None, {})

    def check(c: Path):
        rec = latest.get(str(c.relative_to(src)))
        if not rec or rec.get("status") not in ARCHIVED or not _archive_ok(dst, rec):
            return rec, None, None
        pts = tracks[c].desc["MAPGPSTrack"] if tracks[c].desc else []
        try:
            return rec, *verify_clip(c, dst / rec["archive"], pts, opts.quick, opts.ssim_warn, ssim=not _is_copy(rec))
        except OSError as e:
            watch.check(c)  # the source went: stop (exit 75); else this clip or its archive is bad
            return rec, [f"{type(e).__name__}: {e}"], {}
        except SourceGone:
            raise
        except Exception as e:  # e.g. a corrupt archive: read_camm raises ValueError / struct.error
            return rec, [f"unreadable archive: {type(e).__name__}: {e}"], {}

    bad = 0
    jobs = opts.jobs or default_jobs("software")   # verify decodes on the CPU
    _say(opts, f"verifying with {jobs} job(s)")
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for c, (rec, issues, facts) in zip(clips, ex.map(check, clips)):
            rel = str(c.relative_to(src))
            if issues is None:
                bad += 1
                _say(opts, f"  {rel}: not archived")
                continue
            bad += bool(issues)
            if not opts.dry_run:
                status = "ok" if not issues else ("gave-up" if rec.get("status") == "gave-up" else "verify-failed")
                manifest.append({**rec, **facts, "verify": issues or "ok", "status": status,
                                 "verified": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                 "quick": opts.quick})
            _say(opts, f"  {rel}: " + ("ok" if not issues else "; ".join(issues)))
    _summary(clips, src, dst, manifest, opts)
    _say(opts, "OK" if not bad else f"WARNING: {bad} clip(s) not archived or not verified")
    return 1 if bad else 0
