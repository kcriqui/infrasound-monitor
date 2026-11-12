"""Weather parsing, storage and hourly aggregation (no network)."""
import datetime as dt
import math

import numpy as np

from infrasound_monitor import weather

ONE_MIN = """station,station_name,valid(UTC),sknt,drct,gust_sknt,tmpf
SJC,SAN JOSE INTL A,2026-11-01 00:00,9,334,10,64
SJC,SAN JOSE INTL A,2026-11-01 00:01,M,331,14,64
"""

METAR = """station,valid,sknt,drct,gust,tmpf,p01i
SJC,2026-11-10 00:53,5.00,320.00,M,59.00,0.0001
SJC,2026-11-10 01:53,M,M,M,M,0.12
"""


def test_parse_both_products_to_si_units():
    a = weather.parse(ONE_MIN)
    assert a[0][0] == "2026-11-01T00:00:00Z"
    assert math.isclose(a[0][1], 9 * weather.KT_MS)
    assert math.isclose(a[0][2], 10 * weather.KT_MS)
    assert math.isclose(a[0][4], (64 - 32) * 5 / 9)
    assert math.isnan(a[1][1])                     # 'M' = missing
    m = weather.parse(METAR)
    assert math.isclose(m[1][5], 0.12 * 25.4)      # inches -> mm
    assert math.isnan(m[1][1])


def test_empty_result_parses_to_nothing():
    assert weather.parse("station,station_name,valid(UTC),sknt\n") == []


def _store(tmp_path, station, product, rows):
    weather.merge(weather.store_path(station, product, tmp_path), rows, "0", "9")


def test_hourly_means_and_station_fallback(tmp_path):
    nan = math.nan
    # SJC: hour 00 has 1-minute data (mean 2 m/s); hour 01 has only 1 observation
    _store(tmp_path, "SJC", "asos1min",
           [(f"2026-11-10T00:{m:02d}:00Z", 1.0 + (m % 3), nan, 0.0, 10.0, nan) for m in range(60)])
    _store(tmp_path, "SJC", "asos",
           [("2026-11-10T00:53:00Z", 2.0, 6.0, 0.0, 12.0, 0.0),
            ("2026-11-10T01:53:00Z", 9.0, nan, 0.0, 12.0, 1.5)])
    # NUQ covers hour 01 well
    _store(tmp_path, "NUQ", "asos",
           [(f"2026-11-10T01:{m:02d}:00Z", 4.0, nan, 0.0, 11.0, nan) for m in (5, 25, 45)])
    times = [dt.datetime(2026, 11, 10, h) for h in (0, 1, 2)]
    h = weather.hourly(times, ["SJC", "NUQ"], tmp_path)
    assert np.isclose(h["wind"][0], (60 * 2.0 + 2.0) / 61)    # 1-min mean ~2, plus the METAR
    assert h["gust"][0] == 6.0
    assert list(h["station"]) == [0, 1, -1]                   # hour 01 from NUQ, 02 none
    assert h["wind"][1] == 4.0
    assert math.isnan(h["wind"][2])


def test_merge_replaces_the_window_and_keeps_the_rest(tmp_path):
    nan = math.nan
    p = weather.store_path("SJC", "asos", tmp_path)
    row = lambda day, w: (f"2026-11-{day:02d}T00:53:00Z", w, nan, 0.0, 12.0, 0.0)
    assert weather.merge(p, [row(d, 1.0) for d in (1, 2, 3, 4)], "0", "9") == 4
    # re-fetch days 2-3: day 2 revised, day 3 unchanged, a late day-3 report added
    late = ("2026-11-03T05:53:00Z", 7.0, nan, 0.0, 12.0, 0.0)
    n = weather.merge(p, [row(2, 3.0), row(3, 1.0), late],
                      "2026-11-02T00:00:00Z", "2026-11-04T00:00:00Z")
    assert n == 1
    back = weather.load("SJC", tmp_path)
    assert list(back["wind_ms"]) == [1.0, 3.0, 1.0, 7.0, 1.0]       # still sorted, day 4 kept
    assert weather._newest(p) == "2026-11-04T00:53:00Z"
