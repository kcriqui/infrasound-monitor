"""Read the archive as runs of contiguous samples ("pieces").

The analysis must not invent data.  ``Stream.merge(fill_value=0)`` does: it pads
every break between traces with zeros, and before 2026-11 the recorder left a
false 2 s break every few hours (its clock model drifted against the sensor's;
no samples were missing).  Zeros there add a step and a burst of broadband
energy to that hour.

So readers here join traces whose break is shorter than ``JOIN_TOL_S`` (those
are timing corrections, not missing data: a real outage lasts at least the
recorder's 15 s warm-up) and keep everything longer as a real gap, never filled.

Traces may also differ slightly in sample rate: the old archive is at 51.4287, the
current one at the configured rate, and from 2026-11-12 to 11-14 the recorder wrote
each segment's measured rate.  ObsPy's ``merge`` refuses mixed rates, so plotting
code that needs one trace should call :func:`harmonize` first.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np

JOIN_TOL_S = 2.5


@dataclass
class Piece:
    start: float          # epoch seconds of the first sample
    fs: float             # header rate of the piece's first trace
    data: np.ndarray      # raw counts

    @property
    def end(self) -> float:
        return self.start + len(self.data) / self.fs


def pieces(st, join_tol: float = JOIN_TOL_S) -> list[Piece]:
    """Group a Stream's traces into contiguous pieces, oldest first.

    Traces starting within ``join_tol`` of the previous piece's end are appended to
    it.  A trace overlapping the previous piece by more than that has its overlap
    dropped, so no sample is counted twice."""
    trs = sorted((tr for tr in st if tr.stats.npts),
                 key=lambda tr: tr.stats.starttime.timestamp)
    out: list[Piece] = []
    parts: list[np.ndarray] = []
    start = end = fs = 0.0
    for tr in trs:
        s = tr.stats.starttime.timestamp
        rate = tr.stats.sampling_rate
        data = np.asarray(tr.data)
        if parts and s < end - join_tol:               # big overlap: keep only the new tail
            k = int(np.ceil((end - s) * rate))
            if k >= len(data):
                continue
            data, s = data[k:], s + k / rate
        if parts and abs(s - end) < join_tol:
            parts.append(data)
            end = s + len(data) / rate         # this trace's own end: steps don't add up
            continue
        if parts:
            out.append(Piece(start, fs, np.concatenate(parts)))
        parts, start, fs, end = [data], s, rate, s + len(data) / rate
    if parts:
        out.append(Piece(start, fs, np.concatenate(parts)))
    return out


def get_pieces(client, cfg, t0, t1, join_tol: float = JOIN_TOL_S) -> list[Piece]:
    """Contiguous pieces of ``cfg``'s channel in [t0, t1) from an SDS ``client``."""
    st = client.get_waveforms(cfg.network, cfg.station, cfg.location, cfg.channel,
                              t0, t1, merge=False)
    return pieces(st, join_tol)


def harmonize(st):
    """Give every trace the stream's median rate, so ``Stream.merge`` accepts them.

    For display only: rates differ by ~1e-4 at most, which shifts sample times
    within a trace by well under a second per hour."""
    rates = [tr.stats.sampling_rate for tr in st]
    if rates:
        fs = float(np.median(rates))
        for tr in st:
            if abs(tr.stats.sampling_rate / fs - 1) < 1e-3:
                tr.stats.sampling_rate = fs
    return st
