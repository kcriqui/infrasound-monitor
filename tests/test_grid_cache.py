"""Tests for the memory-mapped grid cache and the streaming dashboard statistics."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from infrasound_monitor import psd as psdmod
from infrasound_monitor.psd import (save_grid, load_grid, append_grid, update_grid,
                                    build_cache, have_mask, _psd_path, GRID_DTYPE)
import report
import tonehunt

T0 = dt.datetime(2026, 8, 4)


def _grid(n, nfreq=40, seed=0, t0=T0):
    rng = np.random.default_rng(seed)
    times = np.array([t0 + dt.timedelta(hours=i) for i in range(n)])
    freqs = np.linspace(0.05, 20, nfreq)
    psd = rng.normal(-40, 6, size=(n, nfreq))
    return dict(times=times, freqs=freqs, psd_db=psd, seed_id="XX.INFRA.00.SDF",
                nperseg=8192, start=t0.isoformat(),
                end=(t0 + dt.timedelta(hours=n)).isoformat())


# ------------------------------------------------------------------ cache ----
def test_round_trip_is_a_float32_memory_map(tmp_path):
    g = _grid(300)
    cache = tmp_path / "grid_full.npz"
    save_grid(g, cache)
    back = load_grid(cache)
    assert isinstance(back["psd_db"], np.memmap)
    assert back["psd_db"].dtype == GRID_DTYPE
    assert np.allclose(back["psd_db"], g["psd_db"], atol=1e-4)
    assert list(back["times"]) == list(g["times"])
    assert "psd_db" not in np.load(cache, allow_pickle=True).files   # rows live beside it


def test_append_adds_rows_in_place(tmp_path):
    g = _grid(300)
    cache = tmp_path / "grid_full.npz"
    save_grid(dict(g, times=g["times"][:200], psd_db=g["psd_db"][:200]), cache)
    n = append_grid(cache, list(g["times"][200:]), g["psd_db"][200:], T0)
    assert n == 300
    back = load_grid(cache)
    assert np.allclose(back["psd_db"], g["psd_db"], atol=1e-4)
    assert _psd_path(cache).stat().st_size == 300 * 40 * 4


def test_rows_left_by_an_interrupted_append_are_ignored_then_trimmed(tmp_path):
    g = _grid(300)
    cache = tmp_path / "grid_full.npz"
    save_grid(dict(g, times=g["times"][:200], psd_db=g["psd_db"][:200]), cache)
    with open(_psd_path(cache), "ab") as f:          # rows written, metadata never updated
        f.write(np.full((7, 40), 99, GRID_DTYPE).tobytes())
    assert load_grid(cache)["psd_db"].shape == (200, 40)
    append_grid(cache, list(g["times"][200:]), g["psd_db"][200:], T0)
    back = load_grid(cache)
    assert back["psd_db"].shape == (300, 40)
    assert np.allclose(back["psd_db"], g["psd_db"], atol=1e-4)   # the junk rows are gone


def test_truncated_rows_are_reported_not_misread(tmp_path):
    cache = tmp_path / "grid_full.npz"
    save_grid(_grid(100), cache)
    with open(_psd_path(cache), "r+b") as f:
        f.truncate(50 * 40 * 4)
    with pytest.raises(ValueError, match="damaged"):
        load_grid(cache)


def test_legacy_single_file_cache_loads_and_converts_on_append(tmp_path):
    g = _grid(300)
    cache = tmp_path / "grid_full.npz"
    np.savez_compressed(cache, **dict(g, times=g["times"][:200], psd_db=g["psd_db"][:200]))
    assert np.allclose(load_grid(cache)["psd_db"], g["psd_db"][:200])   # still readable
    append_grid(cache, list(g["times"][200:]), g["psd_db"][200:], T0)
    back = load_grid(cache)
    assert isinstance(back["psd_db"], np.memmap)
    assert np.allclose(back["psd_db"], g["psd_db"], atol=1e-4)


def _fake_compute(full, empty_days=()):
    """compute_grid stand-in that slices a synthetic grid; days in empty_days have no data."""
    def compute_grid(archive, start, end, cfg=None, nperseg=8192, verbose=True):
        sel = [i for i, t in enumerate(full["times"]) if start <= t < end]
        if not sel or all(full["times"][i].date() in empty_days for i in sel):
            raise RuntimeError("no data")
        psd = full["psd_db"][sel].copy()
        for k, i in enumerate(sel):
            if full["times"][i].date() in empty_days:
                psd[k] = np.nan
        return dict(full, times=full["times"][sel], psd_db=psd,
                    start=start.isoformat(), end=end.isoformat())
    return compute_grid


def test_build_cache_seeds_month_by_month_keeping_leading_and_middle_gaps(tmp_path, monkeypatch):
    g = _grid(24 * 100)                                  # 100 days
    empty = {(T0 + dt.timedelta(days=d)).date() for d in list(range(0, 35)) + list(range(60, 92))}
    monkeypatch.setattr(psdmod, "compute_grid", _fake_compute(g, empty))
    cache = tmp_path / "grid_full.npz"
    n = build_cache(cache, None, T0, T0 + dt.timedelta(days=100), chunk_days=30, verbose=False)
    back = load_grid(cache)
    assert n == len(back["times"]) == 2400
    assert list(back["times"]) == list(g["times"])
    hm = have_mask(back["psd_db"])
    want = np.array([t.date() not in empty for t in g["times"]])
    assert (hm == want).all()
    assert np.allclose(back["psd_db"][want], g["psd_db"][want], atol=1e-4)
    assert str(back["start"]) == T0.isoformat()


def test_update_grid_appends_only_new_hours(tmp_path, monkeypatch):
    g = _grid(500)
    monkeypatch.setattr(psdmod, "compute_grid", _fake_compute(g))
    cache = tmp_path / "grid_full.npz"
    save_grid(dict(g, times=g["times"][:400], psd_db=g["psd_db"][:400]), cache)
    grid, n_new = update_grid(cache, None, end=T0 + dt.timedelta(hours=500), verbose=False)
    assert n_new == 100 and grid["psd_db"].shape == (500, 40)
    assert np.allclose(grid["psd_db"], g["psd_db"], atol=1e-4)
    _, n_new = update_grid(cache, None, end=T0 + dt.timedelta(hours=500), verbose=False)
    assert n_new == 0


def test_first_archive_day_finds_the_earliest_sds_file(tmp_path):
    import refresh
    from infrasound_monitor.config import DEFAULT_STATION as cfg
    from infrasound_monitor.convert import sds_path
    assert refresh.first_archive_day(tmp_path, cfg) is None
    for year, jday in ((2026, 216), (2025, 300), (2026, 1)):
        p = sds_path(tmp_path, cfg, year, jday)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    (p.parent / (p.name + ".tmp")).touch()               # not a day file
    assert refresh.first_archive_day(tmp_path, cfg) == dt.datetime(2025, 10, 27)


# ------------------------------------------------- streaming == whole-grid ----
def _with_gaps_and_a_tone(n=24 * 40, nfreq=200):
    g = _grid(n, nfreq=nfreq, seed=3)
    psd = g["psd_db"]
    psd[50:80] = np.nan                                  # a gap
    hrs = np.array([t.hour for t in g["times"]])
    psd[np.isin(hrs, [10, 11, 12]), 120] += 12           # a night-only tone
    return g


def _night_means_whole_grid(times, resid, have, night_hours):
    """Reference: the original whole-grid night waterfall (mean residual per night)."""
    anchor = night_hours[0]
    nmask = np.isin([t.hour for t in times], night_hours) & have
    labels = [(t - dt.timedelta(hours=anchor)).date() for t in times]
    udates = sorted({labels[i] for i in np.flatnonzero(nmask)})
    out = np.array([np.nanmean(resid[[i for i in np.flatnonzero(nmask) if labels[i] == d]], axis=0)
                    for d in udates])
    return out, [dt.datetime(d.year, d.month, d.day) for d in udates], nmask


def test_night_day_stats_match_the_whole_grid_computation():
    g = _with_gaps_and_a_tone()
    times, freqs, psd = list(g["times"]), g["freqs"], g["psd_db"]
    have = np.isfinite(psd).all(axis=1)
    night_hours, _ = tonehunt.quiet_window(times, freqs, psd, have, 5)
    # the original whole-grid path
    resid, _ = tonehunt.tone_residual(psd, freqs, 1.0)
    wf0, dd0, nm0 = _night_means_whole_grid(times, resid, have, night_hours)
    pn0 = np.mean(resid[nm0] > 6.0, axis=0)
    pd0 = np.mean(resid[have & ~nm0] > 6.0, axis=0)
    # streaming
    wf, dd, nm, pn, pd_ = tonehunt.night_day_stats(times, freqs, psd, have, night_hours, 1.0, 6.0)
    assert dd == dd0 and (nm == nm0).all()
    assert np.allclose(wf, wf0, atol=1e-4, equal_nan=True)   # accumulated as float32
    assert np.allclose(pn, pn0) and np.allclose(pd_, pd0)


def test_quiet_window_is_unchanged_by_blocking():
    g = _with_gaps_and_a_tone()
    times, freqs, psd = list(g["times"]), g["freqs"], g["psd_db"]
    have = np.isfinite(psd).all(axis=1)
    hours_utc = np.array([t.hour for t in times])
    band = (freqs >= 1) & (freqs <= 20)
    level = np.nanmedian(psd[:, band], axis=1)
    diurnal0 = np.array([np.nanmedian(level[(hours_utc == h) & have]) for h in range(24)])
    _, diurnal = tonehunt.quiet_window(times, freqs, psd, have, 5)
    assert np.allclose(diurnal, diurnal0)


def test_histogram_percentiles_are_within_a_bin_of_exact():
    g = _with_gaps_and_a_tone()
    psd = g["psd_db"]
    have = np.isfinite(psd).all(axis=1)
    got = report.hist_percentiles(report.psd_histogram(psd, have), [10, 50, 90])
    want = np.nanpercentile(psd[have], [10, 50, 90], axis=0)
    for a, b in zip(got, want):
        assert np.max(np.abs(a - b)) < report.HIST_STEP


def test_pdf_matches_a_1_db_histogram():
    g = _with_gaps_and_a_tone()
    psd, freqs = g["psd_db"], g["freqs"]
    have = np.isfinite(psd).all(axis=1)
    counts = report.psd_histogram(psd, have)
    k = 10
    coarse = counts[400:1600].reshape(-1, k, len(freqs)).sum(axis=1)   # -100..+20 dB
    data = psd[have]
    for j in (0, 77, 120, 199):
        c, _ = np.histogram(data[:, j], bins=np.arange(-100, 21, 1.0))
        assert (coarse[:, j] == c).all()
