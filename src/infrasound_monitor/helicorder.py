"""Drum / helicorder view of the raw waveform (Phase 3, optional).

This is a thin wrapper over ObsPy's ``Stream.dayplot`` -- the classic seismic
drum-recorder rendering AmaSeis emulated.  It exists so the raw-data view is
available from the same archive, but note Swarm and ObsPy already provide rich
interactive versions; a bespoke UI (full Phase 3) may be unnecessary.
"""
from __future__ import annotations
import argparse
import datetime as dt

from obspy import UTCDateTime
from obspy.clients.filesystem.sds import Client

from .config import StationConfig, DEFAULT_STATION, PA_PER_COUNT


def helicorder(archive, day: dt.date, out_png: str,
               cfg: StationConfig = DEFAULT_STATION,
               interval_min: int = 15, to_pascals: bool = True) -> str:
    client = Client(str(archive))
    t0 = UTCDateTime(day.year, day.month, day.day)
    st = client.get_waveforms(cfg.network, cfg.station, cfg.location,
                              cfg.channel, t0, t0 + 86400)
    if not len(st):
        raise RuntimeError(f"no data for {day}")
    if to_pascals:
        for tr in st:
            tr.data = tr.data.astype("float64") * PA_PER_COUNT
    st.merge(method=1, fill_value=None)
    st.plot(type="dayplot", interval=interval_min, outfile=out_png,
            title=f"{cfg.seed_id}  {day}  (Pa)" if to_pascals else f"{cfg.seed_id}  {day}",
            vertical_scaling_range=None, size=(1000, 1300))
    return out_png


def main(argv=None):
    p = argparse.ArgumentParser(description="Render a helicorder/drum plot for one UTC day.")
    p.add_argument("archive")
    p.add_argument("day", help="YYYY-MM-DD")
    p.add_argument("--out", default="helicorder.png")
    p.add_argument("--interval", type=int, default=15, help="minutes per row")
    a = p.parse_args(argv)
    d = dt.datetime.strptime(a.day, "%Y-%m-%d").date()
    print("wrote", helicorder(a.archive, d, a.out, interval_min=a.interval))


if __name__ == "__main__":
    main()
