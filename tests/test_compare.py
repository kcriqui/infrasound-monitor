"""The wind-matched before/after comparison."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare  # noqa: E402

T0 = dt.datetime(2026, 8, 4)


def _setup(n_days=60, after_days=14, step_db=0.0, windier_after=True, seed=0):
    rng = np.random.default_rng(seed)
    times = [T0 + dt.timedelta(hours=i) for i in range(24 * n_days)]
    split = T0 + dt.timedelta(days=n_days - after_days)
    after = np.array([t >= split for t in times])
    wind = rng.gamma(2.0, 1.2, len(times))
    if windier_after:
        wind[after] *= 1.8                            # a windier later period
    nb = 3
    L = 40 + 3.0 * wind[:, None] + rng.normal(0, 1.0, (len(times), nb))   # level rises with wind
    L[after, 2] += step_db                            # a new source in band 2 only
    wx = dict(wind=wind, precip=np.zeros(len(times)))
    return times, L, wx, split


def test_wind_matching_removes_a_weather_only_change():
    times, L, wx, split = _setup(step_db=0.0)
    res = compare.compare(times, L, wx, split, np.ones(len(times), bool),
                          ["a", "b", "c"], n_boot=100)
    assert (res["raw"] > 2.0).all()                    # raw: "louder", but it was windier
    assert (np.abs(res["delta"]) < 0.7).all()          # matched: no change
    assert ((res["lo"] < 0) & (res["hi"] > 0)).all()   # and the interval says so


def test_a_real_step_is_found_in_its_band_only():
    times, L, wx, split = _setup(step_db=4.0)
    res = compare.compare(times, L, wx, split, np.ones(len(times), bool),
                          ["a", "b", "c"], n_boot=100)
    assert abs(res["delta"][2] - 4.0) < 0.8 and res["lo"][2] > 2.0
    assert abs(res["delta"][0]) < 0.7


def test_rain_and_missing_weather_hours_are_excluded():
    times, L, wx, split = _setup()
    wx["precip"][:100] = 2.0
    wx["wind"][100:200] = np.nan
    res = compare.compare(times, L, wx, split, np.ones(len(times), bool),
                          ["a", "b", "c"], n_boot=10)
    assert res["n_before"] + res["n_after"] == len(times) - 200


def test_band_levels_integrate_power():
    freqs = np.linspace(0.05, 20, 4000)
    psd = np.full((3, len(freqs)), -40.0)             # 1e-4 Pa^2/Hz everywhere
    have = np.array([True, True, False])
    bands = compare.band_defs(freqs)
    L = compare.band_levels(freqs, psd, have, bands)
    j = [b[0] for b in bands].index("4 Hz")
    width = bands[j][1].sum() * (freqs[1] - freqs[0])
    expect = 10 * np.log10(1e-4 * width / (20e-6) ** 2)
    assert abs(L[0, j] - expect) < 1e-6
    assert np.isnan(L[2]).all()
