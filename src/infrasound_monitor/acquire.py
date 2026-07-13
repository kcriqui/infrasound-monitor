"""Live acquisition from the INFRA20 serial port -> miniSEED SDS archive.

Serial framing (confirmed against hardware, 2026-07-11)
------------------------------------------------------
The INFRA20 streams **ASCII, one signed integer count per CRLF-terminated line**
at 9600 baud 8N1, e.g. ``b"-00123\\r\\n"`` (sign + 5 zero-padded digits, so the
range spans the int16 counts the sensor produces).  The delivered rate is
~51.4 samples/s -- matching :data:`NOMINAL_FS` -- i.e. one sample per line.  On
port open the adapter emits a short transient: one partial/garbage line followed
by a fraction of a second of zeros while the 0.05 Hz high-pass filter settles;
``parse_line`` drops the garbage and a small warm-up discard skips the rest.

What this daemon does
---------------------
* Timestamps each sample from the **system clock as it arrives** (more accurate
  than AmaSeis's hour binning).
* Places samples on a fixed-rate clock anchored to the first sample of each
  contiguous *run*, so a run is gap-free and single-rate -- exactly what a real
  datalogger produces and what merges cleanly in ObsPy/Swarm.  A genuine serial
  dropout (gap > ``gap_tol``) ends the run and starts a new one, leaving an
  explicit gap.
* Appends completed segments to the SeisComP Data Structure (SDS) day file for
  their UTC day, splitting automatically at UTC midnight, and (re)writes
  ``station.xml`` alongside the archive.

Only one program can hold the COM port, so this REPLACES AmaSeis on that machine.
A future step can additionally serve the archive over SeedLink for live views.
"""
from __future__ import annotations
import argparse
import datetime as dt
import time
from pathlib import Path

import numpy as np

from .config import StationConfig, DEFAULT_STATION, NOMINAL_FS
from .convert import sds_path, _new_trace
from .metadata import build_inventory


def parse_line(line: bytes):
    """One signed integer count per line; returns ``int`` or ``None``.

    Tolerates the connect-time garbage line (non-numeric bytes) and any stray
    ``chan,count`` style framing by scanning for the first parseable integer.
    """
    s = line.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        for tok in s.replace(b",", b" ").split():
            try:
                return int(tok)
            except ValueError:
                continue
    return None


class SdsWriter:
    """Accumulate samples on a fixed-rate clock and append them to the SDS archive.

    Samples are grouped into *runs* of contiguous data.  Within a run, sample i
    is timestamped ``run_anchor + i / fs`` so successive flushed segments abut
    exactly (gap-free, no overlap) and merge into a single trace on read.  A run
    ends -- and a new one is anchored to wall-clock -- when a real dropout is
    detected (the next sample arrives more than ``gap_tol`` seconds late) so that
    genuine outages appear as explicit gaps rather than silently compressed time.
    """

    def __init__(self, archive, cfg: StationConfig = DEFAULT_STATION,
                 fs: float = NOMINAL_FS, flush_seconds: float = 300.0,
                 gap_tol: float = 2.0, encoding: str = "STEIM2"):
        self.archive = Path(archive)
        self.cfg = cfg
        self.fs = fs
        self.flush_seconds = flush_seconds
        self.gap_tol = gap_tol
        self.encoding = encoding

        self.run_anchor: dt.datetime | None = None  # UTC time of sample 0 of the run
        self.n_run = 0            # samples added in the current run (incl. buffered)
        self.n_flushed = 0        # samples of the current run already written
        self.buf: list[int] = []

        self.archive.mkdir(parents=True, exist_ok=True)
        build_inventory(cfg).write(str(self.archive / "station.xml"),
                                   format="STATIONXML")

    # -- time helpers -------------------------------------------------------
    def _sample_time(self, index: int) -> dt.datetime:
        """Wall-clock UTC of sample ``index`` within the current run."""
        return self.run_anchor + dt.timedelta(seconds=index / self.fs)

    def _start_new_run(self, when: dt.datetime):
        self.run_anchor = when
        self.n_run = 0
        self.n_flushed = 0
        self.buf.clear()

    # -- ingest -------------------------------------------------------------
    def add(self, count: int, when: dt.datetime):
        """Add one sample that arrived at wall-clock ``when`` (tz-aware UTC)."""
        if self.run_anchor is None:
            self._start_new_run(when)
        else:
            # Dropout detection: if this sample arrives well after where the
            # fixed-rate clock expected it, we lost samples -> close the run.
            expected = self._sample_time(self.n_run)
            if (when - expected).total_seconds() > self.gap_tol:
                self.flush()
                self._start_new_run(when)

        # Split at UTC midnight so a segment never straddles two day files.
        if self.buf:
            buf_start_day = self._sample_time(self.n_flushed).date()
            if self._sample_time(self.n_run).date() != buf_start_day:
                self.flush()

        self.buf.append(count)
        self.n_run += 1

        if (self.n_run - self.n_flushed) / self.fs >= self.flush_seconds:
            self.flush()

    # -- output -------------------------------------------------------------
    def flush(self):
        """Append the buffered segment to its SDS day file (append, never clobber)."""
        if not self.buf or self.run_anchor is None:
            return
        start = self._sample_time(self.n_flushed)
        tr = _new_trace(np.array(self.buf, dtype=np.int32), start, self.cfg, self.fs)
        out = sds_path(self.archive, self.cfg, start.year,
                       int(start.strftime("%j")))
        out.parent.mkdir(parents=True, exist_ok=True)

        from obspy import Stream
        # miniSEED files are concatenated records, so appending is valid and keeps
        # existing hours intact (Stream.write always overwrites a *path*, so we
        # hand it an append-mode handle instead).
        with open(out, "ab") as fh:
            Stream([tr]).write(fh, format="MSEED", encoding=self.encoding, reclen=512)

        self.n_flushed = self.n_run
        self.buf.clear()
        print(f"flushed {tr.stats.npts} samples "
              f"@ {start.isoformat()} -> {out.name}", flush=True)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_live(path, buf, fs, t_end):
    """Best-effort atomic dump of the rolling live buffer for a live viewer.

    Written to a *local* path (not the Drive-synced archive) and wrapped so a
    failure here can never disturb acquisition or the archive.
    """
    try:
        import io, os
        b = io.BytesIO()
        np.savez(b, y=np.fromiter(buf, dtype=np.int32), fs=np.float64(fs),
                 t_end=t_end.isoformat())
        tmp = str(path) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(b.getvalue())
        os.replace(tmp, path)                       # atomic swap -> viewer never sees a partial file
    except Exception:
        pass


def run(port: str, archive, cfg: StationConfig = DEFAULT_STATION, baud: int = 9600,
        fs: float = NOMINAL_FS, flush_seconds: float = 300.0, warmup: float = 2.0,
        gap_tol: float = 2.0, reconnect_delay: float = 5.0,
        live_file=None, live_seconds: float = 600.0):
    """Acquire from ``port`` into the SDS ``archive`` until interrupted.

    If ``live_file`` is set, also mirror the most recent ``live_seconds`` of raw
    samples into that (local) file every ~2 s so a live viewer can tail it.
    """
    import serial
    from collections import deque

    writer = SdsWriter(archive, cfg, fs=fs, flush_seconds=flush_seconds, gap_tol=gap_tol)
    live = deque(maxlen=int(live_seconds * fs)) if live_file else None
    last_live = 0.0
    print(f"acquiring {port}@{baud} -> {archive}  ({cfg.seed_id}, {fs:.4f} sps)  "
          f"Ctrl-C to stop", flush=True)
    try:
        while True:
            try:
                with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                   timeout=1) as ser:
                    _discard_warmup(ser, warmup)
                    while True:
                        line = ser.readline()
                        c = parse_line(line)
                        if c is None:
                            continue
                        now = _now_utc()
                        writer.add(c, now)
                        if live is not None:
                            live.append(c)
                            if time.time() - last_live >= 2.0:
                                _write_live(live_file, live, fs, now)
                                last_live = time.time()
            except serial.SerialException as e:
                # USB glitch / cable pull: flush what we have and retry.
                writer.flush()
                print(f"serial error: {e}; reconnecting in {reconnect_delay:.0f}s ...",
                      flush=True)
                time.sleep(reconnect_delay)
    except KeyboardInterrupt:
        pass
    finally:
        writer.flush()
        print("stopped, buffer flushed", flush=True)


def _discard_warmup(ser, seconds: float):
    """Read and drop the connect transient (garbage line + filter settling)."""
    if seconds <= 0:
        return
    end = time.time() + seconds
    while time.time() < end:
        ser.readline()


def sniff(port: str, baud: int = 9600, seconds: float = 5.0) -> None:
    """Print raw lines from the port so the framing can be eyeballed."""
    import serial
    with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=1) as ser:
        end = time.time() + seconds
        while time.time() < end:
            print(repr(ser.readline()))


def main(argv=None):
    p = argparse.ArgumentParser(description="Live INFRA20 serial acquisition -> miniSEED SDS.")
    p.add_argument("port", help="serial port, e.g. COM4 or /dev/ttyUSB0")
    p.add_argument("archive", nargs="?", help="SDS archive root (required unless --sniff)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--fs", type=float, default=NOMINAL_FS,
                   help="sample rate written to the miniSEED headers (sps)")
    p.add_argument("--flush-seconds", type=float, default=300.0,
                   help="append a segment to the day file at least this often")
    p.add_argument("--warmup", type=float, default=2.0,
                   help="seconds of post-open data to discard (connect transient)")
    p.add_argument("--gap-tol", type=float, default=2.0,
                   help="a sample this many seconds late ends the run (explicit gap)")
    p.add_argument("--live-file", default=None,
                   help="also mirror recent raw samples to this LOCAL file for a live viewer")
    p.add_argument("--live-seconds", type=float, default=600.0,
                   help="length of the rolling live buffer (s)")
    p.add_argument("--sniff", action="store_true", help="just print raw lines and exit")
    a = p.parse_args(argv)
    if a.sniff:
        sniff(a.port, a.baud)
        return
    if not a.archive:
        p.error("archive is required unless --sniff is given")
    run(a.port, a.archive, baud=a.baud, fs=a.fs, flush_seconds=a.flush_seconds,
        live_file=a.live_file, live_seconds=a.live_seconds,
        warmup=a.warmup, gap_tol=a.gap_tol)


if __name__ == "__main__":
    main()
