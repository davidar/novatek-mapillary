"""`archive` end to end on decodable synthetic clips (libx264 standing in for AV1/HEVC)."""
import datetime as dt
import json
import struct
import subprocess

import pytest
from synth_clip import DEMO_START, DEMO_TZ, have_ffmpeg, simulate, write_card, write_decodable_clip

from novatek_mapillary import archive, build, cli, mp4write, novatek

pytestmark = pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg and ffprobe")


def make_card(root, seconds=15, clip_kwargs=None, dat=True):
    day = simulate(DEMO_START, -33.0, 147.0, [("drive", seconds, 20.0, 45.0)])
    return write_card(root, day, DEMO_TZ, clip_seconds=5, writer=write_decodable_clip, min_seconds=3,
                      clip_kwargs=clip_kwargs, dat=dat)


def opts(**kw):
    kw = {"codec": "h264", "encoder": "software", "quick": True, "verbose": False, "jobs": 3, **kw}
    return archive.Options(**kw)


def manifest(dst):
    return [json.loads(line) for line in (dst / archive.MANIFEST).read_text().splitlines()]


def tags(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["format"].get("tags", {})


def expected(clips, tz=None):
    per_day, _, _ = build.build(clips, tz, verbose=False)
    return {d["filename"]: d for descs in per_day.values() for d in descs}


@pytest.fixture
def card(tmp_path):
    return tmp_path / "card", make_card(tmp_path / "card")


def test_archive_keeps_frames_audio_and_gps(card, tmp_path):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(quick=False, make="Vantrue", model="N4")) == 0
    want = expected(clips)
    recs = {r["path"]: r for r in manifest(dst)}
    assert len(recs) == 3
    for c in clips:
        rel = str(c.relative_to(src))
        out = dst / rel
        a, b = archive.probe(c), archive.probe(out)
        assert (a["frames"], a["audio_packets"], a["first_pts"], a["last_pts"]) == \
               (b["frames"], b["audio_packets"], b["first_pts"], b["last_pts"])
        assert b["frames"] == 150 and b["audio_packets"] > 0
        pts = want[str(c.resolve())]["MAPGPSTrack"]
        camm = mp4write.read_camm(out)
        assert archive.compare_gps(pts, camm) == []
        assert all(p["fix"] == 3 for p in camm)                 # altitude from the .dat
        assert abs(camm[0]["ve"] - camm[0]["vn"]) < 0.01 and camm[0]["ve"] > 10  # course 45 degrees at 20 m/s
        start = pts[0][5] - pts[0][0] / 1000
        t = tags(out)
        created = dt.datetime.fromisoformat(t["creation_time"].replace("Z", "+00:00")).timestamp()
        assert created == int(start)                             # mvhd holds whole seconds
        assert (t["make"], t["model"]) == ("Vantrue", "N4")
        assert t["location"].startswith("-32.99") and "+147.00" in t["location"]
        _, _, moov = mp4write.read_moov(out)
        kids = {k.type for k in moov.find(b"udta").children}
        assert {b"@mak", b"@mod", b"\xa9mak", b"\xa9mod", b"\xa9xyz"} <= kids
        types = [x[0] for x in mp4write.top_level(out)]
        assert types.index(b"moov") < types.index(b"mdat")      # faststart
        r = recs[rel]
        assert r["status"] == "ok" and r["verify"] == "ok" and r["camm"] == len(pts)
        assert r["creation_source"] == "gps" and r["sha256"] == archive.sha256(c)
        assert r["src_size"] == c.stat().st_size and r["archive_bytes"] == out.stat().st_size
        # a real encode, not garbage; the exact value depends on the ffmpeg/x264 build
        # (0.98 with ffmpeg 8, 0.94 with Ubuntu 24.04's 6.1)
        assert r["ssim"] > 0.9
    side = json.loads((dst / "20260115.gps.json").read_text())
    assert [s["clip"] for s in side] == [str(c.relative_to(src)) for c in clips]
    assert all(len(s["records"]) == 5 for s in side)
    assert not list(dst.rglob("*.part"))


def test_no_faststart_leaves_moov_last(card, tmp_path):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(faststart=False)) == 0
    types = [x[0] for x in mp4write.top_level(dst / clips[0].relative_to(src))]
    assert types[-1] == b"moov"


def test_clip_without_fixes_is_archived_without_camm(tmp_path):
    src = tmp_path / "card"
    clips = make_card(src, clip_kwargs={1: {"valid": False}}, dat=False)
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts()) == 0
    recs = {r["path"]: r for r in manifest(dst)}
    r = recs[str(clips[1].relative_to(src))]
    assert r["status"] == "ok" and r["camm"] is None and "no embedded fixes" in r["camm_reason"]
    assert r["creation_source"] == "filename" and r["creation_time"] == "2026-01-14T22:00:05.000Z"
    assert mp4write.read_camm(dst / r["path"]) is None
    t = tags(dst / r["path"])
    assert t["creation_time"].startswith("2026-01-14T22:00:05")  # filename time, camera clock UTC+10
    assert recs[str(clips[0].relative_to(src))]["creation_source"] == "gps"


def test_resume_after_a_crash_and_after_renaming_the_source(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"
    real = archive.encode
    encoded = []

    def crash_on_second(clip, out, *a, **k):
        if clip == clips[1]:
            out.write_bytes(b"half an encode")
            raise KeyboardInterrupt
        encoded.append(clip.name)
        return real(clip, out, *a, **k)
    monkeypatch.setattr(archive, "encode", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        archive.run(src, dst, opts(jobs=1))
    assert encoded == [clips[0].name]
    assert not list(dst.rglob("*.part"))
    assert [r["path"] for r in manifest(dst)] == [str(clips[0].relative_to(src))]

    def counting(clip, out, *a, **k):
        encoded.append(clip.name)
        return real(clip, out, *a, **k)
    monkeypatch.setattr(archive, "encode", counting)
    encoded.clear()
    assert archive.run(src, dst, opts(jobs=1)) == 0
    assert encoded == [clips[1].name, clips[2].name]           # clip 1 isn't re-encoded
    assert not list(dst.rglob("*.part"))
    assert len(manifest(dst)) == 3

    # the folder is renamed (and the copy resets mtimes): nothing is re-encoded, the archives move
    (src / "Normal").rename(src / "Front")
    for c in (src / "Front").iterdir():
        c.touch()
    encoded.clear()
    assert archive.run(src, dst, opts()) == 0
    assert encoded == []
    lines = manifest(dst)
    assert len(lines) == 6
    for r in lines[3:]:
        assert r["path"].startswith("Front/") and r["moved_from"].startswith("Normal/")
        assert (dst / r["path"]).is_file()
    assert not list((dst / "Normal").glob("*.MP4"))
    encoded.clear()
    assert archive.run(src, dst, opts()) == 0 and encoded == [] and len(manifest(dst)) == 6


def test_failed_clip_is_recorded_and_retried(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"
    real = archive.encode

    def fail_second(clip, out, *a, **k):
        if clip == clips[1]:
            raise archive.EncodeError("ffmpeg exited 1: simulated")
        return real(clip, out, *a, **k)
    monkeypatch.setattr(archive, "encode", fail_second)
    assert archive.run(src, dst, opts(jobs=1)) == 1
    st = {r["path"]: r["status"] for r in manifest(dst)}
    assert list(st.values()) == ["ok", "failed", "ok"]
    monkeypatch.setattr(archive, "encode", real)
    assert archive.run(src, dst, opts()) == 0
    assert [r["path"] for r in manifest(dst)][-1] == str(clips[1].relative_to(src))


def test_lost_manifest_line_is_reverified_not_reencoded(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts()) == 0
    (dst / archive.MANIFEST).write_text("")               # crash after the rename, before the line
    monkeypatch.setattr(archive, "encode", lambda *a, **k: pytest.fail("re-encoded"))
    assert archive.run(src, dst, opts()) == 0
    recs = manifest(dst)
    assert len(recs) == 3 and all(r["recovered"] and r["status"] == "ok" for r in recs)
    assert all(r["encoder"]["encoder"] == "libx264" and r["encoder"]["kind"] == "software" for r in recs)


def test_clock_step_day_uses_the_fixed_times(tmp_path):
    src = tmp_path / "card"
    clips = make_card(src, seconds=20, clip_kwargs={1: {"step": {k: 14 for k in range(0, 7)}}})
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts()) == 0
    want = expected(clips, DEMO_TZ)
    raw = {s["clip"]: s for s in json.loads((dst / "20260115.gps.json").read_text())}
    stepped = clips[1]
    camm = mp4write.read_camm(dst / stepped.relative_to(src))
    fixed = want[str(stepped.resolve())]["MAPGPSTrack"]
    assert [c["epoch"] for c in camm] == [p[5] for p in fixed]
    raw_epochs = [r["epoch"] for r in raw[str(stepped.relative_to(src))]["records"]]
    assert [c["epoch"] for c in camm] == [e - 14 for e in raw_epochs]   # not the raw, stepped times
    rec = next(r for r in manifest(dst) if r["path"] == str(stepped.relative_to(src)))
    assert rec["creation_time"].startswith("2026-01-14T22:00:05")


def test_desc_dir_is_used_and_stale_entries_rebuilt(card, tmp_path, capsys):
    src, clips = card
    desc = tmp_path / "desc"
    per_day, _, _ = build.run(src, desc, verbose=False)
    descs = json.loads((desc / "20260115.json").read_text())
    descs[0]["MAPGPSTrack"] = descs[0]["MAPGPSTrack"][:3]      # a marker: this one is used as given
    descs[1]["filesize"] += 1                                  # stale: rebuilt
    (desc / "20260115.json").write_text(json.dumps(descs))
    (desc / "all.json").unlink()
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(desc_dir=desc, verbose=True)) == 0
    assert "rebuilding its day" in capsys.readouterr().out
    assert len(mp4write.read_camm(dst / clips[0].relative_to(src))) == 3
    assert len(mp4write.read_camm(dst / clips[1].relative_to(src))) == 5


def test_desc_dir_alone_still_dates_clips_without_gps(tmp_path):
    src = tmp_path / "card"
    clips = make_card(src, clip_kwargs={2: {"valid": False}}, dat=False)
    desc = tmp_path / "desc"
    build.run(src, desc, verbose=False)
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(desc_dir=desc)) == 0     # nothing rebuilt, tz inferred
    r = {x["path"]: x for x in manifest(dst)}[str(clips[2].relative_to(src))]
    assert r["camm"] is None and r["creation_source"] == "filename" and r["tz"] == "+10:00"


def test_source_gone_exits_75(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"

    def drop(clip, out, *a, **k):
        out.write_bytes(b"partial")
        monkeypatch.setattr(archive.SourceWatch, "gone", lambda self, clip=None: True)
        raise archive.EncodeError("ffmpeg exited 1: Input/output error")
    monkeypatch.setattr(archive, "encode", drop)
    assert archive.run(src, dst, opts(jobs=1)) == archive.EX_TEMPFAIL
    assert not list(dst.rglob("*.part"))
    assert not (dst / archive.MANIFEST).exists()


def test_dry_run_writes_nothing_and_copy_other_copies(card, tmp_path, capsys):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(dry_run=True, copy_other=True, verbose=True)) == 0
    out = capsys.readouterr().out
    assert "would encode" in out and "would copy GPS/20260115.dat" in out
    assert not dst.exists()
    assert archive.run(src, dst, opts(copy_other=True)) == 0
    assert (dst / "GPS" / "20260115.dat").read_bytes() == (src / "GPS" / "20260115.dat").read_bytes()


def test_verify_only_flags_a_damaged_archive(card, tmp_path):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts()) == 0
    assert archive.run(src, dst, opts(verify_only=True)) == 0
    out = dst / clips[2].relative_to(src)
    size = out.stat().st_size
    # swap in a clip with a different frame count, same size so the manifest still matches it
    other = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-t", "2", "-c", "copy", str(other)], check=True)
    data = other.read_bytes()
    out.write_bytes(data + bytes(size - len(data)))
    assert archive.run(src, dst, opts(verify_only=True)) == 1
    last = manifest(dst)[-1]
    assert last["status"] == "verify-failed" and any("video packets" in i for i in last["verify"])


def test_cli_archive(card, tmp_path):
    src, _ = card
    dst = tmp_path / "arch"
    with pytest.raises(SystemExit) as e:
        cli.main(["archive", str(src), str(dst), "--codec", "h264", "--encoder", "software", "--quick"])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["archive", str(src), str(dst), "--verify-only", "--quick"])
    assert e.value.code == 0


def video_packets(path):
    """(pts, dts, size, CRC32) of every video packet."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts,dts,size,data_hash", "-show_data_hash", "CRC32", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def test_codec_copy_remuxes_and_still_adds_gps(card, tmp_path):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(codec="copy", encoder="auto", quick=False, make="Vantrue")) == 0
    want = expected(clips)
    for r in manifest(dst):
        c = src / r["path"]
        out = dst / r["path"]
        assert video_packets(out) == video_packets(c)          # the very same frames
        assert r["status"] == "ok" and r["ssim"] is None
        assert r["encoder"] == {"codec": "copy", "encoder": "copy", "kind": "copy", "faststart": True}
        pts = want[str(c.resolve())]["MAPGPSTrack"]
        assert archive.compare_gps(pts, mp4write.read_camm(out)) == []
        assert tags(out)["make"] == "Vantrue" and "creation_time" in tags(out)
    assert (dst / "20260115.gps.json").exists()
    for bad in ({"quality": 30}, {"bitrate": "4M"}, {"ten_bit": True}, {"height": 90}):
        with pytest.raises(SystemExit, match="--codec copy"):
            archive.run(src, tmp_path / "x", opts(codec="copy", **bad))


def test_height_downscales_and_ssim_compares_at_source_size(card, tmp_path):
    src, clips = card
    dst = tmp_path / "arch"
    # testsrc2 is all sharp edges: halved and scaled back up it reads about 0.89, under the
    # default tripwire, so lower it here
    assert archive.run(src, dst, opts(height=90, ssim_warn=0.5)) == 0
    for r in manifest(dst):
        out = dst / r["path"]
        a, b = archive.probe(src / r["path"]), archive.probe(out)
        assert (b["width"], b["height"]) == (160, 90)
        assert (a["frames"], a["first_pts"], a["last_pts"]) == (b["frames"], b["first_pts"], b["last_pts"])
        assert r["encoder"]["height"] == 90 and r["height"] == 90
        assert r["status"] == "ok" and 0.7 < r["ssim"] < 0.95    # scaled back up to 320x180 to compare
    with pytest.raises(SystemExit, match="even"):
        archive.run(src, tmp_path / "x", opts(height=91))


def test_make_and_model_flags_win_over_the_description(card, tmp_path):
    src, clips = card
    desc = tmp_path / "desc"
    build.run(src, desc, make="DescMake", model="DescModel", verbose=False)
    for flags, want in (({}, ("DescMake", "DescModel")),
                        ({"make": "FlagMake", "model": "FlagModel"}, ("FlagMake", "FlagModel"))):
        dst = tmp_path / f"arch{len(flags)}"
        assert archive.run(src, dst, opts(desc_dir=desc, **flags)) == 0
        out = dst / clips[0].relative_to(src)
        t = tags(out)
        assert (t["make"], t["model"]) == want
        _, _, moov = mp4write.read_moov(out)
        kids = {k.type: k.data for k in moov.find(b"udta").children}
        assert (kids[b"@mak"].decode(), kids[b"@mod"].decode()) == want


def test_defaults_and_option_checks():
    o = archive.Options()
    assert archive.ten_bit(o) and archive.target_height(o) == 1440
    assert archive.target_height(archive.Options(height=0)) is None          # keep the source size
    assert not archive.ten_bit(archive.Options(codec="h264"))
    assert not archive.ten_bit(archive.Options(ten_bit=False))
    assert archive.target_height(archive.Options(codec="copy")) is None
    assert archive.DEFAULT_QUALITY[("av1", "nvenc")] == 44 and archive.DEFAULT_QUALITY[("av1", "software")] == 38
    assert [archive.default_jobs("software", n) for n in (1, 8, 16, 32, 64, 128)] == [1, 1, 2, 4, 4, 4]
    assert archive.default_jobs("nvenc", 64) == 1 and archive.default_jobs("copy", 64) == 1
    nv = archive.video_args("av1", "nvenc", "av1_nvenc", None, None, True, 60, 1440)
    assert nv[nv.index("-cq") + 1] == "44" and nv[nv.index("-pix_fmt") + 1] == "p010le"
    assert {"-rc-lookahead", "-spatial-aq", "-temporal-aq"} <= set(nv)
    assert nv[nv.index("-vf") + 1] == "scale=-2:1440"
    hv = archive.video_args("hevc", "nvenc", "hevc_nvenc", None, None, True, 60)
    assert hv[hv.index("-cq") + 1] == "37" and "-rc-lookahead" in hv and "-vf" not in hv
    sw = archive.video_args("av1", "software", "libsvtav1", None, None, True, 60, None, "8")
    assert sw[sw.index("-preset") + 1] == "8" and sw[sw.index("-crf") + 1] == "38"
    assert sw[sw.index("-pix_fmt") + 1] == "yuv420p10le"
    x5 = archive.video_args("hevc", "software", "libx265", None, None, False, 60)
    assert x5[x5.index("-preset") + 1] == "slow"
    for bad, msg in (({"height": 91}, "even"), ({"encoder": "nvenc", "preset": "6"}, "software encoders"),
                     ({"codec": "copy", "preset": "6"}, "--codec copy"), ({"jobs": 0}, "at least 1")):
        with pytest.raises(SystemExit, match=msg):
            archive.check_options(archive.Options(**bad))
    archive.check_options(archive.Options(codec="copy", height=0, ten_bit=False))   # no-ops are fine
    with pytest.raises(SystemExit, match="auto picked"):
        archive.check_encoder(archive.Options(preset="6"), "nvenc", "av1_nvenc")
    with pytest.raises(SystemExit, match="a number"):
        archive.check_encoder(archive.Options(preset="slow"), "software", "libsvtav1")


def test_small_sources_are_not_upscaled(card, tmp_path, capsys):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(jobs=None, verbose=True)) == 0      # default height 1440
    assert f"jobs {archive.default_jobs('software')} (default)" in capsys.readouterr().out
    for r in manifest(dst):
        assert r["encoder"]["height"] == 1440 and r["height"] == 180
        assert r["mbps"] > 0 and r["src_mbps"] > 0


@pytest.mark.skipif(not have_ffmpeg() or not archive.encoder_works("libsvtav1"), reason="needs libsvtav1")
def test_software_av1_is_ten_bit_with_the_given_preset(tmp_path):
    src = tmp_path / "card"
    make_card(src, seconds=5)
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts(codec="av1", preset="12")) == 0
    (r,) = manifest(dst)
    assert r["encoder"]["encoder"] == "libsvtav1" and r["encoder"]["preset"] == "12"
    assert r["encoder"]["ten_bit"] and r["encoder"]["quality"] == 38
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                          "-of", "csv=p=0", str(dst / r["path"])], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "yuv420p10le"


def test_a_multi_day_run_only_ever_does_new_work(tmp_path, monkeypatch):
    src = tmp_path / "card"
    clips = make_card(src, seconds=20)                 # four clips; the fourth arrives later
    later = tmp_path / "later.MP4"
    clips[3].rename(later)
    dst = tmp_path / "arch"
    real = archive.encode
    encoded = []

    def stop_at_second(clip, out, *a, **k):            # day 1 ends mid-clip
        if clip == clips[1]:
            out.write_bytes(b"half an encode")
            raise KeyboardInterrupt
        encoded.append(clip.name)
        return real(clip, out, *a, **k)

    def counting(clip, out, *a, **k):
        encoded.append(clip.name)
        return real(clip, out, *a, **k)

    def current(dst):
        lines = manifest(dst)
        latest = {}
        for r in lines:
            latest[r["path"]] = r
        return lines, latest

    monkeypatch.setattr(archive, "encode", stop_at_second)
    with pytest.raises(KeyboardInterrupt):
        archive.run(src, dst, opts(jobs=1))
    assert encoded == [clips[0].name]

    monkeypatch.setattr(archive, "encode", counting)
    for step, expect in (("day 2: nothing changed", [clips[1].name, clips[2].name]),
                         ("day 3: the manifest file was touched", []),
                         ("day 4: a fourth clip arrived", [clips[3].name])):
        if "touched" in step:
            (dst / archive.MANIFEST).touch()
        if "fourth" in step:
            later.rename(clips[3])
        encoded.clear()
        assert archive.run(src, dst, opts(jobs=1)) == 0, step
        assert encoded == expect, step
        assert not list(dst.rglob("*.part")), step
    lines, latest = current(dst)
    assert len(lines) == 4                              # exactly one line per clip, ever
    assert sorted(latest) == sorted(str(c.relative_to(src)) for c in clips)
    assert all(r["status"] == "ok" for r in latest.values())
    assert archive.run(src, dst, opts(verify_only=True)) == 0
    _, latest = current(dst)
    assert len(latest) == 4 and all(r["status"] == "ok" and r["verify"] == "ok" for r in latest.values())


def test_a_verify_failure_is_retried_once_then_given_up(card, tmp_path, monkeypatch, capsys):
    src, clips = card
    dst = tmp_path / "arch"
    real_verify, real_encode = archive.verify_clip, archive.encode
    encoded = []

    def bad_second(clip, out, *a, **k):
        issues, facts = real_verify(clip, out, *a, **k)
        return (issues + ["simulated"] if clip == clips[1] else issues), facts

    def counting(clip, out, *a, **k):
        encoded.append(clip.name)
        return real_encode(clip, out, *a, **k)
    monkeypatch.setattr(archive, "verify_clip", bad_second)
    monkeypatch.setattr(archive, "encode", counting)
    rel = str(clips[1].relative_to(src))

    assert archive.run(src, dst, opts(jobs=1)) == 1
    last = [r for r in manifest(dst) if r["path"] == rel][-1]
    assert last["status"] == "verify-failed" and last["attempts"] == 1

    encoded.clear()
    assert archive.run(src, dst, opts(jobs=1, verbose=True)) == 1
    assert encoded == [clips[1].name]                          # retried from scratch, nothing else
    assert "encoding it again (attempt 2)" in capsys.readouterr().out
    last = [r for r in manifest(dst) if r["path"] == rel][-1]
    assert last["status"] == "gave-up" and last["attempts"] == 2

    encoded.clear()
    assert archive.run(src, dst, opts(jobs=1, verbose=True)) == 0
    assert encoded == []                                       # skipped from now on
    assert "GAVE UP on 1 clip(s)" in capsys.readouterr().out and not list(dst.rglob("*.part"))

    (dst / rel).unlink()                                       # the documented way to try again
    monkeypatch.setattr(archive, "verify_clip", real_verify)
    assert archive.run(src, dst, opts(jobs=1)) == 0
    assert encoded == [clips[1].name]
    last = [r for r in manifest(dst) if r["path"] == rel][-1]
    assert last["status"] == "ok" and last["attempts"] == 1


def test_desc_dir_with_all_json_matches_moved_clips_by_filename(card, tmp_path, capsys):
    src, clips = card
    desc = tmp_path / "desc"
    build.run(src, desc, verbose=False)
    assert (desc / "all.json").exists()
    descs = json.loads((desc / "20260115.json").read_text())
    descs[0]["MAPGPSTrack"] = descs[0]["MAPGPSTrack"][:3]      # marker: used only if matched
    (desc / "20260115.json").write_text(json.dumps(descs))
    moved = tmp_path / "moved"
    src.rename(moved)                                          # the paths in the JSON no longer resolve
    dst = tmp_path / "arch"
    assert archive.run(moved, dst, opts(desc_dir=desc, verbose=True)) == 0
    assert "rebuilding" not in capsys.readouterr().out
    assert len(mp4write.read_camm(dst / clips[0].relative_to(src))) == 3


def test_a_failed_ssim_comparison_fails_verification(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"
    monkeypatch.setattr(archive, "ssim_spots", lambda *a, **k: ([], "ffmpeg exited 1: simulated"))
    assert archive.run(src, dst, opts()) == 1
    for r in manifest(dst):
        assert r["status"] == "verify-failed" and r["ssim"] is None
        assert any("SSIM comparison failed" in i for i in r["verify"])
    dst2 = tmp_path / "copy"                                   # a remux doesn't compare, so null is fine
    assert archive.run(src, dst2, opts(codec="copy", encoder="auto")) == 0


def test_verify_only_survives_corrupt_archives(card, tmp_path, monkeypatch):
    src, clips = card
    dst = tmp_path / "arch"
    assert archive.run(src, dst, opts()) == 0
    bad = dst / clips[0].relative_to(src)
    data = bytearray(bad.read_bytes())
    data[32:4096] = bytes(len(data[32:4096]))                  # wipe moov, keep the size
    bad.write_bytes(bytes(data))
    real = mp4write.read_camm

    def broken_camm(path):                                     # a CAMM table that doesn't parse
        if path.name == clips[1].name:
            raise struct.error("unpack requires a buffer of 8 bytes")
        return real(path)
    monkeypatch.setattr(mp4write, "read_camm", broken_camm)
    assert archive.run(src, dst, opts(verify_only=True)) == 1  # finishes, doesn't raise
    latest = {r["path"]: r for r in manifest(dst)}
    assert latest[str(clips[0].relative_to(src))]["status"] == "verify-failed"
    r1 = latest[str(clips[1].relative_to(src))]
    assert r1["status"] == "verify-failed" and "unreadable archive: error" in r1["verify"][0]
    assert latest[str(clips[2].relative_to(src))]["status"] == "ok"


def test_src_inside_dst_is_never_cleaned_and_dst_inside_src_is_not_a_source(tmp_path):
    dst = tmp_path / "store"
    src = dst / "raw"
    clips = make_card(src, seconds=10)
    keep = src / "Normal" / "camera-notes.part"                # a file of the user's that looks stray
    keep.write_text("mine")
    stray = dst / "Normal" / "x.MP4.part"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"left by a crash")
    assert archive.run(src, dst, opts()) == 0
    assert keep.read_text() == "mine" and not stray.exists()
    assert len(manifest(dst)) == len(clips)

    inner = src / "archive"                                    # DST inside SRC: its clips aren't sources
    assert archive.run(src, inner, opts()) == 0
    assert archive.run(src, inner, opts()) == 0
    assert len(manifest(inner)) == len(clips)
    assert build.find_clips(src, exclude=inner) == clips
    assert len(build.find_clips(src)) == 2 * len(clips)


def test_each_source_clip_is_parsed_once(card, tmp_path, monkeypatch):
    src, clips = card
    calls = []
    real = novatek.parse_clip

    def counting(path):
        calls.append(path.name)
        return real(path)
    monkeypatch.setattr(novatek, "parse_clip", counting)
    assert archive.run(src, tmp_path / "arch", opts()) == 0
    assert sorted(calls) == sorted(c.name for c in clips)
