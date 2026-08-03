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
        self.dtr = self.rts = False
        self.kwargs = k
        _SilentPort.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def readline(self):
        return b""


def test_dtr_and_rts_are_asserted_because_they_power_the_sensor(tmp_path, monkeypatch):
    """The INFRA20 is powered parasitically off the handshake lines, so leaving
    these to pyserial's default would make the sensor's power supply implicit."""
    _SilentPort.opened.clear()
    _install_fake_serial(monkeypatch, _SilentPort)
    with pytest.raises(AcquisitionStalled):
        run("/dev/fake", tmp_path, warmup=0, stall_timeout=0.3, live_file=None)
    port = _SilentPort.opened[0]
    assert port.dtr is True and port.rts is True
    # hardware flow control would let the driver toggle those same lines
    assert port.kwargs.get("rtscts") is False
    assert port.kwargs.get("dsrdtr") is False


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
