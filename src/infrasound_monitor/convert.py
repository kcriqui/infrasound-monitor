"""Convert legacy AmaSeis ``.Z`` files into a standard miniSEED (SDS) archive.

Design decisions
----------------
* Samples are written as **raw integer counts** (Steim2-compressed).  The
  counts->Pascals calibration lives in the StationXML (see :mod:`metadata`).
* Consecutive hours are concatenated into **gap-free, single-rate traces** at the
  nominal sample rate, anchored to the first hour's UTC top.  A run is broken
  (leaving an explicit gap) wherever an hour is missing.  This is exactly what a
  real datalogger produces -- one rate, no overlaps -- and merges cleanly in any
  tool.  AmaSeis only bins to the hour, so within a run individual sample times
  may differ from wall-clock by <~0.3 s; irrelevant for infrasound analysis.
* Output follows the SeisComP Data Structure (SDS), which ObsPy, Swarm, and the
  Raspberry Shake toolchain all read natively:
  ``<root>/<YYYY>/<NET>/<STA>/<CHA>.D/<NET>.<STA>.<LOC>.<CHA>.D.<YYYY>.<JJJ>``
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np
from obspy import Trace, Stream, UTCDateTime

from .amaseis import iter_hour_files, read_counts, HourFile
from .config import StationConfig, DEFAULT_STATION, NOMINAL_FS
from .metadata import build_inventory

SECONDS_PER_HOUR = 3600.0


def _new_trace(counts, start, cfg: StationConfig, fs: float) -> Trace:
    tr = Trace(data=np.asarray(counts, dtype=np.int32))
    tr.stats.network = cfg.network
    tr.stats.station = cfg.station
    tr.stats.location = cfg.location
    tr.stats.channel = cfg.channel
    tr.stats.sampling_rate = fs
    tr.stats.starttime = UTCDateTime(start)
    return tr


def _runs_to_stream(hours: list[HourFile], cfg: StationConfig, fs: float,
                    rate_tol: float, stats: dict) -> Stream:
    """Concatenate consecutive UTC hours into gap-free single-rate traces.

    A new trace is started whenever the previous hour is missing (a real gap).
    """
    st = Stream()
    run_counts: list = []
    run_start = None
    prev_hour = None
    for hf in hours:
        try:
            counts = read_counts(hf.path)
        except Exception as e:
            print(f"  !! skip {hf.path}: {e}"); stats["warnings"] += 1; continue
        eff = len(counts) / SECONDS_PER_HOUR
        if abs(eff - fs) / fs > rate_tol:
            print(f"  ?? {hf.path.name}: effective rate {eff:.3f} deviates "
                  f"{abs(eff-fs)/fs*100:.1f}% (short/partial hour?)")
            stats["warnings"] += 1
        contiguous = (prev_hour is not None and hf.hour == prev_hour + 1)
        if not contiguous and run_counts:
            st += _new_trace(np.concatenate(run_counts), run_start, cfg, fs)
            run_counts = []
        if not run_counts:
            run_start = hf.start_utc
        run_counts.append(counts)
        prev_hour = hf.hour
        stats["files_in"] += 1
        stats["samples"] += len(counts)
    if run_counts:
        st += _new_trace(np.concatenate(run_counts), run_start, cfg, fs)
    return st


def sds_path(root: Path, cfg: StationConfig, year: int, julday: int) -> Path:
    return (root / f"{year}" / cfg.network / cfg.station / f"{cfg.channel}.D" /
            f"{cfg.network}.{cfg.station}.{cfg.location}.{cfg.channel}.D.{year}.{julday:03d}")


def convert(amaseis_root, sds_root, cfg: StationConfig = DEFAULT_STATION,
            encoding: str = "STEIM2", rate_tol: float = 0.05, verbose: bool = True) -> dict:
    sds_root = Path(sds_root)
    # group hour files by (year, julday) so each SDS file is one UTC day
    by_day: dict[tuple[int, int], list[HourFile]] = defaultdict(list)
    for hf in iter_hour_files(amaseis_root):
        t = UTCDateTime(hf.start_utc)
        by_day[(t.year, t.julday)].append(hf)

    stats = {"files_in": 0, "day_files_out": 0, "samples": 0, "warnings": 0}
    for (year, julday), hours in sorted(by_day.items()):
        st = _runs_to_stream(hours, cfg, NOMINAL_FS, rate_tol, stats)
        if not st:
            continue
        out = sds_path(sds_root, cfg, year, julday)
        out.parent.mkdir(parents=True, exist_ok=True)
        st.write(str(out), format="MSEED", encoding=encoding, reclen=512)
        stats["day_files_out"] += 1
        if verbose:
            print(f"  {year}.{julday:03d}: {len(st):2d} hrs -> {out.name}")

    # always (re)write the station metadata next to the archive
    inv = build_inventory(cfg)
    inv_path = sds_root / "station.xml"
    sds_root.mkdir(parents=True, exist_ok=True)
    inv.write(str(inv_path), format="STATIONXML")
    stats["stationxml"] = str(inv_path)
    return stats


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert an AmaSeis .Z tree to a miniSEED SDS archive + StationXML.")
    p.add_argument("amaseis_root", help="root containing YYYY/MM/DD/HH.Z (e.g. Y:\\AmaSeis)")
    p.add_argument("sds_root", help="output SDS archive root")
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    p.add_argument("--encoding", default="STEIM2")
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    print(f"converting {a.amaseis_root} -> {a.sds_root}  ({cfg.seed_id})")
    stats = convert(a.amaseis_root, a.sds_root, cfg, encoding=a.encoding)
    print(f"\nDONE  {stats['files_in']} hour-files -> {stats['day_files_out']} day-files, "
          f"{stats['samples']:,} samples, {stats['warnings']} warnings")
    print(f"      metadata: {stats['stationxml']}")


if __name__ == "__main__":
    main()
