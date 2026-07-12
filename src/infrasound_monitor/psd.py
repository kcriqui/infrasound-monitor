"""Reduce the miniSEED archive to an hourly PSD grid (the waterfall backbone).

For each UTC hour in a date range we compute a Welch power spectral density in
pressure units (Pa^2/Hz).  The result is a ``time x frequency`` grid that the
waterfall renderer draws directly.  Grids are cached to ``.npz`` so re-rendering
a date range is instant.
"""
from __future__ import annotations
import datetime as dt
from pathlib import Path

import numpy as np
from scipy import signal
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client

from .config import (StationConfig, DEFAULT_STATION, PA_PER_COUNT, PASSBAND_HZ)

SECONDS_PER_HOUR = 3600


def _hour_range(start: dt.datetime, end: dt.datetime):
    t = UTCDateTime(start.year, start.month, start.day, start.hour)
    end_u = UTCDateTime(end)
    while t < end_u:
        yield t
        t += SECONDS_PER_HOUR


def compute_grid(archive, start: dt.datetime, end: dt.datetime,
                 cfg: StationConfig = DEFAULT_STATION, nperseg: int = 8192,
                 fmin: float = PASSBAND_HZ[0], fmax: float = PASSBAND_HZ[1],
                 verbose: bool = True) -> dict:
    """Return {'times','freqs','psd_db','seed_id',...} over [start, end) by UTC hour."""
    client = Client(str(archive))
    hours = list(_hour_range(start, end))
    freqs = None
    cols, times = [], []
    n_ok = 0
    for i, t in enumerate(hours):
        st = client.get_waveforms(cfg.network, cfg.station, cfg.location,
                                  cfg.channel, t, t + SECONDS_PER_HOUR)
        col = None
        if len(st):
            st.merge(method=1, fill_value=0)
            tr = max(st, key=lambda x: x.stats.npts)
            if tr.stats.npts >= nperseg:
                fs = tr.stats.sampling_rate
                x = tr.data.astype(np.float64) * PA_PER_COUNT     # -> Pascals
                x -= x.mean()
                f, pxx = signal.welch(x, fs=fs, nperseg=nperseg)
                if freqs is None:
                    band = (f >= fmin) & (f <= fmax)
                    freqs = f[band]; _band = band
                col = pxx[_band]
                n_ok += 1
        times.append(t.datetime)
        cols.append(col)
        if verbose and i % 240 == 0:
            print(f"  {i}/{len(hours)} hours ({t.date})", flush=True)

    if freqs is None:
        raise RuntimeError("no data with >= nperseg samples found in range")
    nfreq = len(freqs)
    psd = np.full((len(cols), nfreq), np.nan)
    for j, c in enumerate(cols):
        if c is not None:
            psd[j] = c
    with np.errstate(divide="ignore"):
        psd_db = 10 * np.log10(psd)
    if verbose:
        print(f"  grid: {psd.shape[0]} hours x {nfreq} freqs, {n_ok} hours with data")
    return dict(times=np.array(times), freqs=freqs, psd_db=psd_db,
                seed_id=cfg.seed_id, nperseg=nperseg,
                start=start.isoformat(), end=end.isoformat())


def update_grid(cache, archive, cfg: StationConfig = DEFAULT_STATION,
                nperseg: int = 8192, end: dt.datetime | None = None,
                verbose: bool = True) -> tuple[dict, int]:
    """Extend a cached PSD grid up to ``end`` (default: now), computing only the
    new hours since the cache's last hour and appending them.  Keeps a daily
    rebuild fast.  Returns (grid, n_new_hours).  Falls back to a full recompute
    if the frequency axis somehow changed.
    """
    grid = load_grid(cache)
    times = list(grid["times"])
    freqs = grid["freqs"]
    end = end or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    end = end.replace(minute=0, second=0, microsecond=0)
    new_start = times[-1] + dt.timedelta(hours=1)
    new_hours = [t.datetime for t in _hour_range(new_start, end)]
    if not new_hours:
        if verbose:
            print("grid already current")
        return grid, 0
    try:
        ng = compute_grid(archive, new_start, end, cfg, nperseg=nperseg, verbose=verbose)
        if len(ng["freqs"]) != len(freqs):
            if verbose:
                print("freq axis changed -> full recompute")
            full = compute_grid(archive, dt.datetime.fromisoformat(str(grid["start"])),
                                end, cfg, nperseg=nperseg, verbose=verbose)
            save_grid(full, cache)
            return full, len(full["times"]) - len(times)
        new_times = list(ng["times"]); new_psd = ng["psd_db"]
    except RuntimeError:            # no data yet in the new window -> record as gaps
        new_times = new_hours
        new_psd = np.full((len(new_hours), len(freqs)), np.nan)

    merged = dict(grid)
    merged["times"] = np.array(times + list(new_times))
    merged["psd_db"] = np.vstack([grid["psd_db"], new_psd])
    merged["end"] = end.isoformat()
    save_grid(merged, cache)
    if verbose:
        print(f"appended {len(new_times)} hours -> grid now {merged['psd_db'].shape[0]} hours")
    return merged, len(new_times)


def save_grid(grid: dict, path):
    np.savez_compressed(path, **grid)


def load_grid(path) -> dict:
    d = np.load(path, allow_pickle=True)
    return {k: d[k] for k in d.files}
