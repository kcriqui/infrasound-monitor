#!/usr/bin/env python3
"""Long-term station quality + noise report (an ISPAQ/MUSTANG-style summary).

ISPAQ (EarthScope's portable MUSTANG client) is Linux/R-only; its headline output
is the long-term PDF-PSD, which is the same McNamara-Buland PPSD we already build.
This tool produces the equivalent report from the ObsPy stack, entirely from the
cached hourly PSD grid (so it runs in seconds), as a self-contained HTML file:

  * station metadata (from StationXML),
  * data availability / uptime %, gap count and longest gap,
  * PDF-PSD (probability density of hourly PSD vs frequency) with 10/50/90 pct,
  * per-band noise levels (RMS in Pa and dB SPL),
  * persistent-tone check (bright horizontal line = steady source).

    python tools/report.py "C:/Users/you/infra-archive" \
        --start 2026-04-09 --end 2026-07-12 --cache analysis/grid_full.npz
"""
from __future__ import annotations
import argparse
import base64
import datetime as dt
import io
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.ndimage import median_filter

from obspy import read_inventory
from infrasound_monitor.config import StationConfig, DEFAULT_STATION, P_REF_PA
from infrasound_monitor.psd import compute_grid, save_grid, load_grid


def _parse_date(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s}")


def _fig_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def availability(times, have):
    """Uptime + gap stats from the hourly data mask."""
    n = len(have)
    ndata = int(have.sum())
    uptime = 100.0 * ndata / n if n else 0.0
    gaps = []  # (start_idx, len_hours)
    i = 0
    while i < n:
        if not have[i]:
            j = i
            while j < n and not have[j]:
                j += 1
            gaps.append((i, j - i))
            i = j
        else:
            i += 1
    total_gap_h = sum(g[1] for g in gaps)
    longest = max((g[1] for g in gaps), default=0)
    # timeline strip
    fig, ax = plt.subplots(figsize=(12, 0.8))
    xnum = mdates.date2num(list(times))
    ax.imshow(have.reshape(1, -1), aspect="auto", cmap="RdYlGn", vmin=0, vmax=1,
              extent=[xnum[0], xnum[-1], 0, 1])
    ax.set_yticks([]); ax.xaxis_date()
    ax.set_title("Hourly data availability (green = data, red = gap)", fontsize=10)
    fig.autofmt_xdate()
    return dict(uptime=uptime, ndata=ndata, nhours=n, ngaps=len(gaps),
                total_gap_h=total_gap_h, longest_gap_h=longest, img=_fig_b64(fig))


def pdf_psd(freqs, psd, have):
    """PDF-PSD (probability density of hourly PSD vs frequency) + percentiles."""
    data = psd[have]
    db_edges = np.arange(-100, 21, 1.0)
    db_cent = 0.5 * (db_edges[:-1] + db_edges[1:])
    pdf = np.zeros((len(db_cent), len(freqs)))
    for j in range(len(freqs)):
        c, _ = np.histogram(data[:, j], bins=db_edges)
        s = c.sum()
        if s:
            pdf[:, j] = 100.0 * c / s
    p10, p50, p90 = np.nanpercentile(data, [10, 50, 90], axis=0)
    fig, ax = plt.subplots(figsize=(11, 5.5))
    pdf_m = np.ma.masked_equal(pdf, 0.0)
    vmax = np.percentile(pdf[pdf > 0], 98) if (pdf > 0).any() else 5
    pm = ax.pcolormesh(freqs, db_cent, pdf_m, cmap="viridis", vmin=0, vmax=vmax)
    ax.plot(freqs, p50, "w-", lw=1.4, label="median")
    ax.plot(freqs, p10, "w--", lw=1.0, label="10th pct")
    ax.plot(freqs, p90, "w:", lw=1.0, label="90th pct")
    ax.set_xscale("log"); ax.set_xlim(freqs.min(), freqs.max())
    ax.set_xlabel("Frequency (Hz)"); ax.set_ylabel("PSD (dB re Pa²/Hz)")
    ax.set_title("PDF-PSD — probability density of hourly PSD")
    ax.legend(loc="upper right", fontsize=8)
    plt.colorbar(pm, ax=ax, label="probability (%)")
    return dict(img=_fig_b64(fig), freqs=freqs, p10=p10, p50=p50, p90=p90)


def tone_view(times, freqs, psd, have, baseline_hz=1.0, thr=6.0, vmax=8.0):
    df = float(np.median(np.diff(freqs)))
    win = max(3, int(round(baseline_hz / df)) | 1)
    with np.errstate(invalid="ignore"):
        resid = psd - median_filter(psd, size=(1, win), mode="nearest")
    xnum = mdates.date2num(list(times))
    fig, ax = plt.subplots(figsize=(12, 5))
    im = ax.imshow(resid.T, origin="lower", aspect="auto",
                   extent=[xnum[0], xnum[-1], float(freqs.min()), float(freqs.max())],
                   vmin=0, vmax=vmax, cmap="magma")
    ax.xaxis_date(); ax.set_ylabel("Frequency (Hz)")
    ax.set_title("Tone-hunt — dB above local spectral background "
                 "(steady bright horizontal line = persistent tone)")
    plt.colorbar(im, ax=ax, label="dB above background")
    fig.autofmt_xdate()
    res_ok = resid[have]
    persist = np.mean(res_ok > thr, axis=0) if res_ok.size else np.zeros(len(freqs))
    med = np.median(res_ok, axis=0) if res_ok.size else np.zeros(len(freqs))
    order = np.argsort(persist)[::-1][:8]
    rows = [(float(freqs[i]), float(persist[i] * 100), float(med[i]))
            for i in sorted(order, key=lambda k: freqs[k])]
    peak_pct = float(persist.max() * 100) if persist.size else 0.0
    return dict(img=_fig_b64(fig), rows=rows, peak_pct=peak_pct, thr=thr)


def band_stats(freqs, psd, have):
    data = psd[have]
    med = np.nanmedian(data, axis=0)                 # median PSD per freq (dB)
    df = float(np.median(np.diff(freqs)))
    bands = [(0.05, 0.5), (0.5, 5.0), (5.0, 20.0), (0.05, 20.0)]
    out = []
    for f1, f2 in bands:
        m = (freqs >= f1) & (freqs < f2)
        if not m.any():
            continue
        power = np.sum(10 ** (med[m] / 10.0)) * df   # Pa^2 (median-hour)
        rms = float(np.sqrt(power))
        spl = 20 * np.log10(rms / P_REF_PA) if rms > 0 else float("nan")
        out.append(dict(band=f"{f1:g}–{f2:g} Hz",
                        med_db=float(np.median(med[m])), rms_pa=rms, spl=spl))
    return out


def build(archive, start, end, out_html, cfg, cache=None, nperseg=8192,
          baseline_hz=1.0):
    if cache and Path(cache).exists():
        print(f"loading cached grid {cache}")
        grid = load_grid(cache)
    else:
        print("computing PSD grid ...")
        grid = compute_grid(archive, start, end, cfg, nperseg=nperseg)
        if cache:
            save_grid(grid, cache)
    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = np.asarray(grid["psd_db"], float)
    have = np.isfinite(psd).all(axis=1)

    # station metadata
    meta = {}
    inv_path = Path(archive) / "station.xml"
    if inv_path.exists():
        try:
            inv = read_inventory(str(inv_path))
            ch = inv[0][0][0]
            meta = dict(lat=ch.latitude, lon=ch.longitude, elev=ch.elevation,
                        fs=ch.sample_rate,
                        sens=inv[0][0][0].response.instrument_sensitivity.value,
                        site=inv[0][0].site.name)
        except Exception as e:
            print(f"  (metadata read failed: {e})")

    av = availability(times, have)
    pdf = pdf_psd(freqs, psd, have)
    tone = tone_view(times, freqs, psd, have, baseline_hz=baseline_hz)
    bands = band_stats(freqs, psd, have)

    html = _render_html(cfg, start, end, meta, av, pdf, tone, bands, grid)
    Path(out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html).write_text(html, encoding="utf-8")
    print(f"wrote {out_html}")
    print(f"  uptime {av['uptime']:.1f}%  ({av['ndata']}/{av['nhours']} h)  "
          f"gaps {av['ngaps']} (longest {av['longest_gap_h']} h)")
    print(f"  tone check: peak persistence {tone['peak_pct']:.0f}% of hours "
          f"(>{'50' } % at a fixed freq = real tone)")
    return out_html


def _row(cells, header=False):
    tag = "th" if header else "td"
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


def _render_html(cfg, start, end, meta, av, pdf, tone, bands, grid):
    def g(k, fmt="{}", d="—"):
        v = meta.get(k)
        return fmt.format(v) if v is not None else d
    grade = ("excellent" if av["uptime"] >= 95 else "good" if av["uptime"] >= 85
             else "fair" if av["uptime"] >= 70 else "poor")
    tone_verdict = ("<b>tone-free</b> (no steady narrowband source)"
                    if tone["peak_pct"] < 50 else
                    "<b>possible persistent tone</b> — investigate")
    meta_rows = "".join(_row(r) for r in [
        ("Station", f"{cfg.seed_id}"),
        ("Site", g("site")),
        ("Location", f'{g("lat","{:.5f}")}, {g("lon","{:.5f}")}  ·  {g("elev","{:.1f}")} m'),
        ("Sample rate", g("fs", "{:.4f} sps")),
        ("Sensitivity", g("sens", "{:.0f} counts/Pa")),
        ("Span (UTC)", f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"),
    ])
    qc_rows = "".join(_row(r) for r in [
        ("Uptime", f'{av["uptime"]:.1f}% &nbsp;<span class="grade">{grade}</span>'),
        ("Hours with data", f'{av["ndata"]} / {av["nhours"]}'),
        ("Gaps", f'{av["ngaps"]}'),
        ("Total gap time", f'{av["total_gap_h"]} h'),
        ("Longest gap", f'{av["longest_gap_h"]} h'),
    ])
    band_rows = _row(("Band", "Median PSD (dB re Pa²/Hz)", "RMS (Pa)", "≈ dB SPL"), header=True)
    band_rows += "".join(_row((b["band"], f'{b["med_db"]:.1f}',
                               f'{b["rms_pa"]:.4f}', f'{b["spl"]:.1f}')) for b in bands)
    tone_rows = _row(("Frequency", f'% of hours &gt;{tone["thr"]:.0f} dB', "median prom."), header=True)
    tone_rows += "".join(_row((f'{f:.2f} Hz', f'{p:.0f}%', f'{m:+.1f} dB'))
                         for f, p, m in tone["rows"])
    return f"""<!doctype html><meta charset="utf-8">
<title>Infrasound station report {cfg.seed_id}</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#1a1a1a}}
 h1{{font-size:1.5rem;margin-bottom:0}} h2{{font-size:1.1rem;margin-top:1.8em;border-bottom:2px solid #eee;padding-bottom:4px}}
 .sub{{color:#666;margin-top:2px}} table{{border-collapse:collapse;margin:8px 0}} td,th{{padding:4px 12px;border-bottom:1px solid #eee;text-align:left;font-size:.92rem}}
 th{{color:#555}} img{{max-width:100%;height:auto;margin:6px 0;border:1px solid #eee}}
 .grade{{background:#e8f5e9;color:#2e7d32;border-radius:4px;padding:1px 6px;font-size:.8rem}}
 .verdict{{background:#f5f7fa;border-left:4px solid #4a90d9;padding:10px 14px;margin:10px 0}}
</style>
<h1>Infrasound station report — {cfg.seed_id}</h1>
<div class="sub">MUSTANG/ISPAQ-style summary · generated from the hourly PSD grid · Infiltec INFRA20</div>

<h2>Station</h2><table>{meta_rows}</table>

<h2>Data quality</h2><table>{qc_rows}</table>
<img src="{av['img']}" alt="availability timeline">

<h2>Long-term spectral noise (PDF-PSD)</h2>
<img src="{pdf['img']}" alt="PDF-PSD">
<table>{band_rows}</table>

<h2>Persistent-tone check</h2>
<div class="verdict">Verdict: {tone_verdict}. Peak persistence {tone['peak_pct']:.0f}% of hours.</div>
<img src="{tone['img']}" alt="tone-hunt waterfall">
<table>{tone_rows}</table>
<p class="sub">A datacenter tone would appear as a steady bright horizontal line and climb toward ~100% persistence at a fixed frequency.</p>
"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Long-term infrasound station report (ISPAQ-style).")
    p.add_argument("archive")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date, required=True)
    p.add_argument("--out", default=None, help="output HTML (default: <archive>/../analysis)")
    p.add_argument("--cache", default=None, help="PSD grid .npz (share with infra-waterfall)")
    p.add_argument("--nperseg", type=int, default=8192)
    p.add_argument("--baseline-hz", type=float, default=1.0)
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    out = Path(a.out) if a.out else Path(a.archive).parent / "analysis" / \
        f"report_{cfg.seed_id}.html"
    build(a.archive, a.start, a.end, out, cfg, cache=a.cache,
          nperseg=a.nperseg, baseline_hz=a.baseline_hz)


if __name__ == "__main__":
    main()
