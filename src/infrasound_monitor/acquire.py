"""Live acquisition from the INFRA20 serial port -> miniSEED SDS archive.

STATUS: design skeleton -- NOT yet validated against real hardware.
================================================================
The INFRA20 documents its output as "9600 bps, 8N1, 16-bit ASCII" but the exact
line framing is not published here.  The parser below assumes the common case of
**one signed integer count per newline-terminated line**; this MUST be confirmed
by sniffing the actual port (see ``sniff()``) before trusting acquired data.

Once framing is confirmed, this daemon:
  * timestamps samples from the system clock as they arrive (more accurate than
    AmaSeis's hour binning),
  * buffers one UTC hour, then appends a gap-free trace to the SDS day file,
  * writes/refreshes ``station.xml`` alongside the archive.

Only one program can hold the COM port, so this REPLACES AmaSeis on that machine.
A future step can additionally serve the archive over SeedLink for live views.
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import numpy as np

from .config import StationConfig, DEFAULT_STATION, NOMINAL_FS
from .convert import sds_path, _new_trace
from .metadata import build_inventory


def sniff(port: str, baud: int = 9600, seconds: float = 5.0) -> None:
    """Print raw bytes/lines from the port so the framing can be reverse-engineered."""
    import serial
    with serial.Serial(port, baud, timeout=1) as ser:
        import time
        end = time.time() + seconds
        while time.time() < end:
            line = ser.readline()
            print(repr(line))


def parse_line(line: bytes):
    """Best-guess parser: one signed integer count per line. VERIFY against device."""
    s = line.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        # tolerate lines like "chan,count" or hex; refine once framing is known
        for tok in s.replace(b",", b" ").split():
            try:
                return int(tok)
            except ValueError:
                continue
    return None


class HourWriter:
    """Accumulate samples for the current UTC hour and flush to the SDS archive."""

    def __init__(self, archive, cfg: StationConfig = DEFAULT_STATION, fs: float = NOMINAL_FS):
        self.archive = Path(archive)
        self.cfg = cfg
        self.fs = fs
        self.hour_start = None
        self.buf: list[int] = []
        build_inventory(cfg).write(str(self.archive / "station.xml"), format="STATIONXML")

    def add(self, count: int, when: dt.datetime):
        h = when.replace(minute=0, second=0, microsecond=0)
        if self.hour_start is None:
            self.hour_start = h
        elif h != self.hour_start:
            self.flush()
            self.hour_start = h
        self.buf.append(count)

    def flush(self):
        if not self.buf or self.hour_start is None:
            return
        tr = _new_trace(np.array(self.buf, dtype=np.int32), self.hour_start, self.cfg, self.fs)
        out = sds_path(self.archive, self.cfg, self.hour_start.year,
                       int(self.hour_start.strftime("%j")))
        out.parent.mkdir(parents=True, exist_ok=True)
        # append if the day file already exists
        mode = "a" if out.exists() else "w"
        from obspy import Stream
        Stream([tr]).write(str(out), format="MSEED", encoding="STEIM2", reclen=512,
                           **({} if mode == "w" else {"flush": True}))
        print(f"flushed {tr.stats.npts} samples -> {out.name}")
        self.buf.clear()


def run(port: str, archive, cfg: StationConfig = DEFAULT_STATION, baud: int = 9600):
    import serial
    writer = HourWriter(archive, cfg)
    print(f"acquiring {port}@{baud} -> {archive}  ({cfg.seed_id})  Ctrl-C to stop")
    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            while True:
                line = ser.readline()
                c = parse_line(line)
                if c is None:
                    continue
                writer.add(c, dt.datetime.now(dt.timezone.utc))
    except KeyboardInterrupt:
        pass
    finally:
        writer.flush()
        print("stopped, buffer flushed")


def main(argv=None):
    p = argparse.ArgumentParser(description="Live INFRA20 serial acquisition (design skeleton).")
    p.add_argument("port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    p.add_argument("archive", help="SDS archive root")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--sniff", action="store_true", help="just print raw lines and exit")
    a = p.parse_args(argv)
    if a.sniff:
        sniff(a.port, a.baud)
        return
    run(a.port, a.archive)


if __name__ == "__main__":
    main()
