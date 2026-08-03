#!/usr/bin/env python3
"""Mini PiTFT 1.14in status display for the INFRA20 monitor.

A small always-on health panel for an Adafruit Mini PiTFT (240x135, ST7789) on a
Raspberry Pi running the acquisition + analysis stack. Deliberately lightweight:
it reads the daemon's rolling ``live.npz`` (last-sample age, live RMS level, and the
dominant tone from a short FFT) plus ``/proc``, ``shutil.disk_usage`` and file mtimes.
It does NOT import obspy, so it barely competes with acquisition on a 1 GB Pi 3B.

Pages (top button = next, bottom button = previous):
  STATUS  - alive?/stale, seconds since last sample, RMS level bar, dominant tone, uptime
  SYSTEM  - today's data (MB), archive total, CPU temp, last publish, disk-free bar
  WAVE    - live waveform of the data being collected (last ~12 s, auto-scaled)

Buttons on the Mini PiTFT are GPIO23 (top) and GPIO24 (bottom), active-low.

Run headless as a service (deploy/infra-display.service). To preview the layout on
any machine (no Pi hardware needed):

    python tools/tft_status.py --snapshot preview.png          # STATUS page
    python tools/tft_status.py --snapshot sys.png  --page 1    # SYSTEM page
    python tools/tft_status.py --snapshot wave.png --page 2    # WAVE page
"""
from __future__ import annotations
import argparse
import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Project config -- a stdlib-only import (no obspy), safe to load on the display box.
try:
    from infrasound_monitor.config import (
        PA_PER_COUNT, UTC_OFFSET_HOURS, DEFAULT_STATION,
        ARCHIVE_DIR, LIVE_FILE, PROJECT_ROOT, SERIAL_PORT,
    )
except ModuleNotFoundError:                      # running from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from infrasound_monitor.config import (
        PA_PER_COUNT, UTC_OFFSET_HOURS, DEFAULT_STATION,
        ARCHIVE_DIR, LIVE_FILE, PROJECT_ROOT, SERIAL_PORT,
    )

WIDTH, HEIGHT = 240, 135      # landscape drawing canvas (panel is rotated 90)

# Dark palette
BG     = (12, 14, 18)
FG     = (230, 233, 238)
DIM    = (120, 128, 140)
RULE   = (40, 44, 52)
OK     = (60, 200, 110)
WARN   = (240, 180, 60)
BAD    = (235, 80, 80)
ACCENT = (90, 170, 245)
BARBG  = (36, 40, 48)

STALE_S = 15.0                # no fresh live.npz for this long -> STALE
LEVEL_FULLSCALE_PA = 0.5      # RMS level bar full scale (typical indoor ambient)
WAVE_SECONDS = 12.0           # window shown on the live WAVE page


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for p in (f"/usr/share/fonts/truetype/dejavu/{name}", name):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


F_SM = _font(13)
F_MD = _font(17, bold=True)
F_LG = _font(30, bold=True)


# --------------------------------------------------------------------------- collectors

def read_live():
    """Health from the daemon's rolling live buffer -- cheap, no obspy."""
    out = {"present": False, "age_s": None, "last_local": None,
           "rms_pa": None, "dom_hz": None, "wave": None, "wave_s": None}
    p = Path(LIVE_FILE)
    try:
        out["age_s"] = time.time() - p.stat().st_mtime      # freshness = file mtime
    except OSError:
        return out
    try:
        with np.load(p, allow_pickle=False) as d:
            y = d["y"].astype(np.float64)
            fs = float(d["fs"])
            t_end = str(d["t_end"])
    except Exception:
        return out
    out["present"] = True
    try:
        ts = dt.datetime.fromisoformat(t_end)
        if ts.tzinfo is not None:
            ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
        out["last_local"] = ts + dt.timedelta(hours=UTC_OFFSET_HOURS)
    except ValueError:
        pass
    if y.size:
        y = y - y.mean()
        pa = y * PA_PER_COUNT
        out["rms_pa"] = float(np.sqrt(np.mean(pa ** 2)))
        wn = int(min(y.size, WAVE_SECONDS * fs))
        if wn >= 2:
            w = pa[-wn:]
            out["wave"] = w - w.mean()
            out["wave_s"] = wn / fs
        if y.size >= 128:
            win = np.hanning(y.size)
            mag = np.abs(np.fft.rfft(y * win))
            freq = np.fft.rfftfreq(y.size, 1.0 / fs)
            band = (freq >= 0.5) & (freq <= 20.0)
            if band.any():
                out["dom_hz"] = float(freq[band][int(np.argmax(mag[band]))])
    return out


def read_diagnostics(unit="infra-acquire"):
    """Why is it stale?  Cheap answers to the first questions you'd SSH in to ask.

    ``systemctl show`` needs no privileges (unlike ``journalctl -u`` for a system
    unit), so this works as the unqualified ``infra`` user the service runs as.
    """
    import subprocess
    out = {"unit": unit, "serial_port": str(SERIAL_PORT), "port_present": None,
           "active": None, "sub": None, "restarts": None, "since": None,
           "stale_after_s": STALE_S}
    try:
        out["port_present"] = Path(SERIAL_PORT).exists()
    except OSError:
        pass
    props = ("ActiveState", "SubState", "NRestarts", "ExecMainStartTimestamp")
    try:
        r = subprocess.run(["systemctl", "show", unit, "--property=" + ",".join(props)],
                           capture_output=True, text=True, timeout=5)
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        out["active"] = kv.get("ActiveState") or None
        out["sub"] = kv.get("SubState") or None
        out["restarts"] = int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None
        out["since"] = kv.get("ExecMainStartTimestamp") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass                                    # not systemd / not installed: leave None
    return out


def diagnose(live, diag):
    """One line naming the most likely cause, for the status page and /healthz.

    The distinction that matters: a *running* service with a *present* port and no
    data is a dead sensor or cable, not a software fault -- restarting won't help.
    """
    if live.get("present") and live.get("age_s") is not None and live["age_s"] < STALE_S:
        return "acquiring normally"
    if diag.get("active") not in (None, "active"):
        return f"acquisition service is {diag['active']}/{diag.get('sub')} -- check journalctl"
    if diag.get("port_present") is False:
        return (f"{diag['serial_port']} is gone -- USB adapter unplugged or dropped off "
                f"the bus; check dmesg, then the cable")
    if not live.get("present"):
        return "no live buffer yet -- daemon has not written a sample since it started"
    return (f"{diag['serial_port']} is open and the service is running, but no samples "
            f"are arriving -- suspect the sensor, its power, or the cable, not the Pi")


def read_system():
    out = {"uptime_s": None, "cpu_c": None, "disk_free_pct": None}
    try:
        with open("/proc/uptime") as f:
            out["uptime_s"] = float(f.read().split()[0])
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            out["cpu_c"] = int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    try:
        u = shutil.disk_usage(str(ARCHIVE_DIR))
        out["disk_free_pct"] = 100.0 * u.free / u.total
    except OSError:
        pass
    return out


def _sds_day_file(when=None):
    when = when or dt.datetime.now(dt.timezone.utc)
    c, yr, jd = DEFAULT_STATION, when.year, when.timetuple().tm_yday
    return (Path(ARCHIVE_DIR) / str(yr) / c.network / c.station / f"{c.channel}.D"
            / f"{c.network}.{c.station}.{c.location}.{c.channel}.D.{yr}.{jd:03d}")


def read_archive():
    out = {"today_mb": None, "total_gb": None}
    try:
        f = _sds_day_file()
        if f.exists():
            out["today_mb"] = f.stat().st_size / 1e6
    except OSError:
        pass
    try:
        total = 0
        for root, _dirs, files in os.walk(str(ARCHIVE_DIR)):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        out["total_gb"] = total / 1e9
    except OSError:
        pass
    return out


def read_publish(site_index):
    try:
        return time.time() - Path(site_index).stat().st_mtime
    except OSError:
        return None


# --------------------------------------------------------------------------- formatting

def fmt_age(s):
    if s is None:
        return "--"
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_dur(s):
    if s is None:
        return "--"
    s = int(s)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# --------------------------------------------------------------------------- rendering

def _bar(draw, x, y, w, h, frac, color):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=3, fill=BARBG)
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        draw.rounded_rectangle([x, y, x + int(w * frac), y + h], radius=3, fill=color)


def _header(draw, title):
    draw.text((6, 4), f"{DEFAULT_STATION.network}.{DEFAULT_STATION.station}",
              font=F_SM, fill=ACCENT)
    draw.text((WIDTH // 2, 4), title, font=F_SM, fill=DIM, anchor="ma")
    draw.text((WIDTH - 6, 4), dt.datetime.now().strftime("%H:%M"),
              font=F_SM, fill=DIM, anchor="ra")
    draw.line([6, 21, WIDTH - 6, 21], fill=RULE)


def page_live(draw, m):
    _header(draw, "STATUS")
    live = m["live"]
    ok = live["present"] and live["age_s"] is not None and live["age_s"] < STALE_S
    color = OK if ok else BAD
    label = "OK" if ok else ("STALE" if live["present"] else "NO DATA")
    draw.ellipse([8, 33, 26, 51], fill=color)
    draw.text((34, 28), label, font=F_LG, fill=color)
    draw.text((WIDTH - 6, 40), f"{fmt_age(live['age_s'])} ago" if live["present"] else "--",
              font=F_SM, fill=DIM, anchor="ra")

    rms = live.get("rms_pa")
    draw.text((6, 64), "level", font=F_SM, fill=DIM)
    draw.text((WIDTH - 6, 61), f"{rms:.3f} Pa" if rms is not None else "-- Pa",
              font=F_MD, fill=FG, anchor="ra")
    _bar(draw, 6, 80, WIDTH - 12, 8, (rms or 0.0) / LEVEL_FULLSCALE_PA, ACCENT)

    dom = live.get("dom_hz")
    draw.text((6, 95), "tone", font=F_SM, fill=DIM)
    draw.text((WIDTH - 6, 92), f"{dom:.1f} Hz" if dom else "-- Hz",
              font=F_MD, fill=FG, anchor="ra")
    draw.text((6, 121), f"up {fmt_dur(m['sys'].get('uptime_s'))}", font=F_SM, fill=DIM)


def page_system(draw, m):
    _header(draw, "SYSTEM")
    a, s = m["arch"], m["sys"]
    rows = [
        ("today", f"{a['today_mb']:.1f} MB" if a.get("today_mb") is not None else "--"),
        ("archive", f"{a['total_gb']:.2f} GB" if a.get("total_gb") is not None else "--"),
        ("cpu", f"{s['cpu_c']:.0f} C" if s.get("cpu_c") is not None else "--"),
        ("publish", f"{fmt_age(m['pub'])} ago" if m.get("pub") is not None else "--"),
    ]
    y = 28
    for k, v in rows:
        draw.text((6, y), k, font=F_SM, fill=DIM)
        draw.text((WIDTH - 6, y - 2), v, font=F_MD, fill=FG, anchor="ra")
        y += 22
    df = s.get("disk_free_pct")
    draw.text((6, 118), "disk", font=F_SM, fill=DIM)
    if df is not None:
        col = OK if df > 20 else (WARN if df > 8 else BAD)
        _bar(draw, 44, 120, WIDTH - 90, 8, df / 100.0, col)
        draw.text((WIDTH - 6, 118), f"{df:.0f}%", font=F_SM, fill=col, anchor="ra")


def page_wave(draw, m):
    _header(draw, "WAVE")
    live = m["live"]
    x0, y0, x1, y1 = 6, 26, WIDTH - 6, 114
    draw.rectangle([x0, y0, x1, y1], outline=RULE)
    ymid = (y0 + y1) // 2
    wave = live.get("wave")
    if wave is None or len(wave) < 2:
        draw.text(((x0 + x1) // 2, ymid), "waiting for data", font=F_SM, fill=DIM, anchor="mm")
        return
    draw.line([x0, ymid, x1, ymid], fill=(30, 34, 42))          # zero baseline
    n = len(wave)
    amp = float(np.max(np.abs(wave))) or 1e-6                    # symmetric auto-scale
    half = (y1 - y0) / 2 - 2
    w = x1 - x0
    pts = [(x0 + int(i * w / (n - 1)), ymid - int(float(wave[i]) / amp * half))
           for i in range(n)]
    draw.line(pts, fill=ACCENT)
    draw.text((x0 + 2, y1 + 3), f"{live.get('wave_s', 0):.0f}s", font=F_SM, fill=DIM)
    draw.text((x1 - 2, y1 + 3), f"±{amp:.2f} Pa", font=F_SM, fill=DIM, anchor="ra")


PAGES = [page_live, page_system, page_wave]


def gather(cache, site_index, force_slow=False):
    m = {"live": read_live()}
    if force_slow or time.time() - cache["t"] > 20:
        cache.update(t=time.time(), sys=read_system(),
                     arch=read_archive(), pub=read_publish(site_index))
    m["sys"], m["arch"], m["pub"] = cache["sys"], cache["arch"], cache["pub"]
    return m


def build_image(page, m):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    PAGES[page % len(PAGES)](ImageDraw.Draw(img), m)
    return img


def _init_display(baudrate=64_000_000):
    import board
    import digitalio
    from adafruit_rgb_display import st7789
    cs = digitalio.DigitalInOut(board.CE0)
    dc = digitalio.DigitalInOut(board.D25)
    disp = st7789.ST7789(board.SPI(), cs=cs, dc=dc, rst=None, baudrate=baudrate,
                         width=135, height=240, x_offset=53, y_offset=40)
    backlight = digitalio.DigitalInOut(board.D22)
    backlight.switch_to_output()
    backlight.value = True
    btn_a = digitalio.DigitalInOut(board.D23)
    btn_a.switch_to_input()
    btn_b = digitalio.DigitalInOut(board.D24)
    btn_b.switch_to_input()
    return disp, backlight, btn_a, btn_b


def main():
    p = argparse.ArgumentParser(description="Mini PiTFT status display for the INFRA20 monitor.")
    p.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    p.add_argument("--rotation", type=int, default=90, choices=[90, 270],
                   help="panel rotation; 270 if mounted upside down (default 90)")
    p.add_argument("--page", type=int, default=0, help="starting page (0=STATUS, 1=SYSTEM, 2=WAVE)")
    p.add_argument("--site-index", default=str(Path(PROJECT_ROOT) / "site" / "index.html"),
                   help="published index.html whose mtime is the 'last publish' time")
    p.add_argument("--snapshot", metavar="PNG",
                   help="render one frame to a PNG and exit (no Pi hardware needed)")
    a = p.parse_args()

    cache = {"t": 0.0, "sys": {}, "arch": {}, "pub": None}

    if a.snapshot:
        img = build_image(a.page, gather(cache, a.site_index, force_slow=True))
        img.save(a.snapshot)
        print(f"wrote {a.snapshot}")
        return

    try:
        disp, backlight, btn_a, btn_b = _init_display()
    except (ImportError, NotImplementedError) as e:
        sys.exit(f"display init failed ({e}).\n"
                 f"This needs a Raspberry Pi with the Mini PiTFT and the 'display' extras "
                 f"(pip install -e '.[display]') and SPI enabled. "
                 f"Use --snapshot to preview the layout on any machine.")

    # Poll the buttons at ~20 Hz so presses feel responsive, but only redraw (and re-read
    # live.npz) every --interval or immediately when the page changes.
    page, a_prev, b_prev, last_draw = a.page, True, True, 0.0
    try:
        while True:
            now = time.monotonic()
            redraw = now - last_draw >= a.interval
            av, bv = btn_a.value, btn_b.value          # active-low: pressed == False
            if a_prev and not av:                      # top button -> next page
                page = (page + 1) % len(PAGES); redraw = True
            if b_prev and not bv:                      # bottom button -> previous page
                page = (page - 1) % len(PAGES); redraw = True
            a_prev, b_prev = av, bv
            if redraw:
                disp.image(build_image(page, gather(cache, a.site_index)), a.rotation)
                last_draw = now
            time.sleep(0.05)
    except KeyboardInterrupt:
        backlight.value = False


if __name__ == "__main__":
    main()
