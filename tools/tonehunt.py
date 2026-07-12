#!/usr/bin/env python3
"""Tone-hunting spectral waterfall: reveal PERSISTENT NARROWBAND tones over many days.

The plain waterfall (``infra-waterfall``) is dominated by the day/night cycle
(vertical banding) and the strong <5 Hz natural energy, which bury a faint steady
tone.  This tool subtracts, for each hour, a smooth spectral baseline (a rolling
median over frequency), leaving only NARROWBAND prominence.  A persistent source
-- e.g. a datacenter -- then shows as a steady **bright horizontal line**.

It writes a PNG (time x frequency, "dB above local background") and prints a
ranked list of the most persistent narrowband frequencies, so the answer is both
visual and quantitative.

    python tools/tonehunt.py "C:/Users/you/infra-archive" \
        --start 2026-04-09 --end 2026-07-12 --cache analysis/grid_full.npz

Reuse the same ``--cache`` grid that ``infra-waterfall`` builds to render instantly.
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.ndimage import median_filter

from infrasound_monitor.config import StationConfig, DEFAULT_STATION
from infrasound_monitor.psd import compute_grid, save_grid, load_grid


def _parse_date(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s} (use YYYY-MM-DD)")


def tone_residual(psd_db: np.ndarray, freqs: np.ndarray, baseline_hz: float):
    """Per-hour narrowband prominence = PSD minus a rolling freq-median baseline."""
    df = float(np.median(np.diff(freqs)))
    win = max(3, int(round(baseline_hz / df)) | 1)          # odd window in bins
    baseline = median_filter(psd_db, size=(1, win), mode="nearest")
    with np.errstate(invalid="ignore"):
        return psd_db - baseline, win


def quiet_window(times, freqs, psd, have, nhours: int):
    """Auto-detect the quietest N consecutive UTC hours (the 'night' window).

    A datacenter runs 24/7 but the background drops at night, so a steady tone is
    most detectable in the quiet pre-dawn hours.  We find that window empirically
    from the diurnal cycle of the 1-20 Hz median level -- no timezone assumptions.
    Returns the window as consecutive UTC hours (may wrap past midnight).
    """
    hours_utc = np.array([t.hour for t in times])
    band = (freqs >= 1) & (freqs <= 20)
    level = np.nanmedian(psd[:, band], axis=1)
    diurnal = np.array([np.nanmedian(level[(hours_utc == h) & have])
                        if ((hours_utc == h) & have).any() else np.nan
                        for h in range(24)])
    best = None
    for s in range(24):
        idx = [(s + k) % 24 for k in range(nhours)]
        tot = np.nansum(diurnal[idx])
        if best is None or tot < best[0]:
            best = (tot, idx)
    return best[1], diurnal          # idx is consecutive (start..start+n), wraps


def night_dates_matrix(times, resid, have, night_hours):
    """Average the per-hour residual over each night -> one column per night."""
    anchor = night_hours[0]
    hours_utc = np.array([t.hour for t in times])
    nmask = np.isin(hours_utc, night_hours) & have
    # assign each night-hour to a night label = date of (t - anchor hours)
    labels = [(t - dt.timedelta(hours=anchor)).date() for t in times]
    udates = sorted({labels[i] for i in range(len(times)) if nmask[i]})
    idx = {d: k for k, d in enumerate(udates)}
    nfreq = resid.shape[1]
    out = np.full((len(udates), nfreq), np.nan)
    acc = {d: [] for d in udates}
    for i in range(len(times)):
        if nmask[i]:
            acc[labels[i]].append(resid[i])
    for d, rows in acc.items():
        if rows:
            out[idx[d]] = np.nanmean(np.vstack(rows), axis=0)
    day_dt = [dt.datetime(d.year, d.month, d.day) for d in udates]
    return out, day_dt, nmask


def render(times, freqs, resid, out_png: Path, seed: str, vmax: float):
    xnum = mdates.date2num(list(times))
    fig, ax = plt.subplots(figsize=(14, 6))
    im = ax.imshow(resid.T, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=0, vmax=vmax, cmap="magma")
    ax.xaxis_date()
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title(f"Tone-hunt waterfall  {seed}  —  dB above local spectral background\n"
                 f"a persistent tone = steady BRIGHT HORIZONTAL line")
    plt.colorbar(im, ax=ax, label="dB above background")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(str(out_png), dpi=120)
    plt.close(fig)


def render_nightly(day_dt, freqs, night_wf, out_png: Path, seed: str, vmax: float,
                   local_label: str):
    xnum = mdates.date2num(day_dt)
    fig, ax = plt.subplots(figsize=(14, 6))
    im = ax.imshow(night_wf.T, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=0, vmax=vmax, cmap="magma")
    ax.xaxis_date(); ax.set_ylabel("Frequency (Hz)")
    ax.set_title(f"NIGHT-ONLY tone waterfall  {seed}  ({local_label})  —  "
                 f"nightly-mean dB above background\n"
                 f"a persistent tone = steady BRIGHT HORIZONTAL line")
    plt.colorbar(im, ax=ax, label="dB above background")
    fig.autofmt_xdate(); fig.tight_layout()
    fig.savefig(str(out_png), dpi=120); plt.close(fig)


def report_night_vs_day(resid, freqs, times, night_mask, have, thr, topn=10):
    day_mask = have & ~night_mask
    pn = np.mean(resid[night_mask] > thr, axis=0) if night_mask.any() else np.zeros(len(freqs))
    pd_ = np.mean(resid[day_mask] > thr, axis=0) if day_mask.any() else np.zeros(len(freqs))
    diff = pn - pd_
    order = np.argsort(diff)[::-1][:topn]
    print(f"\nnight hours: {int(night_mask.sum())}   day hours: {int(day_mask.sum())}")
    print("frequencies MORE persistent at NIGHT than day (the datacenter-like signature):")
    for i in sorted(order, key=lambda k: freqs[k]):
        flag = "  <-- watch" if (pn[i] > 0.5 and diff[i] > 0.2) else ""
        print(f"  {freqs[i]:6.2f} Hz : night {pn[i]*100:3.0f}% vs day {pd_[i]*100:3.0f}% "
              f"(+{diff[i]*100:2.0f} pts){flag}")
    print("  night persistence >~50% at a fixed freq that's also >>day = a real steady source")


def report_persistence(resid, freqs, times, thr: float, topn: int = 12):
    have = np.isfinite(resid).all(axis=1)
    res_ok = resid[have]
    if not res_ok.size:
        print("  no data in range"); return
    persist = np.mean(res_ok > thr, axis=0)
    med = np.median(res_ok, axis=0)
    order = np.argsort(persist)[::-1][:topn]
    print(f"\ndata hours: {have.sum()} of {len(times)}")
    print(f"most-persistent narrowband frequencies (fraction of hours >{thr:.0f} dB "
          f"above background):")
    for i in sorted(order, key=lambda k: freqs[k]):
        flag = "  <-- steady tone?" if persist[i] > 0.5 else ""
        print(f"  {freqs[i]:6.2f} Hz : {persist[i]*100:4.0f}% of hours "
              f"(median {med[i]:+.1f} dB){flag}")
    print("  rule of thumb: >~50% at a FIXED freq = a real steady tone; "
          "single-digit %% scattered = normal tone-free noise")


def main(argv=None):
    p = argparse.ArgumentParser(description="Tone-hunting spectral waterfall over many days.")
    p.add_argument("archive", help="SDS archive root")
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD (UTC, exclusive)")
    p.add_argument("--out", default=None, help="output PNG (default: <archive>/../analysis)")
    p.add_argument("--cache", default=None, help="PSD grid .npz to load/save (share with infra-waterfall)")
    p.add_argument("--nperseg", type=int, default=8192)
    p.add_argument("--baseline-hz", type=float, default=1.0,
                   help="width of the rolling freq-median baseline (Hz); narrower = only sharper tones")
    p.add_argument("--threshold", type=float, default=6.0,
                   help="dB-above-background counted as 'present' in the persistence stat")
    p.add_argument("--vmax", type=float, default=8.0, help="color scale max (dB above background)")
    p.add_argument("--night", action="store_true",
                   help="restrict to the quietest hours (auto-detected) -- best datacenter-tone SNR")
    p.add_argument("--night-window", type=int, default=5,
                   help="number of consecutive quiet UTC hours to treat as 'night' (default 5)")
    p.add_argument("--utc-offset", type=float, default=-7.0,
                   help="local = UTC + this, for labelling only (San Jose PDT = -7)")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)

    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    if a.cache and Path(a.cache).exists():
        print(f"loading cached grid {a.cache}")
        grid = load_grid(a.cache)
    else:
        print(f"computing PSD grid {a.start:%Y-%m-%d} -> {a.end:%Y-%m-%d} ...")
        grid = compute_grid(a.archive, a.start, a.end, cfg, nperseg=a.nperseg)
        if a.cache:
            save_grid(grid, a.cache); print(f"cached grid -> {a.cache}")

    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = np.asarray(grid["psd_db"], float)
    resid, win = tone_residual(psd, freqs, a.baseline_hz)
    print(f"baseline: rolling median over {win} freq bins (~{a.baseline_hz:.2f} Hz)")

    have = np.isfinite(psd).all(axis=1)
    analysis = Path(a.archive).parent / "analysis"

    if a.night:
        night_hours, diurnal = quiet_window(times, freqs, psd, have, a.night_window)
        loc = [int((h + a.utc_offset) % 24) for h in night_hours]
        local_label = (f"night = {night_hours[0]:02d}:00-{(night_hours[-1]+1)%24:02d}:00 UTC "
                       f"~ {min(loc):02d}:00-{(max(loc)+1)%24:02d}:00 local")
        print(f"auto night window (quietest {a.night_window} h): UTC {night_hours} "
              f"~ local {sorted(loc)}  [{local_label}]")
        night_wf, day_dt, nmask = night_dates_matrix(times, resid, have, night_hours)
        out = Path(a.out) if a.out else analysis / f"nightwaterfall_{cfg.seed_id}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        render_nightly(day_dt, freqs, night_wf, out, cfg.seed_id, min(a.vmax, 6.0), local_label)
        print(f"wrote {out}  ({len(day_dt)} nights)")
        report_night_vs_day(resid, freqs, times, nmask, have, a.threshold)
    else:
        out = Path(a.out) if a.out else analysis / f"tonewaterfall_{cfg.seed_id}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        render(times, freqs, resid, out, cfg.seed_id, a.vmax)
        print(f"wrote {out}")
        report_persistence(resid, freqs, times, a.threshold)


if __name__ == "__main__":
    main()
