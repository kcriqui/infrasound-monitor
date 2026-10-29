"""Reduce the miniSEED archive to an hourly PSD grid (the waterfall backbone).

For each UTC hour in a date range we compute a Welch power spectral density in
pressure units (Pa^2/Hz).  The result is a ``time x frequency`` grid that the
waterfall renderer draws directly.  Grids are cached so re-rendering a date
range is instant.

Cache layout -- two files, so the nightly job's memory does not grow with the
record:

  grid_full.npz       times, freqs and metadata (small)
  grid_full.psd.f32   the PSD rows: raw little-endian float32, one row per hour

``load_grid`` memory-maps the rows instead of reading them, and ``append_grid``
adds new hours to the end of the file in place.  Pages of a read-only map are
clean and file-backed, so the kernel can drop them under pressure instead of
swapping -- a multi-year grid costs the reader only the block it is looking at.
float32 is ~1e-6 dB of precision, far below anything the analysis resolves.

A cache written before this layout (``psd_db`` inside the ``.npz``) still loads,
fully into memory, and is converted on its next ``save_grid``/``append_grid``.
"""
from __future__ import annotations
import datetime as dt
import os
from pathlib import Path

import numpy as np
from scipy import signal
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client

from .config import (StationConfig, DEFAULT_STATION, PA_PER_COUNT, PASSBAND_HZ)

SECONDS_PER_HOUR = 3600
GRID_DTYPE = np.dtype("<f4")          # on-disk PSD rows (dB)
CHUNK_HOURS = 256                     # rows per block for streaming passes over a grid


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
    meta = _load_meta(cache)
    if "psd_db" in meta:            # legacy single-file cache: convert it once
        save_grid(meta, cache)
        meta = _load_meta(cache)
    times = list(meta["times"])
    freqs = meta["freqs"]
    end = end or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    end = end.replace(minute=0, second=0, microsecond=0)
    new_start = times[-1] + dt.timedelta(hours=1)
    new_hours = [t.datetime for t in _hour_range(new_start, end)]
    if not new_hours:
        if verbose:
            print("grid already current")
        return load_grid(cache), 0
    try:
        ng = compute_grid(archive, new_start, end, cfg, nperseg=nperseg, verbose=verbose)
        if len(ng["freqs"]) != len(freqs):
            if verbose:
                print("freq axis changed -> full recompute")
            build_cache(cache, archive, dt.datetime.fromisoformat(str(meta["start"])),
                        end, cfg, nperseg=nperseg, verbose=verbose)
            full = load_grid(cache)
            return full, len(full["times"]) - len(times)
        new_times = list(ng["times"]); new_psd = ng["psd_db"]
    except RuntimeError:            # no data yet in the new window -> record as gaps
        new_times = new_hours
        new_psd = np.full((len(new_hours), len(freqs)), np.nan)

    n = append_grid(cache, new_times, new_psd, end)
    if verbose:
        print(f"appended {len(new_times)} hours -> grid now {n} hours")
    return load_grid(cache), len(new_times)


def build_cache(cache, archive, start: dt.datetime, end: dt.datetime,
                cfg: StationConfig = DEFAULT_STATION, nperseg: int = 8192,
                chunk_days: int = 30, verbose: bool = True) -> int:
    """Compute a grid over [start, end) straight into a cache file, a month at a
    time, so seeding a multi-year record never holds more than a month of rows.
    Hours before the first data are kept as gap rows, as ``compute_grid`` does.
    Returns the number of hours written."""
    step = dt.timedelta(days=chunk_days)
    pending = []                    # leading hours with no data: freqs not known yet
    nfreq = None                    # set by the first window with data
    written = 0
    t = start
    while t < end:
        t2 = min(t + step, end)
        try:
            g = compute_grid(archive, t, t2, cfg, nperseg=nperseg, verbose=verbose)
        except RuntimeError:        # no data in this window
            hours = [h.datetime for h in _hour_range(t, t2)]
            if written:
                written = append_grid(cache, hours, np.full((len(hours), nfreq), np.nan), t2)
            else:
                pending += hours
            t = t2
            continue
        if not written:
            nfreq = len(g["freqs"])
            if pending:
                g["times"] = np.array(pending + list(g["times"]))
                g["psd_db"] = np.vstack([np.full((len(pending), nfreq), np.nan), g["psd_db"]])
            g["start"] = start.isoformat()
            save_grid(g, cache)
            written = len(g["times"])
        else:
            if len(g["freqs"]) != nfreq:
                raise RuntimeError(f"frequency axis changed at {t:%Y-%m-%d}")
            written = append_grid(cache, list(g["times"]), g["psd_db"], t2)
        t = t2
    if not written:
        raise RuntimeError("no data with >= nperseg samples found in range")
    return written


def _psd_path(path) -> Path:
    return Path(path).with_suffix(".psd.f32")      # grid_full.npz -> grid_full.psd.f32


def _replace_atomically(path: Path, write):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        write(f)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def _save_meta(meta: dict, path):
    _replace_atomically(Path(path), lambda f: np.savez_compressed(f, **meta))


def _load_meta(path) -> dict:
    with np.load(path, allow_pickle=True) as d:
        return {k: d[k] for k in d.files}


def save_grid(grid: dict, path):
    """Write a whole grid: rows to the sidecar, everything else to the ``.npz``.
    Rows are written a block at a time, so saving a memory-mapped grid stays cheap."""
    path = Path(path)
    # Ensure the parent dir exists (e.g. a fresh clone has no analysis/ -- it's gitignored).
    path.parent.mkdir(parents=True, exist_ok=True)
    psd = grid["psd_db"]

    def write_rows(f):
        for lo in range(0, len(psd), CHUNK_HOURS):
            f.write(np.ascontiguousarray(psd[lo:lo + CHUNK_HOURS], dtype=GRID_DTYPE).tobytes())

    # Rows first, then the metadata that says how many there are: a crash between
    # the two leaves extra rows, which load_grid ignores and append_grid trims.
    _replace_atomically(_psd_path(path), write_rows)
    _save_meta({k: v for k, v in grid.items() if k != "psd_db"}, path)


def append_grid(path, new_times, new_psd, end: dt.datetime) -> int:
    """Append hours to a cache in place.  Returns the new row count."""
    path = Path(path)
    meta = _load_meta(path)
    if "psd_db" in meta:
        save_grid(meta, path)
        meta = _load_meta(path)
    n_old, nfreq = len(meta["times"]), len(meta["freqs"])
    new_psd = np.ascontiguousarray(new_psd, dtype=GRID_DTYPE)
    if new_psd.shape != (len(new_times), nfreq):
        raise ValueError(f"rows {new_psd.shape} do not match {len(new_times)} hours x {nfreq} freqs")
    with open(_psd_path(path), "r+b") as f:
        f.truncate(n_old * nfreq * GRID_DTYPE.itemsize)   # drop rows of an interrupted append
        f.seek(0, os.SEEK_END)
        f.write(new_psd.tobytes())
        f.flush(); os.fsync(f.fileno())
    meta["times"] = np.array(list(meta["times"]) + list(new_times))
    meta["end"] = end.isoformat()
    _save_meta(meta, path)
    return len(meta["times"])


def load_grid(path) -> dict:
    """Load a cached grid; ``psd_db`` is a read-only memory map of the rows."""
    grid = _load_meta(path)
    if "psd_db" in grid:            # legacy single-file cache
        return grid
    n, nfreq = len(grid["times"]), len(grid["freqs"])
    side = _psd_path(path)
    need = n * nfreq * GRID_DTYPE.itemsize
    have = side.stat().st_size if side.exists() else -1
    if have < need:
        raise ValueError(f"{side} holds {max(have, 0)} bytes; {n} hours x {nfreq} freqs "
                         f"need {need} -- the cache is damaged, rebuild it")
    grid["psd_db"] = (np.memmap(side, dtype=GRID_DTYPE, mode="r", shape=(n, nfreq))
                      if n else np.empty((0, nfreq), GRID_DTYPE))
    return grid


def iter_blocks(psd, rows=None, chunk: int = CHUNK_HOURS):
    """Yield (lo, hi, block) over a grid's rows, each block a float64 copy.  The
    way to make one pass over a memory-mapped grid without materialising it.
    ``rows`` (a boolean mask) skips blocks that contain no selected row."""
    for lo in range(0, len(psd), chunk):
        hi = min(lo + chunk, len(psd))
        if rows is not None and not rows[lo:hi].any():
            continue
        yield lo, hi, np.asarray(psd[lo:hi], dtype=float)


def have_mask(psd) -> np.ndarray:
    """Hours whose whole spectrum is finite (i.e. not a data gap)."""
    out = np.zeros(len(psd), dtype=bool)
    for lo, hi, blk in iter_blocks(psd):
        out[lo:hi] = np.isfinite(blk).all(axis=1)
    return out
