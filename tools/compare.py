#!/usr/bin/env python3
"""Wind-matched before/after comparison of infrasound levels.

The station exists to answer one question: did the background change when a
nearby source (a datacenter) started running?  Wind is the largest cause of
level changes here, so comparing raw levels mostly measures the weather.  This
compares like with like:

1. Per UTC hour, the level in each octave band (dB SPL re 20 uPa, from the PSD
   grid) and the mean wind at the airport (``infrasound_monitor.weather``).
   Hours with rain or without weather data are left out.
2. From the "before" hours, the baseline's level as a function of wind: the
   median level in narrow wind bins, interpolated between them.
3. Each hour's residual is its level minus that baseline curve at its own wind.
   The change is the median "after" residual minus the median "before" residual:
   "how much louder is it now than the baseline was in the same wind".  "After"
   hours windier or calmer than anything in the baseline can't be judged and
   are left out; ``coverage`` says how many were judged.
4. A 90% interval comes from resampling whole days in each period (a block
   bootstrap, refitting the curve each time): hours within a day are not
   independent.

Until ``[analysis] change_date`` is set in config.toml, "after" is the last
``recent_days`` days and "before" is everything earlier: a running check that
the baseline is stable.  Seasonal changes still show (the ocean microbaroms grow
from September), so read the lowest bands with that in mind.

    python tools/compare.py                          # grid + weather from config
    python tools/compare.py --change-date 2027-02-01 --hours all
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from infrasound_monitor.config import (PROJECT_ROOT, P_REF_PA, WEATHER_DIR,
                                        CHANGE_DATE, RECENT_DAYS)
from infrasound_monitor.psd import load_grid, iter_blocks, have_mask
from infrasound_monitor import weather

OCTAVES = (0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)     # band centres, Hz
WIND_EDGES = (0.0, 1.0, 2.0, 3.0, 4.5, 6.5, 9.0, np.inf)   # m/s, for the summary table/plot
CURVE_EDGES = tuple(np.arange(0.0, 8.01, 0.5)) + (10.0, 13.0, np.inf)   # baseline-curve bins
RAIN_MM = 0.2                                              # hours wetter than this are skipped


def band_defs(freqs, centres=OCTAVES):
    """[(label, mask)] of octave bands that fit inside the grid's frequency range."""
    out = []
    for fc in centres:
        lo, hi = fc / np.sqrt(2), fc * np.sqrt(2)
        m = (freqs >= lo) & (freqs < hi)
        if m.sum() >= 2:
            out.append((f"{fc:g} Hz", m))
    return out


def band_levels(freqs, psd, have, bands):
    """[nhours, nbands] band level in dB SPL; NaN for gap hours.  Streams the grid."""
    df = float(np.median(np.diff(freqs)))
    out = np.full((len(psd), len(bands)), np.nan)
    for lo, hi, blk in iter_blocks(psd, have):
        lin = 10.0 ** (blk / 10.0)                              # Pa^2/Hz
        for j, (_, m) in enumerate(bands):
            p = lin[:, m].sum(axis=1) * df                      # Pa^2
            with np.errstate(divide="ignore", invalid="ignore"):
                out[lo:hi, j] = 10 * np.log10(p / P_REF_PA ** 2)
    out[~have] = np.nan
    return out


def baseline_curve(w, L, min_n=8):
    """Median level per narrow wind bin: (wind at each bin's median, [k, nbands])."""
    cw, cl = [], []
    for lo, hi in zip(CURVE_EDGES[:-1], CURVE_EDGES[1:]):
        m = (w >= lo) & (w < hi)
        if m.sum() >= min_n:
            cw.append(np.median(w[m]))
            cl.append(np.median(L[m], axis=0))
    return np.array(cw), np.array(cl)


def _change(L, w, ib, ia, min_n):
    """Wind-matched change per band, and the share of ``ia`` hours it could judge."""
    nb = L.shape[1]
    cw, cl = baseline_curve(w[ib], L[ib], min_n)
    if len(cw) < 2:
        return np.full(nb, np.nan), 0.0
    ja = ia[(w[ia] >= cw[0]) & (w[ia] <= cw[-1])]       # inside the baseline's wind range
    if len(ja) < min_n:
        return np.full(nb, np.nan), len(ja) / max(len(ia), 1)
    d = np.empty(nb)
    for b in range(nb):
        rb = L[ib, b] - np.interp(w[ib], cw, cl[:, b])
        ra = L[ja, b] - np.interp(w[ja], cw, cl[:, b])
        d[b] = np.median(ra) - np.median(rb)
    return d, len(ja) / len(ia)


def compare(times, L, wx, split, select, labels, min_n=8, n_boot=400, seed=0):
    """Before/after statistics.  ``select`` is a boolean mask of usable hours."""
    t = np.array(times, dtype="datetime64[h]")
    wind = wx["wind"]
    ok = select & np.isfinite(wind) & np.isfinite(L).all(axis=1)
    ok &= ~(wx["precip"] > RAIN_MM)
    split64 = np.datetime64(split, "h")
    ib = np.flatnonzero(ok & (t < split64))
    ia = np.flatnonzero(ok & (t >= split64))
    nbins = len(WIND_EDGES) - 1
    wbin = np.clip(np.digitize(np.nan_to_num(wind), WIND_EDGES) - 1, 0, nbins - 1)
    res = dict(n_before=len(ib), n_after=len(ia), split=split, labels=labels,
               wind_edges=WIND_EDGES, bins=[])
    if not len(ib) or not len(ia):
        res["delta"] = res["lo"] = res["hi"] = res["raw"] = np.full(len(labels), np.nan)
        res["coverage"] = 0.0
        return res
    res["delta"], res["coverage"] = _change(L, wind, ib, ia, min_n)
    res["raw"] = np.median(L[ia], axis=0) - np.median(L[ib], axis=0)
    for j in range(nbins):
        hb, ha = ib[wbin[ib] == j], ia[wbin[ia] == j]
        res["bins"].append(dict(
            lo=WIND_EDGES[j], hi=WIND_EDGES[j + 1], nb=len(hb), na=len(ha),
            before=np.median(L[hb], axis=0) if len(hb) else np.full(len(labels), np.nan),
            after=np.median(L[ha], axis=0) if len(ha) else np.full(len(labels), np.nan)))
    # Block bootstrap over days ("night of" = the date 12 h earlier, so a night is one block).
    day = (t - np.timedelta64(12, "h")).astype("datetime64[D]")

    def blocks(idx):
        d = day[idx]
        return [idx[d == u] for u in np.unique(d)]
    bb, ba = blocks(ib), blocks(ia)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        sb = np.concatenate([bb[k] for k in rng.integers(0, len(bb), len(bb))])
        sa = np.concatenate([ba[k] for k in rng.integers(0, len(ba), len(ba))])
        d, _ = _change(L, wind, sb, sa, min_n)
        boots.append(d)
    boots = np.array(boots)
    with np.errstate(invalid="ignore"):
        res["lo"], res["hi"] = np.nanpercentile(boots, [5, 95], axis=0)
    return res


def wind_level_correlation(L, wx, select):
    """Spearman correlation of each band's level with wind, for picking a station."""
    from scipy.stats import spearmanr
    ok = select & np.isfinite(wx["wind"]) & np.isfinite(L).all(axis=1)
    if ok.sum() < 20:
        return np.full(L.shape[1], np.nan)
    return np.array([spearmanr(wx["wind"][ok], L[ok, j]).statistic for j in range(L.shape[1])])


def analyse(grid, split=None, recent_days=RECENT_DAYS, hours="night", stations=None,
            wx_dir=WEATHER_DIR, night_window=5, n_boot=400):
    """Everything the dashboard and the CLI need, from a loaded grid."""
    import tonehunt                       # sibling tool (tools/ is on sys.path)
    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = grid["psd_db"]
    have = have_mask(psd)
    bands = band_defs(freqs)
    labels = [b[0] for b in bands]
    L = band_levels(freqs, psd, have, bands)
    wx = weather.hourly(times, stations, wx_dir)
    hrs = np.array([t.hour for t in times])
    night_hours, _ = tonehunt.quiet_window(times, freqs, psd, have, night_window)
    night = np.isin(hrs, night_hours)
    select = {"night": night, "day": ~night, "all": np.ones(len(times), bool)}[hours]
    mode = "change date"
    if split is None:
        mode = "recent"
        split = (times[-1] + dt.timedelta(hours=1) - dt.timedelta(days=recent_days)).replace(
            minute=0, second=0, microsecond=0)
    res = compare(times, L, wx, split, select, labels, n_boot=n_boot)
    res.update(mode=mode, hours=hours, night_hours=night_hours, recent_days=recent_days,
               stations=wx["stations"], centres=[float(l.split()[0]) for l in labels],
               wind_hours=int(np.isfinite(wx["wind"]).sum()), n_hours=len(times),
               station_hours=[int((wx["station"] == i).sum()) for i in range(len(wx["stations"]))],
               corr=wind_level_correlation(L, wx, select))
    return res


def figure(res, show_bands=("1 Hz", "4 Hz", "16 Hz")):
    """Left: wind-matched change per band (90% interval), raw change for contrast.
    Right: median level against wind, before and after, for a few bands."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.2), gridspec_kw=dict(width_ratios=[1.25, 1]))
    x = np.array(res["centres"])
    d, lo, hi = res["delta"], res.get("lo"), res.get("hi")
    a1.axhline(0, color="0.6", lw=0.8)
    a1.scatter(x, res["raw"], marker="o", facecolors="none", edgecolors="0.55",
               label="raw change (not wind-matched)", zorder=2)
    if lo is not None:
        a1.errorbar(x, d, yerr=[d - lo, hi - d], fmt="o", color="#0d8b88", capsize=3,
                    label="wind-matched change, 90% interval", zorder=3)
    a1.set_xscale("log"); a1.set_xticks(x); a1.set_xticklabels([f"{c:g}" for c in x])
    a1.set_xlabel("Octave band centre (Hz)"); a1.set_ylabel("After − before (dB)")
    a1.legend(fontsize=8, loc="best")
    a1.set_title(f"{res['n_after']} after vs {res['n_before']} before hours "
                 f"({res['hours']}), {res['coverage']*100:.0f}% judged", fontsize=10)
    centres = [(b["lo"] + min(b["hi"], 12.0)) / 2 for b in res["bins"]]
    colors = ("#3b6fb6", "#c2571a", "#6b8e23", "#8e44ad")
    for k, name in enumerate(show_bands):
        if name not in res["labels"]:
            continue
        j = res["labels"].index(name)
        bef = [b["before"][j] if b["nb"] >= 8 else np.nan for b in res["bins"]]
        aft = [b["after"][j] if b["na"] >= 8 else np.nan for b in res["bins"]]
        c = colors[k % len(colors)]
        a2.plot(centres, bef, "-o", color=c, ms=4, label=f"{name} before")
        a2.plot(centres, aft, "--s", color=c, ms=4, mfc="none", label=f"{name} after")
    a2.set_xlabel("Mean wind at the airport (m/s)"); a2.set_ylabel("Median level (dB SPL)")
    a2.set_title("Level against wind", fontsize=10)
    a2.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    return fig


def print_report(res):
    print(f"\n{res['mode']}: split at {res['split']:%Y-%m-%d %H:00} UTC   hours: {res['hours']} "
          f"(night = UTC {res['night_hours']})")
    print(f"weather: stations {res['stations']} cover {res['wind_hours']} of {res['n_hours']} grid hours "
          f"(per station {res['station_hours']})")
    print(f"usable hours: before {res['n_before']}, after {res['n_after']}; "
          f"{res['coverage']*100:.0f}% of the after hours lie in the baseline's wind range")
    print("\nband     wind-matched change [90% interval]   raw change   level~wind (Spearman)")
    for j, lab in enumerate(res["labels"]):
        lo = res.get("lo", [np.nan] * len(res["labels"]))[j]
        hi = res.get("hi", [np.nan] * len(res["labels"]))[j]
        print(f"  {lab:8s} {res['delta'][j]:+6.1f} dB [{lo:+5.1f}, {hi:+5.1f}]"
              f"          {res['raw'][j]:+6.1f} dB      {res['corr'][j]:+.2f}")
    print("\nhours per wind bin (before / after):")
    for b in res["bins"]:
        hi = "" if np.isinf(b["hi"]) else f"{b['hi']:g}"
        print(f"  {b['lo']:g}-{hi:4s} m/s : {b['nb']:5d} / {b['na']:4d}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Wind-matched before/after comparison of levels.")
    p.add_argument("--cache", default=str(Path(PROJECT_ROOT) / "analysis" / "grid_full.npz"))
    p.add_argument("--change-date", default=CHANGE_DATE,
                   help="YYYY-MM-DD (UTC): after = from this date (default: config)")
    p.add_argument("--recent-days", type=int, default=RECENT_DAYS,
                   help="without a change date, after = the last N days")
    p.add_argument("--hours", choices=("night", "day", "all"), default="night")
    p.add_argument("--stations", nargs="*", default=None, help="default: config [weather]")
    p.add_argument("--png", default=None, help="also write the figure here")
    a = p.parse_args(argv)
    grid = load_grid(a.cache)
    split = dt.datetime.fromisoformat(a.change_date) if a.change_date else None
    res = analyse(grid, split, a.recent_days, a.hours,
                  [s.upper() for s in a.stations] if a.stations is not None else None)
    print_report(res)
    if a.png:
        figure(res).savefig(a.png, dpi=110)
        print(f"\nwrote {a.png}")


if __name__ == "__main__":
    main()
