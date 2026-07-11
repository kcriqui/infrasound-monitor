#!/usr/bin/env python3
"""Standalone INFRA20 serial sniffer -- reverse-engineer the output framing.

Needs ONLY pyserial (no ObsPy, no package install), so it runs on the machine the
INFRA20 is plugged into with minimal setup:

    pip install pyserial          # once
    python sniff_infra20.py --list                 # find the COM port
    python sniff_infra20.py --port COM3             # capture ~10 s
    python sniff_infra20.py --port COM3 --seconds 20

IMPORTANT: close AmaSeis first -- only one program can hold the serial port.

It prints a hex+ASCII dump and a byte-rate estimate (which alone tells us ASCII vs
binary), and saves the raw bytes to sniff_capture_<port>.bin.  Send that file (or
paste the printed dump) back and the parser can be finalized exactly.
"""
import argparse
import sys
import time


def list_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        sys.exit("pyserial not installed. Run:  pip install pyserial")
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Is the USB-serial adapter plugged in?")
        return
    print("Available serial ports:")
    for p in ports:
        print(f"  {p.device:8s}  {p.description}   [{p.hwid}]")


def hexdump(data: bytes, limit: int = 512) -> str:
    out = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:06x}  {hexs:<47}  {asci}")
    return "\n".join(out)


def capture(port: str, baud: int, seconds: float):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed. Run:  pip install pyserial")

    print(f"opening {port} @ {baud} 8N1 for {seconds:.0f}s ... (Ctrl-C to stop early)")
    buf = bytearray()
    lines = []
    t0 = time.time()
    try:
        with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=1) as ser:
            end = t0 + seconds
            while time.time() < end:
                n = ser.in_waiting or 1
                chunk = ser.read(n)
                if chunk:
                    buf.extend(chunk)
    except serial.SerialException as e:
        sys.exit(f"could not open {port}: {e}\n(Is AmaSeis still running and holding the port?)")
    except KeyboardInterrupt:
        pass
    dur = time.time() - t0

    if not buf:
        print("No bytes received. Wrong port, or AmaSeis still holding it?")
        return

    rate = len(buf) / dur
    print(f"\ncaptured {len(buf)} bytes in {dur:.1f}s  =>  {rate:.0f} bytes/sec")
    # heuristic hint
    print("hint: ~100 B/s suggests 2-byte binary @ ~50 sps; "
          "~350-450 B/s suggests ASCII integers per sample.\n")

    print("---- first bytes (hex | ascii) ----")
    print(hexdump(buf))

    # try newline-delimited ASCII interpretation
    text = bytes(buf)
    for sep in (b"\r\n", b"\n", b"\r"):
        if sep in text:
            parts = text.split(sep)
            sample = [p for p in parts[1:21] if p]  # skip possibly-partial first
            print(f"\n---- split on {sep!r}: {len(parts)} pieces; first lines: ----")
            for p in sample[:20]:
                print("   ", p)
            # attempt integer parse
            vals = []
            for p in sample:
                s = p.strip()
                try:
                    vals.append(int(s))
                except ValueError:
                    for tok in s.replace(b",", b" ").split():
                        try:
                            vals.append(int(tok)); break
                        except ValueError:
                            pass
            if vals:
                print(f"   parsed ints (sample): {vals[:12]}")
            break

    out = f"sniff_capture_{port.replace('/', '_')}.bin"
    with open(out, "wb") as fh:
        fh.write(buf)
    print(f"\nraw capture saved -> {out}  (send this back for exact decoding)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sniff the INFRA20 serial output framing.")
    ap.add_argument("--list", action="store_true", help="list available serial ports and exit")
    ap.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--seconds", type=float, default=10.0)
    a = ap.parse_args(argv)
    if a.list or not a.port:
        list_ports()
        if not a.port:
            print("\nThen run:  python sniff_infra20.py --port <PORT>")
        return
    capture(a.port, a.baud, a.seconds)


if __name__ == "__main__":
    main()
