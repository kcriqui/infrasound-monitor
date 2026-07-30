"""Tests for the waterfall's time decimation (no plotting, no real archive)."""
import datetime as dt

import numpy as np

from infrasound_monitor.waterfall import _decimate_time


def _grid(n, nfreq=32, seed=0):
    rng = np.random.default_rng(seed)
    times = np.array([dt.datetime(2026, 4, 9) + dt.timedelta(hours=i)
                      for i in range(n)])
    return times, rng.normal(-40, 5, size=(n, nfreq))


def test_short_record_is_passed_through_untouched():
    times, psd = _grid(2640)                      # ~110 days, under the cap
    t2, z2 = _decimate_time(times, psd, 3000)
    assert t2 is times and z2 is psd


def test_column_count_is_capped_regardless_of_span():
    for n in (8760, 26280, 87600):                # 1, 3, 10 years of hours
        times, psd = _grid(n, nfreq=8)
        t2, z2 = _decimate_time(times, psd, 3000)
        assert z2.shape[0] <= 3000
        assert len(t2) == z2.shape[0]


def test_blocks_are_combined_as_a_median_in_linear_power():
    """Not a median of the dB values: for an even-sized block those differ by
    several dB (dB averaging is a geometric mean in power), which would show up
    as spurious structure in the display."""
    times, psd = _grid(6000)
    _, out = _decimate_time(times, psd, 1000)
    step = int(np.ceil(6000 / 1000))              # 6 -- even, so the two differ
    blk = psd[:step]
    want = 10 * np.log10(np.median(10 ** (blk / 10.0), axis=0))
    assert np.allclose(out[0], want)
    assert not np.allclose(out[0], np.median(blk, axis=0))


def test_gap_hours_do_not_contaminate_their_block():
    times, psd = _grid(6000)
    step = int(np.ceil(6000 / 1000))
    psd[0:step] = np.nan                          # a wholly missing block
    psd[step:step + step // 2] = np.nan           # a partially missing one
    _, out = _decimate_time(times, psd, 1000)
    assert np.isnan(out[0]).all()                 # stays blank in the render
    want = 10 * np.log10(np.nanmedian(10 ** (psd[step:2 * step] / 10.0), axis=0))
    assert np.allclose(out[1], want)


def test_ragged_tail_is_kept():
    times, psd = _grid(3001)
    _, out = _decimate_time(times, psd, 3000)
    assert out.shape[0] == 1501                   # ceil(3001 / 2)
    assert np.isfinite(out).all()                 # final short block not dropped


def test_cap_of_zero_disables_decimation():
    times, psd = _grid(5000)
    t2, z2 = _decimate_time(times, psd, 0)
    assert t2 is times and z2 is psd
