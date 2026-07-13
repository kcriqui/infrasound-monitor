#!/usr/bin/env python3
"""Refresh the published site: extend the PSD grid to now, then rebuild the pages.

Intended for a daily scheduled run.  Incrementally appends new hours to the grid
cache (fast), then regenerates the dashboard (index.html) and the interactive
waterfall into an output directory ready to publish to a static host.

    python tools/refresh.py "C:/Users/you/infra-archive" \
        --cache analysis/grid_full.npz --out-dir C:/Users/you/infra-site
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

from infrasound_monitor.config import (StationConfig, DEFAULT_STATION,
                                        ARCHIVE_DIR, PROJECT_ROOT)
from infrasound_monitor.psd import update_grid
from infrasound_monitor import waterfall
import dashboard          # tools/ sibling (tools is on sys.path when run as a script)


def main(argv=None):
    p = argparse.ArgumentParser(description="Refresh grid + rebuild the published pages.")
    p.add_argument("archive", nargs="?", default=None, help="SDS archive (default: config)")
    p.add_argument("--cache", default=None,
                   help="PSD grid .npz to extend (default: <project>/analysis/grid_full.npz)")
    p.add_argument("--out-dir", default=None,
                   help="output dir for index.html + waterfall_full.html (default: <project>/site)")
    p.add_argument("--nperseg", type=int, default=8192)
    p.add_argument("--network", default=DEFAULT_STATION.network)
    p.add_argument("--station", default=DEFAULT_STATION.station)
    p.add_argument("--location", default=DEFAULT_STATION.location)
    p.add_argument("--channel", default=DEFAULT_STATION.channel)
    a = p.parse_args(argv)
    cfg = StationConfig(network=a.network, station=a.station,
                        location=a.location, channel=a.channel)
    archive = a.archive or ARCHIVE_DIR
    a.out_dir = a.out_dir or str(Path(PROJECT_ROOT) / "site")
    a.cache = a.cache or str(Path(PROJECT_ROOT) / "analysis" / "grid_full.npz")

    cache = Path(a.cache)
    if not cache.exists():
        raise SystemExit(f"cache {cache} not found -- build it once with infra-waterfall --cache first")

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] refreshing {cache.name} ...")
    grid, n_new = update_grid(cache, archive, cfg, nperseg=a.nperseg)
    start = dt.datetime.fromisoformat(str(grid["start"]))
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)

    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    # interactive waterfall first, so the dashboard's link resolves in the same dir
    waterfall.build(archive, start, end, out / "waterfall_full.html", cfg, cache=str(cache))
    dashboard.build(archive, start, end, out / "index.html", cfg, cache=str(cache),
                    nperseg=a.nperseg)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] done (+{n_new} new hours) -> {out}")


if __name__ == "__main__":
    main()
