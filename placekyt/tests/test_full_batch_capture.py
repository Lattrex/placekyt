"""Full-batch trace capture (no rolling-window trim) for one-shot server batches.

A GRC-server batch run drives one bounded burst through simKYT (~130k events for a
319-sample coherent-RX burst). The streaming path keeps only the most-recent
_LIVE_TRACE_MAX events (a rolling window), which TRIMS the START of a single batch —
hiding startup conditions. ``full_capture`` keeps the whole batch so start AND end
are traceable. This tests the TraceModel append/trim contract that backs it.
"""

from engine.trace_model import TraceModel


def _events(n, t0=0):
    """n synthetic port-capture events on x16_out at increasing times."""
    return [{"kind": "port_capture", "port_name": "x16_out", "cell_id": 0,
             "time_ns": float(t0 + i), "value": i & 1} for i in range(n)]


def test_streaming_trims_to_window():
    tm = TraceModel()
    tm.append_live(0, _events(50000), 10)
    tm.trim_to(20000)
    # The streaming window keeps only the tail.
    assert len(tm.transactions) <= 20000
    # The LATEST events survive; the earliest are trimmed.
    times = [t.time_ns for t in tm.transactions]
    assert max(times) >= 49000          # tail kept
    assert min(times) > 1000            # head trimmed away (startup lost)


def test_full_capture_keeps_whole_batch():
    tm = TraceModel()
    # full_capture path: append ALL events, NO trim.
    tm.append_live(0, _events(50000), 10)
    # (no trim_to call — this is what refresh_debug_from_chip does for a batch)
    assert len(tm.transactions) == 50000
    times = [t.time_ns for t in tm.transactions]
    assert min(times) == 0              # START preserved (startup conditions)
    assert max(times) == 49999          # END preserved
