# SPDX-License-Identifier: GPL-3.0-or-later
"""Port-adjacent ROUTING/TRANSIT cells show activity during a GRC batch run.

Bug: in a GRC batch (server-batch / process_batch) run the routing/transit cells
that connect the chip INPUT and OUTPUT ports to the block chain showed NO activity
— no per-word blink, no live face-arrow direction — while data was clearly flowing
(the waveform updated). Block cells animated; the port-adjacent transit cells were
stuck.

Root cause: the batch debug refresh (SimController.refresh_debug_from_chip) emitted
a single FLAT all-cells handshake (one instantaneous glow that, with the refresh
throttle, collapsed to nothing on the transit cells) and never emitted ``cell_faces``
— so the routing-cell arrows stayed frozen at the static build direction. The
interactive timer path (``_emit_single_chip_frame``) emits per-word STEPS + live
faces; the batch path now does too.

This drives the REAL server-batch path the GUI uses (SimServer.process_batch) and
asserts the cells on the route BETWEEN the input port and the block, and BETWEEN the
block and the output port — i.e. the transit cells, NOT the block's own cells —
appear in the emitted handshake steps with a face AND get a live face direction.
"""
import os
import socket
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from engine.io.chip_type_io import load_chip_type
from engine.build import BuildEngine
from ui.controller import AppController
from ui.sim_controller import SimController
from model.connection import ChipPortEndpoint, BlockEndpoint

from tests.conftest import CHIP_YAML as CT_PATH

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _build_gain_chain():
    """Gain block placed DEEP (3,3) so there is a real run of routing/transit
    cells between the input port and the block, and between the block and the
    output port. Returns (ctrl, chip_type, block_name)."""
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    ctrl.new_project("dut", "kyttar_10x12")
    bn = ctrl.place_block("GainBlock", 0, 3, 3, library="lattrex.official",
                          params={"gain": 0.5})
    bn = bn if isinstance(bn, str) else bn.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(bn, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(bn, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_route_all({"kyttar_10x12": ct})
    bres = BuildEngine(ctrl.catalog, ctrl.registry.paths()).build(
        ctrl.project, {"kyttar_10x12": ct})
    assert bres.ok, f"build failed: {bres.errors}"
    return ctrl, ct, bn


def _route_cells_excluding_block(ctrl, block_name):
    """The (x, y) of every routing waypoint, MINUS the block's own cells — i.e.
    the transit cells between the ports and the block. Also returns the port-
    adjacent ones at the route ends specifically."""
    proj = ctrl.project
    block_xy = set()
    for blk in proj.blocks:
        if blk.placement is not None:
            block_xy.update((c.x, c.y) for c in blk.placement.cells)
    transit = set()
    in_adjacent = out_adjacent = None
    for conn in proj.connections:
        if not conn.is_routed or not conn.route:
            continue
        pts = [(p.x, p.y) for p in conn.route]
        for xy in pts:
            if xy not in block_xy:
                transit.add(xy)
        # The cell adjacent to the chip INPUT port is the first non-block waypoint
        # of the input net; the cell adjacent to the OUTPUT port is the last
        # waypoint of the output net.
        src_is_in = isinstance(conn.source, ChipPortEndpoint) \
            and conn.source.port.endswith("_in")
        tgt_is_out = isinstance(conn.target, ChipPortEndpoint) \
            and conn.target.port.endswith("_out")
        if src_is_in:
            for xy in pts:
                if xy not in block_xy:
                    in_adjacent = xy
                    break
        if tgt_is_out:
            for xy in reversed(pts):
                if xy not in block_xy:
                    out_adjacent = xy
                    break
    return transit, in_adjacent, out_adjacent


def _run_batch_capture(ctrl, animate=True):
    """Host the built design on a real SimServer (the GUI server-batch path),
    drive a process_batch burst, and capture every handshake + cell_faces payload
    the SimController emits during the debug refresh. Returns
    (handshake_payloads, face_payloads, output).

    ``animate`` toggles the Enable-cell-animation state — the batch still runs and
    produces output either way, but with animation OFF no handshake/face visuals
    are emitted (the fast path)."""
    from PySide6.QtCore import Qt
    from engine.sim_bridge import send_message, recv_message

    app = _qapp()
    sim = SimController(ctrl)
    # These tests assert the ANIMATED refresh (per-word handshake steps + live
    # faces). Cell animation is OFF by default (the fast path emits no visuals),
    # so enable it to exercise the animated path under test.
    sim.set_animate_cells(bool(animate))
    hs_payloads: list = []
    face_payloads: list = []
    sim.handshakes.connect(lambda hs: hs_payloads.append(hs))
    sim.cell_faces.connect(lambda f: face_payloads.append(f))
    # Wire server_activity → refresh exactly like main_window (queued).
    sim.server_activity.connect(
        lambda fc=False: sim.refresh_debug_from_chip(full_capture=fc),
        Qt.QueuedConnection)

    port = sim.start_gnuradio_server(port=0)
    try:
        x = np.full(8, 0.5, dtype=np.float32)
        c = socket.create_connection(("127.0.0.1", port))
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "complex": False, "raw": False}, x)
        _, p = recv_message(c)
        c.close()
        out = [] if p is None else [float(v) for v in p]
        # Drain the queued server_activity → refresh signals (GUI-thread work).
        for _ in range(30):
            app.processEvents()
            time.sleep(0.01)
        return hs_payloads, face_payloads, out
    finally:
        sim.stop_gnuradio_server()


def test_batch_run_emits_perword_handshake_steps():
    """The batch refresh emits PER-WORD handshake steps (a rolling wave), not a
    single flat all-cells flash — so consecutive words light the transit cells
    one-at-a-time."""
    ctrl, _ct, _bn = _build_gain_chain()
    hs_payloads, _faces, out = _run_batch_capture(ctrl)
    assert out, "the batch produced no output (chain did not run)"
    assert hs_payloads, "no handshake payload emitted during the batch"
    assert all("steps" in hs for hs in hs_payloads), \
        "every batch handshake must carry the per-word 'steps' form"
    # The substantive refresh (the one that drained the burst's events) must carry
    # MANY per-word steps — a rolling wave, NOT a single flat all-cells flash.
    # (A trailing force-refresh on stop can legitimately be empty.)
    most = max(len(hs["steps"]) for hs in hs_payloads)
    assert most > 1, (
        "expected MANY per-word steps (a rolling wave), got a single flat flash")


def test_port_adjacent_routing_cells_have_activity():
    """The routing/transit cells adjacent to the INPUT and OUTPUT ports appear in
    the emitted handshake steps WITH a face — real transits, like block cells."""
    ctrl, _ct, bn = _build_gain_chain()
    transit, in_adj, out_adj = _route_cells_excluding_block(ctrl, bn)
    assert in_adj is not None, "no input-port-adjacent routing cell in the route"
    assert out_adj is not None, "no output-port-adjacent routing cell in the route"

    hs_payloads, _faces, _out = _run_batch_capture(ctrl)
    # Union of every (x, y, face) across every step of every emitted payload.
    active = set()
    for hs in hs_payloads:
        for step in hs.get("steps", []):
            for (_chip, x, y, face) in step.get("cells", []):
                active.add((x, y, face))
    active_xy = {(x, y) for (x, y, _f) in active}

    # The port-adjacent transit cells must show activity with a face — NOT empty.
    assert in_adj in active_xy, (
        f"input-port-adjacent routing cell {in_adj} had NO activity events "
        f"(active cells: {sorted(active_xy)})")
    assert out_adj in active_xy, (
        f"output-port-adjacent routing cell {out_adj} had NO activity events "
        f"(active cells: {sorted(active_xy)})")
    # And EVERY transit cell on the route should light, not just the block cells.
    missing = sorted(transit - active_xy)
    assert not missing, f"transit cells with no activity: {missing}"


def test_batch_run_emits_live_faces_for_routing_cells():
    """The batch refresh emits ``cell_faces`` for the active cells — INCLUDING
    the routing/transit cells — so their arrows reflect the live forwarding
    direction instead of staying frozen (the 'stuck in one direction' symptom)."""
    ctrl, _ct, bn = _build_gain_chain()
    _transit, in_adj, out_adj = _route_cells_excluding_block(ctrl, bn)
    _hs, face_payloads, _out = _run_batch_capture(ctrl)
    assert face_payloads, "no cell_faces emitted during the batch (arrows stay frozen)"
    faces: dict = {}
    for f in face_payloads:
        faces.update(f)
    # cell_faces is keyed by (chip, x, y).
    faced_xy = {(x, y) for (_c, x, y) in faces}
    assert in_adj in faced_xy, (
        f"no live face for input-port-adjacent routing cell {in_adj}")
    assert out_adj in faced_xy, (
        f"no live face for output-port-adjacent routing cell {out_adj}")


def test_steps_from_events_includes_exec_and_arrival():
    """_steps_from_events must NOT drop cells that only have exec_tick /
    data_arrival / instr_arrival events (the 'busy multiplexed-bus cells and
    executing cells don't flash' bug). exec cells carry the 'exec' sentinel face;
    arrival cells carry their forward (exit) face."""
    # _steps_from_events is a pure helper needing only self._width — call it on a
    # minimal stand-in so the test stays fast (no chip/server).
    class _S:
        _width = 10
        _steps_from_events = SimController._steps_from_events
    s = _S()
    events = [
        {"kind": "exec_tick", "cell_id": 34, "time_ns": 1.0},
        {"kind": "data_arrival", "cell_id": 12, "time_ns": 2.0,
         "face": "W", "exit_face": "S"},
        {"kind": "instr_arrival", "cell_id": 5, "time_ns": 3.0,
         "face": "N", "exit_face": "E"},
        {"kind": "output_ready", "cell_id": 7, "time_ns": 4.0, "face": "S"},
        # A word CONSUMED here (execute_locally, HOP_CNT==31): NO exit_face. It
        # must NOT take the arrival face ("W") — that would point the arrow
        # BACKWARD at the source. It becomes "exec" (whole-cell glow, no arrow).
        {"kind": "instr_arrival", "cell_id": 8, "time_ns": 5.0,
         "face": "W", "action": "execute_locally"},
    ]
    steps = s._steps_from_events(events, 0)
    all_cells = [c for st in steps for c in st["cells"]]
    # exec_tick → (0, 34%10, 34//10, "exec")
    assert (0, 4, 3, "exec") in all_cells, f"exec_tick dropped: {all_cells}"
    # data_arrival → forward face 'S'
    assert (0, 2, 1, "S") in all_cells, f"data_arrival dropped: {all_cells}"
    # instr_arrival → forward face 'E'
    assert (0, 5, 0, "E") in all_cells, f"instr_arrival dropped: {all_cells}"
    # output_ready → exit face 'S'
    assert (0, 7, 0, "S") in all_cells, f"output_ready dropped: {all_cells}"
    # consumed instr_arrival (no exit_face) → "exec", NEVER the arrival face "W".
    assert (0, 8, 0, "exec") in all_cells, \
        f"consumed word must be 'exec', not the arrival face: {all_cells}"
    assert (0, 8, 0, "W") not in all_cells, \
        "a consumed word must NEVER set the arrow to its arrival face (backward)"


def test_animation_off_emits_no_visuals_but_still_runs():
    """With cell animation OFF (the default), a batch run still produces output,
    but the SimController emits NO handshake / cell_faces visuals — the fast path
    with zero fabric-animation overhead."""
    ctrl, _ct, _bn = _build_gain_chain()
    hs_payloads, face_payloads, out = _run_batch_capture(ctrl, animate=False)
    assert out, "the batch produced no output (chain did not run)"
    assert not hs_payloads, (
        "animation OFF must emit NO handshake steps (got "
        f"{len(hs_payloads)} payloads)")
    assert not face_payloads, (
        "animation OFF must emit NO cell_faces (got "
        f"{len(face_payloads)} payloads)")
