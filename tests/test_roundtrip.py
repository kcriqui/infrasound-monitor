"""Self-contained round-trip tests (no dependency on the real Y:\\AmaSeis data)."""
import struct
import datetime as dt
from pathlib import Path

import numpy as np
from obspy import read, read_inventory

from infrasound_monitor.amaseis import read_counts, sample_count, iter_hour_files
from infrasound_monitor.convert import convert
from infrasound_monitor.config import DEFAULT_STATION, PA_PER_COUNT, NOMINAL_FS


def _write_z(path: Path, counts: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<I", len(counts)))
        fh.write(counts.astype("<i2").tobytes())


def _make_tree(root: Path, day: dt.date, hours, n=185150, seed=0):
    rng = np.random.default_rng(seed)
    for h in hours:
        counts = (rng.standard_normal(n) * 40).astype(np.int16)
        p = root / f"{day.year}" / f"{day.month:02d}" / f"{day.day:02d}" / f"{h:02d}.Z"
        _write_z(p, counts)


def test_reader_roundtrip(tmp_path):
    counts = np.array([0, 0, -22, 14, 1000, -890], dtype=np.int16)
    p = tmp_path / "2026/07/01/12.Z"
    _write_z(p, counts)
    assert sample_count(p) == len(counts)
    np.testing.assert_array_equal(read_counts(p), counts.astype(np.int32))


def test_iter_hour_files_sorted(tmp_path):
    _make_tree(tmp_path, dt.date(2026, 7, 1), [0, 5, 23], n=1000)
    hfs = list(iter_hour_files(tmp_path))
    assert [hf.hour for hf in hfs] == [0, 5, 23]
    assert all(hf.start_utc.tzinfo is not None for hf in hfs)


def test_convert_contiguous_and_calibration(tmp_path):
    src = tmp_path / "amaseis"
    arch = tmp_path / "archive"
    _make_tree(src, dt.date(2026, 7, 1), range(0, 24), n=185150, seed=1)
    stats = convert(src, arch, DEFAULT_STATION, verbose=False)
    assert stats["files_in"] == 24
    assert stats["day_files_out"] == 1

    dayfile = next((arch).glob("2026/**/XX.INFRA*"))
    st = read(str(dayfile))
    st.merge(method=0)                       # must not raise: no overlaps
    assert len(st) == 1                      # one gap-free trace
    assert abs(st[0].stats.sampling_rate - NOMINAL_FS) < 1e-3   # float32 in miniSEED
    assert not st.get_gaps()

    inv = read_inventory(str(arch / "station.xml"))
    tr = st[0].copy()
    tr.remove_sensitivity(inv)
    expect = st[0].data.astype(float) * PA_PER_COUNT
    assert np.max(np.abs(tr.data - expect)) < 1e-9


def test_convert_gap_when_hour_missing(tmp_path):
    src = tmp_path / "amaseis"
    arch = tmp_path / "archive"
    _make_tree(src, dt.date(2026, 7, 1), [0, 1, 2, 4, 5], n=185150, seed=2)  # hour 3 missing
    convert(src, arch, DEFAULT_STATION, verbose=False)
    st = read(str(next((arch).glob("2026/**/XX.INFRA*"))))
    assert len(st) == 2                      # broken into two runs by the gap
