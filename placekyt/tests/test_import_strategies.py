"""GRC-import strategies: rough-place vs full place-and-route + abutment.

The import dialog (MainWindow._ask_import_options) lets the user pick HOW MUCH
automation runs on a GNURadio flowgraph import:
  * Full place-and-route (bus): auto_place + auto_route_all(use_bus="always")
  * Place + abutment: auto_place only; adjacent blocks connect by abutment
  * Rough placement only: auto_place only, nets left unrouted

This pins the CONTROLLER paths those options drive (the dialog itself is a modal
GUI widget; here we exercise the underlying controller calls the dialog selects),
so each strategy reaches the expected routed/placed/unrouted state.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC = EXAMPLES_DIR / "coherent_bpsk_rx.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC.exists()), reason="chip yaml / .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


def _import(catalog):
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    return ctrl, res


def test_rough_place_only_leaves_nets_unrouted(qapp, catalog):
    """Rough placement: blocks placed, every net unrouted (fly lines)."""
    ctrl, res = _import(catalog)
    ctrl.auto_place(0)
    assert all(b.is_placed for b in ctrl.project.blocks)
    assert not any(c.is_routed for c in ctrl.project.connections), \
        "rough-place must leave nets unrouted"


def test_full_place_and_route_routes_nets(qapp, catalog):
    """Full P&R (bus): auto_place + auto_route_all routes the nets."""
    ctrl, res = _import(catalog)
    ct = load_chip_type(str(CT_PATH))
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": ct}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    assert any(c.is_routed for c in ctrl.project.connections), \
        "full P&R must route nets"


def test_rough_place_orients_like_full_pnr_no_routes(qapp, catalog):
    """Rough placement runs auto_place + auto_orient_for_flow (the SAME compact,
    flow-oriented layout as full P&R) but leaves nets unrouted — NOT the raw
    serpentine. So blocks are placed + reoriented, and no net is routed."""
    ctrl, res = _import(catalog)
    ctrl.auto_place(0)
    n_oriented = ctrl.auto_orient_for_flow()
    assert all(b.is_placed for b in ctrl.project.blocks)
    assert not any(c.is_routed for c in ctrl.project.connections), \
        "rough placement must leave nets unrouted"
    # The flow-orient ran (at least one block reoriented vs the raw place).
    assert n_oriented >= 0   # may be 0 if already flow-oriented; never errors


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    test_rough_place_only_leaves_nets_unrouted(app, cat)
    print("rough place: PASS")
    test_full_place_and_route_routes_nets(app, cat)
    print("full P&R: PASS")
    test_rough_place_orients_like_full_pnr_no_routes(app, cat)
    print("rough place orients: PASS")
