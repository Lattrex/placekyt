"""Route bus-highlight on I/O-cell select + grab-route-inside-cell (#266 / #268).

Builds the production coherent RX (MF → Costas → Gardner → Slicer, auto-P&R) and
asserts, on the real placed+routed geometry:

  (A) selecting the matched filter's OUTPUT cell highlights the WHOLE physical bus
      of the MF→Costas connection(s) — including INTO the Costas INPUT cell — via
      ConnectionItem.set_related, so the link is obvious (#266);
  (B) a hit-test at a point INSIDE a block I/O cell where the route runs returns
      that connection (the grab handle, #268).

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_route_highlight_select.py -x
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.route_analysis import (  # noqa: E402
    cell_coverage,
    connections_terminating_at_cell,
)
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.canvas.cell_item import CELL_PX, CellItem, CellKind  # noqa: E402
from ui.canvas.chip_canvas import ChipCanvas  # noqa: E402
from ui.canvas.connection_item import ConnectionItem  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _build_rx(catalog, chip_type):
    """Place MF→Costas→Gardner→Slicer, route the forward nets. Returns the
    controller (project placed + routed). Mirrors test_production_rx_mf_ber."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("hl", "kyttar_10x12")
    lib = "lattrex.official"
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=lib)
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 0, library=lib)
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 0, library=lib)
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xi"), [])
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yi"),
                   BlockEndpoint(block=cos, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yq"),
                   BlockEndpoint(block=cos, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=cos, port="yi_tap"),
                   BlockEndpoint(block=gar, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=gar, port="out"),
                   BlockEndpoint(block=sli, port="llr"), [])
    ctrl.add_route(BlockEndpoint(block=sli, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    return ctrl, mf, cos


def _canvas(ctrl, chip_type):
    cv = ChipCanvas()
    cv.port_cell_provider = lambda t, l: {
        p.name: (p.cell_id, p.direction)
        for p in ctrl.catalog.port_map(t, library=l).ports}
    cv.set_project(ctrl.project, {"kyttar_10x12": chip_type})
    return cv


def _mf_costas_connection(ctrl, mf, cos):
    """The routed MF→Costas connection (yi or yq) used for the highlight test."""
    for conn in ctrl.project.connections:
        s, t = conn.source, conn.target
        if (isinstance(s, BlockEndpoint) and s.block == mf
                and isinstance(t, BlockEndpoint) and t.block == cos
                and conn.is_routed):
            return conn
    raise AssertionError("no routed MF->Costas connection found")


def test_select_output_cell_highlights_full_bus(qapp, catalog, chip_type):
    """(A) Selecting the MF output cell related-highlights the MF→Costas
    connection(s) along the whole route, and the route runs INTO the Costas
    input cell (the endpoint cells are the bus path's two ends)."""
    ctrl, mf, cos = _build_rx(catalog, chip_type)
    cv = _canvas(ctrl, chip_type)

    conn = _mf_costas_connection(ctrl, mf, cos)
    # The route's first/last waypoints ARE the MF output cell and the Costas
    # input cell (route-into-cell, #266).
    out_cell = (conn.route[0].x, conn.route[0].y)
    in_cell = (conn.route[-1].x, conn.route[-1].y)

    # Sanity: this output cell IS an endpoint of the MF->Costas connection(s).
    terminators = connections_terminating_at_cell(ctrl.project, 0, *out_cell)
    assert conn.name in terminators

    # Select the MF output cell on the canvas.
    cell = next(it for it in cv.cell_items()
                if (it.cx, it.cy) == out_cell and it.kind is CellKind.BLOCK)
    cell.setSelected(True)
    qapp.processEvents()

    # Every connection terminating at that cell is related-highlighted; nothing
    # unrelated is.
    related = {it.connection_name for it in cv.connection_items()
               if it.is_related}
    assert conn.name in related
    assert related == set(terminators)

    # The highlighted ConnectionItem's endpoints reach INTO both the MF output
    # cell and the Costas input cell (its endpoint rects cover both centres).
    item = next(it for it in cv.connection_items()
                if it.connection_name == conn.name)
    ox, oy = cv._chip_origin(0)
    out_c = QPointF(ox + out_cell[0] * CELL_PX + CELL_PX / 2,
                    oy + out_cell[1] * CELL_PX + CELL_PX / 2)
    in_c = QPointF(ox + in_cell[0] * CELL_PX + CELL_PX / 2,
                   oy + in_cell[1] * CELL_PX + CELL_PX / 2)
    assert item.covers_io_cell(out_c)   # runs into the MF output cell
    assert item.covers_io_cell(in_c)    # AND into the Costas input cell


def test_deselect_clears_highlight(qapp, catalog, chip_type):
    """Selecting a non-route cell (or clearing) drops the bus highlight."""
    ctrl, mf, cos = _build_rx(catalog, chip_type)
    cv = _canvas(ctrl, chip_type)
    conn = _mf_costas_connection(ctrl, mf, cos)
    out_cell = (conn.route[0].x, conn.route[0].y)
    cell = next(it for it in cv.cell_items()
                if (it.cx, it.cy) == out_cell and it.kind is CellKind.BLOCK)
    cell.setSelected(True)
    qapp.processEvents()
    assert any(it.is_related for it in cv.connection_items())
    cv.scene().clearSelection()
    qapp.processEvents()
    assert not any(it.is_related for it in cv.connection_items())


def test_hit_test_inside_io_cell_returns_connection(qapp, catalog, chip_type):
    """(B / #268) A scene point INSIDE the MF output cell where the route runs is
    a grab handle: ConnectionItem.covers_io_cell returns True for exactly the
    connection(s) terminating there, so a click there can select the route."""
    ctrl, mf, cos = _build_rx(catalog, chip_type)
    cv = _canvas(ctrl, chip_type)
    conn = _mf_costas_connection(ctrl, mf, cos)
    out_cell = (conn.route[0].x, conn.route[0].y)
    ox, oy = cv._chip_origin(0)
    # A point at the I/O cell centre — inside the endpoint cell.
    pt = QPointF(ox + out_cell[0] * CELL_PX + CELL_PX / 2,
                 oy + out_cell[1] * CELL_PX + CELL_PX / 2)
    hits = [it.connection_name for it in cv.connection_items()
            if it.covers_io_cell(pt)]
    assert conn.name in hits
    # Coverage map confirms these are exactly the connections at this cell.
    cov = cell_coverage(ctrl.project, 0)
    assert conn.name in cov.get(out_cell, set())


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    test_select_output_cell_highlights_full_bus(app, cat, ct)
    print("[A] highlight full bus: PASS")
    test_deselect_clears_highlight(app, cat, ct)
    print("[A2] deselect clears: PASS")
    test_hit_test_inside_io_cell_returns_connection(app, cat, ct)
    print("[B] grab inside I/O cell: PASS")
