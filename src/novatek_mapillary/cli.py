"""novatek-mapillary: Novatek dashcam clips -> mapillary_tools video descriptions."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import build as build_mod
from . import novatek
from .dat import default_dat_dir, load_track
from .geo import format_tz, infer_tz, name_time, parse_tz
from .prune import parse_zone, prune
from .verify import clip_start, verify


def _tz(s: str):
    return None if s == "auto" else parse_tz(s)


def cmd_parse(a):
    """Print the GPS records embedded in clips (one line per record, or JSON)."""
    for path in a.clips:
        info = novatek.parse_clip(path)
        if info is None:
            print(f"{path}: no moov box (not an MP4, or truncated)", file=sys.stderr)
            continue
        recs = novatek.read_records(path, info)
        fit = novatek.clip_track(path, min_points=1)
        if a.json:
            print(json.dumps({"clip": str(path), "duration": info.duration,
                              "start_epoch": fit[0] if fit else None,
                              "records": [r.__dict__ for r in recs]}))
            continue
        start = dt.datetime.fromtimestamp(fit[0], dt.timezone.utc).isoformat() if fit else "unknown"
        print(f"{path}: {info.duration:.1f} s, {len(info.gps_index)} index entries, "
              f"{len(recs)} sentences, video starts {start}")
        for r in recs:
            t = dt.datetime.fromtimestamp(r.epoch, dt.timezone.utc).strftime("%H:%M:%S")
            crs = f"{r.course:6.1f}" if r.course is not None else "     -"
            print(f"  {r.k:3d}  video {r.video_t:6.2f} s  fix {t}Z  {'A' if r.valid else 'V'}  "
                  f"{r.lat:11.6f} {r.lon:11.6f}  {r.speed:5.1f} m/s  {crs}")


def cmd_build(a):
    out = a.out or a.root.resolve().parent / "desc"
    build_mod.run(a.root, out, _tz(a.tz), a.dat_dir, make=a.make, model=a.model, lead=a.lead,
                  min_points=a.min_points, dat_offset=a.dat_offset)


def _load_dat_for(descs, dat_dir, tz):
    if dat_dir is None:
        dat_dir = default_dat_dir({Path(d["filename"]).parent for d in descs})
    if dat_dir is None:
        return []
    if tz is None:
        tz = infer_tz((clip_start(d), name_time(Path(d["filename"]).name)) for d in descs
                      if name_time(Path(d["filename"]).name) is not None)
    return load_track(dat_dir, tz, -1.0, verbose=False) if tz is not None else []


def cmd_verify(a):
    bad = 0
    for path in a.desc:
        descs = json.loads(path.read_text())
        tz = _tz(a.tz)
        track = _load_dat_for(descs, a.dat_dir, tz)
        lines, problems = verify(descs, track, tz, a.max_gap, a.max_speed, a.dat_tolerance)
        print(f"{path.stem}: {len(descs)} clips checked")
        print("\n".join(lines))
        for name, issues in problems[:25]:
            print(f"  {name}: " + "; ".join(issues))
        if len(problems) > 25:
            print(f"  ... {len(problems) - 25} more")
        print("OK" if not problems else f"WARNING: {len(problems)} clips with issues")
        bad += bool(problems)
    sys.exit(1 if bad else 0)


def cmd_prune(a):
    for path in a.desc:
        descs = json.loads(path.read_text())
        tz = _tz(a.tz) or infer_tz((clip_start(d), name_time(Path(d["filename"]).name)) for d in descs
                                   if name_time(Path(d["filename"]).name) is not None) or dt.timezone.utc
        kept, lines = prune(descs, a.buffer, a.min_stop, a.gap,
                            [parse_zone(z) for z in a.exclude_near],
                            [parse_zone(z) for z in a.cut_after_entering], tz, path.stem)
        out = a.out or path.resolve().parent / "upload"
        out.mkdir(parents=True, exist_ok=True)
        (out / path.name).write_text(json.dumps(kept, separators=(",", ":")))
        (out / f"{path.stem}.pruned.txt").write_text("\n".join(lines) + "\n")
        print("\n".join(lines[:-1]))
        print(f"  times in UTC{format_tz(tz)}  -> {out / path.name}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="novatek-mapillary", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    tz_help = "camera clock UTC offset, e.g. +10:00, or 'auto' to infer it from the embedded GPS [default: auto]"

    p = sub.add_parser("parse", help="print the GPS records embedded in clips")
    p.add_argument("clips", type=Path, nargs="+")
    p.add_argument("--json", action="store_true", help="one JSON object per clip")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("build", help="write one description JSON per day",
                       description=build_mod.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", type=Path, help="card copy: every .mp4 under it is used (any folder)")
    p.add_argument("--out", type=Path, default=None, help="output dir [default: <root>/../desc]")
    p.add_argument("--tz", default="auto", help=tz_help)
    p.add_argument("--dat-dir", type=Path, default=None,
                   help="folder of *.dat track files [default: a GPS/ folder next to the clips, if any]")
    p.add_argument("--make", default=None, help="MAPDeviceMake [default: omitted]")
    p.add_argument("--model", default=None, help="MAPDeviceModel [default: omitted]")
    p.add_argument("--lead", type=float, default=0.0,
                   help="seconds of .dat track to include before/after a clip that falls back to it")
    p.add_argument("--min-points", type=int, default=5, help="skip clips with fewer track points")
    p.add_argument("--dat-offset", type=float, default=-1.0,
                   help="seconds added to .dat row times; rows are stamped 1 s after the fix they contain [default: -1]")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("verify", help="sanity-check description JSON (exit 1 on problems)")
    p.add_argument("desc", type=Path, nargs="+")
    p.add_argument("--tz", default="auto", help=tz_help)
    p.add_argument("--dat-dir", type=Path, default=None, help="for the cross-check [default: a GPS/ folder next to the clips]")
    p.add_argument("--max-gap", type=float, default=10.0, help="seconds between points")
    p.add_argument("--max-speed", type=float, default=60.0, help="m/s implied between points")
    p.add_argument("--dat-tolerance", type=float, default=5.0, help="per-clip median .dat distance (m) considered fine")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="drop clips around parking, stops and private places",
                       description=sys.modules[prune.__module__].__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("desc", type=Path, nargs="+", help="per-day description JSON(s) from build")
    p.add_argument("--out", type=Path, default=None, help="output dir [default: <desc dir>/upload]")
    p.add_argument("--buffer", type=float, default=120.0, help="seconds cut before/after each parking window")
    p.add_argument("--min-stop", type=float, default=180.0, help="stationary seconds that count as parked")
    p.add_argument("--gap", type=float, default=5.0, help="timeline gap in seconds that counts as camera off")
    p.add_argument("--exclude-near", action="append", default=[], metavar="LAT,LON,METRES",
                   help="drop clips with any point within METRES of LAT,LON (repeatable)")
    p.add_argument("--cut-after-entering", action="append", default=[], metavar="LAT,LON,METRES",
                   help="drop every clip from the first time the track is within METRES of LAT,LON "
                        "(minus --buffer) to the end of the day (repeatable)")
    p.add_argument("--tz", default="auto", help="timezone for the report; " + tz_help)
    p.set_defaults(func=cmd_prune)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
