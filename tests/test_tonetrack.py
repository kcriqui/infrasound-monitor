"""The tone tracker on a synthetic grid with known tones."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import tonetrack  # noqa: E402

DAYS = 35
T0 = dt.datetime(2026, 9, 7, 8)          # a Monday, 00:00 local at UTC-8


def _grid(seed=0):
    rng = np.random.default_rng(seed)
    times = [T0 + dt.timedelta(hours=i) for i in range(24 * DAYS)]
    freqs = np.arange(0.05, 20.0, 0.01)
    psd = rng.normal(-50.0, 0.7, (len(times), len(freqs)))
    split = times[-1] + dt.timedelta(hours=1) - dt.timedelta(days=7)

    def add(i, f, db):
        k = int(round((f - freqs[0]) / 0.01))
        psd[i, k] += db
        psd[i, k - 1] += db - 4
        psd[i, k + 1] += db - 4

    for i, t in enumerate(times):
        loc = t - dt.timedelta(hours=8)
        # A: 5 Hz, wandering +-0.04 Hz over days, weekdays 02-12 local
        if loc.weekday() < 5 and 2 <= loc.hour < 12:
            fa = 5.0 + 0.04 * np.sin(2 * np.pi * i / (24 * 9))
            add(i, fa, 12.0)
            add(i, 2 * fa, 8.0)                       # its harmonic
        # B: 9.3 Hz, steady around the clock, only in the last week
        if t >= split:
            add(i, 9.3, 10.0)
    return dict(times=np.array(times, dtype=object), freqs=freqs,
                psd_db=psd.astype(np.float32)), split


@pytest.fixture(autouse=True)
def _utc_minus_8(monkeypatch):
    monkeypatch.setattr(tonetrack, "TIMEZONE", None)
    monkeypatch.setattr(tonetrack, "UTC_OFFSET_HOURS", -8.0)
    monkeypatch.setattr(tonetrack, "KNOWN_TONES", [])


def _near(res, f):
    return next(s for s in res["sources"] if abs(s["f"] - f) < 0.1)


def test_a_wandering_scheduled_tone_is_one_source_with_its_schedule():
    grid, split = _grid()
    res = tonetrack.analyse(grid, split)
    a = _near(res, 5.0)
    assert a["f_hi"] - a["f_lo"] > 0.05                 # it wandered, and stayed one source
    assert not a["continuous"]
    on = np.flatnonzero(a["occ_hour"] > 0.5)
    assert on.min() >= 2 and on.max() <= 11            # 02-12 local
    assert a["occ_weekday"][5:].max() < 0.1            # off at weekends
    assert tonetrack.schedule(a).startswith("02–12 h")
    assert "weekends" in tonetrack.schedule(a)
    assert _near(res, 10.0)["harmonic_of"].startswith("2×")


def test_a_new_steady_tone_is_flagged():
    grid, split = _grid()
    res = tonetrack.analyse(grid, split)
    assert [round(s["f"], 1) for s in res["flag"]] == [9.3]
    b = _near(res, 9.3)
    assert b["occ_base"] == 0 and b["occ_recent"] > 0.9 and b["continuous_recent"]


def test_a_known_frequency_does_not_hide_a_newly_steady_source(monkeypatch):
    monkeypatch.setattr(tonetrack, "KNOWN_TONES", [("Old pump", 9.2, 9.4, False)])
    grid, split = _grid()
    res = tonetrack.analyse(grid, split)
    assert [s["known"] for s in res["flag"]] == ["Old pump"]
    monkeypatch.setattr(tonetrack, "KNOWN_TONES", [("Old pump", 9.2, 9.4, True)])
    assert tonetrack.analyse(grid, split)["flag"] == []


def test_noise_alone_makes_no_sources():
    rng = np.random.default_rng(3)
    times = [T0 + dt.timedelta(hours=i) for i in range(24 * 10)]
    freqs = np.arange(0.05, 20.0, 0.01)
    grid = dict(times=np.array(times, dtype=object), freqs=freqs,
                psd_db=rng.normal(-50, 0.7, (len(times), len(freqs))).astype(np.float32))
    res = tonetrack.analyse(grid)
    assert res["sources"] == [] and res["flag"] == []
