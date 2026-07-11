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

    out = Path(a.out) if a.out else Path(a.archive).parent / "analysis" / \
        f"tonewaterfall_{cfg.seed_id}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    render(times, freqs, resid, out, cfg.seed_id, a.vmax)
    print(f"wrote {out}")
    report_persistence(resid, freqs, times, a.threshold)


if __name__ == "__main__":
    main()
