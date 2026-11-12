"""Weather from nearby airport stations, for wind-matched infrasound analysis.

Wind is the largest source of infrasound noise at a home station: turbulence
pressing on the house and the sensor.  A level change between two periods
means little until both are compared at the same wind, and that needs a wind
record.  Fan noise also rises with outdoor temperature, so temperature is kept
too.

Source: the Iowa Environmental Mesonet (IEM) archive of US airport (ASOS)
observations.  It is free and needs no key.  Two products, fetched for every
configured station that has them:

* ``asos1min``: ASOS 1-minute data.  Each minute carries the 2-minute average
  wind and the peak 5-s gust.  It arrives from NCEI a few days late, so recent
  days are re-requested on every fetch.
* ``asos``: METARs, i.e. the routine hourly reports, specials, and the 5-minute
  high-frequency METARs.  These are current within the hour.

Observations are kept as plain CSV, one file per station and product, under
``<archive>/weather/``, so every backup of the archive carries them:

    time_utc,wind_ms,gust_ms,dir_deg,temp_c,precip_mm

Missing values are written as ``nan``.  :func:`hourly` aggregates them onto UTC
hours like the PSD grid's.

    infra-weather                      # extend every configured station to now
    infra-weather --start 2026-04-01   # backfill from a date
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

from .config import WEATHER_STATIONS, WEATHER_DIR, ARCHIVE_DIR, DEFAULT_STATION

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/"
USER_AGENT = "infrasound-monitor (+https://github.com/kcriqui/infrasound-monitor)"
PRODUCTS = ("asos1min", "asos")
COLUMNS = ("time_utc", "wind_ms", "gust_ms", "dir_deg", "temp_c", "precip_mm")
REFETCH_DAYS = {"asos1min": 10, "asos": 2}      # re-request recent days: late arrivals
CHUNK_DAYS = 31                                 # days per request
PAUSE_S = 6.0                                   # between requests; IEM throttles bursts
KT_MS = 0.514444
IN_MM = 25.4


# ------------------------------------------------------------------ fetch ----
def _url(product: str, station: str, d0: dt.date, d1: dt.date) -> str:
    """Request for [d0, d1) UTC."""
    if product == "asos1min":
        q = [("station", station)]
        q += [("vars", v) for v in ("sknt", "drct", "gust_sknt", "tmpf")]
        q += [("sts", f"{d0:%Y-%m-%d}T00:00Z"), ("ets", f"{d1:%Y-%m-%d}T00:00Z"),
              ("sample", "1min"), ("what", "download"), ("delim", "comma"), ("gis", "no")]
        return IEM + "asos1min.py?" + urllib.parse.urlencode(q)
    q = [("station", station)]
    q += [("data", v) for v in ("sknt", "drct", "gust", "tmpf", "p01i")]
    q += [("year1", d0.year), ("month1", d0.month), ("day1", d0.day),
          ("year2", d1.year), ("month2", d1.month), ("day2", d1.day),
          ("tz", "Etc/UTC"), ("format", "onlycomma"), ("missing", "M"), ("trace", "0.0001")]
    q += [("report_type", r) for r in (1, 3, 4)]   # HFMETAR (5-min), routine, special
    return IEM + "asos.py?" + urllib.parse.urlencode(q)


def _get(url: str, tries: int = 5) -> str:
    """GET an IEM CSV.  Both products' CSVs start with a ``station,...`` header; anything
    else (``Too many requests ...``, an HTTP error) is retried with a growing pause."""
    wait = 30.0
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode("utf-8", "replace")
        except OSError as e:          # network error, or HTTP 429/5xx (HTTPError is one)
            text = f"error: {e}"
        if text.startswith("station"):
            return text
        if i + 1 < tries:
            time.sleep(wait)
            wait *= 2
    raise OSError(f"IEM request failed after {tries} tries: {text[:120]!r}")


def _num(s: str, scale: float = 1.0, offset: float = 0.0) -> float:
    s = (s or "").strip()
    if s in ("", "M", "T"):
        return math.nan
    try:
        return (float(s) + offset) * scale
    except ValueError:
        return math.nan


def parse(text: str) -> list[tuple]:
    """IEM CSV (either product) -> rows of COLUMNS, time as 'YYYY-MM-DDTHH:MM:00Z'."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if not lines:
        return []
    rd = csv.DictReader(lines)
    out = []
    for r in rd:
        when = r.get("valid(UTC)") or r.get("valid") or ""
        if len(when) < 16:
            continue
        t = when[:10] + "T" + when[11:16] + ":00Z"
        wind = _num(r.get("sknt"), KT_MS)
        gust = _num(r.get("gust_sknt", r.get("gust")), KT_MS)
        tmp_f = _num(r.get("tmpf"))
        temp = (tmp_f - 32.0) * 5.0 / 9.0 if not math.isnan(tmp_f) else math.nan
        out.append((t, wind, gust, _num(r.get("drct")), temp, _num(r.get("p01i"), IN_MM)))
    return out


def store_path(station: str, product: str, wx_dir=WEATHER_DIR) -> Path:
    return Path(wx_dir) / f"{station.upper()}_{product}.csv"


def _fmt(v) -> str:
    return "nan" if isinstance(v, float) and math.isnan(v) else (
        f"{v:.2f}" if isinstance(v, float) else str(v))


def _line(row) -> str:
    return ",".join([row[0]] + [_fmt(v) for v in row[1:]]) + "\n"


def merge(path: Path, rows, t0: str, t1: str) -> int:
    """Merge ``rows`` (parsed, all within [t0, t1)) into the time-sorted CSV ``path``.

    Streams the file: rows outside the window are copied through untouched, so
    memory is set by the window, not by the length of the record.  Fetched rows
    replace stored ones with the same time.  Returns how many rows were new."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    window: dict[str, str] = {}
    fetched = {r[0]: _line(r) for r in rows}
    tmp = path.with_name(path.name + ".tmp")
    n_old = 0
    with open(tmp, "w", newline="") as out:
        out.write(",".join(COLUMNS) + "\n")

        def flush_window():
            nonlocal n_old
            n_old = len(window)
            window.update(fetched)
            out.writelines(window[k] for k in sorted(window))

        done = False
        if path.exists():
            with open(path, newline="") as fh:
                fh.readline()
                for ln in fh:
                    key = ln.split(",", 1)[0]
                    if not key:
                        continue
                    if key < t0:
                        out.write(ln)
                    elif key < t1:
                        window[key] = ln
                    else:
                        if not done:
                            flush_window(); done = True
                        out.write(ln)
        if not done:
            flush_window()
    os.replace(tmp, path)
    return len(window) - n_old


def _newest(path: Path) -> str | None:
    """Time of the last row of a sorted CSV, reading only its tail."""
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 4096))
        lines = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if ln]
    key = lines[-1].split(",", 1)[0] if lines else ""
    return key if key[:1].isdigit() else None


def update(station: str, product: str, start: dt.date | None = None,
           end: dt.date | None = None, wx_dir=WEATHER_DIR, verbose: bool = True) -> int:
    """Fetch ``product`` for ``station`` and merge it into its CSV; returns new rows.

    Without ``start``, resumes REFETCH_DAYS before the newest stored row (or, for a
    new file, from the first day of the archive)."""
    path = store_path(station, product, wx_dir)
    today = dt.datetime.now(dt.timezone.utc).date()
    end = end or today + dt.timedelta(days=1)
    if start is None:
        newest = _newest(path)
        if newest:
            start = dt.date.fromisoformat(newest[:10]) - dt.timedelta(days=REFETCH_DAYS[product])
        else:
            start = _first_archive_day() or today - dt.timedelta(days=30)
    added = 0
    d0 = start
    while d0 < end:
        d1 = min(d0 + dt.timedelta(days=CHUNK_DAYS), end)
        got = parse(_get(_url(product, station, d0, d1)))
        added += merge(path, got, f"{d0:%Y-%m-%d}T00:00:00Z", f"{d1:%Y-%m-%d}T00:00:00Z")
        if verbose:
            print(f"  {station} {product} {d0} -> {d1}: {len(got)} obs", flush=True)
        d0 = d1
        if d0 < end:
            time.sleep(PAUSE_S)
    return added


def _first_archive_day(archive=ARCHIVE_DIR, cfg=DEFAULT_STATION) -> dt.date | None:
    sid = cfg.seed_id
    days = []
    for p in Path(archive).glob(f"*/{cfg.network}/{cfg.station}/{cfg.channel}.D/{sid}.D.*.*"):
        y, j = p.name.rsplit(".", 2)[1:]
        if y.isdigit() and j.isdigit():
            days.append((int(y), int(j)))
    if not days:
        return None
    y, j = min(days)
    return dt.date(y, 1, 1) + dt.timedelta(days=j - 1)


# ------------------------------------------------------------------- read ----
READ_CHUNK_BYTES = 4 << 20       # ~80k rows per block when reading a CSV


def iter_obs(station: str, wx_dir=WEATHER_DIR):
    """Yield blocks of a station's stored observations, every product:
    (times as datetime64[m], [n, 5] float array of COLUMNS[1:]).  Blocks are
    bounded, so reading a long record never holds all of it."""
    for product in PRODUCTS:
        path = store_path(station, product, wx_dir)
        if not path.exists():
            continue
        with open(path, newline="") as fh:
            fh.readline()
            while True:
                block = [ln for ln in fh.readlines(READ_CHUNK_BYTES)
                         if ln.count(",") == len(COLUMNS) - 1]
                if not block:
                    break
                t = np.array([ln[:16] for ln in block], dtype="datetime64[m]")
                v = np.loadtxt(block, delimiter=",", usecols=range(1, len(COLUMNS)), ndmin=2)
                yield t, v


def load(station: str, wx_dir=WEATHER_DIR) -> dict:
    """All stored observations of a station as arrays sorted by time (for inspection;
    the analysis uses :func:`hourly`, which streams)."""
    blocks = list(iter_obs(station, wx_dir))
    t = np.concatenate([b[0] for b in blocks]) if blocks else np.array([], "datetime64[m]")
    v = np.concatenate([b[1] for b in blocks]) if blocks else np.empty((0, len(COLUMNS) - 1))
    order = np.argsort(t, kind="stable")
    out = {"time": t[order]}
    for j, name in enumerate(COLUMNS[1:]):
        out[name] = v[order, j]
    return out


def hourly(times, stations=None, wx_dir=WEATHER_DIR, min_obs: int = 3) -> dict:
    """Weather on the UTC hours ``times`` (hour starts, naive UTC datetimes, sorted).

    Per hour: ``wind`` = mean wind speed (m/s), ``gust`` = peak gust or wind,
    ``temp`` = mean temperature (C), ``precip`` = largest hourly-precipitation
    report (mm), ``station`` = index into ``stations`` of the station used.
    Stations are tried in order; the first with at least ``min_obs`` wind
    observations in an hour supplies that hour.  NaN where none does."""
    stations = list(WEATHER_STATIONS if stations is None else stations)
    n = len(times)
    out = {k: np.full(n, np.nan) for k in ("wind", "gust", "temp", "precip")}
    out["station"] = np.full(n, -1)
    out["stations"] = stations
    if not n or not stations:
        return out
    grid_h = np.array(times, dtype="datetime64[h]")
    for si, st in enumerate(stations):
        wsum, wcnt, tsum, tcnt = (np.zeros(n) for _ in range(4))
        gmax, pmax = np.full(n, np.nan), np.full(n, np.nan)
        for t, v in iter_obs(st, wx_dir):
            h = t.astype("datetime64[h]")
            r = np.searchsorted(grid_h, h)
            ok = r < n
            ok[ok] &= grid_h[r[ok]] == h[ok]
            r, v = r[ok], v[ok]
            wind, gust, _, temp, prec = v.T
            g = np.isfinite(wind)
            wsum += np.bincount(r[g], weights=wind[g], minlength=n)
            wcnt += np.bincount(r[g], minlength=n)
            g = np.isfinite(temp)
            tsum += np.bincount(r[g], weights=temp[g], minlength=n)
            tcnt += np.bincount(r[g], minlength=n)
            peak = np.fmax(gust, wind)
            g = np.isfinite(peak)
            np.fmax.at(gmax, r[g], peak[g])
            g = np.isfinite(prec)
            np.fmax.at(pmax, r[g], prec[g])
        take = (out["station"] < 0) & (wcnt >= min_obs)
        with np.errstate(invalid="ignore", divide="ignore"):
            out["wind"][take] = (wsum / wcnt)[take]
            out["temp"][take] = np.where(tcnt > 0, tsum / np.maximum(tcnt, 1), np.nan)[take]
        out["gust"][take], out["precip"][take] = gmax[take], pmax[take]
        out["station"][take] = si
    return out


# -------------------------------------------------------------------- cli ----
def main(argv=None):
    p = argparse.ArgumentParser(description="Fetch airport weather observations (IEM ASOS).")
    p.add_argument("--stations", nargs="*", default=None,
                   help="station ids, e.g. SJC NUQ (default: config [weather].stations)")
    p.add_argument("--start", default=None, help="backfill from YYYY-MM-DD (UTC)")
    p.add_argument("--end", default=None, help="stop before YYYY-MM-DD (default: tomorrow)")
    p.add_argument("--dir", default=WEATHER_DIR, help="where the CSVs live")
    p.add_argument("--products", nargs="*", default=list(PRODUCTS), choices=PRODUCTS)
    a = p.parse_args(argv)
    stations = [s.upper() for s in (a.stations if a.stations is not None else WEATHER_STATIONS)]
    if not stations:
        print("no weather stations configured ([weather] stations in config.toml) -- skipping")
        return
    start = dt.date.fromisoformat(a.start) if a.start else None
    end = dt.date.fromisoformat(a.end) if a.end else None
    failed = 0
    for i, st in enumerate(stations):
        for j, prod in enumerate(a.products):
            if i or j:
                time.sleep(PAUSE_S)
            try:
                n = update(st, prod, start, end, wx_dir=a.dir)
                print(f"{st} {prod}: +{n} rows -> {store_path(st, prod, a.dir)}", flush=True)
            except OSError as e:
                failed += 1
                print(f"{st} {prod}: FAILED ({e})", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
