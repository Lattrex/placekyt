# SPDX-License-Identifier: GPL-3.0-or-later
"""BatchDebugHooks: breakpoints/pause/step/stop/speed in a GRC batch run.

The hooks are pure threading primitives (Qt-free), so we exercise them directly
the way SimServer.process_batch's per-sample loop does: call after_sample(...)
from a worker thread while a 'GUI' thread drives pause/resume/step/stop.
"""
import threading
import time

import pytest

from engine.batch_debug import BatchDebugHooks
from engine.sim_bridge import BatchAborted


def _drive(hooks, nsamp, *, recorder):
    """Mimic the process_batch loop: after_sample once per sample, recording how
    far it got. Stops early (records the abort) on BatchAborted."""
    try:
        for k in range(nsamp):
            hooks.after_sample(chip=None, sample_index=k, port="x16_out")
            recorder.append(k)
    except BatchAborted:
        recorder.append("aborted")


def test_runs_flat_out_with_no_controls():
    """No pause/breakpoint/delay → after_sample is a no-op, loop completes."""
    hooks = BatchDebugHooks()
    rec = []
    _drive(hooks, 5, recorder=rec)
    assert rec == [0, 1, 2, 3, 4]


def test_pause_blocks_then_resume_continues():
    hooks = BatchDebugHooks()
    rec = []
    hooks.pause()                      # paused before the run starts
    t = threading.Thread(target=_drive, args=(hooks, 3), kwargs={"recorder": rec})
    t.start()
    time.sleep(0.2)
    assert rec == [], "loop must block at the first sample while paused"
    assert hooks.is_paused
    hooks.resume()
    t.join(timeout=2)
    assert not t.is_alive()
    assert rec == [0, 1, 2]


def test_step_advances_one_sample_then_repauses():
    hooks = BatchDebugHooks()
    rec = []
    hooks.pause()
    t = threading.Thread(target=_drive, args=(hooks, 5), kwargs={"recorder": rec})
    t.start()
    time.sleep(0.15)
    assert rec == []
    hooks.step()                       # exactly one sample
    time.sleep(0.15)
    assert rec == [0], "step runs one sample then re-pauses"
    assert hooks.is_paused
    hooks.step()
    time.sleep(0.15)
    assert rec == [0, 1]
    hooks.resume()                     # let the rest run
    t.join(timeout=2)
    assert rec == [0, 1, 2, 3, 4]


def test_stop_aborts_the_burst():
    hooks = BatchDebugHooks()
    rec = []
    hooks.pause()
    t = threading.Thread(target=_drive, args=(hooks, 100), kwargs={"recorder": rec})
    t.start()
    time.sleep(0.15)
    hooks.stop()                       # abort while paused
    t.join(timeout=2)
    assert not t.is_alive()
    assert rec[-1] == "aborted"
    assert all(isinstance(x, int) for x in rec[:-1])
    assert len(rec) - 1 < 100, "must NOT have run the whole burst"


def test_breakpoint_pauses_the_loop():
    """A breakpoint_check that fires on sample 2 pauses the loop there."""
    fired = {"count": 0}

    def bp(chip, idx):
        # Fire ONCE at sample 2. (After resume the host clears the hit; a
        # check that kept returning True for idx==2 would be fine since we
        # advance past it, but guard against re-fire to keep intent explicit.)
        if idx == 2 and fired["count"] == 0:
            fired["count"] += 1
            return True
        return False

    hooks = BatchDebugHooks(breakpoint_check=bp)
    rec = []
    t = threading.Thread(target=_drive, args=(hooks, 6),
                         kwargs={"recorder": rec}, daemon=True)
    t.start()
    time.sleep(0.25)
    # The loop calls after_sample AFTER running each sample but BEFORE recording
    # it; the breakpoint on sample 2 fires inside that call and blocks, so sample
    # 2 has run on the chip but is not yet appended — the loop is paused POSITIONED
    # at sample 2. Hence rec == [0, 1] (samples 0 and 1 fully completed).
    assert rec == [0, 1]
    assert hooks.is_paused
    hooks.resume()
    t.join(timeout=2)
    assert not t.is_alive(), "loop must resume past the breakpoint"
    assert rec == [0, 1, 2, 3, 4, 5]


def test_speed_delay_paces_samples():
    hooks = BatchDebugHooks()
    hooks.set_delay(0.05)              # 50 ms/sample
    rec = []
    t0 = time.perf_counter()
    _drive(hooks, 4, recorder=rec)
    dt = time.perf_counter() - t0
    assert rec == [0, 1, 2, 3]
    assert dt >= 0.05 * 4 * 0.8, "the per-sample delay must actually pace the loop"


def test_faulty_breakpoint_check_never_wedges():
    def bad(chip, idx):
        raise RuntimeError("boom")

    hooks = BatchDebugHooks(breakpoint_check=bad)
    rec = []
    _drive(hooks, 3, recorder=rec)     # must not raise, must not hang
    assert rec == [0, 1, 2]
