#!/usr/bin/env bash
# One-shot Linux / Raspberry Pi setup for the INFRA20 tool.
#
#   bash deploy/setup.sh                              # acquisition daemon only
#   bash deploy/setup.sh --dashboard                  # + daily dashboard rebuild (systemd timer)
#   bash deploy/setup.sh --display                    # + Mini PiTFT status display service
#   bash deploy/setup.sh --swap                       # bump swap to 2 GB (recommended on a 1 GB Pi)
#   bash deploy/setup.sh --backup <DEST>              # + nightly archive backup (rsync to DEST)
#   bash deploy/setup.sh --dashboard --display --swap --backup user@nas:/infra   # the full Pi build
#
# Installs into a project-local virtualenv (.venv), creates config.toml, adds you to the
# 'dialout' group for serial access, and installs systemd units that run at boot and restart
# on failure. Uses sudo for the systemd / group / swap steps.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"
cd "$ROOT"

DASH=0; DISPLAY_TFT=0; SWAP=0; BACKUP_DEST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dashboard) DASH=1 ;;
        --display)   DISPLAY_TFT=1 ;;
        --swap)      SWAP=1 ;;
        --backup)    shift; BACKUP_DEST="${1:-}"
                     [ -n "$BACKUP_DEST" ] || { echo "ERROR: --backup needs a destination"; exit 1; } ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
    shift
done

echo "== INFRA20 setup ==  project: $ROOT"

# 1. Python 3.10+
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), sys.version' \
    || { echo "ERROR: need Python 3.10+"; exit 1; }
echo "  Python: $(python3 --version)"

# 1b. Build prerequisites (Debian/Raspberry Pi OS). --display pulls adafruit-blinka, whose
#     RPi.GPIO / rpi_ws281x deps sometimes have no prebuilt wheel and compile from source --
#     that needs the Python headers (Python.h from python3-dev) and a C toolchain.
if [ "$DISPLAY_TFT" = 1 ] && command -v apt-get >/dev/null; then
    need=""
    dpkg -s python3-dev    >/dev/null 2>&1 || need="$need python3-dev"
    dpkg -s build-essential >/dev/null 2>&1 || need="$need build-essential"
    if [ -n "$need" ]; then
        echo "  installing build prerequisites:$need"
        sudo apt-get update -qq
        sudo apt-get install -y $need
    fi
fi

# 2. Virtualenv + editable install (Raspberry Pi OS is PEP-668 'externally managed', so a
#    venv is required; ARM wheels for numpy/scipy/obspy come from piwheels automatically)
[ -d .venv ] || { echo "  creating virtualenv .venv ..."; python3 -m venv .venv; }
VENV_PY="$ROOT/.venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip -q
EXTRAS="."; [ "$DISPLAY_TFT" = 1 ] && EXTRAS=".[display]"
echo "  installing the package '$EXTRAS' (can take several minutes on a Pi -- scipy/obspy) ..."
"$VENV_PY" -m pip install -e "$EXTRAS"

render() { sed -e "s|__USER__|$USER|g" -e "s|__PROJECT__|$ROOT|g" -e "s|__PYTHON__|$VENV_PY|g" "$1"; }

# 3. config.toml
NEW_CONFIG=0
if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    NEW_CONFIG=1
    echo "  created config.toml -- EDIT it: port = \"/dev/ttyUSB0\", your coordinates, sample_rate"
fi

# 4. Serial access (dialout group)
if ! id -nG "$USER" | grep -qw dialout; then
    echo "  adding $USER to 'dialout' (serial access) -- a reboot makes this fully effective"
    sudo usermod -aG dialout "$USER"
fi

# 4b. Swap (analysis on a 1 GB Pi can OOM without it)
if [ "$SWAP" = 1 ]; then
    if command -v dphys-swapfile >/dev/null; then
        echo "  setting swap to 2048 MB ..."
        sudo dphys-swapfile swapoff || true
        sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon
    else
        echo "  (dphys-swapfile not found -- skipping; install it or configure zram manually)"
    fi
fi

# 5. Acquisition service (boot-start + auto-restart)
echo "  installing systemd service 'infra-acquire' ..."
render deploy/infra-acquire.service | sudo tee /etc/systemd/system/infra-acquire.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable infra-acquire.service
if [ "$NEW_CONFIG" = 1 ]; then
    echo "  service ENABLED for boot but NOT started -- edit config.toml first, then:"
    echo "      sudo systemctl start infra-acquire"
else
    sudo systemctl restart infra-acquire.service
    echo "  service started."
fi

# 6. Optional dashboard timer
if [ "$DASH" = 1 ]; then
    echo "  installing dashboard timer 'infra-dashboard' ..."
    render deploy/infra-dashboard.service | sudo tee /etc/systemd/system/infra-dashboard.service >/dev/null
    sudo cp deploy/infra-dashboard.timer /etc/systemd/system/infra-dashboard.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now infra-dashboard.timer
    echo "  (build the PSD grid cache once before the first run -- see DEPLOY.md)"
fi

# 7. Optional Mini PiTFT status display
if [ "$DISPLAY_TFT" = 1 ]; then
    echo "  enabling SPI + installing display service 'infra-display' ..."
    command -v raspi-config >/dev/null && sudo raspi-config nonint do_spi 0 || true   # 0 = enable
    for grp in spi gpio; do
        if getent group "$grp" >/dev/null && ! id -nG "$USER" | grep -qw "$grp"; then
            sudo usermod -aG "$grp" "$USER"
        fi
    done
    render deploy/infra-display.service | sudo tee /etc/systemd/system/infra-display.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now infra-display.service || true
    echo "  (if the panel stays blank, reboot so SPI + the spi/gpio groups take effect)"
fi

# 8. Optional nightly archive backup
if [ -n "$BACKUP_DEST" ]; then
    echo "  installing nightly archive backup -> $BACKUP_DEST"
    sed -e "s|__USER__|$USER|g" -e "s|__PROJECT__|$ROOT|g" -e "s|__BACKUP_DEST__|$BACKUP_DEST|g" \
        deploy/infra-backup.service | sudo tee /etc/systemd/system/infra-backup.service >/dev/null
    sudo cp deploy/infra-backup.timer /etc/systemd/system/infra-backup.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now infra-backup.timer
    echo "  (first run is 04:30; test now with:  sudo systemctl start infra-backup)"
fi

echo
echo "Done. Useful commands:"
echo "  systemctl status infra-acquire         # is acquisition running?"
echo "  journalctl -u infra-acquire -f         # live acquisition log"
[ "$DISPLAY_TFT" = 1 ] && echo "  journalctl -u infra-display -f         # display log"
[ -n "$BACKUP_DEST" ] && echo "  systemctl start infra-backup           # run a backup now"
echo "  .venv/bin/python tools/doctor.py       # verify the whole setup"
