import datetime as dt
import json

import pytest
from synth_clip import DEMO_START, DEMO_TZ, demo_day, simulate, write_card

from novatek_mapillary import build, cli, dat, geo, prune, verify


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "card"
    clips = write_card(root, demo_day(), DEMO_TZ)
    return root, clips


def test_build_infers_timezone_and_uses_embedded_gps(card, tmp_path):
    root, clips = card
    per_day, skipped, tz = build.run(root, tmp_path / "desc", verbose=False)
    assert geo.format_tz(tz) == "+10:00"
    assert list(per_day) == ["20260115"] and not skipped
    descs = per_day["20260115"]
    assert len(descs) == len(clips)
    assert "MAPDeviceMake" not in descs[0]
    for d in descs:
        pts = d["MAPGPSTrack"]
        assert all(len(p) == 6 for p in pts)
        assert all(p[3] is not None for p in pts)          # altitude from the .dat
    on = json.loads((tmp_path / "desc" / "20260115.json").read_text())
    assert len(on) == len(clips)


def test_explicit_wrong_timezone_is_caught_by_verify(card, tmp_path):
    root, _ = card
    per_day, _, _ = build.run(root, tmp_path / "desc", tz=geo.parse_tz("+10:00"), verbose=False)
    descs = per_day["20260115"]
    _, problems = verify.verify(descs, [], geo.parse_tz("+11:00"))
    assert problems and "camera clock changed" in problems[0][1][-1]


def test_verify_passes_and_cross_checks_dat(card, tmp_path):
    root, _ = card
    per_day, _, tz = build.run(root, tmp_path / "desc", verbose=False)
    track = dat.load_track(root / "GPS", tz, verbose=False)
    lines, problems = verify.verify(per_day["20260115"], track, None)
    assert not problems
    assert any("vs .dat track" in l for l in lines)


def test_falls_back_to_dat_when_no_embedded_fix(tmp_path):
    root = tmp_path / "card"
    write_card(root, demo_day(), DEMO_TZ, clip_kwargs={0: {"valid": False}})
    per_day, skipped, _ = build.run(root, tmp_path / "desc", tz=DEMO_TZ, verbose=False)
    first = per_day["20260115"][0]
    assert not skipped and len(first["MAPGPSTrack"]) >= 55


def test_no_fix_and_no_dat_is_skipped(tmp_path):
    root = tmp_path / "card"
    write_card(root, demo_day(), DEMO_TZ, dat=False, clip_kwargs={0: {"valid": False}})
    per_day, skipped, _ = build.run(root, tmp_path / "desc", verbose=False)
    assert len(skipped) == 1 and "no embedded fixes" in skipped[0]["reason"]


def test_clip_entirely_inside_clock_step_is_reanchored(tmp_path):
    root = tmp_path / "card"
    day = simulate(DEMO_START, -33.0, 147.0, [("drive", 300, 25.0, 0.0)])
    write_card(root, day, DEMO_TZ, clip_kwargs={2: {"step": {k: 14 for k in range(0, 61)}}})
    per_day, _, _ = build.run(root, tmp_path / "desc", tz=DEMO_TZ, verbose=False)
    descs = per_day["20260115"]
    _, problems = verify.verify(descs, [], DEMO_TZ)
    assert not problems
    _, lines = prune.prune(descs, tz=DEMO_TZ)
    assert not any("camera off" in l for l in lines)       # no phantom gap


def test_prune_finds_stops_gaps_and_zones(card, tmp_path):
    root, _ = card
    per_day, _, _ = build.run(root, tmp_path / "desc", verbose=False)
    descs = per_day["20260115"]
    kept, lines = prune.prune(descs, tz=DEMO_TZ)
    report = "\n".join(lines)
    for why in ("day start", "day end", "stationary 6 min", "camera off 20 min"):
        assert why in report
    assert 0 < len(kept) < len(descs)
    # nothing kept within 120 s of the stop (08:10-08:16 local)
    stop0 = dt.datetime(2026, 1, 15, 8, 10, tzinfo=DEMO_TZ).timestamp()
    stop1 = dt.datetime(2026, 1, 15, 8, 16, tzinfo=DEMO_TZ).timestamp()
    for d in kept:
        for p in d["MAPGPSTrack"]:
            assert not (stop0 - 120 < p[5] < stop1 + 120)

    # an exclusion zone around a point on the last leg drops the clips that pass it
    last = kept[-1]["MAPGPSTrack"][30]
    kept2, lines2 = prune.prune(descs, tz=DEMO_TZ, exclude_near=[(last[2], last[1], 200)])
    assert len(kept2) < len(kept) and any("exclusion zones" in l for l in lines2)
    for d in kept2:
        assert all(geo.hav(p[2], p[1], last[2], last[1]) > 200 for p in d["MAPGPSTrack"])

    # cut-after-entering drops everything from the zone to the end of the day
    entry = kept[0]["MAPGPSTrack"][30]
    kept3, _ = prune.prune(descs, tz=DEMO_TZ, cut_after_entering=[(entry[2], entry[1], 100)])
    assert all(d["MAPGPSTrack"][-1][5] < entry[5] for d in kept3)


def test_dat_clock_step_is_undone():
    rows = [(1000 + i, 0.0, 0.0, 0.0, 10.0) for i in range(100)]
    rows[30:60] = [(r[0] + 14,) + r[1:] for r in rows[30:60]]
    fixed = dat.undo_clock_steps(rows)
    assert [r[0] for r in fixed] == list(range(1000, 1100))


def test_infer_tz_majority_and_half_hours():
    naive = dt.datetime(2026, 1, 15, 8, 0)
    adl = dt.timezone(dt.timedelta(hours=10, minutes=30))
    e = naive.replace(tzinfo=adl).timestamp()
    pairs = [(e + 0.6, naive)] * 5 + [(e - 3600, naive)]
    assert geo.format_tz(geo.infer_tz(pairs)) == "+10:30"
    assert geo.infer_tz([]) is None


def test_name_time_patterns():
    assert geo.name_time("20260927_120016_00229_N_A.MP4") == dt.datetime(2026, 9, 27, 12, 0, 16)
    assert geo.name_time("2026_0927_120016_001.MP4") == dt.datetime(2026, 9, 27, 12, 0, 16)
    assert geo.name_time("FILE0001.MP4") is None


def test_cli_end_to_end(card, tmp_path, capsys):
    root, clips = card
    out = tmp_path / "desc"
    cli.main(["parse", str(clips[0])])
    assert "60 sentences" in capsys.readouterr().out
    cli.main(["build", str(root), "--out", str(out), "--make", "Vantrue"])
    assert json.loads((out / "20260115.json").read_text())[0]["MAPDeviceMake"] == "Vantrue"
    with pytest.raises(SystemExit) as e:
        cli.main(["verify", str(out / "20260115.json")])
    assert e.value.code == 0
    cli.main(["prune", str(out / "20260115.json"), "--exclude-near=-33.0,147.0,300"])
    assert (out / "upload" / "20260115.json").exists()
    assert "stationary 6 min" in (out / "upload" / "20260115.pruned.txt").read_text()
