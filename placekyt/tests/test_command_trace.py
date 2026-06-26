# SPDX-License-Identifier: GPL-3.0-or-later
"""The command trace captures every operation and replays it EXACTLY.

Every GUI interaction is a Command, so recording at the CommandManager captures a
complete, faithful session. The trace can be exported to a runnable .py script or
a structured .kytrace JSON and replayed on another machine to reproduce a session
(or a bug) precisely — the basis for "send me your trace" bug reports.
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.controller import AppController
from model.connection import BlockEndpoint, ChipPortEndpoint
from tests.conftest import CHIP_YAML as CT_PATH

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _session(ctrl):
    """Perform a representative spread of operations through the controller."""
    ctrl.new_project("t", "kyttar_10x12")
    ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official",
                     params={"gain": 0.5})
    bn = ctrl.project.blocks[0].name
    ctrl.move_block(bn, 1, 1)
    ctrl.set_cell_face(bn, 0, "north")
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(bn, "sample"), name="n1")
    ctrl.edit_params(bn, {"gain": 0.25, "gain_range": 15})
    return bn


def _snapshot(ctrl):
    b = ctrl.project.blocks[0]
    return (b.name,
            [(c.x, c.y, c.face.value) for c in b.placement.cells],
            dict(b.params),
            sorted(c.name for c in ctrl.project.connections))


def test_trace_records_every_operation():
    ctrl = AppController()
    _session(ctrl)
    ops = [e.get("op") for e in ctrl.trace.events()]
    # Each user op is captured with its replayable controller method.
    assert "place_block" in ops
    assert "move_block" in ops
    assert "set_cell_face" in ops
    assert "add_logical_connection" in ops
    assert "edit_params" in ops


def test_trace_replays_to_identical_state():
    ctrl = AppController()
    _session(ctrl)
    expected = _snapshot(ctrl)

    # Replay the trace onto a FRESH controller — reproduces the exact state.
    replay = AppController()
    replay.new_project("t", "kyttar_10x12")
    replay.replay_trace(ctrl.trace.events())
    assert _snapshot(replay) == expected


def test_export_is_python_only(tmp_path):
    """The trace exports as a SINGLE format: a runnable Python replay script. The
    .py IS the trace (documents + replays); export always writes Python regardless
    of the path's suffix — there is no separate JSON/.kytrace format."""
    ctrl = AppController()
    _session(ctrl)

    py = tmp_path / "trace.py"
    ctrl.export_trace(str(py))
    script = py.read_text()
    # A readable, runnable replay script (calls ctrl.<op>(...)).
    assert "ctrl.move_block(" in script
    assert "ctrl.set_cell_face(" in script
    assert script.lstrip().startswith("#")  # python comment header

    # export_trace writes PYTHON even when the path has a non-.py suffix.
    other = tmp_path / "trace.kytrace"
    ctrl.export_trace(str(other))
    assert other.read_text().lstrip().startswith("#")  # not JSON

    # Replaying the .py from disk (exec) reproduces the state.
    replay = AppController()
    replay.new_project("t", "kyttar_10x12")
    ns = {"controller": replay, "ctrl": replay}
    exec(compile(py.read_text(), str(py), "exec"), ns)  # noqa: S102
    assert _snapshot(replay) == _snapshot(ctrl)


def test_trace_survives_project_swap():
    """The trace is one continuous session log across a new_project (which
    rebuilds the CommandManager) — so an import-then-edit flow is one trace."""
    ctrl = AppController()
    ctrl.new_project("a", "kyttar_10x12")
    ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official",
                     params={"gain": 0.5})
    n_after_first = len(ctrl.trace.events())
    assert n_after_first >= 1
    # A second new_project must NOT wipe the trace (same trace object).
    t = ctrl.trace
    ctrl.new_project("b", "kyttar_10x12")
    assert ctrl.trace is t


def test_undo_redo_are_traced():
    ctrl = AppController()
    bn = _session(ctrl)
    ctrl.move_block(bn, 1, 0)
    ctrl.undo()
    ctrl.redo()
    kinds = [e["kind"] for e in ctrl.trace.events()]
    assert "undo" in kinds and "redo" in kinds


# --- high-level ops (import / auto-place / auto-route) are replayable too ------

GRC = Path(__file__).resolve().parents[1] / "tests" / "data" / "grc" \
    / "coherent_bpsk_rx_mf_demo.grc"


@pytest.mark.skipif(not GRC.exists(), reason="grc fixture absent")
def test_import_autoplace_autoroute_are_traced_and_replay():
    """import_grc / auto_place / auto_route_all are NOT single Commands, but they
    ARE replayable controller ops — so they must appear in the trace (not as
    '(manual)' gaps) and a fresh controller must replay them to the same state."""
    ctrl = AppController()
    ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    ctrl.auto_route_all()
    ops = [e.get("op") for e in ctrl.trace.events()]
    assert ops[0] == "import_grc", "import must be the first replayable op"
    assert "auto_place" in ops
    assert "auto_route_all" in ops

    def snap(c):
        return sorted(
            (b.name, tuple((cc.x, cc.y, cc.face.value) for cc in b.placement.cells))
            for b in c.project.blocks if b.placement)

    replay = AppController()
    replay.replay_trace(ctrl.trace.events())  # NO manual prefix needed
    assert snap(replay) == snap(ctrl)
