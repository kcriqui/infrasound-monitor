"""Reading the archive as contiguous pieces, and the Welch average over them."""
import numpy as np
from obspy import Stream, Trace, UTCDateTime
from scipy import signal

from infrasound_monitor.waveforms import pieces, harmonize
from infrasound_monitor.psd import welch_pieces

FS = 51.42
T0 = UTCDateTime(2026, 9, 1)


def _tr(start, n, fs=FS, value=None):
    data = (np.arange(n) % 97 if value is None else np.full(n, value)).astype(np.int32)
    tr = Trace(data=data)
    tr.stats.sampling_rate = fs
    tr.stats.starttime = start
    return tr


def test_a_false_two_second_break_is_joined_without_filling():
    """The old recorder's 2 s breaks lost no samples, so the piece must hold exactly
    the recorded samples -- no zeros inserted."""
    a = _tr(T0, 1000)
    b = _tr(a.stats.endtime + a.stats.delta + 2.0, 500)
    ps = pieces(Stream([b, a]))                       # out of order on purpose
    assert len(ps) == 1
    assert len(ps[0].data) == 1500
    assert ps[0].start == T0.timestamp


def test_a_real_outage_stays_a_gap():
    a = _tr(T0, 1000)
    b = _tr(a.stats.endtime + 30.0, 500)
    assert [len(p.data) for p in pieces(Stream([a, b]))] == [1000, 500]


def test_mixed_rates_join_and_harmonize_lets_merge_work():
    a = _tr(T0, 1000, fs=51.4200)
    b = _tr(T0 + 1000 / 51.4200, 1000, fs=51.4262)
    assert len(pieces(Stream([a, b]))) == 1
    st = harmonize(Stream([a, b]))
    st.merge(method=1)
    assert len(st) == 1


def test_duplicated_data_is_not_counted_twice():
    a = _tr(T0, 1000)                                # 19.4 s
    dup = _tr(T0 + 5.0, 600)                         # 5 .. 16.7 s: entirely inside a
    tail = _tr(T0 + 15.0, 500)                       # 15 .. 24.7 s: overlaps a's end
    ps = pieces(Stream([a, dup, tail]))
    assert len(ps) == 1
    assert len(ps[0].data) == 1000 + int(round((15.0 + 500 / FS - 1000 / FS) * FS))


def test_welch_over_one_piece_is_scipy_welch():
    x = np.random.default_rng(0).standard_normal(20000)
    f, p = welch_pieces([x], FS, 4096)
    f0, p0 = signal.welch(x, fs=FS, nperseg=4096)
    assert np.allclose(f, f0) and np.allclose(p, p0)


def test_welch_skips_short_pieces_and_weights_by_segments():
    rng = np.random.default_rng(1)
    long_, short = rng.standard_normal(40000), rng.standard_normal(100)
    f, p = welch_pieces([long_, short], FS, 4096)
    assert np.allclose(p, signal.welch(long_, fs=FS, nperseg=4096)[1])
    assert welch_pieces([short], FS, 4096) == (None, None)
