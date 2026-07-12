"""Central configuration and physical constants for the INFRA20 pipeline.

Values are collected here so acquisition, conversion, metadata, and display
all agree on the same station identity, calibration, and units.
"""
from dataclasses import dataclass, field

# ---- Infiltec INFRA20 physical calibration (source: infiltec.com/Infrasound@home) ----
PA_PER_COUNT = 0.001          # 1000 counts == 1.000 Pascal (factory spec, ~+/-20%)
FULL_SCALE_PA = 25.0          # +/- range
PASSBAND_HZ = (0.05, 20.0)    # 8-pole elliptic anti-alias, 20 Hz corner
NOISE_FLOOR_PA = 0.020        # ~20 counts electronic noise (~60 dB SPL re 20 uPa)
P_REF_PA = 20e-6              # reference pressure for dB SPL

# ---- Timing ----
NOMINAL_FS = 51.4287          # measured samples/sec on this unit (nominal ~50)
# Local wall clock = UTC + this. San Jose is America/Los_Angeles: PDT (-7, summer)
# / PST (-8, winter). A single constant can't track DST, so night-based analysis
# derives the quiet window empirically from the data rather than trusting this.
UTC_OFFSET_HOURS = -7   # PDT (summer); previously -6, which was wrong for this site


@dataclass
class StationConfig:
    """FDSN source identifiers for this sensor.

    Network codes 1-9, X*, Y*, Z* are reserved for temporary/local deployments,
    which is appropriate for a private station not registered with the FDSN.
    Channel SDF = S (short-period, 10-80 sps) + D (pressure) + F (infrasound).
    """
    network: str = "XX"           # reserved test code; request an FDSN temp code before sharing
    station: str = "INFRA"        # <= 5 chars
    location: str = "00"
    channel: str = "SDF"
    site_name: str = "San Jose, CA (INFRA20)"
    latitude: float = 37.428581   # deg N
    longitude: float = -121.971208  # deg E (121.971208 W)
    elevation: float = 0.5         # metres (USGS ground elevation; N. San Jose / Alviso, ~sea level)
    sensor_description: str = "Infiltec INFRA20 microbarometer"

    @property
    def seed_id(self) -> str:
        return f"{self.network}.{self.station}.{self.location}.{self.channel}"


DEFAULT_STATION = StationConfig()
