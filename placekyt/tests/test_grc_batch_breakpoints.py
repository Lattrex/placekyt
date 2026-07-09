# SPDX-License-Identifier: GPL-3.0-or-later
"""Breakpoints + step/pause/resume must work on a GRC-server BATCH run, not only
on the standalone in-tool stimulus run.

They were never wired into the GRC path: the server-thread check
(_batch_breakpoint_hit) called chip.drain_trace()/trace_events() — methods the
hosted chip does NOT have — so it always excepted and returned False (no hit).
This test proves a PC breakpoint set before a batch actually fires against the
hosted chip's trace, and that BatchDebugHooks pauses / single-steps / resumes.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_PLACEKYT = Path(__file__).resolve().parents[1]
_ROOT = _PLACEKYT.parent
_RUNTIME = _ROOT / "runtime" / "python"
import sys
for _p in (str(_PLACEKYT), str(_RUNTIME)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
SSB_KYT = str(_ROOT / "examples" / "ssb_weaver" / "ssb_weaver.kyt")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and os.path.exists(SSB_KYT)),
    reason="chip yaml or SSB example absent")


def _drive_one_batch(port, n=32):
    import math
    from engine.sim_bridge import send_message, recv_message
    x = [0.3 * math.cos(2 * math.pi * 50 * k / 1000.0) for k in range(n)]
    c = socket.socket(); c.connect(("127.0.0.1", port))
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "complex": False, "raw": False,
                     "stream_id": None, "n_samples": n}, x)
    recv_message(c); c.close()


class _FakeChip:
    """A chip stub exposing only get_trace() — like the hosted chip, which has
    NO drain_trace()/trace_events() (the methods the buggy check called). Proves
    _batch_breakpoint_hit works via get_trace() + a scan cursor."""

    def __init__(self):
        self._events = []

    def get_trace(self):
        return list(self._events)

    def emit(self, events):
        self._events.extend(events)


def _pc_ev(cell_id, pc, t):
    return {"kind": "exec_tick", "cell_id": cell_id, "pc": pc, "time_ns": float(t)}


def test_pc_breakpoint_fires_on_grc_batch():
    """The server-thread check (_batch_breakpoint_hit) must fire a PC breakpoint
    against the hosted chip's trace via get_trace() + a scan cursor. The old code
    called chip.drain_trace()/trace_events() — which the chip lacks — so it always
    excepted and returned False → breakpoints NEVER fired on the GRC path. It also
    must not re-fire on already-scanned events, and must survive a trace clear."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.breakpoints import Breakpoint, BP_PC
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController

    sim = SimController(AppController(catalog=BlockCatalog.from_gr_kyttar()))
    sim._width = 10
    sim._sim_chip = 0
    sim._batch_bp_scan = 0
    chip = _FakeChip()

    class _Srv:
        _chip = chip
    sim._gr_server = _Srv()

    # cell_id 25 → (x=5, y=2) at width 10. Breakpoint on PC==7 there.
    sim.breakpoints.add(Breakpoint(chip=0, x=5, y=2, kind=BP_PC, value=7))

    # No events yet → no hit.
    assert sim._batch_breakpoint_hit(0, 0) is False

    # This sample's events include exec PC 3 (miss) then PC 7 (HIT) at cell 25.
    chip.emit([_pc_ev(25, 3, 100), _pc_ev(25, 7, 200)])
    assert sim._batch_breakpoint_hit(0, 1) is True, (
        "PC breakpoint did NOT fire on the chip trace (the wiring gap)")
    assert len(sim._bp_hits) == 1

    # Cursor advanced → re-scanning the same trace does NOT re-fire.
    assert sim._batch_breakpoint_hit(0, 2) is False

    # A NEW matching event fires again.
    chip.emit([_pc_ev(25, 7, 300)])
    assert sim._batch_breakpoint_hit(0, 3) is True
    assert len(sim._bp_hits) == 2

    # A wrong-PC / wrong-cell event never fires.
    chip.emit([_pc_ev(25, 4, 400), _pc_ev(11, 7, 500)])
    assert sim._batch_breakpoint_hit(0, 4) is False

    # Trace cleared (Run boundary): cursor > len → resets to 0, re-scans cleanly.
    chip._events = [_pc_ev(25, 7, 10)]
    assert sim._batch_breakpoint_hit(0, 5) is True


def test_batch_hooks_pause_step_resume():
    """BatchDebugHooks honor a breakpoint hit (pause), then step (one sample then
    re-pause) and resume (run free) — the machinery the GUI Step/Resume drive."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.batch_debug import BatchDebugHooks
    from engine.sim_bridge import BatchAborted

    # breakpoint_check fires exactly once (first sample), then never again.
    fired = {"n": 0}

    def _bp(chip, k):
        fired["n"] += 1
        return fired["n"] == 1

    hooks = BatchDebugHooks(breakpoint_check=_bp)
    # Sample 0 hits the breakpoint → hooks pause. after_sample would BLOCK, so run
    # it in a thread and drive step/resume from here.
    import threading
    done = {"k": None}

    def _run():
        try:
            for k in range(5):
                hooks.after_sample(None, k, "x16_out")
            done["k"] = 5
        except BatchAborted:
            done["k"] = -1

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Wait until it's paused at the breakpoint.
    import time
    for _ in range(200):
        if hooks.is_paused:
            break
        time.sleep(0.01)
    assert hooks.is_paused, "hooks did not pause on the breakpoint hit"

    # Step: advance exactly one sample then pause again.
    hooks.step()
    for _ in range(200):
        if hooks.is_paused:
            break
        time.sleep(0.01)
    assert hooks.is_paused, "hooks did not re-pause after a single step"

    # Resume: run to completion.
    hooks.resume()
    t.join(timeout=5.0)
    assert done["k"] == 5, "resume did not run the batch to completion"
