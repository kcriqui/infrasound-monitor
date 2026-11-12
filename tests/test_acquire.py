"""Tests for the live acquisition writer (no real serial port required).

Samples are fed to :class:`SdsWriter` with synthetic arrival times so we can
exercise timestamping, append-without-clobber, gap detection, and UTC day-file
splitting deterministically.
"""
import datetime as dt

import numpy as np
from obspy import read, Stream, UTCDateTime

from infrasound_monitor.acquire import SdsWriter, parse_line
from infrasound_monitor.config import DEFAULT_STATION, NOMINAL_FS
from infrasound_monitor.convert import sds_path

FS = NOMINAL_FS


def _feed(writer, counts, t0, fs=FS, jitter=None):
    """Feed samples at ideal arrival times t0 + i/fs (+ optional jitter secs)."""
    for i, c in enumerate(counts):
        when = t0 + dt.timedelta(seconds=i / fs + (jitter[i] if jitter else 0.0))
        writer.add(int(c), when)


def _read_archive(arch):
    """Read every SDS day file under ``arch`` and merge (obspy read() can't recurse)."""
    st = Stream()
    for f in sorted(arch.rglob(f"{DEFAULT_STATION.network}.{DEFAULT_STATION.station}*")):
        st += read(str(f))
    st.merge(method=0)
    return st


def test_parse_line():
    assert parse_line(b"-00123\r\n") == -123
    assert parse_line(b"00000\r\n") == 0
    assert parse_line(b" 42 \n") == 42
    assert parse_line(b"\x00\xb8\xf900000\r\n") is None   # connect garbage
    assert parse_line(b"\r\n") is None
    assert parse_line(b"7,88\r\n") == 7                    # tolerate chan,count


def test_contiguous_run_appends_without_clobber(tmp_path):
    """Many small flushes must accumulate into ONE gap-free trace, not overwrite."""
    arch = tmp_path / "archive"
    # flush_seconds tiny -> forces ~1 flush/sec, so append is exercised repeatedly
    w = SdsWriter(arch, DEFAULT_STATION, fs=FS, flush_seconds=1.0)
    t0 = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    rng = np.random.default_rng(0)
    counts = (rng.standard_normal(600) * 50).astype(np.int32)   # ~11.7 s of data
    _feed(w, counts, t0)
    w.flush()

    st = _read_archive(arch)
    assert len(st) == 1, "segments must merge into one gap-free trace"
    tr = st[0]
    assert tr.stats.npts == len(counts)                # nothing lost/overwritten
    assert not st.get_gaps()
    assert abs(tr.stats.sampling_rate - FS) < 1e-3
    assert abs(UTCDateTime(t0) - tr.stats.starttime) < (0.5 / FS)
    np.testing.assert_array_equal(tr.data.astype(np.int32), counts)


def test_real_dropout_becomes_an_explicit_gap(tmp_path):
    arch = tmp_path / "archive"
    w = SdsWriter(arch, DEFAULT_STATION, fs=FS, flush_seconds=3600, gap_tol=2.0)
    t0 = dt.datetime(2026, 7, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    _feed(w, np.arange(100), t0)
    # resume 30 s later than the fixed-rate clock expected -> a true outage
    t1 = t0 + dt.timedelta(seconds=100 / FS + 30.0)
    _feed(w, np.arange(100), t1)
    w.flush()

    st = _read_archive(arch)
    gaps = st.get_gaps()
    assert len(gaps) == 1, "a >gap_tol outage must leave one explicit gap"
    assert gaps[0][6] > 25.0                            # gap duration ~30 s


def _sensor(n, t0, rate_fn, latency, seed=1):
    """Arrival times of n samples from a free-running clock: sample i is taken at
    the integral of 1/rate, then delayed by a random latency."""
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    rate = rate_fn(idx)
    taken = t0 + np.concatenate([[0.0], np.cumsum(1.0 / rate[:-1])])
    return taken, taken + rng.uniform(0.0, latency, n)


def _pieces(arch):
    from infrasound_monitor.waveforms import pieces
    return pieces(_read_raw(arch))


def _read_raw(arch):
    st = Stream()
    for f in sorted(arch.rglob(f"{DEFAULT_STATION.network}.{DEFAULT_STATION.station}*")):
        st += read(str(f))
    return st


def test_drifting_sensor_clock_leaves_no_gaps_and_keeps_time(tmp_path):
    """The real sensor's rate wanders with temperature, here by 3e-5 either way
    of the nominal.  The old writer cut a fake 2 s gap whenever the drift reached
    gap_tol; now the run must stay one gap-free trace and stay on time."""
    arch = tmp_path / "archive"
    w = SdsWriter(arch, DEFAULT_STATION, fs=51.422, flush_seconds=300, verbose=False)
    n = int(4 * 3600 * 51.42)                                     # 4 hours
    t0 = UTCDateTime(2026, 11, 10, 3).timestamp
    wander = lambda i: 51.4219 + 0.0015 * np.sin(2 * np.pi * i / (3 * 3600 * 51.42))
    taken, arrive = _sensor(n, t0, wander, latency=0.016)
    for i in range(n):
        w.add(i % 200, arrive[i])
    w.flush()

    st = _read_raw(arch)
    assert len(st) == 1, f"ObsPy must see one trace, got {len(st)}"   # rates kept in band
    st.merge(method=1)                                               # must not raise
    ps = _pieces(arch)
    assert len(ps) == 1 and len(ps[0].data) == n                    # nothing lost or added
    # The archive's clock: the next segment would start at w.seg_start.  Compare with
    # when sample n was really taken (one more sample period after the last).
    true_end = taken[-1] + 1.0 / wander(np.array([n - 1]))[0]
    assert abs(w.seg_start - true_end) < 0.05, w.seg_start - true_end
    assert abs(UTCDateTime(t0) - st[0].stats.starttime) < 0.02     # first sample on time


def test_rate_far_from_nominal_converges_with_a_logged_correction(tmp_path, capsys):
    """A badly configured nominal rate (0.1% off) is outside the rate band, so the
    writer re-anchors once, then tracks the real rate."""
    arch = tmp_path / "archive"
    w = SdsWriter(arch, DEFAULT_STATION, fs=51.48, flush_seconds=300, verbose=False)
    n = int(2 * 3600 * 51.42)
    t0 = UTCDateTime(2026, 11, 10, 3).timestamp
    taken, arrive = _sensor(n, t0, lambda i: np.full(len(i), 51.42), latency=0.016)
    for i in range(n):
        w.add(0, arrive[i])
    w.flush()
    assert "timing correction" in capsys.readouterr().out
    assert abs(w.fs_true - 51.42) < 0.002
    assert abs(w.seg_start - (taken[-1] + 1 / 51.42)) < 0.05
    assert len(_pieces(arch)) == 1                         # a correction is not a gap


def test_reader_stall_with_buffered_samples_is_not_a_gap(tmp_path):
    """Samples held up 6 s (a stalled reader, the tty buffering them) and then
    delivered in a burst were not lost, so they must not end the run."""
    arch = tmp_path / "archive"
    w = SdsWriter(arch, DEFAULT_STATION, fs=FS, flush_seconds=3600, verbose=False)
    n = int(120 * FS)
    t0 = UTCDateTime(2026, 11, 10, 3).timestamp
    taken = t0 + np.arange(n) / FS
    arrive = taken.copy()
    stall = (taken > t0 + 60) & (taken < t0 + 66)
    arrive[stall] = t0 + 66 + np.arange(stall.sum()) * 1e-4         # the burst
    for i in range(n):
        w.add(1, arrive[i])
    w.flush()
    ps = _pieces(arch)
    assert len(ps) == 1 and len(ps[0].data) == n
    assert not _read_raw(arch).get_gaps()


def test_utc_midnight_splits_into_two_day_files(tmp_path):
    arch = tmp_path / "archive"
    w = SdsWriter(arch, DEFAULT_STATION, fs=FS, flush_seconds=3600)
    # start ~5 s before midnight so the run crosses into the next UTC day
    t0 = dt.datetime(2026, 7, 1, 23, 59, 55, tzinfo=dt.timezone.utc)
    counts = np.arange(600)                             # ~11.7 s -> crosses midnight
    _feed(w, counts, t0)
    w.flush()

    f1 = sds_path(arch, DEFAULT_STATION, 2026, 182)     # 2026-07-01
    f2 = sds_path(arch, DEFAULT_STATION, 2026, 183)     # 2026-07-02
    assert f1.exists() and f2.exists(), "run must split at the UTC day boundary"

    # across the two files the data is still contiguous (no gap at midnight)
    st = read(str(f1)) + read(str(f2))
    st.merge(method=0)
    assert len(st) == 1
    assert st[0].stats.npts == len(counts)
    assert not st.get_gaps()
