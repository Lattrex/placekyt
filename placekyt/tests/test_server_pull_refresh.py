# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI-side pull-timer refresh for a GRC batch run (the freeze fix).

The verified root cause of the "window frozen for the whole batch" bug: the
burst runs on the SERVER thread, and each sample emitted a queued Qt signal to
the GUI (``server_activity``); queued slot invocations arrive faster than Qt
can drain + repaint, so the event loop never gets a paint turn — the window is
dead until the batch ends. The fix inverts control: the server thread does NO
per-sample GUI signaling; a QTimer OWNED BY THE GUI THREAD pulls the delta at
~30 Hz with a BOUNDED per-tick ingest, so the GUI paces itself and can never
be flooded.

Offscreen tests CANNOT observe paint/responsiveness (all three failed prior
fixes were green here while the real GUI was dead) — so these tests lock in
the STRUCTURAL properties the design is correct-by-construction on:

  * ZERO per-sample ``server_activity`` emissions on the free-running path
    (O(few) per batch, not O(nsamp));
  * LOCKSTEP (animation on, below top speed) still emits per sample — that
    path is self-paced by construction (the chip blocks on frame_done, so at
    most one queued refresh is ever outstanding);
  * the capped incremental ingest converges to the SAME final TraceModel as a
    single whole-batch append, advancing the residual (never re-draining);
  * a Run-boundary reset drops the stale residual;
  * the pull timer runs exactly while the server is hosted;
  * SimController.stop() aborts an in-flight batch (BatchAborted) and the next
    batch re-arms (clear_stop) so Stop → Run-again works.
"""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.batch_debug import BatchDebugHooks
from engine.catalog import BlockCatalog
from engine.sim_bridge import BatchAborted
from engine.trace_model import TraceModel
from ui import sim_controller as sc


# ---------------------------------------------------------------------------
# Harness (same pattern as test_batch_trace_retention): the minimal slice of
# SimController state refresh_debug_from_chip reads, so the bounded-ingest
# behaviour is exercised directly with a synthetic drainable chip.
# ---------------------------------------------------------------------------
def _fake_event(t_ns):
    return {"kind": "exec_tick", "cell_id": 0, "time_ns": float(t_ns)}


class _Chip:
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
    def __init__(self):
        self.count = 0

    def emit(self, *a, **k):
        self.count += 1


class _Harness:
    refresh_debug_from_chip = sc.SimController.refresh_debug_from_chip
    _states_from_events = sc.SimController._states_from_events
    _steps_from_events = sc.SimController._steps_from_events

    def __init__(self, events, *, retain_all=True):
        self.engine = _Engine(events)
        self.trace_model = TraceModel()
        self._width = 10
        self._sim_chip = 0
        self._live_trace_max = 50
        self._server_batch_retain_all = retain_all
        self._last_server_refresh = 0.0
        self._pending_trace_reset = False
        self._trace_time_origin = None
        self._animate_cells = False
        for name in ("cell_states", "cell_faces", "handshakes", "trace_updated",
                     "cell_state_refreshed"):
            setattr(self, name, _Sig())

    def _trace_scan_reset(self):
        pass

    def unthrottled_refresh(self, **kw):
        """One refresh with the rate throttle + adaptive back-off bypassed (the
        tests drive ticks explicitly; wall-clock pacing is not under test)."""
        self._last_server_refresh = 0.0
        self._last_refresh_cost = 0.0
        self.refresh_debug_from_chip(**kw)


N = 500
CAP = 120     # small per-tick cap to force multiple ticks over N events


def test_capped_ingest_converges_to_whole_batch_reference(monkeypatch):
    """Successive non-forced pull ticks ingest at most CAP events each, never
    re-drain, never shrink the model, terminate in ceil(N/CAP) ticks, and end
    with EXACTLY the trace a single whole-batch append produces."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])

    sizes = []
    for tick in range(50):                      # hard guard: no runaway
        h.unthrottled_refresh(full_capture=True)
        sizes.append(len(h.trace_model.transactions))
        # The chip was drained ONCE (first tick) and stays empty — the residual
        # is advanced GUI-side, never re-drained from the chip.
        assert h.engine.chip.get_trace() == []
        if not h._pending_batch_events:
            break
    ticks = len(sizes)
    assert ticks == math.ceil(N / CAP), \
        f"expected ceil({N}/{CAP}) bounded ticks, took {ticks}"
    # Monotonic build, bounded per tick.
    assert sizes == sorted(sizes)
    assert sizes[0] == CAP, "first tick must ingest exactly the cap"
    assert all(b - a <= CAP for a, b in zip(sizes, sizes[1:]))

    # Reference: the same events appended in ONE whole-batch refresh.
    ref = _Harness([_fake_event(i) for i in range(N)])
    ref.unthrottled_refresh(force=True, full_capture=True)
    got = [t.time_ns for t in h.trace_model.transactions]
    want = [t.time_ns for t in ref.trace_model.transactions]
    assert len(h.trace_model.transactions) == N
    assert got == want, "incremental ingest must equal the whole-batch trace"


def test_force_flush_ingests_entire_residual(monkeypatch):
    """force=True (batch end / server stop) drains the WHOLE residual in one
    call — the settled trace is complete no matter how much was pending."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])
    h.unthrottled_refresh(full_capture=True)            # one capped tick
    assert len(h.trace_model.transactions) == CAP
    assert len(h._pending_batch_events) == N - CAP
    h.unthrottled_refresh(force=True, full_capture=True)
    assert len(h.trace_model.transactions) == N
    assert h._pending_batch_events == []


def test_run_boundary_reset_drops_stale_residual(monkeypatch):
    """A pending Run-boundary trace reset must drop the pull residual too — the
    leftover events belong to the PREVIOUS Run and must not leak into (or time-
    anchor) the fresh Run's trace."""
    monkeypatch.setattr(sc, "_PULL_MAX_EVENTS_PER_TICK", CAP)
    h = _Harness([_fake_event(i) for i in range(N)])
    h.unthrottled_refresh(full_capture=True)            # leaves a residual
    assert h._pending_batch_events
    h._pending_trace_reset = True                       # new Run (server thread)
    h.unthrottled_refresh(full_capture=True)            # consumed on GUI side
    assert h._pending_batch_events == []
    assert len(h.trace_model.transactions) == 0
    assert h._trace_time_origin is None


def test_streaming_path_unchanged_window_trim():
    """The pure streaming path (full_capture=False) still keeps only the rolling
    window — bounded for an unbounded stream, exactly as before the fix."""
    h = _Harness([_fake_event(i) for i in range(N)], retain_all=False)
    h.unthrottled_refresh(force=True)                   # streaming default
    assert len(h.trace_model.transactions) == h._live_trace_max
    assert h._pending_batch_events == []


# ---------------------------------------------------------------------------
# Real-SimController structural tests (empty-project placeholder host: the
# server binds without a design, which is all these need).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def sim(qapp):
    from ui.controller import AppController
    from ui.sim_controller import SimController

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    s = SimController(ctrl)
    yield s
    s.stop_gnuradio_server()


def test_no_per_sample_gui_signal_free_running(sim):
    """THE core anti-freeze property: with animation OFF (the default), a batch
    of many samples emits ZERO per-sample server_activity signals from the
    server-thread hook — the GUI-side pull timer shows progress instead. The
    prior lockup was exactly O(nsamp) queued emissions."""
    port = sim.start_gnuradio_server(port=0)
    assert port
    emits = []
    sim.server_activity.connect(lambda *a: emits.append(a))
    hooks = sim._batch_debug
    assert isinstance(hooks, BatchDebugHooks)
    NSAMP = 300
    for k in range(NSAMP):                      # what process_batch does
        hooks.after_sample(None, k, "x16_out")
    assert emits == [], (
        f"free-running batch must emit NO per-sample GUI signal, got "
        f"{len(emits)} (O(nsamp) queued emissions is the freeze root cause)")


def test_lockstep_still_emits_per_sample_self_paced(sim):
    """LOCKSTEP (animation on, below top speed) keeps the per-sample forced
    refresh — required so apply_handshakes runs and frame_done releases the
    chip. Self-paced by construction: the chip BLOCKS until the GUI acks, so
    at most one emission is outstanding (pre-armed here via frame_done)."""
    sim.start_gnuradio_server(port=0)
    emits = []
    sim.server_activity.connect(lambda *a: emits.append(a))
    hooks = sim._batch_debug

    sim.set_animate_cells(True)                 # default speed 8 < top ⇒ lockstep
    assert sim._lockstep_active()
    NSAMP = 20
    for k in range(NSAMP):
        hooks.frame_done()                      # pre-arm the frame gate (no GUI)
        hooks.after_sample(None, k, "x16_out")
    assert len(emits) == NSAMP
    assert all(a == (True, True) for a in emits), \
        "lockstep refreshes must be full_capture AND forced past the throttle"

    # Top (fast-forward) speed drops lockstep → back to zero per-sample emits.
    sim.set_speed_index(len(sc.SPEED_STEPS) - 1)
    assert not sim._lockstep_active()
    emits.clear()
    for k in range(NSAMP):
        hooks.after_sample(None, k, "x16_out")
    assert emits == []


def test_pull_timer_runs_exactly_while_server_hosted(sim):
    """The GUI-side pull timer starts with the server and stops with it (idle
    ticks are cheap, but a dangling timer after Stop would be a leak)."""
    assert not sim._server_pull_timer.isActive()
    sim.start_gnuradio_server(port=0)
    assert sim._server_pull_timer.isActive()
    sim.stop_gnuradio_server()
    assert not sim._server_pull_timer.isActive()


def test_controller_stop_aborts_batch_and_next_batch_rearms(sim):
    """Stop during a batch: SimController.stop() trips the hooks so the server
    loop aborts at the next sample boundary; the next batch's clear_stop
    re-arms so Stop → Run-again works (the one-shot latch must not stick)."""
    sim.start_gnuradio_server(port=0)
    hooks = sim._batch_debug
    sim.stop()                                  # user hits Stop mid-batch
    with pytest.raises(BatchAborted):
        hooks.after_sample(None, 0, "x16_out")
    hooks.clear_stop()                          # what the next batch RPC does
    hooks.after_sample(None, 1, "x16_out")      # runs — no abort, no hang
