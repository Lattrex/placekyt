"""Smart route delete (#267): sole-occupant route removes its cells; a route
sharing a multiplexed bus keeps the cells, breaks only its own link to a fly
line, and leaves the co-tenant routed.

The fabric bus is TIME-MULTIPLEXED (the auto-P&R design notes §1.2): two logical
connections can share the SAME physical routing cells. Deleting one must not rip
out cells the other still uses.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_route_smart_delete.py -x
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.route_analysis import cell_coverage, exclusive_route_cells  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
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


def _two_block_project(catalog):
    """Two single-cell blocks (gain at (0,3), agc at (9,3)) with manual routes so
    we control exactly which cells each connection covers."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("smartdel", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 0, 3)
    b = ctrl.place_block("AGCBlock", 0, 9, 3)
    return ctrl, a, b


def test_sole_occupant_route_removes_cells(qapp, catalog):
    """A route no other connection shares: deleting it removes its transit cells
    (the route becomes an unrouted fly line; its cells vanish)."""
    ctrl, a, b = _two_block_project(catalog)
    # One block-to-block route along row 3 (its own cells; nothing else routes).
    path = [(x, 3) for x in range(0, 10)]
    name = ctrl.add_route(BlockEndpoint(block=a, port="out"),
                          BlockEndpoint(block=b, port="in"), path)

    excl = exclusive_route_cells(ctrl.project, ctrl.project.connection(name))
    # Every transit cell (1..8, 3) is exclusive — endpoints (0,3)/(9,3) are block
    # cells and are NOT reported as removable transit cells.
    assert (1, 3) in excl and (8, 3) in excl
    assert (0, 3) not in excl and (9, 3) not in excl

    cmd = ctrl.delete_route(name)
    assert cmd.shared is False
    assert set(cmd.removed_cells) == set(excl)
    # The logical link is preserved (now an unrouted fly line) and the physical
    # route is gone.
    conn = ctrl.project.connection(name)
    assert conn is not None and not conn.is_routed
    # No cell of the deleted route is still covered.
    cov = cell_coverage(ctrl.project, 0)
    for cell in excl:
        assert name not in cov.get(cell, set())

    # Undoable: restores the full route.
    ctrl.undo()
    conn = ctrl.project.connection(name)
    assert conn.is_routed
    assert [(p.x, p.y) for p in conn.route] == path


def test_shared_bus_route_keeps_cells_and_makes_flyline(qapp, catalog):
    """Two connections share the SAME physical cells (a multiplexed bus). Deleting
    one keeps every shared cell, breaks only that connection's link to a fly
    line, and the co-tenant connection stays routed."""
    ctrl, a, b = _two_block_project(catalog)
    c = ctrl.place_block("DCBlockerBlock", 0, 0, 5, params={"length": 2, "long_form": False})
    # Both connections traverse the SAME row-3 lane cells (1..8, 3). net1 goes
    # gain->agc; net2 goes gain->dcblocker but is hand-routed down the SAME lane
    # then drops to (0,5) — so cells (1..8,3) are MULTIPLEXED between the two.
    lane = [(x, 3) for x in range(0, 10)]
    net1 = ctrl.add_route(BlockEndpoint(block=a, port="out"),
                          BlockEndpoint(block=b, port="in"), lane)
    # net2: share the lane, then come back along row 3 is not allowed (repeat),
    # so route gain(0,3) along the lane to (9,3) then down/around is long; instead
    # share a SUBSET: (0,3)->(1,3)->...(4,3)->(4,4)->(4,5)->...->(0,5). Cells
    # (1,3)..(4,3) are shared with net1.
    net2_path = ([(x, 3) for x in range(0, 5)]
                 + [(4, 4), (4, 5)] + [(x, 5) for x in range(3, -1, -1)])
    net2 = ctrl.add_route(BlockEndpoint(block=a, port="out"),
                          BlockEndpoint(block=c, port="in"), net2_path)

    cov = cell_coverage(ctrl.project, 0)
    shared_cells = [(x, 3) for x in range(1, 5)]
    for cell in shared_cells:
        assert cov[cell] == {net1, net2}, (cell, cov.get(cell))

    # Delete net1 (the shared-bus branch).
    cmd = ctrl.delete_route(net1)
    assert cmd.shared is True
    assert cmd.removed_cells == []        # nothing physically removed

    # net1 is now an unrouted fly line; net2 is still routed.
    conn1 = ctrl.project.connection(net1)
    conn2 = ctrl.project.connection(net2)
    assert conn1 is not None and not conn1.is_routed   # broken → fly line
    assert conn2 is not None and conn2.is_routed       # co-tenant untouched

    # The shared cells STAY — still covered by net2.
    cov = cell_coverage(ctrl.project, 0)
    for cell in shared_cells:
        assert net2 in cov.get(cell, set())
        assert net1 not in cov.get(cell, set())

    # Undo restores net1's route.
    ctrl.undo()
    conn1 = ctrl.project.connection(net1)
    assert conn1.is_routed
    assert [(p.x, p.y) for p in conn1.route] == lane


def test_delete_handler_uses_smart_delete(qapp, catalog):
    """The MainWindow delete handler routes through smart delete: a routed
    connection becomes a fly line (link kept), not fully removed."""
    from ui.main_window import MainWindow

    ctrl, a, b = _two_block_project(catalog)
    path = [(x, 3) for x in range(0, 10)]
    name = ctrl.add_route(BlockEndpoint(block=a, port="out"),
                          BlockEndpoint(block=b, port="in"), path)
    w = MainWindow(controller=ctrl)
    w._on_delete_connection(name)
    conn = ctrl.project.connection(name)
    assert conn is not None and not conn.is_routed   # kept as a fly line


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    test_sole_occupant_route_removes_cells(app, cat)
    print("[i] sole-occupant removes cells: PASS")
    test_shared_bus_route_keeps_cells_and_makes_flyline(app, cat)
    print("[ii] shared bus keeps cells + fly line: PASS")
    test_delete_handler_uses_smart_delete(app, cat)
    print("[iii] handler uses smart delete: PASS")
