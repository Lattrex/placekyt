# SPDX-License-Identifier: GPL-3.0-or-later
"""A large batch-complete refresh is ingested in bounded SLICES (GUI-freeze fix).

Bug: on a batch-complete server_activity, SimController.refresh_debug_from_chip
drained the whole burst (hundreds of thousands of events) and normalised/appended
every one SYNCHRONOUSLY on the GUI thread — blocking the Qt event loop for 5–15 s
(no repaint, no input). The fix processes a LARGE full_capture drain in bounded
slices across QTimer.singleShot(0, …) ticks so the event loop runs between slices
and the window stays responsive. This locks in that decision and, critically, that
the FINAL TraceModel state is IDENTICAL to the old synchronous path.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtWidgets import QApplication

from engine.trace_model import TraceModel
from ui import sim_controller as sc


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication([])
    yield app


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


class _Sig:
    def emit(self, *a, **k):
        pass


class _Harness:
    """The minimal slice of SimController state the refresh path reads, plus the
    chunk-job machinery, so we can exercise the chunked ingest directly (no real
    chip/server) while a QApplication pumps the QTimer.singleShot ticks."""

    refresh_debug_from_chip = sc.SimController.refresh_debug_from_chip
    _apply_refresh_slice = sc.SimController._apply_refresh_slice
    _finalize_refresh = sc.SimController._finalize_refresh
    _run_chunk_slice = sc.SimController._run_chunk_slice
    _states_from_events = sc.SimController._states_from_events
    _steps_from_events = sc.SimController._steps_from_events

    def __init__(self, events):
        self.engine = _Engine(events)
        self.trace_model = TraceModel()
        self._width = 10
        self._sim_chip = 0
        self._live_trace_max = 50
        self._server_batch_retain_all = True
        self._last_server_refresh = 0.0
        self._pending_trace_reset = False
        self._trace_time_origin = None
        self._animate_cells = False        # chunked path never animates
        self._chunk_job = None
        self._chunk_pending = False
        for name in ("cell_states", "cell_faces", "handshakes", "trace_updated",
                     "cell_state_refreshed"):
            setattr(self, name, _Sig())

    def _lockstep_active(self):
        return False

    def _trace_scan_reset(self):
        pass


def _pump(qapp, controller):
    """Drive the QTimer.singleShot(0, …) slice callbacks to completion."""
    import time
    deadline = time.monotonic() + 10.0
    while controller._chunk_job is not None and time.monotonic() < deadline:
        qapp.processEvents()
    # A final flush so any tail singleShot (post-job pending refresh) runs.
    qapp.processEvents()


def test_large_batch_is_processed_in_bounded_slices(_qapp, monkeypatch):
    """A large full_capture batch is normalised/appended in MULTIPLE bounded
    slices (no single append_live call ingests all N events), and after all
    slices complete the TraceModel EXACTLY equals a one-shot append of the batch."""
    N = _CHUNK = sc._CHUNK_SIZE
    total = _CHUNK * 3 + 137        # spans 4 slices, last one partial
    events = [_fake_event(i) for i in range(total)]

    # Spy on the sizes handed to append_live.
    sizes = []
    orig_append = TraceModel.append_live

    def _spy(self, chip, raw_events, width):
        sizes.append(len(list(raw_events)))
        return orig_append(self, chip, raw_events, width)

    monkeypatch.setattr(TraceModel, "append_live", _spy)

    h = _Harness(events)
    h.refresh_debug_from_chip(force=True, full_capture=True)
    # A chunk job must be in flight (chunked, not synchronous).
    assert h._chunk_job is not None, "a large batch must take the chunked path"
    _pump(_qapp, h)
    assert h._chunk_job is None, "the chunk job must complete"

    # (a) processed in multiple bounded slices — none ingests the whole batch.
    assert len(sizes) >= 2, f"expected multiple slices, got sizes={sizes}"
    assert max(sizes) <= sc._CHUNK_SIZE, \
        f"no slice may exceed CHUNK_SIZE ({sc._CHUNK_SIZE}); got {sizes}"
    assert sum(sizes) == total, "every event must be ingested exactly once"

    # (b) final state EXACTLY equals the old synchronous one-shot append.
    ref = TraceModel()
    orig_append(ref, 0, events, 10)   # unspied, whole batch in one shot
    ref.trim_to(sc._SERVER_BATCH_TRACE_MAX)
    ref.set_cursor(ref.latest_ns())
    assert len(h.trace_model.transactions) == len(ref.transactions)
    assert [t.time_ns for t in h.trace_model.transactions] == \
           [t.time_ns for t in ref.transactions]
    assert h.trace_model.cursor_ns == ref.cursor_ns
    assert h.trace_model.latest_ns() == ref.latest_ns()

    # The chip trace was drained + cleared exactly once (not per slice).
    assert h.engine.chip.get_trace() == []


def test_small_batch_stays_synchronous(_qapp, monkeypatch):
    """A batch at/under CHUNK_SIZE takes the synchronous path unchanged: ONE
    append_live call, no chunk job, identical final state."""
    total = sc._CHUNK_SIZE        # not > CHUNK_SIZE → synchronous
    events = [_fake_event(i) for i in range(total)]

    sizes = []
    orig_append = TraceModel.append_live

    def _spy(self, chip, raw_events, width):
        sizes.append(len(list(raw_events)))
        return orig_append(self, chip, raw_events, width)

    monkeypatch.setattr(TraceModel, "append_live", _spy)

    h = _Harness(events)
    h.refresh_debug_from_chip(force=True, full_capture=True)
    assert h._chunk_job is None, "a small batch must NOT start a chunk job"
    assert sizes == [total], "small batch = one synchronous append of the whole drain"
    assert len(h.trace_model.transactions) == total


def test_chunked_and_synchronous_agree(_qapp):
    """Same batch, chunked vs synchronous → identical TraceModel transaction times
    and cursor. Drives the chunked path (large) and compares against a direct
    whole-batch append + the same trim/cursor the finalize step applies."""
    total = sc._CHUNK_SIZE * 2 + 5
    events = [_fake_event(i * 7) for i in range(total)]   # non-unit spacing

    h = _Harness([dict(e) for e in events])
    h.refresh_debug_from_chip(force=True, full_capture=True)
    assert h._chunk_job is not None
    _pump(_qapp, h)
    assert h._chunk_job is None

    ref = TraceModel()
    ref.append_live(0, events, 10)
    ref.trim_to(sc._SERVER_BATCH_TRACE_MAX)
    ref.set_cursor(ref.latest_ns())

    assert [t.time_ns for t in h.trace_model.transactions] == \
           [t.time_ns for t in ref.transactions]
    assert h.trace_model.cursor_ns == ref.cursor_ns
