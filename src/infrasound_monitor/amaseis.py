"""Reader for the legacy AmaSeis ``.Z`` hourly file format.

Format (reverse-engineered and verified against a full month of files):

    offset 0 : uint32 little-endian  = N, the number of samples
    offset 4 : N * int16 little-endian = the samples, in raw sensor counts

Files live at ``<root>/<YYYY>/<MM>/<DD>/<HH>.Z`` where ``HH`` is the **UTC**
hour.  Each file holds ~one wall-clock hour of data, so the effective sample
rate of a file is ``N / 3600`` (~51.43 sps on this unit).
"""
from __future__ import annotations
import struct
import datetime as dt
from pathlib import Path
from typing import Iterator, NamedTuple

import numpy as np

HEADER = struct.Struct("<I")   # uint32 sample count


class HourFile(NamedTuple):
    path: Path
    start_utc: dt.datetime      # tz-aware UTC, top of the hour
    hour: int                   # UTC hour 0..23


def read_counts(path: str | Path) -> np.ndarray:
    """Return the raw int16 sensor counts stored in an AmaSeis ``.Z`` file."""
    raw = Path(path).read_bytes()
    (n,) = HEADER.unpack_from(raw, 0)
    expected = HEADER.size + 2 * n
    if len(raw) < expected:
        raise ValueError(
            f"{path}: header claims {n} samples ({expected} bytes) "
            f"but file is only {len(raw)} bytes"
        )
    return np.frombuffer(raw, dtype="<i2", count=n, offset=HEADER.size).astype(np.int32)


def sample_count(path: str | Path) -> int:
    """Read just the 4-byte header (cheap) and return the sample count."""
    with open(path, "rb") as fh:
        (n,) = HEADER.unpack(fh.read(HEADER.size))
    return n


def iter_hour_files(root: str | Path) -> Iterator[HourFile]:
    """Yield every ``<root>/YYYY/MM/DD/HH.Z`` file, sorted by UTC start time."""
    root = Path(root)
    found: list[HourFile] = []
    for zpath in root.glob("[12][0-9][0-9][0-9]/[0-1][0-9]/[0-3][0-9]/[0-2][0-9].Z"):
        try:
            day = zpath.parent
            y, m, d = int(day.parent.parent.name), int(day.parent.name), int(day.name)
            hh = int(zpath.stem)
            start = dt.datetime(y, m, d, hh, tzinfo=dt.timezone.utc)
        except (ValueError, IndexError):
            continue
        found.append(HourFile(zpath, start, hh))
    found.sort(key=lambda hf: hf.start_utc)
    yield from found
