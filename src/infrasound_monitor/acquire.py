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
* A reader thread does nothing but read the port and stamp each sample with the
  **system clock as it arrives**.  Writing happens on the main thread, so a slow
  disk delays the write, not the timestamps.
* The sensor's sample clock free-runs: about 51.42 sps on average, wandering by
  ~3e-4 within hours.  :class:`SdsWriter` estimates when each sample was taken,
  writes one fixed rate throughout, and corrects the archive's clock in a small
  step whenever it is ``max_drift`` off UTC.  The samples stay raw.
* A genuine serial dropout -- samples lost, not merely delivered late -- ends the
  run and starts a new one, leaving an explicit gap.
* Appends completed segments to the SeisComP Data Structure (SDS) day file for
  their UTC day, splitting automatically at UTC midnight, and (re)writes
  ``station.xml`` alongside the archive.

Only one program can hold the serial port.  A future step can additionally serve
the archive over SeedLink for live views.
"""
from __future__ import annotations
import argparse
import datetime as dt
import math
import time
from pathlib import Path

import numpy as np

from .config import (StationConfig, DEFAULT_STATION, NOMINAL_FS,
                     SERIAL_PORT, ARCHIVE_DIR, LIVE_FILE)
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


LATENCY_BLOCK = 256      # samples (~5 s) per earliest-arrival block of the clock estimate
CONFIRM_S = 2.5          # a late stream that flows normally this long, still late, lost data
MAX_PENDING = 6000       # samples held while deciding (~2 min; the tty buffer holds ~10 s)
MIN_MEASURE_S = 60.0     # segments shorter than this don't update the measured rate
RATE_LIMIT = 5e-3        # a "measured" rate further than this from nominal is nonsense


def _utc_day(t: float) -> int:
    return int(t // 86400)


class SdsWriter:
    """Timestamp samples from their arrival times and append them to the SDS archive.

    Samples are grouped into *runs* of contiguous data, and a run into segments of
    ``flush_seconds``, each appended to its UTC day file as it completes.

    Timing.  The INFRA20's sample clock free-runs.  Measured against the Pi's
    NTP clock (within ~4 ms of UTC), it averages ~51.423 sps and wanders by ~3e-4
    within hours, so a fixed-rate clock model drifts by seconds a day.  There is
    no way to keep both a single sample rate and exact time without resampling
    the data, so this writer keeps the rate and bounds the time error:

    * Every segment is written at the one configured rate, so all traces share
      it and ObsPy's ``Stream.merge`` works on any stretch of the archive.
    * The time a sample was taken is estimated from arrival times.  Arrivals are
      only ever delayed (USB latency, scheduling), never early, so the earliest
      arrival relative to the clock model over a ~5 s block marks the model's
      true offset, to a few ms.
    * Segments abut exactly until the archive's clock is ``max_drift`` off that
      estimate.  Then the next segment starts at the estimated true time: a
      logged timing correction, a step of under ``max_drift`` (about 8 a day at
      1 s).  ``waveforms.pieces`` reads across these steps; ``merge`` fills or
      trims the few samples involved.
    * The flush log reports the sensor's measured rate.  Set ``sample_rate`` in
      config.toml to its long-run mean to keep corrections rare.

    The previous scheme, which started a new run whenever the drift reached
    ``gap_tol``, left a false 2 s gap every few hours.  An interim scheme gave each
    segment its own measured rate; the wander made ObsPy split traces at mixed
    rates, which ``merge`` refuses.

    A real dropout (samples lost, not just late) ends the run.  Samples arriving
    more than ``gap_tol`` late are held: if the stream catches up (a burst of
    buffered samples after a stall), nothing was lost and they join the segment;
    if it flows at its normal pace for ``CONFIRM_S`` and is still late, samples
    were lost, the run ends and the archive shows an explicit gap.
    """

    def __init__(self, archive, cfg: StationConfig = DEFAULT_STATION,
                 fs: float = NOMINAL_FS, flush_seconds: float = 300.0,
                 gap_tol: float = 2.0, max_drift: float = 1.0,
                 encoding: str = "STEIM2", verbose: bool = True):
        self.archive = Path(archive)
        self.cfg = cfg
        self.fs_nominal = float(fs)
        self.fs = float(fs)            # the one rate every segment is written at
        self.fs_true = float(fs)       # latest measured sensor rate (for the log)
        self.flush_seconds = flush_seconds
        self.gap_tol = gap_tol
        self.max_drift = max_drift
        self.encoding = encoding
        self.verbose = verbose

        self.seg_start: float | None = None  # epoch s of the segment's first sample; None = no run
        self.seg_exact = False         # True once seg_start continues the previous segment
        self.true_start = 0.0          # estimated true time of seg_start
        self.buf: list[int] = []
        self._pending: list[tuple[int, float]] = []
        self._first_min = math.inf     # earliest arrival offset over a run's first block
        self._first_n = 0
        self._reset_minima()

        self.archive.mkdir(parents=True, exist_ok=True)
        build_inventory(cfg).write(str(self.archive / "station.xml"),
                                   format="STATIONXML")

    # -- clock model --------------------------------------------------------
    def _model(self, k: int) -> float:
        """Archive time of sample ``k`` of the current segment."""
        return self.seg_start + k / self.fs

    def _reset_minima(self):
        self._blk_min, self._blk_n, self._prev_min = math.inf, 0, math.inf

    def _offset(self) -> float:
        """Earliest recent arrival minus the model: how far the model runs behind."""
        m = min(self._blk_min, self._prev_min)
        return 0.0 if m == math.inf else m

    def _start_run(self, t: float):
        self.seg_start, self.seg_exact = t, False
        self._first_min, self._first_n = math.inf, 0
        self._reset_minima()

    # -- ingest -------------------------------------------------------------
    def add(self, count: int, when):
        """Add one sample that arrived at ``when`` (tz-aware datetime or epoch s)."""
        t = when.timestamp() if isinstance(when, dt.datetime) else float(when)
        if self.seg_start is None:
            self._start_run(t)
        k = len(self.buf) + len(self._pending)
        late = t - self._model(k) - self._offset() > self.gap_tol
        if self._pending:
            self._pending.append((count, t))
            if not late:                          # caught up: delayed, not lost
                self._resolve_pending(lost=False)
            elif (t - self._pending[0][1] >= CONFIRM_S
                  or len(self._pending) >= MAX_PENDING):
                self._resolve_pending(lost=True)  # flowing normally, still late
            return
        if late:
            self._pending = [(count, t)]          # lost samples, or just delayed?
            return
        self._append(count, t)

    def _resolve_pending(self, lost: bool):
        pend, self._pending = self._pending, []
        if lost:
            self._close_segment()
            self.seg_start = None
            for c, t in pend:
                self.add(c, t)
        else:
            for c, t in pend:
                self._append(c, t)

    def _append(self, count: int, t: float):
        k = len(self.buf)
        # Split at UTC midnight so a segment never straddles two day files.
        if k and _utc_day(self._model(k)) != _utc_day(self._model(0)):
            self._close_segment()
            k = 0
        o = t - self._model(k)
        self.buf.append(int(count))
        if o < self._blk_min:
            self._blk_min = o
        self._blk_n += 1
        if self._blk_n >= LATENCY_BLOCK:
            self._prev_min, self._blk_min, self._blk_n = self._blk_min, math.inf, 0
        if not self.seg_exact and self._first_n < LATENCY_BLOCK:
            self._first_min = min(self._first_min, o)
            self._first_n += 1
        if (k + 1) / self.fs >= self.flush_seconds:
            self._close_segment()

    # -- output -------------------------------------------------------------
    def _close_segment(self):
        n = len(self.buf)
        if not n:
            return
        if self.seg_exact:
            start = self.seg_start
        else:                                   # a run's first segment: correct its anchor
            start = self.seg_start + (0.0 if self._first_min == math.inf else self._first_min)
            self.true_start = start
        t_meas = self._model(n) + self._offset()     # when sample n would have been taken
        if t_meas - self.true_start >= MIN_MEASURE_S:
            fs_true = n / (t_meas - self.true_start)
            if abs(fs_true / self.fs_nominal - 1) <= RATE_LIMIT:
                self.fs_true = fs_true
        end = start + n / self.fs
        self._write(start, self.fs)
        drift = t_meas - end
        if abs(drift) > self.max_drift:
            print(f"timing correction: archive clock {drift:+.3f} s off UTC "
                  f"(sensor running at {self.fs_true:.4f} sps)", flush=True)
            end = t_meas
        self.seg_start, self.seg_exact, self.true_start = end, True, t_meas
        self.buf = []
        self._reset_minima()

    def _write(self, start: float, fs: float):
        from obspy import Stream, UTCDateTime
        st_utc = UTCDateTime(start)
        tr = _new_trace(np.array(self.buf, dtype=np.int32), st_utc, self.cfg, fs)
        out = sds_path(self.archive, self.cfg, st_utc.year, st_utc.julday)
        out.parent.mkdir(parents=True, exist_ok=True)
        # miniSEED files are concatenated records, so appending is valid and keeps
        # existing hours intact (Stream.write always overwrites a *path*, so we
        # hand it an append-mode handle instead).
        with open(out, "ab") as fh:
            Stream([tr]).write(fh, format="MSEED", encoding=self.encoding, reclen=512)
        if self.verbose:
            print(f"flushed {tr.stats.npts} samples @ {st_utc.isoformat()} "
                  f"(sensor {self.fs_true:.4f} sps) -> {out.name}", flush=True)

    def flush(self):
        """Write out everything buffered; the run continues seamlessly afterwards."""
        if self._pending:
            # Undecided: arriving at the normal pace means late for real (lost samples);
            # arriving in a burst means buffered.
            p = self._pending
            flowing = len(p) > 1 and p[-1][1] - p[0][1] >= 0.5 * (len(p) - 1) / self.fs
            self._resolve_pending(lost=flowing)
        self._close_segment()

    def end_run(self):
        """Flush and forget the run: the next sample starts a new one (after a reconnect)."""
        self.flush()
        self.seg_start = None


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


class AcquisitionStalled(RuntimeError):
    """No samples arrived for too long -- exit so systemd restarts us cleanly."""


def _catch_sigterm():
    """Make SIGTERM unwind through ``finally`` so buffered samples get written.

    systemd sends SIGTERM on stop/restart/shutdown, and Python's default
    disposition for it kills the process outright -- ``finally`` blocks and
    ``atexit`` hooks never run.  Since :class:`SdsWriter` only appends to the day
    file every ``flush_seconds`` (300 s by default), that silently discarded up
    to five minutes of already-acquired samples on every stop, including the
    watchdog's own unattended restarts.

    Re-raising as KeyboardInterrupt reuses the path Ctrl-C already takes, so the
    writer flushes and the record just ends where it should.  Returns whether the
    handler was installed; it is not, in a non-main thread, where signal() raises.
    """
    import signal

    def _stop(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _stop)
        return True
    except (ValueError, OSError, AttributeError):
        return False


_SAMPLE, _RESET, _ERROR = 0, 1, 2      # messages from the reader thread


def _reader(port, baud, warmup, reconnect_delay, stall_timeout, max_reconnects, out, stop):
    """Serial reader thread: stamp each sample on arrival and queue it.

    Nothing here touches the disk, so a slow write on the main thread can delay the
    archive but never the timestamps.  Reconnects are reported as ``_RESET`` (the
    writer ends the run); anything fatal, including the watchdogs below, is handed
    to the main thread as ``_ERROR`` to raise.
    """
    import serial
    fails = 0
    try:
        while not stop.is_set():
            try:
                with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                   timeout=1, rtscts=False, dsrdtr=False) as ser:
                    _assert_power_lines(ser)
                    _discard_warmup(ser, warmup)
                    fails = 0
                    last_sample = time.time()
                    while not stop.is_set():
                        line = ser.readline()
                        now = time.time()
                        c = parse_line(line)
                        if c is None:
                            # readline timed out, or the line was garbage. Neither
                            # raises, so this is where a dead sensor hides.
                            if stall_timeout and now - last_sample > stall_timeout:
                                raise AcquisitionStalled(
                                    f"no valid sample for {stall_timeout:.0f}s on {port} "
                                    f"(port is open, so the sensor or cable is the suspect)")
                            continue
                        last_sample = now
                        out.put((_SAMPLE, c, now))
            except serial.SerialException as e:
                # USB glitch / cable pull: end the run and retry.
                out.put((_RESET, None, None))
                fails += 1
                if max_reconnects and fails >= max_reconnects:
                    raise AcquisitionStalled(
                        f"{fails} consecutive failures opening {port} "
                        f"({e}) -- giving up so systemd can restart us") from e
                print(f"serial error: {e}; reconnecting in {reconnect_delay:.0f}s "
                      f"(attempt {fails}/{max_reconnects or '-'}) ...", flush=True)
                stop.wait(reconnect_delay)
    except BaseException as e:            # incl. KeyboardInterrupt: the main thread decides
        out.put((_ERROR, e, None))


def _hold_stop_signals():
    """Ignore SIGTERM/SIGINT while the final flush runs; returns the old handlers."""
    import signal
    old = {}
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):       # not the main thread
            pass
    return old


def _restore_signals(old):
    import signal
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def run(port: str, archive, cfg: StationConfig = DEFAULT_STATION, baud: int = 9600,
        fs: float = NOMINAL_FS, flush_seconds: float = 300.0, warmup: float = 15.0,
        gap_tol: float = 2.0, max_drift: float = 1.0, reconnect_delay: float = 5.0,
        live_file=None, live_seconds: float = 600.0,
        stall_timeout: float = 120.0, max_reconnects: int = 20):
    """Acquire from ``port`` into the SDS ``archive`` until interrupted.

    If ``live_file`` is set, also mirror the most recent ``live_seconds`` of raw
    samples into that (local) file every ~2 s so a live viewer can tail it.

    Two ways acquisition can wedge without the process dying, both of which used
    to be invisible -- ``Restart=always`` cannot help a daemon that never exits:

    * the port opens but nothing arrives (sensor unpowered, dead adapter, half
      broken cable).  ``readline`` just times out and returns b"" forever.
    * the adapter disappears for good, so every reconnect attempt fails.

    So give up rather than spin: raise ``AcquisitionStalled`` after
    ``stall_timeout`` seconds with no parsed sample, or after ``max_reconnects``
    consecutive failures to open the port.  Exiting non-zero lets systemd
    restart us from scratch, which also re-enumerates the serial device.
    Set either to 0 to disable (the old spin-forever behaviour).
    """
    import queue
    import threading
    from collections import deque

    _catch_sigterm()                    # so a systemd stop flushes instead of dropping
    writer = SdsWriter(archive, cfg, fs=fs, flush_seconds=flush_seconds, gap_tol=gap_tol,
                       max_drift=max_drift)
    live = deque(maxlen=int(live_seconds * fs)) if live_file else None
    last_live = 0.0
    q: queue.SimpleQueue = queue.SimpleQueue()
    stop = threading.Event()
    reader = threading.Thread(target=_reader, name="serial-reader", daemon=True,
                              args=(port, baud, warmup, reconnect_delay, stall_timeout,
                                    max_reconnects, q, stop))
    print(f"acquiring {port}@{baud} -> {archive}  ({cfg.seed_id}, nominal {fs:.4f} sps)  "
          f"stall timeout {stall_timeout:.0f}s  Ctrl-C to stop", flush=True)
    try:
        reader.start()                  # inside try: a stop signal may arrive at once
        while True:
            try:
                kind, a, b = q.get(timeout=1.0)
            except queue.Empty:
                if not reader.is_alive():
                    raise AcquisitionStalled("the serial reader thread exited unexpectedly")
                continue
            if kind == _SAMPLE:
                writer.add(a, b)
                if live is not None:
                    live.append(a)
                    if b - last_live >= 2.0:
                        _write_live(live_file, live, fs,
                                    dt.datetime.fromtimestamp(b, dt.timezone.utc))
                        last_live = b
            elif kind == _RESET:
                writer.end_run()
            else:
                raise a
    except KeyboardInterrupt:
        pass
    finally:
        old = _hold_stop_signals()      # a second SIGTERM must not cut the flush short
        stop.set()
        reader.join(timeout=3.0)
        while True:                     # samples the reader queued before it stopped
            try:
                kind, a, b = q.get_nowait()
            except queue.Empty:
                break
            if kind == _SAMPLE:
                writer.add(a, b)
        writer.flush()
        _restore_signals(old)
        print("stopped, buffer flushed", flush=True)


def _assert_power_lines(ser):
    """Drive DTR and RTS high -- on this rig they are the sensor's power supply.

    Infiltec ships the INFRA20 with a USB-serial adapter chosen because its
    handshake lines source enough current to run the sensor parasitically (DTR =
    DB9 pin 4, RTS = pin 7, against GND pin 5).  pyserial happens to assert both
    on open, so this has always worked by default -- but relying on that default
    means a different adapter or driver could leave the sensor unpowered with no
    obvious symptom beyond silence.  State it explicitly instead.

    Also why ``rtscts``/``dsrdtr`` are off at open: hardware flow control would
    hand these lines to the driver to toggle, i.e. cut power mid-stream.
    """
    for attr in ("dtr", "rts"):
        try:
            setattr(ser, attr, True)
        except (OSError, ValueError) as e:        # some adapters expose neither
            print(f"warning: could not assert {attr.upper()} ({e}); if the sensor "
                  f"is powered from that line it will stay silent", flush=True)


def _discard_warmup(ser, seconds: float):
    """Read and drop the connect transient (garbage line + filter settling).

    Every open is a *cold* start: closing the port drops DTR/RTS, which removes
    power from the sensor, so a reconnect power-cycles it rather than merely
    reattaching.  The INFRA20's 0.05 Hz high-pass corner implies a time constant
    of ~3.2 s, so allow ~5 of those to settle before trusting samples -- else the
    first data after an automatic restart is a decaying transient rather than an
    honest gap.
    """
    if seconds <= 0:
        return
    end = time.time() + seconds
    while time.time() < end:
        ser.readline()


def sniff(port: str, baud: int = 9600, seconds: float = 5.0) -> None:
    """Print raw lines from the port so the framing can be eyeballed."""
    import serial
    with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=1,
                       rtscts=False, dsrdtr=False) as ser:
        _assert_power_lines(ser)          # sniff must power the sensor too, or a
                                          # perfectly good adapter looks dead here
        end = time.time() + seconds
        while time.time() < end:
            print(repr(ser.readline()))


def main(argv=None):
    p = argparse.ArgumentParser(description="Live INFRA20 serial acquisition -> miniSEED SDS.")
    p.add_argument("port", nargs="?", default=None,
                   help="serial port, e.g. /dev/ttyUSB0 (default: config [acquisition].port)")
    p.add_argument("archive", nargs="?", default=None,
                   help="SDS archive root (default: config [acquisition].archive)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--fs", type=float, default=NOMINAL_FS,
                   help="sample rate written to the miniSEED headers (sps)")
    p.add_argument("--flush-seconds", type=float, default=300.0,
                   help="append a segment to the day file at least this often")
    p.add_argument("--warmup", type=float, default=15.0,
                   help="seconds of post-open data to discard; closing the port drops "
                        "DTR/RTS and so unpowers the sensor, making every open a cold "
                        "start that must settle before its samples mean anything")
    p.add_argument("--gap-tol", type=float, default=2.0,
                   help="samples this many seconds late, and staying late, were lost: "
                        "the run ends (explicit gap)")
    p.add_argument("--max-drift", type=float, default=1.0,
                   help="re-anchor the archive's clock when it is this far (s) from UTC")
    p.add_argument("--live-file", default=None,
                   help="mirror recent raw samples to this LOCAL file for a live viewer "
                        "(default: config [acquisition].live_file)")
    p.add_argument("--live-seconds", type=float, default=600.0,
                   help="length of the rolling live buffer (s)")
    p.add_argument("--stall-timeout", type=float, default=120.0,
                   help="exit (so systemd restarts) after this long with no valid "
                        "sample even though the port is open; 0 disables")
    p.add_argument("--max-reconnects", type=int, default=20,
                   help="exit after this many consecutive failures to open the "
                        "port, instead of retrying forever; 0 disables")
    p.add_argument("--sniff", action="store_true", help="just print raw lines and exit")
    p.add_argument("--list", action="store_true", help="list available serial ports and exit")
    a = p.parse_args(argv)
    if a.list:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        print("available serial ports:" if ports else "no serial ports found")
        for pi in ports:
            print(f"  {pi.device}  {pi.description}")
        return
    port = a.port or SERIAL_PORT
    archive = a.archive or ARCHIVE_DIR
    live_file = a.live_file or LIVE_FILE
    if a.sniff:
        sniff(port, a.baud)
        return
    run(port, archive, baud=a.baud, fs=a.fs, flush_seconds=a.flush_seconds,
        live_file=live_file, live_seconds=a.live_seconds,
        warmup=a.warmup, gap_tol=a.gap_tol, max_drift=a.max_drift,
        stall_timeout=a.stall_timeout, max_reconnects=a.max_reconnects)


if __name__ == "__main__":
    main()
