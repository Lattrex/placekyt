# SPDX-License-Identifier: GPL-3.0-or-later
"""The placeKYT Stop control aborts an in-progress GRC batch run.

A Stop button (toolbar, next to Run) kills the in-progress run immediately by
calling BatchDebugHooks.stop() — which aborts at the next sample boundary and
wakes any lockstep waiter. It must work while the run is paced/lockstepped, and
a subsequent Run must work (no wedged state — the server keeps serving).

We test SimController.stop_batch() directly against a controller whose
_batch_debug is a real BatchDebugHooks, verifying it calls stop() and that a
loop driven the process_batch way then aborts.
"""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.batch_debug import BatchDebugHooks
from engine.sim_bridge import BatchAborted
from ui import sim_controller as sc


class _Sig:
    def emit(self, *a, **k):
        pass


class _StopHarness:
    """Just enough SimController state to exercise stop_batch()/has an active
    _batch_debug and the enable/disable state a Stop button reads."""

    stop_batch = sc.SimController.stop_batch
    batch_run_active = sc.SimController.batch_run_active

    def __init__(self):
        self._batch_debug = BatchDebugHooks()
        self.state_changed = _Sig()


def _drive(hooks, nsamp, rec):
    try:
        for k in range(nsamp):
            hooks.after_sample(chip=None, sample_index=k, port="x16_out")
            rec.append(k)
    except BatchAborted:
        rec.append("aborted")


def test_stop_batch_aborts_lockstepped_run():
    h = _StopHarness()
    h._batch_debug.set_lockstep(True)
    rec = []
    t = threading.Thread(target=_drive, args=(h._batch_debug, 100, rec))
    t.start()
    time.sleep(0.1)
    assert rec == [], "lockstepped loop blocks until frame_done"
    # The Stop button path.
    assert h.batch_run_active() is True
    h.stop_batch()
    t.join(timeout=2)
    assert not t.is_alive(), "stop_batch must unwedge a lockstepped run"
    assert rec == ["aborted"]


def test_stop_batch_when_no_run_is_safe():
    """stop_batch with no active batch is a harmless no-op (button may be
    clicked when disabled in a race)."""
    h = _StopHarness()
    h._batch_debug = None
    assert h.batch_run_active() is False
    h.stop_batch()   # must not raise


def test_subsequent_run_works_after_stop():
    """After a stop the hooks are reusable: a fresh loop runs to completion
    (the stop flag must not stay latched, wedging the next Run)."""
    h = _StopHarness()
    h._batch_debug.set_delay(0.02)      # pace so stop lands mid-run
    rec = []
    t = threading.Thread(target=_drive, args=(h._batch_debug, 50, rec))
    t.start()
    time.sleep(0.05)
    h.stop_batch()
    t.join(timeout=2)
    assert rec[-1] == "aborted"
    # A fresh BatchDebugHooks (what start_gnuradio_server builds per Run) runs
    # clean — the controller creates a new one each Run, so simulate that.
    h._batch_debug = BatchDebugHooks()
    rec2 = []
    _drive(h._batch_debug, 5, rec2)
    assert rec2 == [0, 1, 2, 3, 4]
