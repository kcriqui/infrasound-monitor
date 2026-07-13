#!/usr/bin/env python3
"""Check that an INFRA20 deployment is ready to run -- one command, common hiccups.

Verifies the Python version, dependencies, the package install, config.toml, the
serial port, that the archive/live paths are writable, and whether the daemon is
set up. Prints a checklist and exits non-zero if anything failed.

    python tools/doctor.py
"""
from __future__ import annotations
import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"
GLYPH = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}   # ASCII: safe on any console
_results = []


def check(name, status, msg=""):
    _results.append(status)
    line = f"  {GLYPH[status]}  {name}"
    if msg:
        line += f"  --  {msg}"
    print(line)


def _writable(d) -> bool:
    try:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        t = p / ".doctor_write_test"
        t.write_text("x")
        t.unlink()
        return True
    except Exception:
        return False


def main():
    print(f"INFRA20 doctor  --  {platform.system()} {platform.release()},"
          f" Python {platform.python_version()}\n")

    # 1. Python version
    check("Python >= 3.10", OK if sys.version_info >= (3, 10) else FAIL,
          platform.python_version())

    # 2. dependencies
    for mod, pkg in [("numpy", "numpy"), ("scipy", "scipy"), ("obspy", "obspy"),
                     ("matplotlib", "matplotlib"), ("serial", "pyserial"),
                     ("plotly", "plotly")]:
        try:
            m = importlib.import_module(mod)
            check(f"dependency: {pkg}", OK, getattr(m, "__version__", "?"))
        except Exception as e:
            check(f"dependency: {pkg}", FAIL, f"not importable ({e}); run: pip install -e .")

    # 3. the package itself
    try:
        import infrasound_monitor
        loc = Path(infrasound_monitor.__file__).resolve().parent
        check("package infrasound_monitor", OK, str(loc))
    except Exception as e:
        check("package infrasound_monitor", FAIL, f"{e}; run: pip install -e .")
        _summary()
        return

    from infrasound_monitor import config as C

    # 4. config
    cfg = C._find_config()
    if cfg:
        check("config.toml", OK, str(cfg))
    else:
        check("config.toml", WARN,
              "none found -- copy config.example.toml -> config.toml and edit it")
    st = C.DEFAULT_STATION
    check("station id", OK,
          f"{st.seed_id}" + ("  (network XX = FDSN test code)" if st.network == "XX" else ""))
    if st.latitude == 0 and st.longitude == 0:
        check("station coordinates", WARN, "0, 0 -- set latitude/longitude in config.toml")
    else:
        check("station coordinates", OK, f"{st.latitude}, {st.longitude}")

    # 5. serial port
    try:
        from serial.tools import list_ports
        ports = [(p.device, p.description) for p in list_ports.comports()]
        names = [d for d, _ in ports]
        if C.SERIAL_PORT in names:
            desc = next(dd for d, dd in ports if d == C.SERIAL_PORT)
            check(f"serial port {C.SERIAL_PORT}", OK, desc)
        else:
            listing = ", ".join(f"{d} ({dd})" for d, dd in ports) or "none"
            check(f"serial port {C.SERIAL_PORT}", WARN,
                  f"not found. available: {listing}")
    except Exception as e:
        check("serial port", WARN, f"could not list ports ({e})")

    # 6. writable paths
    check("archive dir writable", OK if _writable(C.ARCHIVE_DIR) else FAIL, C.ARCHIVE_DIR)
    live_dir = Path(C.LIVE_FILE).parent
    check("live buffer dir writable", OK if _writable(live_dir) else WARN, str(live_dir))

    # 7. daemon / service registered?
    _check_daemon()

    _summary()


def _check_daemon():
    try:
        if os.name == "nt":
            r = subprocess.run(["schtasks", "/query", "/TN", "InfraAcquire", "/FO", "LIST"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                status = next((l.split(":", 1)[1].strip() for l in r.stdout.splitlines()
                               if l.strip().startswith("Status")), "registered")
                check("acquisition task (InfraAcquire)", OK, status)
            else:
                check("acquisition task (InfraAcquire)", WARN,
                      "not registered -- run deploy\\setup.ps1")
        else:
            r = subprocess.run(["systemctl", "is-active", "infra-acquire"],
                               capture_output=True, text=True, timeout=10)
            state = r.stdout.strip() or "not installed"
            check("acquisition service (infra-acquire)",
                  OK if state == "active" else WARN,
                  state + ("" if state == "active" else " -- run: bash deploy/setup.sh"))
    except Exception as e:
        check("acquisition daemon", WARN, f"could not query ({e})")


def _summary():
    n = len(_results)
    nf = _results.count(FAIL)
    nw = _results.count(WARN)
    print(f"\n{n} checks:  {n - nf - nw} ok, {nw} warning(s), {nf} failure(s)")
    if nf:
        print("Fix the [FAIL] items above, then re-run.  See DEPLOY.md.")
    elif nw:
        print("Ready to run, with warnings noted above.  See DEPLOY.md.")
    else:
        print("All good -- you're ready to acquire.")
    sys.exit(1 if nf else 0)


if __name__ == "__main__":
    main()
