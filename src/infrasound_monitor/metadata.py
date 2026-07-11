"""Build a StationXML inventory describing the INFRA20 channel + calibration.

We follow the seismological convention: sample values are stored as raw integer
*counts*, and the counts->Pascals calibration lives here in the response.  A flat
in-band sensitivity of ``1 / PA_PER_COUNT`` counts/Pa is used; this reproduces the
factory 0.001 Pa/count scaling exactly.  (The INFRA20's 0.05 Hz high-pass and 20 Hz
anti-alias corners are not yet modelled as poles/zeros -- see README.)
"""
from __future__ import annotations
import argparse
import datetime as dt

from obspy import UTCDateTime
from obspy.core.inventory import Inventory, Network, Station, Channel, Site
from obspy.core.inventory.response import Response

from .config import (
    StationConfig, DEFAULT_STATION, PA_PER_COUNT, NOMINAL_FS, FULL_SCALE_PA,
)

COUNTS_PER_PA = 1.0 / PA_PER_COUNT   # 1000 counts / Pa


def build_response() -> Response:
    """Flat pressure response: 1000 counts/Pa, input Pa, output counts."""
    resp = Response.from_paz(
        zeros=[], poles=[],
        stage_gain=COUNTS_PER_PA,
        stage_gain_frequency=1.0,
        input_units="PA",
        output_units="COUNTS",
        normalization_frequency=1.0,
        normalization_factor=1.0,
    )
    # annotate units for readability in StationXML
    resp.instrument_sensitivity.input_units_description = "Pressure in Pascals"
    resp.instrument_sensitivity.output_units_description = "Digitizer counts"
    return resp


def build_inventory(cfg: StationConfig = DEFAULT_STATION,
                    start: dt.datetime | None = None) -> Inventory:
    start_utc = UTCDateTime(start) if start else UTCDateTime("2026-04-09T00:00:00")
    resp = build_response()

    chan = Channel(
        code=cfg.channel, location_code=cfg.location,
        latitude=cfg.latitude, longitude=cfg.longitude,
        elevation=cfg.elevation, depth=0.0,
        sample_rate=NOMINAL_FS,
        clock_drift_in_seconds_per_sample=0.0,
        start_date=start_utc,
        response=resp,
    )
    chan.dip, chan.azimuth = -90.0, 0.0   # pressure: +dP -> +signal (up-going convention)
    chan.sensor = _equipment(cfg.sensor_description)

    sta = Station(
        code=cfg.station, latitude=cfg.latitude, longitude=cfg.longitude,
        elevation=cfg.elevation, site=Site(name=cfg.site_name),
        creation_date=start_utc, channels=[chan],
    )
    net = Network(code=cfg.network, stations=[sta],
                  description="Private infrasound station (INFRA20)")
    return Inventory(networks=[net], source="infrasound-monitor")


def _equipment(description: str):
    from obspy.core.inventory import Equipment
    return Equipment(type="Microbarometer", description=description,
                     manufacturer="Infiltec", model="INFRA20")


def main(argv=None):
    p = argparse.ArgumentParser(description="Write a StationXML file for the INFRA20 station.")
    p.add_argument("-o", "--out", default="station.xml")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--lat", type=float, default=DEFAULT_STATION.latitude)
    p.add_argument("--lon", type=float, default=DEFAULT_STATION.longitude)
    p.add_argument("--elevation", type=float, default=DEFAULT_STATION.elevation)
    p.add_argument("--site", default=DEFAULT_STATION.site_name)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station, location=a.location,
                        latitude=a.lat, longitude=a.lon, elevation=a.elevation,
                        site_name=a.site)
    inv = build_inventory(cfg)
    inv.write(a.out, format="STATIONXML")
    print(f"wrote {a.out}  ({cfg.seed_id}, {COUNTS_PER_PA:.0f} counts/Pa)")


if __name__ == "__main__":
    main()
