#!/usr/bin/env python3
"""Live drum-recorder view of the raw INFRA20 data — AmaSeis-style, local only.

The acquisition daemon owns the serial port, so this doesn't read the port; it
tails the small rolling **live buffer** the daemon writes (run the daemon with
``--live-file``). It renders a scrolling helicorder/drum of the last few minutes
of raw pressure, updating ~once a second — the closest thing to AmaSeis's live
display, without interrupting archiving.

    python tools/live.py                       # uses the default live file
    python tools/live.py --live-file C:\\Users\\Kevin\\infra-daemon\\live.npz
    python tools/live.py --snapshot live.png   # render one frame (no window)

The daemon must be running with a matching --live-file (see infra-daemon\\wrapper.ps1).
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np

from infrasound_monitor.config import PA_PER_COUNT

DEFAULT_LIVE = r"C:\Users\Kevin\infra-daemon\live.npz"


def load_live(path):
    """Return (pa_array, fs, t_end_datetime, age_seconds) or None if unavailable."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = np.load(p, allow_pickle=False)
        y = d["y"].astype(np.float64)
        fs = float(d["fs"])
        t_end = dt.datetime.fromisoformat(str(d["t_end"]))
    except Exception:
        return None
    age = (dt.datetime.now(dt.timezone.utc) - t_end).total_seconds()
    return y * PA_PER_COUNT, fs, t_end, age


def draw(ax, live, window, row, utc_offset):
    ax.clear()
    if live is None:
        ax.text(0.5, 0.5, "waiting for live data…\n(is the InfraAcquire daemon running "
                "with --live-file?)", ha="center", va="center", color="#888",
                transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([]); return

    pa, fs, t_end, age = live
    show = min(len(pa), int(window * fs))
    pa = pa[-show:]
    n = len(pa)
    if n < int(2 * fs):
        ax.text(0.5, 0.5, "buffering…", ha="center", va="center", transform=ax.transAxes)
        return

    age_s = (n - 1 - np.arange(n)) / fs            # seconds before the latest sample
    r = (age_s // row).astype(int)                 # row 0 = newest (drawn at bottom)
    x = row - (age_s % row)                        # newest sample near the right edge
    nrows = int(r.max()) + 1
    amp = max(float(np.percentile(np.abs(pa), 99)), 1e-4)
    scale = 0.42 / amp

    yt, yl = [], []
    for rr in range(nrows):
        m = r == rr
        ax.plot(x[m], rr + pa[m] * scale, lw=0.5, color="#1a4a8a")
        row_end = (t_end - dt.timedelta(seconds=rr * row) + dt.timedelta(hours=utc_offset))
        yt.append(rr); yl.append(row_end.strftime("%H:%M:%S"))
    ax.set_yticks(yt); ax.set_yticklabels(yl, fontsize=8)
    ax.set_ylim(-0.6, nrows - 0.4)
    ax.set_xlim(0, row)
    ax.set_xlabel(f"seconds within each {row:g}-s row")
    ax.set_ylabel("row end (local time)")
    rms = float(np.sqrt(np.mean((pa - pa.mean()) ** 2)))
    stale = "  ⚠ STALE" if age > 15 else ""
    now_local = (t_end + dt.timedelta(hours=utc_offset)).strftime("%Y-%m-%d %H:%M:%S")
    ax.set_title(f"INFRA20 live  ·  {now_local} local  ·  rms {rms*1000:.1f} mPa  "
                 f"·  {age:.0f}s ago{stale}", fontsize=11)


def main(argv=None):
    p = argparse.ArgumentParser(description="Live drum view of the raw INFRA20 data (local).")
    p.add_argument("--live-file", default=DEFAULT_LIVE)
    p.add_argument("--window", type=float, default=600.0, help="total seconds shown (drum height)")
    p.add_argument("--row", type=float, default=60.0, help="seconds per drum row")
    p.add_argument("--utc-offset", type=float, default=-7.0, help="local = UTC + this")
    p.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    p.add_argument("--snapshot", default=None, help="render one frame to this PNG and exit (no window)")
    a = p.parse_args(argv)

    if a.snapshot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 7))
        draw(ax, load_live(a.live_file), a.window, a.row, a.utc_offset)
        fig.tight_layout(); fig.savefig(a.snapshot, dpi=110)
        print(f"wrote {a.snapshot}")
        return

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    fig, ax = plt.subplots(figsize=(11, 7))
    fig.canvas.manager.set_window_title("INFRA20 Live")

    def update(_):
        draw(ax, load_live(a.live_file), a.window, a.row, a.utc_offset)

    ani = FuncAnimation(fig, update, interval=int(a.interval * 1000), cache_frame_data=False)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
