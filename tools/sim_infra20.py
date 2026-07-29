#!/usr/bin/env python3
"""Synthetic INFRA20 serial source -- test acquisition end-to-end with no hardware.

The real sensor can only be on one machine at a time (the serial port is exclusive),
so you can't read the live stream on the Pi while it's still plugged into the PC. This
feeds a *simulated* INFRA20 stream instead, so you can validate the whole acquisition
path on the Pi (miniSEED writing, live.npz, the status display) before the physical swap.

On Linux/macOS it creates a virtual serial port (a pty) and prints its device path;
point the daemon at that path:

    # terminal 1 -- start the fake sensor, note the /dev/pts/N it prints
    python tools/sim_infra20.py

    # terminal 2 -- acquire from it exactly like the real thing
    .venv/bin/python -m infrasound_monitor.acquire /dev/pts/N archive --live-file live.npz

    # terminal 3 (optional) -- watch the display pick it up
    .venv/bin/python tools/tft_status.py --snapshot live.png

The synthetic signal is a low tone + noise, so the display shows a plausible level and
dominant frequency. Use --stdout to just print lines (works on Windows too, for a look).
"""
from __future__ import annotations
import argparse
import math
import os
import random
import sys
import time

try:
    from infrasound_monitor.config import NOMINAL_FS
except Exception:
    NOMINAL_FS = 51.4287


def _line(val: int) -> bytes:
    return f"{val:+06d}\r\n".encode()


def _samples(fs, tone_hz, amp, noise):
    """Yield integer counts forever: a sine tone + Gaussian noise (drift-corrected pace)."""
    dt = 1.0 / fs
    n = 0
    t0 = time.perf_counter()
    while True:
        t = n * dt
        val = amp * math.sin(2 * math.pi * tone_hz * t) + random.gauss(0.0, noise)
        yield int(round(val))
        n += 1
        target = t0 + n * dt
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


def main():
    p = argparse.ArgumentParser(description="Synthetic INFRA20 serial source for testing.")
    p.add_argument("--rate", type=float, default=NOMINAL_FS, help="samples/sec (default from config)")
    p.add_argument("--tone-hz", type=float, default=3.0, help="tone frequency in the signal (Hz)")
    p.add_argument("--amp", type=float, default=200.0, help="tone amplitude (counts)")
    p.add_argument("--noise", type=float, default=25.0, help="Gaussian noise stddev (counts)")
    p.add_argument("--stdout", action="store_true",
                   help="print lines to stdout instead of a pty (cross-platform; for eyeballing)")
    p.add_argument("--count", type=int, default=0, help="stop after N lines (0 = forever)")
    a = p.parse_args()

    gen = _samples(a.rate, a.tone_hz, a.amp, a.noise)
    made = 0

    if a.stdout:
        try:
            for val in gen:
                sys.stdout.write(_line(val).decode())
                sys.stdout.flush()
                made += 1
                if a.count and made >= a.count:
                    break
        except KeyboardInterrupt:
            pass
        return

    if not hasattr(os, "openpty"):
        sys.exit("pty mode needs Linux/macOS; use --stdout on Windows.")

    import tty
    master_fd, slave_fd = os.openpty()
    tty.setraw(slave_fd)                        # no line processing -> bytes pass through verbatim
    slave_name = os.ttyname(slave_fd)
    print(f"feeding synthetic INFRA20 @ {a.rate:.4f} sps on {slave_name}", flush=True)
    print(f"point the daemon at it:  python -m infrasound_monitor.acquire {slave_name} archive "
          f"--live-file live.npz", flush=True)
    print("Ctrl-C to stop.", flush=True)
    try:
        for val in gen:
            try:
                os.write(master_fd, _line(val))
            except OSError:
                break                            # reader went away
            made += 1
            if a.count and made >= a.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master_fd)
        os.close(slave_fd)


if __name__ == "__main__":
    main()
