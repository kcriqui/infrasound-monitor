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
