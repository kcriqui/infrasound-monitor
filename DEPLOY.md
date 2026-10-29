# Deploying the INFRA20 monitor

You need an **[Infiltec INFRA20](https://www.infiltec.com/Infrasound@home/)** on a
serial/USB port, a Linux machine with **Python 3.10+** and systemd, and ideally a
Raspberry Pi. There are two ways to run it:

| Setup | Acquires | Analyses + serves | Guide |
|---|---|---|---|
| **Raspberry Pi** (this station) | the Pi | the Pi | [Raspberry Pi](#raspberry-pi-this-stations-build) |
| Pi + analysis server | the Pi | an always-on box, e.g. TrueNAS SCALE | Pi section, then [deploy/truenas/README.md](deploy/truenas/README.md) |

This station's TrueNAS server also pulls the Pi's archive every night as a snapshotted
backup ([deploy/truenas/README.md](deploy/truenas/README.md)). From Oct 10 to 29, 2026 it
did the rebuild too, because the rebuild's memory then grew with the record and had
outgrown the 1 GB Pi. That's fixed (see "What a 1 GB Pi can carry" below).

Until July 2026 the station ran on a Windows PC with AmaSeis. Windows support was
removed in October 2026; the old `.Z` archive converts with `infra-convert`.

## Raspberry Pi (this station's build)

A dedicated Raspberry Pi is the ideal always-on monitor — it boots straight into
acquiring with **no login required**, so it rides through power cuts and updates.
This station runs a **Pi 3B+ (1 GB)**.

**What a 1 GB Pi can carry.** All of it. With acquisition, the status page and the TFT
running, this station's Pi uses ~235 MB in total. The nightly analysis + site rebuild
covers the whole record. It used to load the whole PSD grid and copy it a dozen times, so its memory
grew ~2.4 GB per year of record. In October 2026 it outgrew this station's Pi and took
it down three nights running. Now the grid cache is memory-mapped (`grid_full.npz`
plus a `grid_full.psd.f32` sidecar of float32 rows) and the rebuild streams over it a
block of hours at a time. Its peak is ~300–350 MB whether the record is three months or
seven years long. That was measured inside a 1 GB container with no swap, and on this
station's Pi 3B+ the real rebuild peaks at 304 MB and takes 75–90 s. What still grows is
**time** (about a minute per year of record on a desktop CPU, several times that on a
Pi) and the grid file on disk (~110 MB per year).

**Distro:** flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. This
station runs Trixie, and Bookworm works too.
No desktop is needed — the tools run headless and the optional TFT is drawn by a Python
script. Use Imager's gear menu to preset the **hostname, SSH, Wi-Fi and locale** so it's
headless from first boot. 64-bit matters: it gets prebuilt ARM wheels for numpy/scipy/
obspy from **piwheels**, so there's no hours-long source compile.

```bash
git clone https://github.com/kcriqui/infrasound-monitor.git
cd infrasound-monitor
cp config.example.toml config.toml       # edit: port = "/dev/ttyUSB0", coordinates, sample_rate

# Full build: acquisition + daily dashboard + Mini PiTFT display + swap + nightly backup
bash deploy/setup.sh --dashboard --display --swap --status --backup user@nas:/infra-backup

# Acquisition-only Pi (analysis on another machine, which pulls the archive over ssh)
bash deploy/setup.sh --display --status --backup /mnt/usb/infra-backup

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

### Find the serial port and confirm the framing

Stop the daemon first (`sudo systemctl stop infra-acquire`); only one program can hold
the port.

```bash
.venv/bin/python -m infrasound_monitor.acquire --list              # list serial ports
.venv/bin/python -m infrasound_monitor.acquire /dev/ttyUSB0 --sniff # watch raw lines
```

You should see one signed integer per line (e.g. `-00123`) at ~51 lines/second — put
that measured rate in `config.toml` as `sample_rate`. Data lands as standard
**miniSEED** in `archive/` (SDS layout) with a `station.xml`, so it works in ObsPy,
Swarm, and the FDSN toolchain.

### `--swap` — a memory safety margin on a 1 GB Pi

The nightly rebuild now peaks at ~300–350 MB (see "What a 1 GB Pi can carry" above),
so on a 1 GB Pi swap is a safety margin rather than a requirement. It also covers the
ad-hoc tools, which are not streamed: `tools/analyze.py` (a PPSD from the raw
archive), and the standalone `tools/report.py` and `tools/tonehunt.py`, which compute
full-resolution tone residuals. `--swap` bumps the swapfile to 2 GB. Each nightly
run only processes the *new* hours, appending them to the grid cache in place. An
acquisition-only Pi doesn't need `--swap`.

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

### `--status` — live status web page

`--status` installs the `infra-status` service running `tools/statusd.py`, a tiny
stdlib web server (no extra dependencies) that serves a live status page so you can
check the monitor from a browser instead of SSHing in. It reuses the same cheap
collectors as the display (last-sample age, level, dominant tone, uptime, today's data,
archive size, disk free, CPU temp, last publish, and a live waveform). Endpoints:

- `/` — an auto-refreshing HTML status page
- `/status.json` — the same data as JSON (for scripts or other dashboards)
- `/healthz` — `ok`/**200** while acquiring, **503** plus a one-line reason when not

`/healthz` reflects *acquisition*, not merely "the web server is up", so pointing an
uptime monitor at it will actually catch a stalled sensor. When something is wrong the
page and `status.json` also carry a plain-language `diagnosis`, plus whether the serial
port still exists, the `infra-acquire` unit's state, and its restart count — enough to
tell "the USB adapter vanished" from "the port is open but the sensor went quiet"
without SSHing in. Those are the two failures that look identical from the outside.

It serves on **port 8080** and has **no authentication**, so reach it only from a
private network — your LAN or a VPN — and never port-forward it to the public internet.
On the same LAN that's `http://<pi-ip>:8080`; remotely, put the Pi on a private VPN and
use its VPN address. Change the port with `tools/statusd.py --port <n>` if 8080 clashes.

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

If an analysis server pulls the archive (the TrueNAS stack does, with no `--delete`, onto
ZFS with snapshots), that pull is your off-Pi backup. A USB stick then serves as a cheap
third copy.

Then `bash deploy/setup.sh --backup /mnt/usb/infra-backup` (or, if already installed,
`sudo systemctl start infra-backup` to test). Confirm the nightly schedule with
`systemctl list-timers 'infra-*'`. Size the drive for your retention — the archive grows
~3.5 GB/year, so a 4 GB stick holds roughly a year.

### Serving the dashboard

The `--dashboard` timer runs `deploy/publish.sh` at 06:15. It extends the PSD grid and
rebuilds `site/index.html` and `site/waterfall_full.html`. It builds them in a scratch
directory and moves the finished pages into place, so a visitor never gets a
half-written page and a failed rebuild leaves the previous day's pages up.

The first run seeds the grid cache (`analysis/grid_full.npz` + `.psd.f32`) from the
archive's first day, so give it at least a few hours of data first. To start the
baseline at a different date, say after moving the sensor, seed it yourself, then
let the timer take over:

```bash
.venv/bin/python tools/refresh.py --start 2026-08-04    # or: rm analysis/grid_full.* and rerun
```

To put the pages on the internet, let Tailscale serve the folder directly. No web
server is needed, and nothing on the Pi needs push credentials. Install Tailscale and
join your tailnet once (the `up` step prints a sign-in link), then enable Funnel:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg "$PWD/site"     # from the project root
tailscale funnel status                    # -> https://<hostname>.<tailnet>.ts.net/
```

This station's dashboard is at `https://infra.tail4c32e5.ts.net/`. The setting persists
across reboots. Funnel relays through Tailscale's edge, so your home IP is never exposed.
Use `tailscale serve` instead of `funnel` to keep it on your tailnet only. The node
needs the `funnel` attribute in the tailnet policy (the default policy grants it to
members). Funnel serves only that folder. **Never funnel the status page (port 8080):**
it has no authentication.

> **Only one machine should build the dashboard.** If the rebuild ever moves (Pi →
> analysis server), disable the Pi's straight away:
> `sudo systemctl disable --now infra-dashboard.timer`.

### Test a new Pi before you move the sensor (avoid a data gap)

There's one sensor and the serial port is exclusive, so you can't read the real stream
on a new Pi while it's still on the old one. But you can validate everything else first with a
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
To rehearse the analysis + publish path, `scp` a few days of real archive from the old machine and
run `tools/report.py` / `tools/dashboard.py` against it. Then `.venv/bin/python tools/doctor.py`
for the overall check (its serial-port test is the only thing that should fail until the
real sensor is attached). Delete `/tmp/testarch` when done.

**The cutover** (single, short gap): stop the old daemon (`sudo systemctl disable --now
infra-acquire`), move the USB adapter to the new Pi, confirm the port
(`ls /dev/ttyUSB*`), set it in `config.toml`, then `sudo systemctl start infra-acquire`.
The daemon records the brief handover as an explicit gap.

### When acquisition stops but nothing looks broken

`Restart=always` only helps if the daemon actually *exits*. Two failures used to leave it
alive and the unit reporting `active (running)` with no data and no log lines: the port
opening but no bytes arriving (dead sensor, unpowered adapter, half-broken cable), and the
adapter vanishing so every reconnect fails. The daemon now gives up on both — after
`--stall-timeout` seconds with no valid sample (default 120), or `--max-reconnects`
consecutive open failures (default 20) — and exits non-zero so systemd restarts it from
scratch, which also re-enumerates the USB device. Pass `0` to either to disable.

If it is still stale after a restart, the port is exclusive, so stop the daemon before
looking at the raw stream — this separates a wedged daemon from dead hardware:

```bash
sudo systemctl stop infra-acquire
timeout 10 .venv/bin/python -m infrasound_monitor.acquire /dev/ttyUSB0 --sniff
sudo systemctl start infra-acquire
```

Numbers scrolling means the sensor is fine. Nothing at all means power-cycle the INFRA20
and replug the adapter; `sudo dmesg | grep -i usb | tail -30` will show a bus-level drop.

> **The adapter may also be powering the sensor.** Infiltec ships the INFRA20 with a
> USB-serial adapter chosen because its handshake lines source enough current to run the
> sensor parasitically (DTR = DB9 pin 4, RTS = pin 7, against GND pin 5). So a replacement
> is *not* a drop-in: what matters is true RS-232 levels **and** handshake-line current,
> not the chip brand. Most "FTDI" products sold today are **TTL-level** (3.3/5 V logic, no
> level shifter) and will neither power nor talk to the sensor; even a genuine RS-232
> adapter built on a bare MAX3232 may source too little current. Measure DTR/RTS on the
> working adapter and match it. The daemon asserts both lines explicitly and disables
> hardware flow control (which would otherwise let the driver toggle them, cutting power
> mid-stream), and `--warmup` defaults to 15 s because closing the port unpowers the
> sensor, so every reconnect is a cold start that has to settle.

Pi specifics:
- **Serial port:** the INFRA20's USB adapter is usually `/dev/ttyUSB0`
  (`ls /dev/ttyUSB* /dev/ttyACM*` to find it). Put it in `config.toml`.
- **First install is slow** — even from piwheels, scipy/obspy are large ARM wheels.
- **Storage:** the INFRA20 writes only ~10 MB/day (~3.5 GB/year), so a 16 GB card holds
  a couple of years after the OS + venv; plan to grow the card or prune before it fills.
  For a long-running station, moving to a USB SSD lifts both the capacity and the SD
  card's write-endurance limit — see "Moving the OS + archive to a USB SSD" below.
- **Headless = no live window.** `tools/live.py` opens a GUI; on a headless Pi use
  `tools/live.py --snapshot live.png`, the TFT panel, or the web dashboard. Analysis
  tools all run headless.
- Reboot once after the first setup so the `dialout`/`spi`/`gpio` groups fully apply.

### Moving the OS + archive to a USB SSD

The archive is a continuous ~10 MB/day write and swap adds more, so on a long-running
station the SD card is the wear item — and it holds the irreplaceable baseline. Cloning
the card to a USB SSD moves the OS, the archive *and* swap onto it in one step and retires
the card entirely, which is tidier than leaving the OS on the card and only the data on
the SSD. It also raises the storage ceiling from a 16 GB card to whatever the SSD is.

**Prerequisite: the board must boot from USB.** A **Pi 3B+** and **Pi 4** can; a plain
**Pi 3B** needs a one-time OTP bit (`program_usb_boot_mode=1`) that is **permanent and
irreversible**. Confirm before starting:

```bash
vcgencmd otp_dump | grep 17:      # 17:3020000a = USB boot enabled
```

A Pi boot disk **cannot be a single partition** — the bootloader reads the kernel from a
FAT32 partition, so you need the small `/boot/firmware` plus an ext4 root. The root can
and should span the whole rest of the disk.

> **Identify the target disk by model, not by device letter.** If a USB backup drive is
> also attached, `/dev/sda` is quite likely *that* drive, not the SSD — and the commands
> below would wipe it. USB enumeration order is not stable across reboots, so re-check
> every time:
>
> ```bash
> lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS
> findmnt /mnt/usb           # whatever this shows is the drive to leave alone
> ```
>
> Substitute your SSD's device for `/dev/sdX` throughout.

```bash
sudo systemctl stop infra-dashboard.timer infra-backup.timer   # whichever are enabled; no data gap

sudo wipefs -a /dev/sdX1 /dev/sdX2 2>/dev/null; sudo wipefs -a /dev/sdX
sudo parted /dev/sdX --script mklabel msdos \
  mkpart primary fat32 1MiB 513MiB \
  mkpart primary ext4 513MiB 100%
sudo mkfs.vfat -F32 /dev/sdX1 && sudo mkfs.ext4 -F /dev/sdX2

sudo mkdir -p /mnt/ssd && sudo mount /dev/sdX2 /mnt/ssd
sudo mkdir -p /mnt/ssd/boot/firmware && sudo mount /dev/sdX1 /mnt/ssd/boot/firmware
```

Copy in **two passes**, so acquisition keeps running through the slow bulk transfer and
the gap is only the second pass plus the reboot (~2–4 minutes, mostly the reboot):

```bash
# PASS 1 -- daemon still running. Moves the bulk; inconsistent files are fine.
sudo rsync -aHAX --one-file-system --info=progress2 / /mnt/ssd/
sudo rsync -aHAX --info=progress2 /boot/firmware/ /mnt/ssd/boot/firmware/

sudo systemctl stop infra-acquire        # the recording gap starts here

# PASS 2 -- only what changed; seconds.
sudo rsync -aHAX --one-file-system --delete --info=progress2 / /mnt/ssd/
sudo rsync -aHAX --delete --info=progress2 /boot/firmware/ /mnt/ssd/boot/firmware/
```

`--one-file-system` keeps rsync out of `/proc`, `/sys`, `/dev` and your mount points; the
second command is needed because `/boot/firmware` is its own FAT partition. Stopping the
daemon before pass 2 lets it close the current miniSEED day file, so the SSD gets a
properly terminated file rather than a torn final record that the daemon would then append
to. `--delete` on pass 2 only, and check your trailing slashes — it is unforgiving if
pointed at the wrong target.

**Repoint the bootloader — after pass 2, or pass 2 overwrites these edits** and the Pi
boots the SSD's kernel while mounting the *SD card's* root, which is silent and confusing:

```bash
sudo blkid /dev/sdX2       # note the PARTUUID
```

On the **SSD copy**, set `root=PARTUUID=…` in `/mnt/ssd/boot/firmware/cmdline.txt`, and
update the `/` and `/boot/firmware` entries in `/mnt/ssd/etc/fstab`. Leave any other fstab
lines (e.g. a `/mnt/usb` backup drive) alone.

Then `sudo sync && sudo shutdown -h now`, **physically remove the SD card**, and power on —
with no card present the Pi falls back to USB, so there is no boot-order setting to change
on a 3B+. Verify before trusting it:

```bash
findmnt /                  # must show your SSD, not /dev/mmcblk0p2
swapon --show
systemctl status infra-acquire
curl -s localhost:8080/status.json      # age_s small, today_mb climbing
```

Let one nightly rebuild run (`deploy/publish.log` should end `[publish] rebuilt`, and
the analysis server's next pull should succeed), then
**keep the SD card untouched** as a rollback — two-minute recovery if the SSD misbehaves,
and a point-in-time snapshot of the archive. Because this is a clone, the hostname, SSH
host keys, VPN identity (and its Funnel setting), `authorized_keys` and systemd units
all carry over unchanged.

On a Pi 3B+ the USB bus is 2.0 and shared with Ethernet, so expect **endurance and
capacity, not speed**. If it won't boot at all, suspect the USB–SATA bridge rather than the
procedure — some are not compatible with Pi 3 USB boot.

> **fstab edits can silently unmount a live filesystem.** systemd generates mount units
> from `/etc/fstab`, and regenerating them can take an unrelated mount down with it — a
> backup drive that quietly disappears is easy to miss. Run `sudo systemctl daemon-reload`
> after any fstab change, and re-check `df -h` for the mounts you expect.

**Later moving the SSD to another Pi.** Almost everything travels with the disk: the
PARTUUIDs don't change, so `cmdline.txt`/`fstab` stay valid, and Raspberry Pi OS ships
kernels and DTBs for every model. The exception is a **Pi 4**, which boots from SPI EEPROM
and may not have USB in its boot order. Updating that generally needs the Pi booted from
something else first, so use the old SD card — with the **sensor unplugged**, or the daemon
will start writing to that card's stale archive and create a divergent copy:

```bash
sudo rpi-eeprom-update -a
sudo raspi-config          # Advanced Options -> Boot Order -> USB Boot
```

Then shut down, remove the card, and move the SSD to a **blue USB 3** port. Check
`ls /dev/ttyUSB*` afterwards — a different USB controller can change enumeration — and use
a proper **USB-C 3 A** supply, since undervoltage on a Pi 4 with an SSD attached causes
intermittent faults that are miserable to diagnose headless.

## Analysis tools

All tools take an archive path (defaults to your configured `archive`) and a date range:

```bash
python -m infrasound_monitor.waterfall archive --start 2026-08-04 --end 2026-09-01 --out waterfall.html --cache analysis/grid_full.npz
python tools/analyze.py    archive --start 2026-08-04 --end 2026-09-01                      # PPSD + dayplot
python tools/tonehunt.py   archive --start 2026-08-04 --end 2026-09-01 --cache analysis/grid_full.npz --night
python tools/report.py     archive --start 2026-08-04 --end 2026-09-01 --cache analysis/grid_full.npz   # station report
python tools/transients.py archive --start 2026-08-04 --end 2026-09-01 --html events/index.html        # event explorer
python tools/live.py                                                                     # live drum view (needs a display)
```

Reuse the same `--cache` grid across tools so re-runs are instant. The analysis tools
are plain Python and also run on a desktop against a copy of the archive.

## Notes

- **One tree.** Everything lives under the project folder: code, `config.toml`, the
  `archive/`, `analysis/`, and generated `site/`. On the acquiring machine, put it on a
  **local** disk (not a network or cloud-synced folder) so the 24/7 daemon writes
  reliably. An analysis server keeps the same shape, with the clone as its dataset root
  and the archive pulled into it.
