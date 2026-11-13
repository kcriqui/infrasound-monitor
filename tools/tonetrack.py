#!/usr/bin/env python3
"""Tone tracker: follow narrowband peaks through time and group them into sources.

The per-bin persistence score in ``tonehunt.py`` asks "how often is bin k more
than 6 dB above its background?".  A real machine tone wanders a few hundredths
of a Hz (temperature, load, mains frequency), so it spreads across neighbouring
0.006 Hz bins and each bin scores only part of the time.  The station's own
7.1 Hz line drifted from 7.14 to 7.10 Hz over the autumn and scored well under
its true presence.  A datacenter's variable-speed fans would wander more.

This works on the hourly PSD grid instead, in three steps:

1. **Peaks.**  In every hour, the peaks of the PSD minus a rolling-median
   baseline (``baseline_hz`` wide) that stand ``thr_db`` above it, with the
   frequency refined between bins.
2. **Tracks.**  Peaks in successive hours within ``tol`` of each other are
   linked into a track, allowing ``max_gap`` missing hours.  A track follows
   its tone as it drifts.  Tracks shorter than ``min_track_h`` hours are noise.
3. **Sources.**  Tracks at about the same frequency are one source.  For each:
   frequency range, how often it is present overall, recently and in the
   baseline, its local hour-of-day and weekday profile, whether it is
   continuous (most hours of the day) or scheduled, harmonics, and the
   ``[[tones.known]]`` label from config.toml if it matches one.

The verdict a datacenter question needs: is there a **steady, unexplained**
source, present in most hours of most days of the recent window, in every part
of the day, that is not a known one?

    python tools/tonetrack.py                     # grid from config, print sources
    python tools/tonetrack.py --png tones.png --csv sources.csv
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.ndimage import median_filter
from scipy.signal import find_peaks

from infrasound_monitor.config import (PROJECT_ROOT, KNOWN_TONES, TIMEZONE, UTC_OFFSET_HOURS,
                                        RECENT_DAYS, CHANGE_DATE)
from infrasound_monitor.psd import load_grid, iter_blocks, have_mask

THR_DB = 5.0            # peak height above the local background
BASELINE_HZ = 1.0       # width of the rolling-median background
FMIN = 0.1              # Hz; below this the sensor's high-pass dominates
TOL_HZ, TOL_REL = 0.03, 0.004    # linking tolerance: the larger of the two
MAX_GAP = 3             # hours a track may skip
MIN_TRACK_H = 3         # shorter tracks are discarded as noise
MIN_SOURCE_H = 24       # a source must be present in at least this many hours
STEADY_OCC = 0.5        # "present" in an hour-of-day bin at or above this fraction
STEADY_HOURS = 20       # a continuous source is present in >= this many of the 24


def _tol(f):
    return np.maximum(TOL_HZ, TOL_REL * np.asarray(f, float))


# ------------------------------------------------------------------ peaks ----
def hour_peaks(freqs, psd, have, thr=THR_DB, baseline_hz=BASELINE_HZ, fmin=FMIN):
    """All narrowband peaks: arrays (hour index, freq Hz, prominence dB).
    Streams the grid a block of hours at a time."""
    freqs = np.asarray(freqs, float)
    df = float(np.median(np.diff(freqs)))
    win = max(3, int(round(baseline_hz / df)) | 1)
    dist = max(1, int(round(TOL_HZ / df)))
    keep = freqs >= fmin
    H, F, P = [], [], []
    for lo, hi, blk in iter_blocks(psd, have):
        res = blk - median_filter(blk, size=(1, win), mode="nearest")
        for i in range(hi - lo):
            if not have[lo + i]:
                continue
            r = np.where(keep, res[i], 0.0)
            pk, _ = find_peaks(r, height=thr, distance=dist)
            pk = pk[(pk > 0) & (pk < len(r) - 1)]
            if not len(pk):
                continue
            y0, y1, y2 = blk[i, pk - 1], blk[i, pk], blk[i, pk + 1]
            den = y0 - 2 * y1 + y2
            with np.errstate(divide="ignore", invalid="ignore"):
                off = np.clip(np.where(den != 0, 0.5 * (y0 - y2) / den, 0.0), -0.5, 0.5)
            H.append(np.full(len(pk), lo + i))
            F.append(freqs[pk] + off * df)
            P.append(r[pk])
    if not H:
        return np.array([], int), np.array([]), np.array([])
    return np.concatenate(H), np.concatenate(F), np.concatenate(P)


# ----------------------------------------------------------------- tracks ----
def link(h, f, p, max_gap=MAX_GAP, min_len=MIN_TRACK_H):
    """Link peaks hour to hour into tracks: a list of index arrays into h/f/p.
    Strongest peaks claim the nearest active track first."""
    order = np.lexsort((-p, h))
    tracks: list[list[int]] = []
    act_f = np.array([]); act_h = np.array([], int); act_id = np.array([], int)
    i = 0
    n = len(order)
    while i < n:
        hour = h[order[i]]
        j = i
        while j < n and h[order[j]] == hour:
            j += 1
        live = hour - act_h <= max_gap + 1
        act_f, act_h, act_id = act_f[live], act_h[live], act_id[live]
        used = np.zeros(len(act_f), bool)
        new_f, new_h, new_id = [], [], []
        for k in order[i:j]:
            if len(act_f):
                d = np.abs(act_f - f[k])
                d[used] = np.inf
                m = int(np.argmin(d))
                if d[m] <= _tol(f[k]):
                    used[m] = True
                    tracks[act_id[m]].append(k)
                    act_f[m], act_h[m] = f[k], hour
                    continue
            tracks.append([k])
            new_f.append(f[k]); new_h.append(hour); new_id.append(len(tracks) - 1)
        if new_f:
            act_f = np.concatenate([act_f, new_f])
            act_h = np.concatenate([act_h, np.array(new_h, int)])
            act_id = np.concatenate([act_id, np.array(new_id, int)])
        i = j
    return [np.array(t) for t in tracks if len(t) >= min_len]


# ---------------------------------------------------------------- sources ----
def _local(times):
    """Local hour-of-day and weekday (Mon=0) for each naive-UTC grid hour."""
    tz = None
    if TIMEZONE:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(TIMEZONE)
        except Exception:
            tz = None
    if tz is not None:
        loc = [t.replace(tzinfo=dt.timezone.utc).astimezone(tz) for t in times]
    else:
        loc = [t + dt.timedelta(hours=UTC_OFFSET_HOURS) for t in times]
    return np.array([x.hour for x in loc]), np.array([x.weekday() for x in loc])


def _known(f):
    """(name, allow_steady) of the known source covering ``f``, or (None, False)."""
    for name, lo, hi, allow in KNOWN_TONES:
        if lo <= f <= hi:
            return name, allow
    return None, False


def build_sources(times, have, h, f, p, tracks, split_idx, min_hours=MIN_SOURCE_H):
    """Group tracks into sources and summarise each.  ``split_idx``: first hour of the
    recent ("after") window."""
    n = len(times)
    lhour, lwd = _local(times)
    tmed = np.array([np.median(f[t]) for t in tracks])
    groups: list[list[int]] = []
    centre = []
    for ti in np.argsort(tmed):
        if groups and abs(tmed[ti] - centre[-1]) <= 2 * _tol(centre[-1]):
            groups[-1].append(ti)
            centre[-1] = np.median(tmed[groups[-1]])
        else:
            groups.append([ti]); centre.append(tmed[ti])
    recent = np.arange(n) >= split_idx
    out = []
    for g in groups:
        idx = np.concatenate([tracks[ti] for ti in g])
        present = np.zeros(n, bool)
        present[h[idx]] = True
        if present.sum() < min_hours:
            continue
        occ_h = np.array([present[(lhour == k) & have].sum() / max(((lhour == k) & have).sum(), 1)
                          for k in range(24)])
        occ_wd = np.array([present[(lwd == k) & have].sum() / max(((lwd == k) & have).sum(), 1)
                           for k in range(7)])
        rec_have, base_have = have & recent, have & ~recent

        def by_hour(mask):
            return np.array([present[(lhour == k) & mask].sum() / max(((lhour == k) & mask).sum(), 1)
                             for k in range(24)])
        rec_h, base_h = by_hour(rec_have), by_hour(base_have)
        fq = f[idx]
        first = int(np.flatnonzero(present)[0]); last = int(np.flatnonzero(present)[-1])
        known, allow_steady = _known(float(np.median(fq)))
        out.append(dict(
            f=float(np.median(fq)), f_lo=float(np.percentile(fq, 5)), f_hi=float(np.percentile(fq, 95)),
            prom=float(np.median(p[idx])), hours=int(present.sum()), ntracks=len(g),
            longest=int(max(len(tracks[ti]) for ti in g)),
            occ=present[have].mean() if have.any() else 0.0,
            occ_base=present[have & ~recent].mean() if (have & ~recent).any() else 0.0,
            occ_recent=present[rec_have].mean() if rec_have.any() else 0.0,
            occ_hour=occ_h, occ_weekday=occ_wd, occ_hour_recent=rec_h,
            first=times[first], last=times[last], present=present,
            points=(h[idx], fq),
            continuous=int((occ_h >= STEADY_OCC).sum()) >= STEADY_HOURS,
            continuous_recent=int((rec_h >= STEADY_OCC).sum()) >= STEADY_HOURS,
            continuous_base=int((base_h >= STEADY_OCC).sum()) >= STEADY_HOURS,
            known=known, allow_steady=allow_steady))
    out.sort(key=lambda s: -s["hours"])
    # harmonics: a weaker source at ~k x a stronger one's frequency
    for s in out:
        s["harmonic_of"] = None
        for b in out:
            if b is s or b["hours"] < s["hours"]:
                continue
            for k in (2, 3, 4):
                if abs(s["f"] - k * b["f"]) <= 2 * _tol(s["f"]):
                    s["harmonic_of"] = f"{k}× {b['f']:.2f} Hz"
                    break
            if s["harmonic_of"]:
                break
    return out


def schedule(s, recent=False):
    """Human-readable presence pattern, e.g. 'continuous' or '02–12 h, weekends off'."""
    occ = s["occ_hour_recent"] if recent else s["occ_hour"]
    if (occ >= STEADY_OCC).sum() >= STEADY_HOURS:
        txt = "continuous"
    else:
        on = occ >= max(STEADY_OCC * occ.max(), 0.15)
        if not on.any():
            return "occasional"
        # longest circular run of "on" hours
        best = (0, 0)
        for start in range(24):
            if on[start] and not on[(start - 1) % 24]:
                k = 0
                while k < 24 and on[(start + k) % 24]:
                    k += 1
                best = max(best, (k, start))
        if best[0] == 0:
            txt = "all day"
        else:
            k, start = best
            txt = f"{start:02d}–{(start + k) % 24:02d} h"
    wd = s["occ_weekday"]
    if wd[:5].mean() > 0 and wd[5:].mean() < 0.5 * wd[:5].mean():
        txt += ", less at weekends"
    return txt


def verdict(sources):
    """(unexplained steady sources in the recent window, text).

    Steady = present in at least half the recent hours, and in at least half of
    the hours at STEADY_HOURS of the 24 local hours of the day.  It is explained
    when it is a harmonic of a stronger source, or a known source that may be
    steady (allow_steady) or already was in the baseline.  A known source that
    turns steady is flagged: a new source can sit at a known frequency."""
    flag = []
    for s in sources:
        if s["harmonic_of"] or not (s["occ_recent"] >= STEADY_OCC and s["continuous_recent"]):
            continue
        if s["known"] and (s["allow_steady"] or s["continuous_base"]):
            continue
        flag.append(s)
    if not flag:
        return flag, "No unexplained steady tone"
    return flag, "Unexplained steady tone at " + ", ".join(f"{s['f']:.2f} Hz" for s in flag)


def analyse(grid, split=None, recent_days=RECENT_DAYS):
    times = list(grid["times"]); freqs = np.asarray(grid["freqs"], float)
    psd = grid["psd_db"]
    have = have_mask(psd)
    if split is None:
        split = times[-1] + dt.timedelta(hours=1) - dt.timedelta(days=recent_days)
    split_idx = int(np.searchsorted(np.array(times, dtype="datetime64[h]"),
                                    np.datetime64(split, "h")))
    h, f, p = hour_peaks(freqs, psd, have)
    tracks = link(h, f, p)
    sources = build_sources(times, have, h, f, p, tracks, split_idx)
    flag, text = verdict(sources)
    return dict(times=times, sources=sources, flag=flag, verdict=text, split=split,
                npeaks=len(h), ntracks=len(tracks), have=have)


# ----------------------------------------------------------------- output ----
COLORS = ("#0d8b88", "#c2571a", "#3b6fb6", "#8e44ad", "#b9791b", "#6b8e23", "#c0392b", "#7f8c8d")


def figure(res, top=8):
    """Track points over time (top sources coloured, known bands shaded) and each top
    source's local hour-of-day presence."""
    times = res["times"]; srcs = res["sources"][:top]
    fig = plt.figure(figsize=(12, 6.2))
    gs = fig.add_gridspec(2, 1, height_ratios=[2.2, 1], hspace=0.35)
    ax = fig.add_subplot(gs[0])
    xnum = mdates.date2num(times)
    for name, lo, hi, _ in KNOWN_TONES:
        ax.axhspan(lo, hi, color="0.85", zorder=0)
    for k, s in enumerate(srcs):
        hh, ff = s["points"]
        ax.scatter(xnum[hh], ff, s=4, color=COLORS[k % len(COLORS)], lw=0,
                   label=f"{s['f']:.2f} Hz" + (f" ({s['known']})" if s["known"] else ""))
    ax.set_yscale("log"); ax.set_ylim(FMIN, 20)
    ax.set_ylabel("Frequency (Hz)"); ax.xaxis_date()
    ax.axvline(mdates.date2num(res["split"]), color="0.4", lw=0.8, ls="--")
    ax.legend(fontsize=7, markerscale=4, loc="upper left", ncol=2)
    ax.set_title("Tracked tones (grey bands: known sources; dashed: start of the recent window)",
                 fontsize=10)
    ax2 = fig.add_subplot(gs[1])
    if srcs:
        m = np.array([s["occ_hour"] for s in srcs])
        im = ax2.imshow(m, aspect="auto", cmap="magma", vmin=0, vmax=1)
        ax2.set_yticks(range(len(srcs)), [f"{s['f']:.2f} Hz" for s in srcs], fontsize=7)
        ax2.set_xticks(range(0, 24, 2))
        ax2.set_xlabel("Local hour of day")
        plt.colorbar(im, ax=ax2, label="fraction of hours present", pad=0.01)
    ax2.set_title("When each source is present", fontsize=10)
    return fig


def print_report(res, top=15):
    print(f"{res['npeaks']} peaks -> {res['ntracks']} tracks -> {len(res['sources'])} sources "
          f"(recent window from {res['split']:%Y-%m-%d %H:00} UTC)")
    print(f"verdict: {res['verdict']}\n")
    print("  freq Hz   range Hz        present: all  base  recent   prom  schedule                     label")
    for s in res["sources"][:top]:
        label = s["known"] or (f"harmonic {s['harmonic_of']}" if s["harmonic_of"] else "")
        print(f"  {s['f']:7.3f}  {s['f_lo']:6.3f}-{s['f_hi']:6.3f}   {s['occ']*100:4.0f}% "
              f"{s['occ_base']*100:4.0f}% {s['occ_recent']*100:5.0f}%  {s['prom']:+5.1f}  "
              f"{schedule(s):28s} {label}")


def write_csv(res, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_hz", "f_lo", "f_hi", "present_pct", "baseline_pct", "recent_pct",
                    "median_prom_db", "hours", "first_utc", "last_utc", "schedule", "known",
                    "harmonic_of"])
        for s in res["sources"]:
            w.writerow([f"{s['f']:.4f}", f"{s['f_lo']:.4f}", f"{s['f_hi']:.4f}",
                        f"{s['occ']*100:.1f}", f"{s['occ_base']*100:.1f}", f"{s['occ_recent']*100:.1f}",
                        f"{s['prom']:.1f}", s["hours"], f"{s['first']:%Y-%m-%dT%H}",
                        f"{s['last']:%Y-%m-%dT%H}", schedule(s), s["known"] or "",
                        s["harmonic_of"] or ""])


def main(argv=None):
    p = argparse.ArgumentParser(description="Track narrowband tones through the PSD grid.")
    p.add_argument("--cache", default=str(Path(PROJECT_ROOT) / "analysis" / "grid_full.npz"))
    p.add_argument("--change-date", default=CHANGE_DATE,
                   help="recent window starts here (default: config, else last recent_days)")
    p.add_argument("--recent-days", type=int, default=RECENT_DAYS)
    p.add_argument("--png", default=None)
    p.add_argument("--csv", default=None)
    a = p.parse_args(argv)
    grid = load_grid(a.cache)
    split = dt.datetime.fromisoformat(a.change_date) if a.change_date else None
    res = analyse(grid, split, a.recent_days)
    print_report(res)
    if a.png:
        figure(res).savefig(a.png, dpi=110, bbox_inches="tight"); print(f"\nwrote {a.png}")
    if a.csv:
        write_csv(res, a.csv); print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()
