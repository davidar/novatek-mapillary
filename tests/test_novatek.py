import datetime as dt

from synth_clip import DEMO_START, Fix, rmc, write_clip

from novatek_mapillary import novatek


def straight(epoch):
    return Fix(-33.0 + (epoch - DEMO_START) * 1e-4, 147.0, 11.0, 0.0)


def test_rmc_sentence_round_trips():
    s = rmc(DEMO_START, -33.5, 147.25, 20.0, 253.76)
    m = novatek.RMC.search(s)
    assert m is not None
    assert m.group(2) == b"A" and m.group(5) == b"S" and m.group(8) == b"E"


def test_parse_clip_layout(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, seconds=60)
    info = novatek.parse_clip(p)
    assert info.duration == 60.0
    assert len(info.gps_index) == 61           # header + one per second
    assert len(info.chunk_offsets) == 1800     # one frame per chunk
    assert abs(info.sample_delta - 1 / 30) < 1e-9


def test_records_land_on_the_right_frame(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, seconds=60)
    recs = novatek.read_records(p)
    assert len(recs) == 60
    assert [r.k for r in recs[:3]] == [1, 2, 3]
    assert abs(recs[0].video_t - 28 / 30) < 1e-9   # written after frame 28, as in real clips
    assert all(r.valid for r in recs)
    assert abs(recs[0].speed - 11.0) < 0.01


def test_clip_start_is_fitted_within_the_phase_uncertainty(tmp_path):
    # Records are written exactly once a second, so a fix shows up 0-1 s (+ latency) after its
    # time and that phase can't be recovered from one clip: start - true = 0.5 - lag.
    for frac in (0.0, 0.25, 0.5, 0.9):
        for latency in (0.1, 0.6):
            true = DEMO_START + frac
            p = write_clip(tmp_path / "a.MP4", true, straight, latency=latency)
            start, fixes, dur = novatek.clip_track(p)
            err = start - true
            assert -0.5 - latency - 1e-6 < err <= 0.5 - latency + 1e-6, (frac, latency, err)
            assert dur == 60.0


def test_clock_step_inside_a_clip_is_restamped(tmp_path):
    # GPS clock jumps +14 s for records 20..40, positions stay continuous
    step = {k: 14 for k in range(20, 41)}
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, step=step)
    start, fixes, _ = novatek.clip_track(p)
    assert abs(start - (DEMO_START + 0.4)) <= 0.6
    epochs = [f.epoch for f in fixes]
    assert epochs == sorted(set(epochs))
    assert epochs[-1] - epochs[0] < 61           # nothing pushed 14 s past the end
    for f in fixes:                              # each fix's position matches its (fixed) time
        assert abs(f.lat - straight(int(f.epoch)).lat) < 1e-6


def test_stalled_timestamps_keep_first_occurrence(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, stall={10, 11})
    start, fixes, _ = novatek.clip_track(p)
    assert len(fixes) == 58                      # two seconds repeated, so two fixes missing
    assert abs(start - (DEMO_START + 0.4)) <= 0.6


def test_no_fix_means_no_track(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START, straight, valid=False)
    assert len(novatek.read_records(p)) == 60
    assert novatek.clip_track(p) is None


def test_mp4_duration_of_garbage_is_none(tmp_path):
    p = tmp_path / "x.MP4"
    p.write_bytes(b"\0\0\0\x08free" + b"x" * 100)
    assert novatek.mp4_duration(p) is None
    assert novatek.parse_clip(p) is None


def test_rmc_time_is_utc(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, latency=0.1)
    r = novatek.read_records(p)[0]
    assert dt.datetime.fromtimestamp(r.epoch, dt.timezone.utc).tzinfo is dt.timezone.utc
    assert r.epoch == DEMO_START + 1                 # floor(0.4 + 28/30 - 0.1)


def test_moov_after_a_large_mdat_is_found(tmp_path):
    # Other cameras write the index at the end of the file; the parser must seek, not slurp.
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, moov_last=True, frame_bytes=1300)
    assert p.stat().st_size > 2 << 20
    info = novatek.parse_clip(p)
    assert info.duration == 60.0 and len(info.gps_index) == 61
    start, fixes, _ = novatek.clip_track(p)
    assert len(fixes) == 60 and abs(start - (DEMO_START + 0.4)) <= 0.6


def test_64bit_mvhd_duration(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, mvhd_version=1)
    assert novatek.parse_clip(p).duration == 60.0
    assert novatek.mp4_duration(p) == 60.0


def test_truncated_clip_is_none(tmp_path):
    p = write_clip(tmp_path / "a.MP4", DEMO_START + 0.4, straight, moov_last=True)
    p.write_bytes(p.read_bytes()[:-100])      # lost the end of moov
    assert novatek.parse_clip(p) is None
