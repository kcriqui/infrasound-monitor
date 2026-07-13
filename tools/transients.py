#!/usr/bin/env python3
"""Detect broadband infrasound transients — candidate trains, aircraft, and local events.

Trains and aircraft don't show up as steady tones; they're brief **broadband,
low-frequency transients** (a rumble that rises and falls over tens of seconds to
a few minutes). This tool band-passes the raw waveform to a broadband band
(default 2–15 Hz), runs a classic STA/LTA detector, and catalogs each burst with
its time, duration, peak frequency, and amplitude — then summarizes the
time-of-day pattern (weekday vs weekend) and saves example spectrograms.

Caveat: with the sensor indoors, local building noise (HVAC, doors, footfalls)
dominates and swamps distant train/aircraft signatures — the event population
mostly tracks office activity. This detector becomes genuinely useful at a quiet
outdoor site, where real train/aircraft transients stand out.

--------------------------------------------------------------------------------
FUTURE WORK — correlate detections with public data to positively ID sources:
  * Train schedules: Caltrain / ACE / Capitol Corridor publish GTFS timetables;
    a real train pass should line up with a scheduled arrival near the station.
  * Aircraft tracks: ADS-B feeds — the OpenSky Network API (free, has historical
    state vectors), ADS-B Exchange, or FlightAware — for SJC arrivals/departures;
    an overflight should coincide with a logged track passing near the station.
  Match detected transient times against these and a train/plane becomes a
  *confirmed* detection rather than a candidate. (Best done post-relocation.)
--------------------------------------------------------------------------------

    python tools/transients.py "<archive>" --start 2026-06-01 --end 2026-06-22 \
        --out-dir analysis/transients --csv analysis/transients/events.csv --examples 4
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import collections
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal as sig
from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client
from obspy.signal.trigger import classic_sta_lta, trigger_onset

from infrasound_monitor.config import StationConfig, DEFAULT_STATION, PA_PER_COUNT


def _parse_date(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s}")


def detect(cli, cfg, start, end, band, sta, lta, thr_on, thr_off,
           min_dur, max_dur, verbose=True):
    """Return a list of events: (utc_datetime, duration_s, peak_hz, rms_pa)."""
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
                seg = x[on:off]
                f, p = sig.welch(seg, fs=fs, nperseg=min(1024, len(seg)))
                m = (f >= 0.5) & (f <= 20)
                pk = float(f[m][np.argmax(p[m])])
                rms = float(np.sqrt(np.mean(seg ** 2)))
                events.append((tr.stats.starttime.datetime + dt.timedelta(seconds=on / fs),
                               dur, pk, rms))
                n0 += 1
            if verbose:
                print(f"  {day.date()}: {n0} events", flush=True)
        day += dt.timedelta(days=1)
    return events


def summarize(events, utc_offset):
    if not events:
        print("no events"); return
    loc = [e[0] + dt.timedelta(hours=utc_offset) for e in events]
    allc = collections.Counter(l.hour for l in loc)
    wk = collections.Counter(l.hour for l in loc if l.weekday() < 5)
    we = collections.Counter(l.hour for l in loc if l.weekday() >= 5)
    print(f"\n=== {len(events)} broadband transients ===")
    print("local hour :  all (weekday / weekend)")
    for h in range(24):
        print(f"  {h:02d}: {allc.get(h,0):3d} ({wk.get(h,0):2d}/{we.get(h,0):2d}) "
              + "#" * allc.get(h, 0))
    durs = np.array([e[1] for e in events]); pks = np.array([e[2] for e in events])
    print(f"\nduration : median {np.median(durs):.0f}s  (10-90pct "
          f"{np.percentile(durs,10):.0f}-{np.percentile(durs,90):.0f}s)")
    print(f"peak freq: median {np.median(pks):.1f} Hz  (10-90pct "
          f"{np.percentile(pks,10):.1f}-{np.percentile(pks,90):.1f} Hz)")
    print("\nNext step to ID sources: correlate these times with Caltrain/ACE GTFS "
          "schedules and ADS-B (OpenSky) SJC tracks -- see the module docstring.")


def write_csv(events, path, utc_offset):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "time_local", "duration_s", "peak_hz", "rms_pa"])
        for t, dur, pk, rms in events:
            lt = t + dt.timedelta(hours=utc_offset)
            w.writerow([t.strftime("%Y-%m-%dT%H:%M:%S"), lt.strftime("%Y-%m-%dT%H:%M:%S"),
                        f"{dur:.1f}", f"{pk:.2f}", f"{rms:.5f}"])
    print(f"wrote {len(events)} events -> {path}")


def save_examples(cli, cfg, events, out_dir, utc_offset, n):
    if not events or n <= 0:
        return
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)

    def spec(ev, tag, k):
        t0 = UTCDateTime(ev[0]) - 60
        st = cli.get_waveforms(cfg.network, cfg.station, cfg.location, cfg.channel,
                               t0, t0 + ev[1] + 180)
        if not st:
            return
        st.merge(method=1, fill_value=0)
        tr = max(st, key=lambda x: x.stats.npts)
        fs = tr.stats.sampling_rate
        x = tr.data.astype(float) * PA_PER_COUNT; x -= x.mean()
        f, tt, Sxx = sig.spectrogram(x, fs=fs, nperseg=256, noverlap=224)
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.pcolormesh(tt, f, 10 * np.log10(Sxx + 1e-20), shading="nearest",
                      cmap="magma", vmin=-60, vmax=-25)
        ax.set_ylim(0, 25); ax.set_xlabel("seconds"); ax.set_ylabel("Frequency (Hz)")
        lt = ev[0] + dt.timedelta(hours=utc_offset)
        ax.set_title(f"{tag} #{k}  {lt:%a %b %d %H:%M} local  "
                     f"dur~{ev[1]:.0f}s  peak~{ev[2]:.1f} Hz")
        fig.tight_layout()
        fig.savefig(out_dir / f"event_{tag}_{k}.png", dpi=110)
        plt.close(fig)

    night = sorted((e for e in events if (e[0] + dt.timedelta(hours=utc_offset)).hour in range(0, 6)),
                   key=lambda e: e[3], reverse=True)[:n]
    dayev = sorted((e for e in events if (e[0] + dt.timedelta(hours=utc_offset)).hour in range(9, 22)),
                   key=lambda e: e[3], reverse=True)[:n]
    for k, e in enumerate(night):
        spec(e, "night", k)
    for k, e in enumerate(dayev):
        spec(e, "day", k)
    print(f"saved {len(night)} night + {len(dayev)} day example spectrograms -> {out_dir}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Detect broadband infrasound transients (trains/aircraft/local).")
    p.add_argument("archive")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date, required=True, help="UTC, exclusive")
    p.add_argument("--band", type=float, nargs=2, default=(2.0, 15.0),
                   metavar=("FMIN", "FMAX"), help="broadband detect band, Hz")
    p.add_argument("--sta", type=float, default=8.0, help="short-term avg window, s")
    p.add_argument("--lta", type=float, default=220.0, help="long-term avg window, s")
    p.add_argument("--thr-on", type=float, default=4.0, help="STA/LTA trigger-on ratio")
    p.add_argument("--thr-off", type=float, default=1.6, help="STA/LTA trigger-off ratio")
    p.add_argument("--min-dur", type=float, default=20.0, help="min event duration, s")
    p.add_argument("--max-dur", type=float, default=600.0, help="max event duration, s")
    p.add_argument("--utc-offset", type=float, default=-7.0, help="local = UTC + this (San Jose PDT = -7)")
    p.add_argument("--csv", default=None, help="write the event catalog to this CSV")
    p.add_argument("--out-dir", default=None, help="directory for example spectrograms")
    p.add_argument("--examples", type=int, default=0, help="save N strongest night + N day example spectrograms")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    cli = Client(a.archive)
    print(f"detecting {a.band[0]:g}-{a.band[1]:g} Hz transients "
          f"{a.start:%Y-%m-%d} -> {a.end:%Y-%m-%d} ...")
    events = detect(cli, cfg, a.start, a.end, tuple(a.band), a.sta, a.lta,
                    a.thr_on, a.thr_off, a.min_dur, a.max_dur)
    summarize(events, a.utc_offset)
    if a.csv:
        write_csv(events, a.csv, a.utc_offset)
    if a.examples and a.out_dir:
        save_examples(cli, cfg, events, a.out_dir, a.utc_offset, a.examples)


if __name__ == "__main__":
    main()
