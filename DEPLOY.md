# Deploying the INFRA20 monitor

Stand up the tool on your own Windows PC. You need an **Infiltec INFRA20** on a
serial/USB port and **Python 3.10+**. (Linux / Raspberry Pi support is planned — see
the end.)

## 1. Get the code

```powershell
git clone https://github.com/kcriqui/infrasound-monitor.git
cd infrasound-monitor
```

Make sure `python` is on your PATH (`python --version` should print 3.10+). If you
install Python from python.org, tick "Add Python to PATH".

## 2. Configure your station

```powershell
copy config.example.toml config.toml
notepad config.toml
```

Set at least the **serial port** and your **coordinates / site name**; the file is
commented. `config.toml` is git-ignored, so your settings are never committed or
overwritten by `git pull`.

To find the serial port and confirm the framing, run the sniffer (close any other
program using the port, e.g. AmaSeis, first):

```powershell
python -m infrasound_monitor.acquire --list      # list serial ports
python -m infrasound_monitor.acquire COM3 --sniff # watch raw lines from a port
```

You should see one signed integer per line (e.g. `-00123`) at ~51 lines/second — put
that measured rate in `config.toml` as `sample_rate`.

## 3. Run setup

```powershell
powershell -ExecutionPolicy Bypass -File deploy\setup.ps1
```

This installs the package (`pip install -e .`), creates `config.toml` if missing, and
registers a scheduled task **`InfraAcquire`** that runs the acquisition daemon at logon
and restarts it on failure. (Add `-Dashboard` to also register the daily dashboard
rebuild.) Per-user tasks need no admin.

Verify the setup at any time — this catches most first-run problems:

```powershell
python tools\doctor.py   # checks Python, deps, config.toml, serial port, writable paths, daemon
```

## 4. Acquire and watch

```powershell
Start-ScheduledTask -TaskName InfraAcquire   # start acquiring now
python tools\live.py                         # AmaSeis-style live drum view
```

Data lands as standard **miniSEED** in `archive\` (SDS layout) with a `station.xml`,
so it works in ObsPy, Swarm, and the FDSN toolchain. Manage the daemon with
`Start-ScheduledTask` / `Stop-ScheduledTask -TaskName InfraAcquire`; its log is
`deploy\acquire.log`. Only one program can hold the port, so keep AmaSeis closed.

## 5. Analyze

All tools take an archive path (defaults to your configured `archive`) and a date range:

```powershell
python tools\waterfall.py archive --start 2026-01-01 --end 2026-02-01 --out waterfall.html --cache analysis\grid_full.npz
python tools\analyze.py   archive --start 2026-01-01 --end 2026-02-01                       # PPSD + dayplot
python tools\tonehunt.py  archive --start 2026-01-01 --end 2026-02-01 --cache analysis\grid_full.npz --night
python tools\report.py    archive --start 2026-01-01 --end 2026-02-01 --cache analysis\grid_full.npz   # station report
python tools\transients.py archive --start 2026-01-01 --end 2026-02-01 --html events\index.html        # event explorer
```

Reuse the same `--cache` grid across tools so re-runs are instant.

## 6. Optional: public dashboard

`tools\dashboard.py` builds a single self-contained `index.html`. To publish it on a
schedule, set up a static host (e.g. a GitHub Pages repo) at `site\`, then run
`setup.ps1 -Dashboard` to register the daily rebuild+push (`deploy\publish.ps1`). Build
the PSD grid cache once first (the `waterfall.py ... --cache` command above).

## Linux / Raspberry Pi

A dedicated Raspberry Pi is the ideal 24/7 monitor — it boots straight into acquiring,
no login required. Same idea as Windows, but with **systemd** instead of Task Scheduler:

```bash
git clone https://github.com/kcriqui/infrasound-monitor.git
cd infrasound-monitor
cp config.example.toml config.toml      # edit: port = "/dev/ttyUSB0", coordinates, sample_rate
bash deploy/setup.sh                     # venv install + systemd service (add --dashboard for the daily rebuild)
sudo systemctl start infra-acquire       # start after editing a freshly-created config
```

`setup.sh` installs into a project-local `.venv` (Raspberry Pi OS blocks system-wide
pip), adds you to the `dialout` group for serial access, and installs a systemd service
that **starts at boot and auto-restarts**. Inspect it with:

```bash
systemctl status infra-acquire
journalctl -u infra-acquire -f           # live log
sudo systemctl restart infra-acquire     # after editing config.toml
.venv/bin/python tools/doctor.py         # verify the setup (deps, config, serial port, paths)
```

Pi specifics:
- **Serial port:** the INFRA20's USB adapter is usually `/dev/ttyUSB0`
  (`ls /dev/ttyUSB* /dev/ttyACM*` to find it). Put it in `config.toml`.
- **First install is slow** — scipy/obspy fetch large ARM wheels (piwheels). Use 64-bit
  Pi OS and a Pi 4/5 for a smoother time.
- **Headless = no live window.** `tools/live.py` opens a GUI, so on a headless Pi use
  `python tools/live.py --snapshot live.png` or the network dashboard. Analysis tools all
  run headless.
- Reboot once after the first setup so the `dialout` group fully applies.

## Notes

- **One tree.** Everything lives under the project folder: code, `config.toml`, the
  `archive/`, `analysis/`, and generated `site/`. Put it on a **local** disk (not a
  network or cloud-synced folder) so the 24/7 daemon writes reliably.
- **WSL** is fine for the *analysis* tools, but serial acquisition in WSL2 needs
  `usbipd-win` to attach the USB device — a native Linux box or Raspberry Pi is simpler
  for the daemon.
