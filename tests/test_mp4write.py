"""The MP4 box writer in isolation, on small clips ffmpeg makes at test time."""
import json
import struct
import subprocess

import pytest
from synth_clip import DEMO_START, Fix, ffmpeg_clip, have_ffmpeg, write_decodable_clip

from novatek_mapillary import mp4write as w
from novatek_mapillary import novatek

pytestmark = pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg and ffprobe")


def straight(epoch):
    return Fix(-33.0 + (epoch - DEMO_START) * 1e-4, 147.0, 11.0, 0.0)


def packets(path, streams=(0, 1)):
    """Every packet of the given streams: (stream, pts, dts, size, flags, CRC32 of the data)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "packet=stream_index,pts,dts,size,flags,data_hash", "-show_data_hash", "CRC32",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    rows = [tuple(line.rstrip(",").split(",")) for line in out.splitlines() if line]
    return [r for r in rows if int(r[0]) in streams]


def probe(path, *args):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-of", "json", *args, str(path)],
                                     capture_output=True, text=True, check=True).stdout)


def decodes(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip()


def points(first_ms=600.0, n=4, alt=True):
    return [[first_ms + 1000.0 * i, 147.0 + i * 1e-4, -33.0 - i * 1e-4, (100.0 + i) if alt else None,
             None, DEMO_START + 1.0 + i] for i in range(n)]


@pytest.fixture
def clip(tmp_path):
    return ffmpeg_clip(tmp_path / "c.mp4", seconds=4)


def test_decodable_synthetic_clip_parses_like_a_real_one(tmp_path):
    for moov_last in (False, True):
        p = write_decodable_clip(tmp_path / f"a{moov_last}.MP4", DEMO_START + 0.4, straight, moov_last=moov_last)
        info = novatek.parse_clip(p)
        assert info.duration == 5.0
        assert len(info.gps_index) == 6                     # header + one per second
        assert len(info.chunk_offsets) == 150               # one frame per chunk
        recs = novatek.read_records(p)
        assert [round(r.video_t * 30) for r in recs] == [28, 58, 88, 118, 148]
        assert decodes(p)
        types = [b[0] for b in w.top_level(p)]
        assert types.index(b"moov") > types.index(b"mdat") if moov_last else types.index(b"moov") < types.index(b"mdat")


def test_decodable_clip_start_is_fitted_within_the_phase_uncertainty(tmp_path):
    # the same bound as test_novatek.test_clip_start_is_fitted_within_the_phase_uncertainty
    for frac in (0.0, 0.5, 0.9):
        for latency in (0.1, 0.6):
            true = DEMO_START + frac
            p = write_decodable_clip(tmp_path / "a.MP4", true, straight, latency=latency)
            start, fixes, dur = novatek.clip_track(p)
            err = start - true
            assert -0.5 - latency - 1e-6 < err <= 0.5 - latency + 1e-6, (frac, latency, err)
            assert len(fixes) == 5 and dur == 5.0


def test_inserting_records_keeps_every_packet(tmp_path):
    src = ffmpeg_clip(tmp_path / "plain.mp4", seconds=5)
    p = write_decodable_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight)
    # same encoder settings and sources, so the same packets, now with records between them
    assert packets(p) == packets(src)


def test_append_camm_track(clip):
    before = packets(clip)
    n = w.append_camm_track(clip, points(), [(1.0, 2.0)] * 4)
    assert n == 4
    streams = probe(clip, "-show_streams")["streams"]
    assert [s["codec_type"] for s in streams] == ["video", "audio", "data"]
    camm = streams[2]
    assert camm["codec_tag_string"] == "camm"
    assert camm["tags"]["handler_name"] == "CameraMetadataMotionHandler"
    assert packets(clip) == before                          # the other tracks are untouched
    assert decodes(clip)
    _, _, moov = w.read_moov(clip)
    mvhd = moov.find(b"mvhd").data
    assert struct.unpack(">I", mvhd[96:100])[0] == 4        # next_track_ID after tracks 1, 2, 3
    trak = list(moov.find_all(b"trak"))[2]
    assert w.mdhd_timescale(trak) == 1000 and trak.find(b"mdia", b"mdhd").data[0] == 1
    assert struct.unpack(">I", trak.find(b"tkhd").data[20:24])[0] == 0xFFFFFFFF
    flags = struct.unpack(">I", trak.find(b"tkhd").data[:4])[0] & 0xFFFFFF
    assert flags & 1 and flags & 2                          # enabled, in the movie


def test_camm_round_trips(clip):
    pts = points(alt=True)
    pts[1][3] = None
    w.append_camm_track(clip, pts, [(1.5, -2.5)] * 4)
    back = w.read_camm(clip)
    assert len(back) == 4
    for p, b in zip(pts, back):
        assert abs(b["ms"] - p[0]) < 1.0
        assert b["lat"] == p[2] and b["lon"] == p[1] and b["epoch"] == p[5]
        assert b["alt"] == p[3] and b["fix"] == (3 if p[3] is not None else 2)
        assert (b["ve"], b["vn"]) == (1.5, -2.5)


def test_last_sample_runs_to_the_end_of_the_video(clip):
    w.append_camm_track(clip, points(first_ms=0.0))
    camm = probe(clip, "-show_streams")["streams"][2]
    assert abs(float(camm["duration"]) - 4.0) < 0.002
    _, _, moov = w.read_moov(clip)
    assert list(moov.find_all(b"trak"))[2].find(b"edts") is None   # first fix at 0: no edit list


def test_edit_list_when_first_fix_is_after_zero(clip):
    w.append_camm_track(clip, points(first_ms=600.0))
    _, _, moov = w.read_moov(clip)
    elst = list(moov.find_all(b"trak"))[2].find(b"edts", b"elst").data
    n = struct.unpack(">I", elst[4:8])[0]
    entries = [struct.unpack(">iihh", elst[8 + 12 * i:20 + 12 * i]) for i in range(n)]
    assert entries == [(600, -1, 1, 0), (3400, 0, 1, 0)]    # empty edit, then video end - first fix
    assert [round(b["ms"]) for b in w.read_camm(clip)] == [600, 1600, 2600, 3600]


def test_faststart_keeps_packets(clip):
    w.append_camm_track(clip, points())
    before = packets(clip, (0, 1, 2))
    assert w.faststart(clip)
    types = [b[0] for b in w.top_level(clip)]
    assert types.index(b"moov") < types.index(b"mdat")
    assert packets(clip, (0, 1, 2)) == before
    assert decodes(clip)
    assert len(w.read_camm(clip)) == 4
    assert not w.faststart(clip)                            # already at the front


def test_offsets_promote_to_co64(clip):
    _, _, moov = w.read_moov(clip)
    w.shift_chunk_offsets(moov, lambda o: o + (1 << 32))
    stbl = next(moov.find_all(b"trak", b"mdia", b"minf", b"stbl"))
    assert stbl.find(b"co64") is not None and stbl.find(b"stco") is None
    assert min(w.chunk_offsets(stbl)) > 1 << 32


def test_udta_make_model_location(clip):
    w.append_camm_track(clip, points(), make="Vantrue", model="N4 Pro", xyz=w.iso6709(-33.0, 147.0, 120.0))
    tags = probe(clip, "-show_format")["format"]["tags"]
    assert tags["make"] == "Vantrue" and tags["model"] == "N4 Pro"
    assert tags["location"].startswith("-33.0000+147.0000")
    _, _, moov = w.read_moov(clip)
    udtas = list(moov.find_all(b"udta"))
    assert len(udtas) == 1                                  # merged into ffmpeg's udta
    kids = {c.type: c.data for c in udtas[0].children}
    assert kids[b"@mak"] == b"Vantrue" and kids[b"@mod"] == b"N4 Pro"
    assert kids[b"\xa9mak"] == struct.pack(">HH", 7, 0x55C4) + b"Vantrue"
    assert b"meta" in kids                                  # ffmpeg's own tags are kept
    w.set_udta(clip, make="Other")                          # rewriting replaces, not duplicates
    _, _, moov = w.read_moov(clip)
    assert [c.type for c in moov.find(b"udta").children].count(b"@mak") == 1


def test_refuses_when_moov_is_not_last(tmp_path):
    p = write_decodable_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight)   # moov first
    with pytest.raises(ValueError, match="not the last box"):
        w.append_camm_track(p, points())


def test_mapillary_tools_reads_our_camm(tmp_path):
    camm_parser = pytest.importorskip("mapillary_tools.camm.camm_parser")
    p = ffmpeg_clip(tmp_path / "c.mp4", seconds=5)
    # sub-millisecond times, first fix after 0 (so the two-entry edit list is exercised), and a
    # fix after the end of the video, as build allows up to 1.5 s past it
    pts = [[812.3 + 1000.0 * i + 0.1 * i, 147.0 + i * 1.23456789e-4, -33.0 - i * 9.87654321e-5,
            (55.5 + i) if i % 2 else None, None, DEMO_START + 1.0 + i] for i in range(5)]
    pts.append([5400.0, 147.001, -33.001, None, None, DEMO_START + 6.0])
    w.append_camm_track(p, pts, make="Vantrue", model="N4")
    w.faststart(p)
    with open(p, "rb") as f:
        info = camm_parser.extract_camm_info(f)
    # mapillary_tools 0.14.7 files CAMMGPSPoint under mini_gps (it checks the parent class first)
    got = sorted([*(info.gps or []), *(info.mini_gps or [])], key=lambda q: q.time)
    assert len(got) == len(pts)
    for p_, g in zip(pts, got):
        assert abs(g.lat - p_[2]) < 1e-7 and abs(g.lon - p_[1]) < 1e-7
        assert abs(g.time - p_[0] / 1000) < 1e-3
        assert g.time_gps_epoch == p_[5]
    assert (info.make, info.model) == ("Vantrue", "N4")
