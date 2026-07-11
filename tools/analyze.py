#!/usr/bin/env python3
"""Analyze the INFRA20 miniSEED archive with standard ObsPy tools.

Reads the SDS archive over a date range, applies the StationXML calibration
(counts -> Pascals), and writes the two views most useful for the datacenter
before/after question:

  * a **PPSD** (probabilistic power spectral density, McNamara-Buland) plus its
    time-spectrogram -- the industry-standard way to characterise background
    noise over days/weeks and reveal a persistent narrowband tone; and
  * (optional) a **calibrated dayplot** for a single UTC day (AmaSeis-style drum).

The INFRA20 has a flat pressure response, so the PPSD uses
``special_handling="hydrophone"`` (sensitivity only, no seismometer
differentiation).  PSD is reported in dB relative to 1 (Pa^2/Hz).

Examples
--------
    py = C:\\Users\\Kevin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe

    # PPSD over the whole baseline
    python tools/analyze.py "G:/My Drive/- My Projects/Infrasound monitor/archive" \
        --start 2026-04-09 --end 2026-07-12

    # ...plus a calibrated drum plot for one day
    python tools/analyze.py "<archive>" --start 2026-07-01 --end 2026-07-02 \
        --dayplot 2026-07-01

Needs the installed package (obspy etc.): ``pip install -e .``
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import matplotlib
matplotlib.use("Agg")   # save PNGs without a display

import numpy as np
from obspy import read_inventory, UTCDateTime
from obspy.clients.filesystem.sds import Client
from obspy.signal import PPSD

from infrasound_monitor.config import DEFAULT_STATION, P_REF_PA


def _parse_date(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s} (use YYYY-MM-DD)")


def _daterange_utc(start: dt.datetime, end: dt.datetime):
    """Yield each UTC day-start in [start, end)."""
    day = dt.datetime(start.year, start.month, start.day)
    end0 = dt.datetime(end.year, end.month, end.day)
    while day < end0:
        yield day
        day += dt.timedelta(days=1)


def build_ppsd(cli: Client, inv, seed, start, end, out_dir: Path,
               ppsd_length: float = 3600.0,
               db_bins: tuple = (-100.0, 20.0, 1.0),
               noise_model=None) -> PPSD | None:
    """Accumulate a PPSD day-by-day (bounded memory) and save its plots.

    ``db_bins`` MUST suit pressure data (Pa^2/Hz, roughly -100..0 dB).  ObsPy's
    default (-200,-50,1) is for seismic acceleration and would clip every value
    at the -50 dB top bin, saturating the whole histogram.
    """
    net, sta, loc, cha = seed
    ppsd = None
    days = added = 0
    for day in _daterange_utc(start, end):
        t = UTCDateTime(day)
        st = cli.get_waveforms(net, sta, loc, cha, t, t + 86400)
        if not st:
            continue
        days += 1
        if ppsd is None:
            ppsd = PPSD(st[0].stats, metadata=inv, ppsd_length=ppsd_length,
                        special_handling="hydrophone", db_bins=db_bins)
        if ppsd.add(st):
            added += 1
        print(f"  {day:%Y-%m-%d}: added {st[0].stats.npts:,} samples")

    if ppsd is None:
        print("  no data in range -- nothing to analyze")
        return None

    seed_id = ".".join(seed)
    p1 = out_dir / f"ppsd_{seed_id}.png"
    p2 = out_dir / f"ppsd_spectrogram_{seed_id}.png"
    _plot_ppsd_histogram(ppsd, p1, noise_model=noise_model)
    try:
        ppsd.plot_spectrogram(filename=str(p2), show=False)
    except Exception as e:                      # some obspy builds lack filename kw
        print(f"  (spectrogram skipped: {e})")
        p2 = None

    print(f"\nPPSD over {days} day(s) with data:")
    print(f"  histogram   -> {p1}  (with the station's own 10/50/90th-pct noise)")
    if p2:
        print(f"  spectrogram -> {p2}")
    _report_persistent_peak(ppsd)
    return ppsd


def _plot_ppsd_histogram(ppsd: PPSD, out_path: Path, noise_model=None):
    """Save the PPSD with the station's own percentile envelope, plus any
    external global noise-model curves supplied via ``noise_model``."""
    import matplotlib.pyplot as plt
    # ObsPy's built-in noise models are seismic (Peterson) and do not apply to
    # pressure, so they stay off; the 10/50/90th percentiles are the station's
    # own low / median / high noise -- the real before/after reference.
    ppsd.plot(show=False, show_noise_models=False, show_percentiles=True,
              percentiles=[10, 50, 90], xaxis_frequency=True,
              period_lim=(0.04, 12.0))
    fig = plt.gcf()
    ax = fig.axes[0]
    for label, freqs, db, style in (noise_model or []):
        ax.plot(freqs, db, style, lw=1.8, label=label)
    if noise_model:
        ax.legend(loc="upper right", fontsize=7)
    fig.savefig(str(out_path), dpi=110)
    plt.close(fig)


def load_noise_model(path: Path):
    """Load external global infrasound noise-model curves from a CSV.

    Expected header ``freq_hz,low_db,high_db[,median_db]`` with PSD in dB
    relative to 1 Pa^2/Hz (the same axis as this PPSD).  Returns a list of
    ``(label, freqs, db, mpl_style)`` tuples, or [] if the file has no data.
    Values are NOT bundled -- digitize them from a published source (e.g.
    Bowman et al. 2005; Brown et al. 2014); see tools/noise_models/.
    """
    import csv
    with open(path, newline="") as fh:
        rows = list(csv.reader(
            ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")))
    if len(rows) < 2:                          # header only / empty -> nothing to plot
        return []
    header = [h.strip() for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    if "freq_hz" not in idx:
        return []
    data = np.array([[float(x) for x in r] for r in rows[1:]], dtype=float)
    f = data[:, idx["freq_hz"]]
    spec = [("low_db", "global low noise", "c--"),
            ("median_db", "global median", "w-."),
            ("high_db", "global high noise", "r--")]
    return [(lbl, f, data[:, idx[col]], st)
            for col, lbl, st in spec if col in idx]


def _report_persistent_peak(ppsd: PPSD, prominence_db: float = 6.0):
    """Look for a persistent NARROWBAND peak standing above the smooth background.

    The natural pressure spectrum is red (biggest at ~0.1 Hz), so the global PSD
    maximum is always that natural peak and tells us nothing.  A datacenter tone
    is instead a narrow spike ABOVE the local broadband level, so we score each
    frequency by its *prominence* over a rolling-median background and report the
    largest -- or declare the baseline tone-free when nothing stands out.
    """
    try:
        periods, med = ppsd.get_percentile(percentile=50)
    except Exception:
        return
    freqs = 1.0 / np.asarray(periods, float)
    med = np.asarray(med, float)
    order = np.argsort(freqs)
    freqs, med = freqs[order], med[order]
    good = np.isfinite(med)
    freqs, med = freqs[good], med[good]
    if med.size < 8:
        return

    # smooth background: rolling median over an odd window of frequency bins
    w = max(5, (med.size // 12) | 1)
    half = w // 2
    bg = np.array([np.median(med[max(0, i - half):i + half + 1])
                   for i in range(med.size)])
    prom = med - bg

    def band(f1, f2):
        m = (freqs >= f1) & (freqs < f2)
        return np.median(med[m]) if m.any() else float("nan")

    print(f"  median PSD: {band(0.1, 1.0):.0f} dB @0.1-1Hz, "
          f"{band(1.0, 10.0):.0f} dB @1-10Hz (re Pa^2/Hz)")
    i = int(np.argmax(prom))
    if prom[i] >= prominence_db:
        print(f"  candidate narrowband peak: {freqs[i]:.2f} Hz, "
              f"+{prom[i]:.1f} dB above background")
        print("  -> confirm it is steady AND present at ~3-4am local "
              "(the datacenter signature).")
    else:
        print(f"  no narrowband peak >{prominence_db:.0f} dB above background "
              f"-> tone-free baseline (good)")
        print("  -> a NEW steady peak (esp. 5-20 Hz, present at ~3-4am local) "
              "would flag the datacenter.")


def calibrated_dayplot(cli: Client, inv, seed, day: dt.datetime, out_dir: Path):
    net, sta, loc, cha = seed
    t = UTCDateTime(day)
    st = cli.get_waveforms(net, sta, loc, cha, t, t + 86400)
    if not st:
        print(f"  dayplot {day:%Y-%m-%d}: no data")
        return
    st.merge(method=0)
    st.remove_sensitivity(inv)                  # counts -> Pa

    tr = st[0]
    data = np.ma.compressed(tr.data) if np.ma.isMaskedArray(tr.data) else tr.data
    rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else float("nan")
    spl = 20.0 * np.log10(rms / P_REF_PA) if rms > 0 else float("nan")

    out = out_dir / f"dayplot_{'.'.join(seed)}_{day:%Y%m%d}.png"
    st.plot(type="dayplot", interval=60, outfile=str(out),
            title=f"{'.'.join(seed)}  {day:%Y-%m-%d} UTC  (Pa)")
    print(f"  dayplot {day:%Y-%m-%d}: RMS {rms:.4f} Pa  (~{spl:.1f} dB SPL re 20 uPa)"
          f"  -> {out}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Analyze the INFRA20 SDS archive with ObsPy (PPSD + dayplot).")
    p.add_argument("archive", help="SDS archive root (contains station.xml)")
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--end", type=_parse_date, required=True,
                   help="YYYY-MM-DD (UTC, exclusive)")
    p.add_argument("--out-dir", default=None,
                   help="where to write PNGs (default: <archive>/../analysis)")
    p.add_argument("--dayplot", type=_parse_date, default=None,
                   help="also render a calibrated dayplot for this UTC day")
    p.add_argument("--ppsd-length", type=float, default=3600.0,
                   help="PPSD segment length in seconds (default 3600)")
    p.add_argument("--db-min", type=float, default=-100.0,
                   help="PPSD lower dB bin edge for pressure PSD (default -100)")
    p.add_argument("--db-max", type=float, default=20.0,
                   help="PPSD upper dB bin edge for pressure PSD (default 20)")
    p.add_argument("--noise-model", default=None,
                   help="CSV of external global noise-model curves to overlay "
                        "(freq_hz,low_db,high_db[,median_db]; dB re Pa^2/Hz). "
                        "See tools/noise_models/.")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)

    archive = Path(a.archive)
    inv = read_inventory(str(archive / "station.xml"))
    cli = Client(str(archive))
    seed = (a.network, a.station, a.location, a.channel)

    out_dir = Path(a.out_dir) if a.out_dir else archive.parent / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    noise_model = []
    if a.noise_model:
        noise_model = load_noise_model(Path(a.noise_model))
        print(f"noise-model overlay: {len(noise_model)} curve(s) from {a.noise_model}"
              if noise_model else
              f"noise-model file {a.noise_model} has no data rows (skipping overlay)")

    print(f"analyzing {'.'.join(seed)}  {a.start:%Y-%m-%d} -> {a.end:%Y-%m-%d}  "
          f"(out: {out_dir})")
    build_ppsd(cli, inv, seed, a.start, a.end, out_dir, ppsd_length=a.ppsd_length,
               db_bins=(a.db_min, a.db_max, 1.0), noise_model=noise_model)
    if a.dayplot:
        calibrated_dayplot(cli, inv, seed, a.dayplot, out_dir)


if __name__ == "__main__":
    main()
