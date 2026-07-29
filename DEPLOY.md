# Deploying the INFRA20 monitor

Stand up the tool on your own Windows PC. You need an
**[Infiltec INFRA20](https://www.infiltec.com/Infrasound@home/)** on a
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

### Surviving reboots (unattended operation)

`InfraAcquire` runs **at logon**, not at boot — so after a power-up or a reboot (a
Windows Update, a power blip), acquisition does **not** resume until someone signs in,
leaving a gap in the archive. (If your archive is on a login-mounted drive such as
Google Drive, that's a second reason a pre-login task can't write.) For a truly
unattended monitor, enable **auto-login** so the machine boots straight to the desktop
and the task fires on its own:

1. `Win+R` → `netplwiz` → uncheck **"Users must enter a user name and password to use
   this computer"** → Apply → enter the account password (leave blank if the account has
   none) → OK.
2. If that checkbox is missing, Windows Hello "Hello-only sign-in" is hiding it. Clear it
   in **Settings → Accounts → Sign-in options** (toggle off "only allow Windows Hello
   sign-in…"), or — if that toggle isn't shown (e.g. a local account) — from an
   **elevated** PowerShell:
   ```powershell
   Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' -Name DevicePasswordLessBuildVersion -Value 0
   ```
   then redo step 1.

**Security tradeoff:** anyone who powers the machine on lands at your desktop with full
access. Fine for a dedicated home sensor PC; not for a shared or sensitive machine. A
**Raspberry Pi** (below) avoids this entirely — its systemd service starts at boot with
no login and no auto-login compromise. Note also that if you later set/change the
account password, auto-login silently breaks and step 1 must be redone.

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

## Raspberry Pi (recommended 24/7 build)

A dedicated Raspberry Pi is the ideal always-on monitor — it boots straight into
acquiring with **no login required**, so it rides through power cuts and updates that
would strand the Windows box (see "Surviving reboots" above). A **Pi 3B (1 GB)** is
enough to run *everything* — acquisition, the nightly analysis + site rebuild, and a
small status screen — as long as you add swap; a Pi 4/5 just does the analysis faster.

**Distro:** flash **Raspberry Pi OS Lite (64-bit, Bookworm)** with Raspberry Pi Imager.
No desktop is needed — the tools run headless and the optional TFT is drawn by a Python
script. Use Imager's gear menu to preset the **hostname, SSH, Wi-Fi and locale** so it's
headless from first boot. 64-bit matters: it gets prebuilt ARM wheels for numpy/scipy/
obspy from **piwheels**, so there's no hours-long source compile.

```bash
git clone https://github.com/kcriqui/infrasound-monitor.git
cd infrasound-monitor
cp config.example.toml config.toml       # edit: port = "/dev/ttyUSB0", coordinates, sample_rate

# Full build: acquisition + daily dashboard + Mini PiTFT display + swap + nightly backup
bash deploy/setup.sh --dashboard --display --swap --backup user@nas:/infra-backup

sudo systemctl start infra-acquire       # start after editing a freshly-created config
sudo reboot                              # once, so dialout/spi/gpio groups + SPI apply
```

`setup.sh` installs into a project-local `.venv` (Raspberry Pi OS blocks system-wide
pip), adds you to the `dialout` group, and installs systemd units that **start at boot
and auto-restart**. Every flag is optional — plain `bash deploy/setup.sh` installs just
the acquisition daemon. Inspect it with:

```bash
systemctl status infra-acquire
journalctl -u infra-acquire -f           # live log
sudo systemctl restart infra-acquire     # after editing config.toml
.venv/bin/python tools/doctor.py         # verify the setup (deps, config, serial port, paths)
```

### `--swap` — keep the analysis from OOM-ing (do this on a 1 GB Pi)

The analysis step (PPSD, wide-range renders) can exceed 1 GB of RAM and get OOM-killed.
`--swap` bumps the swapfile to 2 GB so it completes — slower, but it finishes. The design
already helps: `psd.py` keeps an incremental grid cache, so each nightly run only
processes the *new* hours and the pages render from the cached grid, not the raw archive.

### `--display` — Mini PiTFT 1.14" status screen

`--display` installs the `infra-display` service running `tools/tft_status.py`, which
draws a live health panel on an **Adafruit Mini PiTFT 1.14" (240×135, ST7789)**:

- **LIVE** page — OK/STALE state, seconds since the last sample, live RMS level, the
  dominant tone (short FFT of the live buffer), and uptime.
- **SYSTEM** page — today's data (MB), total archive size, CPU temp, last publish, and
  free-disk bar.

The **top button (GPIO23)** cycles pages; the **bottom button (GPIO24)** toggles the
backlight. `setup.sh --display` also enables SPI and adds you to the `spi`/`gpio` groups
(hence the reboot). It's deliberately light — it reads the daemon's `live.npz` and
`/proc`, and does **not** import obspy, so it barely competes with acquisition. Preview
the layout on any machine without the hardware:

```bash
python tools/tft_status.py --snapshot preview.png          # LIVE page
python tools/tft_status.py --snapshot sys.png --page 1     # SYSTEM page
```

### `--backup <DEST>` — protect the irreplaceable baseline

The archive is your one-of-a-kind "before" record and **SD cards fail**. `--backup`
installs a nightly `rsync` (04:30) of the archive to any destination — a mounted USB
disk (`/mnt/usb/infra-backup`) or an ssh target (`user@nas:/path`). It never uses
`--delete`, so it only adds/updates. Run one on demand with
`sudo systemctl start infra-backup`. Also prefer a **high-endurance** SD card for the
24/7 write load.

**Preparing a USB drive as the target.** Identify it with `lsblk -f`, then — if it's
empty/expendable — format it ext4 and auto-mount it at `/mnt/usb` (assuming the stick is
`/dev/sda1`):

```bash
sudo wipefs -a /dev/sda1 && sudo mkfs.ext4 -L INFRABACKUP /dev/sda1
sudo mkdir -p /mnt/usb
sudo blkid /dev/sda1                          # note the new UUID
# add to /etc/fstab (nofail lets the Pi boot even if the drive is absent):
echo "UUID=<uuid>  /mnt/usb  ext4  defaults,nofail,noatime  0  2" | sudo tee -a /etc/fstab
sudo mount -a && sudo chown "$USER":"$USER" /mnt/usb
```

Then `bash deploy/setup.sh --backup /mnt/usb/infra-backup` (or, if already installed,
`sudo systemctl start infra-backup` to test). Confirm the nightly schedule with
`systemctl list-timers 'infra-*'`. Size the drive for your retention — the archive grows
~3.5 GB/year, so a 4 GB stick holds roughly a year.

### Test the Pi before you move the sensor (avoid a data gap)

There's one sensor and the serial port is exclusive, so you can't read the real stream
on the Pi while it's still on the PC. But you can validate everything else first with a
**simulated** stream, so the only unavoidable gap is the ~1-minute physical USB swap.

`tools/sim_infra20.py` creates a virtual serial port and feeds synthetic INFRA20 lines
(a tone + noise). Point the daemon at it — into a **scratch archive**, so the simulated
data never touches your real baseline:

```bash
# terminal 1 -- fake sensor; note the /dev/pts/N it prints
.venv/bin/python tools/sim_infra20.py

# terminal 2 -- acquire from it into a throwaway archive dir. Omit --live-file so it
# writes the live buffer to the config path the display reads (config.toml live_file,
# default <project>/live.npz); the archive still goes to the scratch dir.
.venv/bin/python -m infrasound_monitor.acquire /dev/pts/N /tmp/testarch
```

Let it run a minute, then Ctrl-C. That exercises the full path — miniSEED writing, the
rolling `live.npz`, gap handling. With both running, the **PiTFT LIVE page** should show
`OK`, a ~3 Hz tone and a level, which also confirms the panel, SPI, wiring and buttons.
(The display always reads `live_file` from `config.toml`; if you point acquire's
`--live-file` somewhere else the panel will show `NO DATA` even though acquisition works.)
To rehearse the analysis + publish path, `scp` a few days of real archive from the PC and
run `tools/report.py` / `tools/dashboard.py` against it. Then `.venv/bin/python tools/doctor.py`
for the overall check (its serial-port test is the only thing that should fail until the
real sensor is attached). Delete `/tmp/testarch` when done.

**The cutover** (single, short gap): stop the PC daemon (`Stop-ScheduledTask InfraAcquire`),
unplug the USB adapter from the PC, plug it into the Pi, confirm the port
(`ls /dev/ttyUSB*`), set it in `config.toml`, then `sudo systemctl start infra-acquire`.
The daemon records the brief handover as an explicit gap.

Pi specifics:
- **Serial port:** the INFRA20's USB adapter is usually `/dev/ttyUSB0`
  (`ls /dev/ttyUSB* /dev/ttyACM*` to find it). Put it in `config.toml`.
- **First install is slow** — even from piwheels, scipy/obspy are large ARM wheels.
- **Storage:** the INFRA20 writes only ~10 MB/day (~3.5 GB/year), so a 16 GB card holds
  a couple of years after the OS + venv; plan to grow the card or prune before it fills.
- **Headless = no live window.** `tools/live.py` opens a GUI; on a headless Pi use
  `tools/live.py --snapshot live.png`, the TFT panel, or the web dashboard. Analysis
  tools all run headless.
- Reboot once after the first setup so the `dialout`/`spi`/`gpio` groups fully apply.

## Notes

- **One tree.** Everything lives under the project folder: code, `config.toml`, the
  `archive/`, `analysis/`, and generated `site/`. Put it on a **local** disk (not a
  network or cloud-synced folder) so the 24/7 daemon writes reliably.
- **WSL** is fine for the *analysis* tools, but serial acquisition in WSL2 needs
  `usbipd-win` to attach the USB device — a native Linux box or Raspberry Pi is simpler
  for the daemon.
