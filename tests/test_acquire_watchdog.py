"""The daemon must exit rather than wedge, so systemd's Restart=always can act.

Both failure modes below leave the process alive and the systemd unit reporting
``active (running)`` in the original code, which is how a ~6 h outage went
unnoticed on 2026-08-03: the port stayed open, readline() just timed out into
`continue` forever, and nothing was ever logged.
"""
import sys
import types

import pytest

from infrasound_monitor.acquire import run, AcquisitionStalled


class _FakeSerialException(Exception):
    pass


def _install_fake_serial(monkeypatch, serial_cls):
    mod = types.ModuleType("serial")
    mod.Serial = serial_cls
    mod.SerialException = _FakeSerialException
    monkeypatch.setitem(sys.modules, "serial", mod)


class _SilentPort:
    """Opens fine, then returns nothing -- a dead sensor behind a live adapter."""
    opened = []

    def __init__(self, *a, **k):
        self.history = []                      # every DTR/RTS assignment, in order
        self.dtr = self.rts = False
        self.history.clear()
        self.kwargs = k
        _SilentPort.opened.append(self)

    def __setattr__(self, name, value):
        if name in ("dtr", "rts") and "history" in self.__dict__:
            self.history.append((name, value))
        object.__setattr__(self, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def readline(self):
        return b""


@pytest.fixture(autouse=True)
def _no_power_off_wait(monkeypatch):
    import infrasound_monitor.acquire as acq
    monkeypatch.setattr(acq, "POWER_OFF_S", 0.0)


def test_dtr_and_rts_are_asserted_because_they_power_the_sensor(tmp_path, monkeypatch):
    """The INFRA20 is powered parasitically off the handshake lines, so leaving
    these to pyserial's default would make the sensor's power supply implicit.
    A stall then cuts that power on purpose, so the restart is a cold start."""
    _SilentPort.opened.clear()
    _install_fake_serial(monkeypatch, _SilentPort)
    with pytest.raises(AcquisitionStalled, match="power cut"):
        run("/dev/fake", tmp_path, warmup=0, stall_timeout=0.3, live_file=None)
    port = _SilentPort.opened[0]
    assert port.history[:2] == [("dtr", True), ("rts", True)]     # powered first
    assert port.dtr is False and port.rts is False                 # then cut on stall
    # hardware flow control would let the driver toggle those same lines
    assert port.kwargs.get("rtscts") is False
    assert port.kwargs.get("dsrdtr") is False


def test_closing_the_port_leaves_the_sensor_powered():
    """HUPCL off: a restart must not power-cycle the sensor (its clock comes back
    at a different rate).  Checked on a real pty."""
    import os
    import termios
    from infrasound_monitor.acquire import _keep_power_on_close
    if not hasattr(os, "openpty"):
        pytest.skip("needs a pty")
    master, slave = os.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[2] |= termios.HUPCL
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        class _Port:
            def fileno(self):
                return slave
        assert _keep_power_on_close(_Port()) is True
        assert not termios.tcgetattr(slave)[2] & termios.HUPCL
    finally:
        os.close(master); os.close(slave)


def test_main_exits_without_interpreter_teardown(tmp_path, monkeypatch):
    """main() must leave via os._exit once run() returns: the teardown is where a
    stop hung for ~30 s.  Non-zero when run() raised, so systemd restarts it."""
    import infrasound_monitor.acquire as acq
    codes = []
    monkeypatch.setattr(acq.os, "_exit", lambda c: codes.append(c))
    monkeypatch.setattr(acq, "run", lambda *a, **k: None)
    acq.main(["/dev/fake", str(tmp_path)])
    assert codes == [0]

    def boom(*a, **k):
        raise AcquisitionStalled("x")
    monkeypatch.setattr(acq, "run", boom)
    codes.clear()
    acq.main(["/dev/fake", str(tmp_path)])
    assert codes[0] == 1


class _UnopenablePort:
    """Every open attempt fails -- the adapter is gone for good."""
    def __init__(self, *a, **k):
        raise _FakeSerialException("no such device")


def test_silent_port_raises_instead_of_spinning_forever(tmp_path, monkeypatch):
    _install_fake_serial(monkeypatch, _SilentPort)
    with pytest.raises(AcquisitionStalled, match="no valid sample"):
        run("/dev/fake", tmp_path, warmup=0, stall_timeout=0.5, live_file=None)


def test_repeated_open_failures_give_up(tmp_path, monkeypatch):
    _install_fake_serial(monkeypatch, _UnopenablePort)
    with pytest.raises(AcquisitionStalled, match="consecutive failures"):
        run("/dev/fake", tmp_path, warmup=0, reconnect_delay=0.0,
            max_reconnects=3, live_file=None)


def test_sigterm_handler_raises_the_exception_run_unwinds_on():
    """Platform-independent half of the check below: the installed handler must
    raise KeyboardInterrupt, the one exception run() flushes on."""
    import signal
    from infrasound_monitor.acquire import _catch_sigterm

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert _catch_sigterm() is True
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sigterm_flushes_buffered_samples_instead_of_dropping_them(tmp_path, monkeypatch):
    """systemd stops the daemon with SIGTERM, whose default disposition kills the
    process outright -- `finally: writer.flush()` never runs and everything since
    the last flush (up to flush_seconds, 300 s) is silently lost."""
    import os
    import signal

    if not hasattr(signal, "SIGTERM") or os.name != "posix":
        pytest.skip("POSIX signal delivery required")

    counts = iter([b"%d\r\n" % v for v in range(500)])
    sent = []

    class _FeedThenSigterm(_SilentPort):
        def readline(self):
            try:
                return next(counts)
            except StopIteration:
                if not sent:                           # once, as systemd would
                    sent.append(1)
                    os.kill(os.getpid(), signal.SIGTERM)
                return b""

    _install_fake_serial(monkeypatch, _FeedThenSigterm)
    # flush_seconds far larger than the run, so nothing flushes on its own:
    # anything on disk afterwards can only have come from the shutdown path.
    run("/dev/fake", tmp_path, warmup=0, flush_seconds=1e6, stall_timeout=0,
        live_file=None)

    written = list(tmp_path.rglob("*.SDF.D.*"))
    assert written, "SIGTERM dropped the buffer -- nothing reached the day file"
    assert written[0].stat().st_size > 0


def test_watchdogs_can_be_disabled(tmp_path, monkeypatch):
    """0 restores the old spin-forever behaviour; prove it does not raise early."""
    calls = {"n": 0}

    class _BrieflySilent(_SilentPort):
        def readline(self):
            calls["n"] += 1
            if calls["n"] > 50:
                raise KeyboardInterrupt          # stand-in for "stopped by hand"
            return b""

    _install_fake_serial(monkeypatch, _BrieflySilent)
    run("/dev/fake", tmp_path, warmup=0, stall_timeout=0, live_file=None)
    assert calls["n"] > 50                       # never bailed out on its own
