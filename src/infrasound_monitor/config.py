"""Central configuration and physical constants for the INFRA20 pipeline.

Station-specific settings — identity, coordinates, timezone, serial port, archive
path — are read from a user-editable ``config.toml`` so the code stays generic and
deployable. Copy ``config.example.toml`` to ``config.toml`` and edit it; ``config.toml``
is git-ignored, so your settings never get committed or clobbered by ``git pull``.
Anything not set there falls back to the generic defaults below.

The physical calibration constants are INFRA20 hardware specs (the same for every
unit) and are not user-configurable.

Config file search order: ``$INFRASOUND_CONFIG`` → ``./config.toml`` (cwd) →
``<project-root>/config.toml``.
"""
from __future__ import annotations
import os
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path

# ---- Infiltec INFRA20 physical calibration (hardware spec; not user config) ----
PA_PER_COUNT = 0.001          # 1000 counts == 1.000 Pascal (factory spec, ~+/-20%)
FULL_SCALE_PA = 25.0          # +/- range
PASSBAND_HZ = (0.05, 20.0)    # 8-pole elliptic anti-alias, 20 Hz corner
NOISE_FLOOR_PA = 0.020        # ~20 counts electronic noise (~60 dB SPL re 20 uPa)
P_REF_PA = 20e-6              # reference pressure for dB SPL

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _find_config() -> Path | None:
    env = os.environ.get("INFRASOUND_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    for c in (Path.cwd() / "config.toml", _PROJECT_ROOT / "config.toml"):
        if c.exists():
            return c
    return None


def _load_config() -> dict:
    p = _find_config()
    if not p:
        return {}
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except Exception as e:                       # malformed file -> warn, use defaults
        warnings.warn(f"could not read {p}: {e}; using built-in defaults")
        return {}


_CFG = _load_config()
_ST = _CFG.get("station", {})
_ACQ = _CFG.get("acquisition", {})

# ---- Timing (overridable in config.toml [station]) ----
NOMINAL_FS = float(_ST.get("sample_rate", 51.4287))     # measured samples/sec for the unit
# Local wall clock = UTC + this. A single number can't track DST; night-based
# analysis derives the quiet window from the data rather than trusting this.
UTC_OFFSET_HOURS = float(_ST.get("utc_offset_hours", -8))


@dataclass
class StationConfig:
    """FDSN source identifiers for the sensor.

    Generic defaults; a deployment overrides them via ``config.toml``. Network codes
    1-9, X*, Y*, Z* are reserved for temporary/local deployments (appropriate for a
    private station not registered with the FDSN). Channel SDF = S (short-period) +
    D (pressure) + F (infrasound).
    """
    network: str = "XX"
    station: str = "INFRA"        # <= 5 chars
    location: str = "00"
    channel: str = "SDF"
    site_name: str = "My infrasound station"
    latitude: float = 0.0
    longitude: float = 0.0
    elevation: float = 0.0         # metres
    sensor_description: str = "Infiltec INFRA20 microbarometer"

    @property
    def seed_id(self) -> str:
        return f"{self.network}.{self.station}.{self.location}.{self.channel}"


def _station_from_config() -> StationConfig:
    s = _ST
    return StationConfig(
        network=s.get("network", "XX"),
        station=s.get("station", "INFRA"),
        location=s.get("location", "00"),
        channel=s.get("channel", "SDF"),
        site_name=s.get("site_name", "My infrasound station"),
        latitude=float(s.get("latitude", 0.0)),
        longitude=float(s.get("longitude", 0.0)),
        elevation=float(s.get("elevation", 0.0)),
    )


DEFAULT_STATION = _station_from_config()


def _resolve(path: str) -> str:
    """Resolve a config path: absolute as-is, else relative to the project root."""
    p = Path(path)
    return str(p if p.is_absolute() else _PROJECT_ROOT / p)


# ---- Acquisition / paths (overridable in config.toml [acquisition]) ----
SERIAL_PORT = _ACQ.get("port", "COM3")
ARCHIVE_DIR = _resolve(_ACQ.get("archive", "archive"))
LIVE_FILE = _resolve(_ACQ.get("live_file", "live.npz"))
