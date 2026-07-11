"""Render the long-term spectral waterfall (the primary display).

Produces a self-contained interactive HTML file (Plotly, JS inlined) so it opens
in any browser with no server and can be emailed/shared as-is.  Time on the x
axis, frequency (log) on the y axis, PSD in dB (Pa^2/Hz) as colour.  Data gaps
render as blank columns.
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np

from .config import StationConfig, DEFAULT_STATION
from .psd import compute_grid, save_grid, load_grid


def _logbin_freq(freqs, psd_db, nbins):
    """Average the PSD into log-spaced frequency bins (in power) for a compact,
    smooth log-axis display without shipping thousands of linear bins.
    Empty or all-gap bins become NaN (rendered as blanks)."""
    import warnings
    freqs = np.asarray(freqs, float)
    fmin = max(freqs[freqs > 0].min(), 1e-3)
    edges = np.logspace(np.log10(fmin), np.log10(freqs.max()), nbins + 1)
    centers = np.sqrt(edges[:-1] * edges[1:])
    power = 10 ** (np.asarray(psd_db, float) / 10.0)        # [ntime, nfreq]
    out = np.full((power.shape[0], nbins), np.nan)
    idx = np.digitize(freqs, edges) - 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)     # empty/all-NaN bins -> NaN
        for b in range(nbins):
            sel = idx == b
            if sel.any():
                out[:, b] = np.nanmean(power[:, sel], axis=1)
    # Bins finer than the source resolution (low freq) come out empty; fill those
    # interior holes per time-column by interpolation. Leave genuine data-gap
    # hours (columns with <2 finite bins) untouched.
    for j in range(out.shape[0]):
        col = out[j]
        good = np.isfinite(col)
        if good.sum() >= 2:
            out[j] = np.interp(centers, centers[good], col[good])
    with np.errstate(divide="ignore", invalid="ignore"):
        return centers, 10 * np.log10(out)


def render_waterfall(grid: dict, out_html, title: str | None = None,
                     colorscale: str = "Viridis", freq_bins: int = 240) -> str:
    import plotly.graph_objects as go

    times = grid["times"]
    freqs = np.asarray(grid["freqs"], dtype=float)
    psd_db = np.asarray(grid["psd_db"], dtype=float)         # [ntime, nfreq]
    if freq_bins and len(freqs) > freq_bins:
        freqs, psd_db = _logbin_freq(freqs, psd_db, freq_bins)
    z = psd_db.T                                             # [nfreq, ntime]
    finite = z[np.isfinite(z)]
    zmin, zmax = np.percentile(finite, [5, 99]) if finite.size else (-60, -10)

    seed = str(grid.get("seed_id", ""))
    title = title or f"Infrasound spectral waterfall  {seed}"

    fig = go.Figure(go.Heatmap(
        x=times, y=freqs, z=z, zmin=zmin, zmax=zmax,
        colorscale=colorscale, colorbar=dict(title="PSD dB<br>(Pa²/Hz)"),
        hovertemplate="%{x}<br>%{y:.3f} Hz<br>%{z:.1f} dB<extra></extra>",
    ))
    fig.update_yaxes(type="log", title="Frequency (Hz)")
    fig.update_xaxes(title="Time (UTC)")
    fig.update_layout(title=title, template="plotly_dark",
                      margin=dict(l=70, r=20, t=60, b=50))
    out_html = str(out_html)
    fig.write_html(out_html, include_plotlyjs=True, full_html=True)
    return out_html


def build(archive, start: dt.datetime, end: dt.datetime, out_html,
          cfg: StationConfig = DEFAULT_STATION, nperseg: int = 8192,
          cache=None) -> str:
    if cache and Path(cache).exists():
        print(f"loading cached grid {cache}")
        grid = load_grid(cache)
    else:
        grid = compute_grid(archive, start, end, cfg, nperseg=nperseg)
        if cache:
            save_grid(grid, cache); print(f"cached grid -> {cache}")
    path = render_waterfall(grid, out_html)
    print(f"wrote {path}")
    return path


def _parse_date(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s} (use YYYY-MM-DD)")


def main(argv=None):
    p = argparse.ArgumentParser(description="Render an infrasound spectral waterfall from an SDS archive.")
    p.add_argument("archive", help="SDS archive root")
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD (UTC, exclusive)")
    p.add_argument("--out", default="waterfall.html")
    p.add_argument("--nperseg", type=int, default=8192)
    p.add_argument("--cache", default=None, help="optional .npz PSD cache path")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    build(a.archive, a.start, a.end, a.out, cfg, nperseg=a.nperseg, cache=a.cache)


if __name__ == "__main__":
    main()
