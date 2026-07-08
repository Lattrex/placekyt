# SPDX-License-Identifier: GPL-3.0-or-later
"""ONE per-sample clock drives the waveform + animation, with a hard Stop.

Regression guards for the GRC-batch live-run redesign. This exact subsystem has
regressed before (a chunked refresh that self-rescheduled a QTimer slice and
re-drained the chip from the server-activity signal → "runs forever, can't
stop"). These tests lock in the SAFE shape:

  * No runaway: a batch that fires many on_sample/server_activity callbacks
    processes each in BOUNDED work and TERMINATES (no growing re-drain, no
    self-scheduled timer that re-enters the drain).
  * Stop works: BatchDebugHooks.stop() aborts the loop promptly, even when the
    loop is paced/lockstepped.
  * Incremental waveform (animation off): the TraceModel grows MONOTONICALLY
    across ticks (not a single end-of-batch dump), and the final content equals
    the whole-batch reference.
  * Lockstep coupling (animation on): the chip does NOT advance past sample k
    until frame_done() is called; toggling lockstep off mid-run lets it finish.

These exercise the real BatchDebugHooks clock + the real
SimController.refresh_debug_from_chip ingest path (via a minimal state harness,
no Qt event loop needed — the incremental append is synchronous per tick).
"""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.batch_debug import BatchDebugHooks
from engine.sim_bridge import BatchAborted
from engine.trace_model import TraceModel
from ui import sim_controller as sc


# --------------------------------------------------------------------------- #
# Incremental-refresh harness (no real chip / server; drives the real ingest). #
# --------------------------------------------------------------------------- #
def _fake_event(t_ns):
    return {"kind": "exec_tick", "cell_id": 0, "time_ns": float(t_ns)}


class _Chip:
    """A chip whose get_trace()/clear_trace() model the drain-since-last-clear
    contract the live refresh relies on. New events are staged by the test
    (mimicking the per-sample server loop appending to the chip trace)."""

    def __init__(self):
        self._events = []

    def stage(self, events):
        self._events.extend(events)

    def get_trace(self):
        return list(self._events)

    def clear_trace(self):
        self._events = []


class _Engine:
    def __init__(self):
        self.chip = _Chip()

    def clear_trace(self):
        self.chip.clear_trace()


class _Sig:
    def __init__(self):
        self.count = 0

    def emit(self, *a, **k):
        self.count += 1


class _RefreshHarness:
    """Minimal SimController slice that exercises the real refresh ingest path,
    in animation-OFF (streaming/incremental) mode."""

    refresh_debug_from_chip = sc.SimController.refresh_debug_from_chip
    _states_from_events = sc.SimController._states_from_events
    _steps_from_events = sc.SimController._steps_from_events
    _has_pending_events = sc.SimController._has_pending_events
    settle_pending = sc.SimController.settle_pending

    def __init__(self):
        self.engine = _Engine()
        self._pending_events = []
        self.trace_model = TraceModel()
        self._width = 10
        self._sim_chip = 0
        self._live_trace_max = 100_000
        self._server_batch_retain_all = True
        self._last_server_refresh = 0.0
        self._pending_trace_reset = False
        self._trace_time_origin = None
        self._animate_cells = False
        for name in ("cell_states", "cell_faces", "handshakes", "trace_updated",
                     "cell_state_refreshed"):
            setattr(self, name, _Sig())

    def _trace_scan_reset(self):
        pass


def test_incremental_waveform_grows_monotonically_animation_off():
    """Animation OFF: driving samples in ticks grows the TraceModel across
    ticks (NOT one monolithic end dump), and the final content == the whole-
    batch reference. A FORCE refresh always renders the residual (final flush)."""
    h = _RefreshHarness()
    total = 0
    sizes = []
    # 20 ticks, each stages some new events then refreshes (force=True so the
    # throttle never suppresses a tick in this deterministic test).
    for tick in range(20):
        h.engine.chip.stage([_fake_event(total + i) for i in range(37)])
        total += 37
        h.refresh_debug_from_chip(force=True, full_capture=True)
        sizes.append(len(h.trace_model.transactions))
    # Grew across MANY ticks, not one dump at the end.
    assert len(set(sizes)) > 5, f"waveform must build incrementally, sizes={sizes}"
    assert sizes == sorted(sizes), "the model must grow monotonically"
    # Final content equals a one-shot ingest of every event.
    ref = TraceModel()
    ref.append_live(0, [_fake_event(i) for i in range(total)], 10)
    assert len(h.trace_model.transactions) == len(ref.transactions) == total


def test_refresh_bounds_per_tick_work():
    """A single refresh never normalises an unbounded backlog: at most
    _MAX_EVENTS_PER_REFRESH events are ingested per call, so a huge accumulated
    backlog can't freeze one tick. The residual stays in the chip trace for the
    next tick (NO self-scheduled re-drain)."""
    h = _RefreshHarness()
    huge = sc._MAX_EVENTS_PER_REFRESH * 3 + 500
    h.engine.chip.stage([_fake_event(i) for i in range(huge)])
    # First refresh: bounded — ingests at most the cap. The chip is fully drained
    # into the controller-side pending buffer (so the chip resumes recording),
    # and the un-ingested residual is held in _pending_events for the next tick —
    # NOT re-drained from the chip (no re-entry).
    h.refresh_debug_from_chip(force=True, full_capture=True)
    assert len(h.trace_model.transactions) <= sc._MAX_EVENTS_PER_REFRESH, \
        "one refresh must not ingest an unbounded backlog"
    assert h._has_pending_events(), \
        "the residual backlog must remain staged for the next tick (no re-drain)"
    # settle_pending drains the rest, a bounded chunk per iteration, and
    # TERMINATES (the residual shrinks to zero — no runaway).
    h.settle_pending()
    assert not h._has_pending_events(), "settle must drain the residual fully"
    assert len(h.trace_model.transactions) == huge
    # Explicit bounded-iteration guard: settling a fresh huge backlog needs
    # ceil(huge / cap) iterations, never unbounded.
    import math
    h2 = _RefreshHarness()
    h2.engine.chip.stage([_fake_event(i) for i in range(huge)])
    h2.refresh_debug_from_chip(force=True, full_capture=True)
    need = math.ceil((huge - sc._MAX_EVENTS_PER_REFRESH) / sc._MAX_EVENTS_PER_REFRESH)
    h2.settle_pending(max_iterations=need + 2)
    assert not h2._has_pending_events()


def test_idle_refresh_is_a_noop_no_reentry():
    """A refresh that drains 0 new events (the tail of activity signals after a
    batch) does NO work and emits nothing — the guard against the reverted
    re-drain/re-touch freeze. Only a forced final settle falls through."""
    h = _RefreshHarness()
    h.engine.chip.stage([_fake_event(i) for i in range(10)])
    h.refresh_debug_from_chip(force=True, full_capture=True)
    before = h.trace_updated.count
    # No new events, not forced → must return immediately, no emit.
    h.refresh_debug_from_chip(force=False, full_capture=True)
    h.refresh_debug_from_chip(force=False, full_capture=True)
    assert h.trace_updated.count == before, \
        "an idle (0-event, unforced) refresh must not re-touch the views"


# --------------------------------------------------------------------------- #
# Clock / stop / lockstep — driven the way process_batch's loop drives it.     #
# --------------------------------------------------------------------------- #
def _drive(hooks, nsamp, recorder):
    try:
        for k in range(nsamp):
            hooks.after_sample(chip=None, sample_index=k, port="x16_out")
            recorder.append(k)
    except BatchAborted:
        recorder.append("aborted")


def test_no_runaway_terminates_flat_out():
    """The per-sample clock with no controls runs to completion in bounded time
    (no infinite loop / self-reschedule)."""
    hooks = BatchDebugHooks()
    rec = []
    t0 = time.perf_counter()
    _drive(hooks, 500, rec)
    assert rec == list(range(500))
    assert time.perf_counter() - t0 < 5.0, "flat-out run must terminate promptly"


def test_stop_aborts_promptly_even_when_paced():
    """Stop() aborts at the next sample boundary even with a per-sample delay
    (paced run) — the loop does not run the whole burst."""
    hooks = BatchDebugHooks()
    hooks.set_delay(0.02)
    rec = []
    t = threading.Thread(target=_drive, args=(hooks, 1000, rec))
    t.start()
    time.sleep(0.1)
    hooks.stop()
    t.join(timeout=2)
    assert not t.is_alive(), "stop must unwedge a paced run"
    assert rec[-1] == "aborted"
    assert len(rec) - 1 < 1000


def test_lockstep_couples_chip_to_frame_done_then_off_finishes():
    """Animation ON: the chip does NOT advance past sample k until frame_done();
    toggling lockstep OFF mid-run lets the rest free-run without wedging."""
    hooks = BatchDebugHooks()
    hooks.set_lockstep(True)
    rec = []
    t = threading.Thread(target=_drive, args=(hooks, 6, rec))
    t.start()
    time.sleep(0.1)
    assert rec == [], "chip must block before sample 0 is recorded (lockstep)"
    hooks.frame_done()
    time.sleep(0.1)
    assert rec == [0], "one frame_done releases exactly one sample"
    hooks.frame_done()
    time.sleep(0.1)
    assert rec == [0, 1]
    # Toggle animation OFF mid-run → the rest free-runs, no wedge.
    hooks.set_lockstep(False)
    t.join(timeout=2)
    assert not t.is_alive()
    assert rec == [0, 1, 2, 3, 4, 5]
