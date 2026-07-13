#!/usr/bin/env python3
"""Infrasound event explorer — detect, characterize, and classify transient events.

Originally a datacenter-tone hunt; broadened into a general explorer of *what makes
infrasound around the station* — trains, aircraft, machinery, weather, impacts, and
local activity. The pipeline:

  1. DETECT       STA/LTA on a broadband band (default 2-15 Hz) -> candidate events.
  2. CHARACTERIZE per-event features: duration, amplitude, spectral centroid,
                  bandwidth, tonality (narrowband vs broadband), frequency drift
                  (a Doppler tell), and low/mid/high band-energy split.
  3. CLASSIFY     transparent rule-based labels (tonal / rumble / gliding /
                  impulsive / broadband) -- openly heuristic, meant to organize the
                  catalog, not to be ground truth.
  4. EXPLORE      a browsable HTML catalog (timeline + class breakdown + a table of
                  notable events with thumbnail spectrograms) and a CSV.

Caveat: indoors, local building noise dominates and swamps distant sources; this is
most powerful at a quiet outdoor site.

FUTURE — ATTRIBUTION (next step, needs network + best post-relocation): correlate
event times with public data to positively ID sources:
  * Train schedules: Caltrain / ACE / Capitol Corridor GTFS timetables.
  * Aircraft tracks: ADS-B via the OpenSky Network API / ADS-B Exchange / FlightAware
    for SJC arrivals/departures.

    python tools/transients.py "<archive>" --start 2026-06-01 --end 2026-06-22 \
        --html analysis/events/index.html --csv analysis/events/events.csv --top 24
"""
from __future__ import annotations
import argparse
import base64
import csv
import datetime as dt
import collections
import io
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy import signal as sig
from scipy.ndimage import median_filter
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client
from obspy.signal.trigger import classic_sta_lta, trigger_onset

from infrasound_monitor.config import StationConfig, DEFAULT_STATION, PA_PER_COUNT

# label -> (short description, plot color). Heuristic categories; thresholds in
# classify() are calibrated to indoor data and may need adjusting at a quiet site.
CLASSES = {
    "tonal":  ("narrowband tone (machinery / resonance)",   "#e8b13a"),
    "glide":  ("frequency glide (aircraft-like)",           "#4aa3df"),
    "rumble": ("sustained low-freq (vehicle / train / wind)", "#7d5fb2"),
    "burst":  ("short low-freq burst (local / impact / building ring)", "#8a9ba1"),
}


def _parse_date(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s}")


def characterize(seg, fs):
    """Feature vector for one event segment (already band-limited counts->Pa)."""
    rms = float(np.sqrt(np.mean(seg ** 2)))
    f, p = sig.welch(seg, fs=fs, nperseg=min(1024, len(seg)))
    band = (f >= 0.5) & (f <= 20)
    f, p = f[band], p[band]
    ptot = p.sum() + 1e-30
    centroid = float((f * p).sum() / ptot)
    bw = float(np.sqrt(((f - centroid) ** 2 * p).sum() / ptot))
    pk = float(f[np.argmax(p)])
    # tonality = how far the sharpest spectral peak rises above its LOCAL background
    # (a rolling freq-median). A narrowband tone spikes above its neighbours; a smooth
    # (red) broadband rumble sits on its own baseline, so this stays low for it.
    pdb = 10 * np.log10(p + 1e-30)
    nb = max(5, (int(round(2.0 / (f[1] - f[0]))) | 1))    # ~2 Hz window, odd
    tonality = float(np.max(pdb - median_filter(pdb, size=nb, mode="nearest")))
    frac = lambda a, b: float(p[(f >= a) & (f < b)].sum() / ptot)
    lo, mid, hi = frac(0.5, 2), frac(2, 8), frac(8, 20)

    # frequency drift (Hz/s): track the spectral centroid over the event, fit a slope
    drift = 0.0
    if len(seg) > int(4 * fs):
        ff, tt, Sxx = sig.spectrogram(seg, fs=fs, nperseg=int(2 * fs),
                                      noverlap=int(1.5 * fs))
        m = (ff >= 1) & (ff <= 20)
        ff, Sxx = ff[m], Sxx[m]
        cen = (ff[:, None] * Sxx).sum(0) / (Sxx.sum(0) + 1e-30)
        if len(tt) >= 3:
            drift = float(np.polyfit(tt, cen, 1)[0])
    return dict(rms=rms, centroid=centroid, bw=bw, peak=pk, tonality=tonality,
                drift=drift, lo=lo, mid=mid, hi=hi)


def classify(ev):
    """Transparent rule-based label. Heuristic -- organizes the catalog, not truth."""
    if ev["tonality"] >= 12:                       # a peak stands well above its neighbours
        return "tonal"
    if abs(ev["drift"]) >= 0.06 and ev["dur"] >= 25:   # frequency sweeps during the event
        return "glide"
    if ev["dur"] >= 30:                            # sustained (longer than a quick burst)
        return "rumble"
    return "burst"                                 # short low-freq burst (the dominant type)


def detect(cli, cfg, start, end, band, sta, lta, thr_on, thr_off,
           min_dur, max_dur, verbose=True):
    """Return a list of event dicts (time + features + class)."""
    events = []
    day = dt.datetime(start.year, start.month, start.day)
    while day < end:
        t0 = UTCDateTime(day.year, day.month, day.day)
        st = cli.get_waveforms(cfg.network, cfg.station, cfg.location, cfg.channel,
                               t0, t0 + 86400)
        if st:
            st.merge(method=1, fill_value=0)
            tr = max(st, key=lambda x: x.stats.npts)
            fs = tr.stats.sampling_rate
            tr.detrend("demean")
            tr.filter("bandpass", freqmin=band[0], freqmax=band[1], corners=4)
            x = tr.data.astype(float) * PA_PER_COUNT
            cft = classic_sta_lta(x, int(sta * fs), int(lta * fs))
            n0 = 0
            for on, off in trigger_onset(cft, thr_on, thr_off):
                dur = (off - on) / fs
                if not (min_dur <= dur <= max_dur):
                    continue
                ev = dict(t=tr.stats.starttime.datetime + dt.timedelta(seconds=on / fs),
                          dur=dur)
                ev.update(characterize(x[on:off], fs))
                ev["cls"] = classify(ev)
                events.append(ev)
                n0 += 1
            if verbose:
                print(f"  {day.date()}: {n0} events", flush=True)
        day += dt.timedelta(days=1)
    return events


# ------------------------------------------------------------------ output ----
def summarize(events, utc_offset):
    if not events:
        print("no events"); return
    loc = [e["t"] + dt.timedelta(hours=utc_offset) for e in events]
    print(f"\n=== {len(events)} events ===")
    cc = collections.Counter(e["cls"] for e in events)
    for k in CLASSES:
        if cc.get(k):
            print(f"  {cc[k]:4d}  {k:10s} {CLASSES[k][0]}")
    durs = np.array([e["dur"] for e in events]); cen = np.array([e["centroid"] for e in events])
    print(f"\nduration : median {np.median(durs):.0f}s  (10-90pct "
          f"{np.percentile(durs,10):.0f}-{np.percentile(durs,90):.0f}s)")
    print(f"centroid : median {np.median(cen):.1f} Hz  (10-90pct "
          f"{np.percentile(cen,10):.1f}-{np.percentile(cen,90):.1f} Hz)")
    ah = collections.Counter(l.hour for l in loc)
    print("\nlocal-hour histogram:")
    for h in range(24):
        print(f"  {h:02d}: {ah.get(h,0):3d} " + "#" * ah.get(h, 0))


def write_csv(events, path, utc_offset):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["dur", "rms", "centroid", "bw", "peak", "tonality", "drift", "lo", "mid", "hi", "cls"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "time_local"] + cols)
        for e in events:
            lt = e["t"] + dt.timedelta(hours=utc_offset)
            w.writerow([e["t"].strftime("%Y-%m-%dT%H:%M:%S"), lt.strftime("%Y-%m-%dT%H:%M:%S")]
                       + [f"{e[c]:.3f}" if isinstance(e[c], float) else e[c] for c in cols])
    print(f"wrote {len(events)} events -> {path}")


def _b64(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _thumb(cli, cfg, ev):
    t0 = UTCDateTime(ev["t"]) - 30
    st = cli.get_waveforms(cfg.network, cfg.station, cfg.location, cfg.channel,
                           t0, t0 + ev["dur"] + 120)
    if not st:
        return ""
    st.merge(method=1, fill_value=0); tr = max(st, key=lambda x: x.stats.npts)
    fs = tr.stats.sampling_rate
    x = tr.data.astype(float) * PA_PER_COUNT; x -= x.mean()
    f, tt, Sxx = sig.spectrogram(x, fs=fs, nperseg=256, noverlap=224)
    fig, ax = plt.subplots(figsize=(3.2, 1.6))
    ax.pcolormesh(tt, f, 10 * np.log10(Sxx + 1e-20), shading="nearest",
                  cmap="magma", vmin=-60, vmax=-25)
    ax.set_ylim(0, 22); ax.set_xticks([]); ax.set_yticks([0, 10, 20])
    ax.tick_params(labelsize=6)
    return _b64(fig)


def write_html(cli, cfg, events, path, utc_offset, top):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    loc = [e["t"] + dt.timedelta(hours=utc_offset) for e in events]

    # timeline scatter: time x centroid freq, colored by class, sized by amplitude
    fig, ax = plt.subplots(figsize=(12, 4.2))
    xs = mdates.date2num(loc)
    amp = np.array([e["rms"] for e in events]); amp = 8 + 120 * (amp / (amp.max() + 1e-30))
    for k in CLASSES:
        idx = [i for i, e in enumerate(events) if e["cls"] == k]
        if idx:
            ax.scatter([xs[i] for i in idx], [events[i]["centroid"] for i in idx],
                       s=[amp[i] for i in idx], c=CLASSES[k][1], alpha=0.55,
                       edgecolors="none", label=k)
    ax.xaxis_date(); ax.set_ylabel("centroid freq (Hz)"); ax.set_ylim(0, 20)
    ax.legend(loc="upper right", fontsize=7, ncol=5)
    ax.set_title("Detected infrasound events (size = amplitude, color = class, local time)")
    fig.autofmt_xdate(); fig.tight_layout()
    timeline = _b64(fig)

    cc = collections.Counter(e["cls"] for e in events)
    tiles = "".join(
        f'<div class="tile"><span class="sw" style="background:{CLASSES[k][1]}"></span>'
        f'<b>{cc.get(k,0)}</b> {k}<br><small>{CLASSES[k][0]}</small></div>'
        for k in CLASSES if cc.get(k))

    strongest = sorted(range(len(events)), key=lambda i: events[i]["rms"], reverse=True)[:top]
    rows = ""
    for i in strongest:
        e = events[i]; lt = loc[i]
        rows += (f'<tr><td>{lt:%a %b %d %H:%M}</td>'
                 f'<td><span class="sw" style="background:{CLASSES[e["cls"]][1]}"></span>{e["cls"]}</td>'
                 f'<td class="n">{e["dur"]:.0f}</td><td class="n">{e["centroid"]:.1f}</td>'
                 f'<td class="n">{e["bw"]:.1f}</td><td class="n">{e["tonality"]:.0f}</td>'
                 f'<td class="n">{e["drift"]:+.2f}</td>'
                 f'<td><img src="{_thumb(cli, cfg, e)}"></td></tr>')

    html = f"""<!doctype html><meta charset="utf-8">
<title>Infrasound event explorer — {cfg.seed_id}</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:1080px;margin:24px auto;padding:0 16px;color:#1a1a1a}}
 h1{{font-size:1.4rem;margin:0}} .sub{{color:#666;margin:2px 0 18px}}
 img{{display:block;max-width:100%;border:1px solid #eee;border-radius:6px}}
 .tiles{{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}}
 .tile{{border:1px solid #e5e5e5;border-radius:8px;padding:8px 12px;font-size:.85rem}}
 .sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}}
 table{{border-collapse:collapse;width:100%;margin-top:12px;font-size:.85rem}}
 th,td{{padding:5px 9px;border-bottom:1px solid #eee;text-align:left;vertical-align:middle}}
 th{{color:#555;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}}
 td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
 td img{{width:220px}}
</style>
<h1>Infrasound event explorer — {cfg.seed_id}</h1>
<div class="sub">{len(events)} broadband transients detected · classes are heuristic
(local building noise dominates indoors)</div>
<div class="tiles">{tiles}</div>
<img src="{timeline}" alt="event timeline">
<h2 style="font-size:1.05rem;margin-top:26px">{len(strongest)} strongest events</h2>
<table><thead><tr><th>Local time</th><th>Class</th><th class="n">Dur s</th>
<th class="n">Centroid Hz</th><th class="n">BW Hz</th><th class="n">Tonality dB</th>
<th class="n">Drift Hz/s</th><th>Spectrogram</th></tr></thead><tbody>{rows}</tbody></table>
"""
    path.write_text(html, encoding="utf-8")
    print(f"wrote event explorer ({len(strongest)} featured) -> {path}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Infrasound event explorer (detect + characterize + classify).")
    p.add_argument("archive")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date, required=True, help="UTC, exclusive")
    p.add_argument("--band", type=float, nargs=2, default=(2.0, 15.0), metavar=("FMIN", "FMAX"))
    p.add_argument("--sta", type=float, default=8.0)
    p.add_argument("--lta", type=float, default=220.0)
    p.add_argument("--thr-on", type=float, default=4.0)
    p.add_argument("--thr-off", type=float, default=1.6)
    p.add_argument("--min-dur", type=float, default=20.0)
    p.add_argument("--max-dur", type=float, default=600.0)
    p.add_argument("--utc-offset", type=float, default=-7.0)
    p.add_argument("--csv", default=None, help="write the event catalog CSV")
    p.add_argument("--html", default=None, help="write the browsable event-explorer HTML")
    p.add_argument("--top", type=int, default=24, help="# strongest events to feature in the HTML")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    cli = Client(a.archive)
    print(f"detecting {a.band[0]:g}-{a.band[1]:g} Hz events "
          f"{a.start:%Y-%m-%d} -> {a.end:%Y-%m-%d} ...")
    events = detect(cli, cfg, a.start, a.end, tuple(a.band), a.sta, a.lta,
                    a.thr_on, a.thr_off, a.min_dur, a.max_dur)
    summarize(events, a.utc_offset)
    if a.csv:
        write_csv(events, a.csv, a.utc_offset)
    if a.html:
        write_html(cli, cfg, events, a.html, a.utc_offset, a.top)


if __name__ == "__main__":
    main()
