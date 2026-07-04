"""Fly-line clarity for manual routing (CM request).

Two behaviours make it clear how to hand-route:
  1. An INPUT-port net (chip x16_in -> first cell) draws a fly line — previously it
     was skipped entirely, leaving the port->first-cell connection invisible.
  2. A block port's fly line anchors to the SIDE matching its direction: an OUTPUT
     anchors at the cell's output face (the arrow POINT), an INPUT anchors at the
     OPPOSITE (wide) side — so input vs output is unambiguous.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _canvas_with_gain_chain():
    """A 2-gain chain (x16_in -> gain -> gain -> x16_out), unrouted, rendered on a
    headless canvas with the port-cell provider wired."""
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from ui.canvas.chip_canvas import ChipCanvas
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from tests.conftest import CHIP_YAML

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    ctrl = AppController(catalog=cat)
    ctrl.new_project("fly", getattr(ct, "name", "kyttar_10x12"))
    g0 = ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official",
                          params={"gain": 0.5})
    g1 = ctrl.place_block("GainBlock", 0, 5, 2, library="lattrex.official",
                          params={"gain": 0.5})
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=g0, port="in"), [])
    ctrl.add_route(BlockEndpoint(block=g0, port="out"),
                   BlockEndpoint(block=g1, port="in"), [])
    ctrl.add_route(BlockEndpoint(block=g1, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    canvas = ChipCanvas()
    canvas.port_cell_provider = lambda bt, lib, params=None: {
        p.name: (p.cell_id, p.direction)
        for p in cat.port_map(bt, params, library=lib).ports}
    canvas.set_project(ctrl.project, {getattr(ct, "name", "kyttar_10x12"): ct})
    canvas.render_scene()
    return ctrl, canvas, (g0, g1)


def test_input_port_net_draws_a_flyline():
    _app()
    from ui.canvas.connection_item import ConnectionItem
    from model.connection import ChipPortEndpoint

    ctrl, canvas, _ = _canvas_with_gain_chain()
    inport = [c for c in ctrl.project.connections
              if isinstance(c.source, ChipPortEndpoint)
              and c.source.port.endswith("_in")]
    assert inport, "expected an x16_in-sourced net in the fixture"
    fly_names = {getattr(it, "connection_name", None)
                 for it in canvas._scene.items()
                 if isinstance(it, ConnectionItem) and getattr(it, "_fly", False)}
    for c in inport:
        assert c.name in fly_names, (
            f"input-port net {c.name} must draw a fly line (was missing before)")


def test_input_anchor_is_opposite_the_output_anchor():
    """For a gain (input + output on the same cell, same face), the input fly-line
    anchor sits on the OPPOSITE side of the cell centre from the output anchor —
    proving input attaches to the wide side, output to the arrow point."""
    _app()
    from model.connection import BlockEndpoint

    ctrl, canvas, (g0, _g1) = _canvas_with_gain_chain()
    # Resolve the block's REAL input/output port names from its PortMap.
    pm = ctrl.catalog.port_map("GainBlock", {"gain": 0.5}, library="lattrex.official")
    in_port = next(p.name for p in pm.ports if p.direction == "in")
    out_port = next(p.name for p in pm.ports if p.direction == "out")
    # Cell centre of g0.
    from ui.canvas.chip_canvas import CELL_PX
    blk = ctrl.project.block(g0)
    cell = blk.placement.cells[0]
    ox, oy = canvas._chip_origin(blk.placement.chip)
    cx = ox + cell.x * CELL_PX + CELL_PX / 2
    cy = oy + cell.y * CELL_PX + CELL_PX / 2

    a_in = canvas._block_port_anchor(BlockEndpoint(block=g0, port=in_port))
    a_out = canvas._block_port_anchor(BlockEndpoint(block=g0, port=out_port))
    assert a_in is not None and a_out is not None
    # The two anchors are on OPPOSITE sides of the cell centre (their offsets from
    # centre point in opposite directions along the face axis).
    din = (a_in.x() - cx, a_in.y() - cy)
    dout = (a_out.x() - cx, a_out.y() - cy)
    dot = din[0] * dout[0] + din[1] * dout[1]
    assert dot < 0, (
        f"input anchor {din} must be opposite the output anchor {dout} (dot {dot})")
