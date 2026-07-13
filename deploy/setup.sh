#!/usr/bin/env bash
# One-shot Linux / Raspberry Pi setup for the INFRA20 tool.
#
#   bash deploy/setup.sh              # install + enable the acquisition daemon
#   bash deploy/setup.sh --dashboard  # also install the daily dashboard rebuild (systemd timer)
#
# Installs into a project-local virtualenv (.venv), creates config.toml, adds you to the
# 'dialout' group for serial access, and installs a systemd service that runs at boot and
# restarts on failure. Uses sudo for the systemd/group steps.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"
cd "$ROOT"
DASH=0; [ "${1:-}" = "--dashboard" ] && DASH=1

echo "== INFRA20 setup ==  project: $ROOT"

# 1. Python 3.10+
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), sys.version' \
    || { echo "ERROR: need Python 3.10+"; exit 1; }
echo "  Python: $(python3 --version)"

# 2. Virtualenv + editable install (Raspberry Pi OS is PEP-668 'externally managed',
#    so a venv is required; ARM wheels come from piwheels automatically)
[ -d .venv ] || { echo "  creating virtualenv .venv ..."; python3 -m venv .venv; }
VENV_PY="$ROOT/.venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip -q
echo "  installing the package (can take several minutes on a Pi -- scipy/obspy) ..."
"$VENV_PY" -m pip install -e .

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

# 5. systemd service (boot-start + auto-restart)
render() { sed -e "s|__USER__|$USER|g" -e "s|__PROJECT__|$ROOT|g" -e "s|__PYTHON__|$VENV_PY|g" "$1"; }
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

echo
echo "Done. Useful commands:"
echo "  systemctl status infra-acquire        # is it running?"
echo "  journalctl -u infra-acquire -f        # live log"
echo "  sudo systemctl restart infra-acquire  # after editing config.toml"
