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


def _g_polezero_db(f):
    """ISO 7196 G-weighting from its pole-zero definition (poles in Hz, x 2*pi), as an
    independent check on the table: four zeros at 0 Hz, eight poles."""
    poles = 2 * np.pi * np.array([-0.707 + 0.707j, -0.707 - 0.707j,
                                  -19.27 + 5.16j, -19.27 - 5.16j,
                                  -14.11 + 14.11j, -14.11 - 14.11j,
                                  -5.16 + 19.27j, -5.16 - 19.27j])

    def h(x):
        s = 2j * np.pi * np.asarray(x, float)
        return np.abs(s ** 4 / np.prod([s - p for p in poles], axis=0))
    return 20 * np.log10(h(f) / h(10.0))


def test_g_weighting_matches_iso_7196():
    f = np.array([r[0] for r in compare.G_TABLE])
    assert np.allclose(compare.g_weight_db(f), [r[1] for r in compare.G_TABLE])
    assert compare.g_weight_db(10.0)[0] == 0.0
    band = (f >= 1.0) & (f <= 40.0)               # where the pole-zero form is quoted to 0.1 dB
    assert np.abs(compare.g_weight_db(f[band]) - _g_polezero_db(f[band])).max() < 0.5
    w = compare.g_weights(np.array([10.0, 19.9, 20.5]))
    assert w[0] == 1.0 and w[1] > 7.0 and w[2] == 0.0  # nothing above the sensor's 20 Hz corner


def test_g_level_of_a_10_hz_tone_is_its_spl():
    freqs = np.linspace(0.05, 25, 5000)
    df = freqs[1] - freqs[0]
    psd = np.full((1, len(freqs)), -200.0)
    k = np.argmin(np.abs(freqs - 10.0))
    psd[0, k] = 10 * np.log10(1.0 / df)              # a 1 Pa^2 (1 Pa rms) tone at 10 Hz
    L = compare.band_levels(freqs, psd, np.array([True]),
                            [("G", compare.g_weights(freqs))])
    assert abs(L[0, 0] - 10 * np.log10(1.0 / (20e-6) ** 2)) < 0.05   # 94 dB(G), weight 0 dB


def test_g_levels_summaries():
    times = [T0 + dt.timedelta(hours=i) for i in range(24 * 10)]
    G = np.full(len(times), 60.0)
    G[-5] = 88.0                                       # one loud recent hour
    G[3] = np.nan
    night = np.array([t.hour in (10, 11, 12) for t in times])
    split = T0 + dt.timedelta(days=7)
    g = compare.g_levels(times, G, night, split)["g"]
    assert g["after_all"]["max"] == 88.0 and g["after_all"]["t_max"] == times[-5]
    assert g["before_night"]["med"] == 60.0 and g["before_all"]["n"] == 24 * 7 - 1
    assert g["quietest"] == 60.0 and len(g["days"]) >= 10
