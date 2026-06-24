# SPDX-License-Identifier: GPL-3.0-or-later
"""A GRC batch run retains the WHOLE trace in the waveform, not just the tail.

Bug: a bounded GRC batch ran the whole burst server-side, but the per-sample
refreshes trimmed the TraceModel to the rolling window (_LIVE_TRACE_MAX), so only
the last window's worth survived — the user saw the END of the batch, never the
start. The fix: while a GRC server hosts the chip (_server_batch_retain_all), every
refresh retains all drained events (no trim). This locks in that decision.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.trace_model import TraceModel
from ui import sim_controller as sc


def _fake_event(t_ns):
    # A minimal exec_tick event the TraceModel normaliser accepts.
    return {"kind": "exec_tick", "cell_id": 0, "time_ns": float(t_ns)}


class _Chip:
    """Drainable chip stub: get_trace() returns the recorded events, clear empties."""
    def __init__(self, events):
        self._events = list(events)

    def get_trace(self):
        return list(self._events)

    def clear_trace(self):
        self._events = []


class _Engine:
    def __init__(self, events):
        self.chip = _Chip(events)

    def clear_trace(self):
        self.chip.clear_trace()


class _RetentionHarness:
    """The minimal slice of SimController state that refresh_debug_from_chip reads,
    so we can exercise the retain-all decision directly (no real chip/server)."""

    # borrow the real methods under test
    refresh_debug_from_chip = sc.SimController.refresh_debug_from_chip
    _states_from_events = sc.SimController._states_from_events
    _trace_scan_reset = sc.SimController._trace_scan_reset

    def __init__(self, events, *, retain_all):
        self.engine = _Engine(events)
        self.trace_model = TraceModel()
        self._width = 10
        self._sim_chip = 0
        self._live_trace_max = 50          # tiny window to force the bug if present
        self._server_batch_retain_all = retain_all
        self._last_server_refresh = 0.0
        # signals the method emits — stub them out
        for name in ("cell_states", "handshakes", "trace_updated",
                     "cell_state_refreshed"):
            setattr(self, name, _Sig())

    def _trace_scan_reset(self):   # noqa: D401 - simple stub
        pass


class _Sig:
    def emit(self, *a, **k):
        pass


N = 500   # far more than the 50-event window


def test_streaming_trims_to_window():
    """An interactive stream (retain_all False) keeps only the rolling window."""
    h = _RetentionHarness([_fake_event(i) for i in range(N)], retain_all=False)
    h.refresh_debug_from_chip(force=True)
    assert len(h.trace_model.transactions) == h._live_trace_max, \
        "streaming must keep only the most-recent window"


def test_server_batch_retains_everything():
    """A GRC batch (retain_all True) keeps ALL events — start to end."""
    h = _RetentionHarness([_fake_event(i) for i in range(N)], retain_all=True)
    h.refresh_debug_from_chip(force=True)
    assert len(h.trace_model.transactions) == N, \
        "a bounded GRC batch must retain the WHOLE trace, not just the tail"
    times = [t.time_ns for t in h.trace_model.transactions]
    assert times[0] == 0.0, "the START of the batch must be present (was being dropped)"
    assert times[-1] == float(N - 1)


def test_server_cap_exceeds_live_cap():
    """The server-mode chip-side trace cap must be far larger than the streaming
    cap so a whole burst isn't silently dropped mid-batch (hard cap, not a ring)."""
    assert sc._SERVER_CHIP_CAP > sc._LIVE_CHIP_CAP
