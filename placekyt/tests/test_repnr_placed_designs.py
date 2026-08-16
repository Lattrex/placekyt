# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-P&R on ALREADY-PLACED designs (the open-a-shipped-.kyt-and-press-Auto-P&R
user path).

Two defects guarded here (both verified live on examples/qpsk_modem/qpsk_modem.kyt,
2026-08-13):

1. DOUBLE-ROTATION: the planners (serpentine ``_wh`` + CP-SAT footprints) model
   each block's CANONICAL shape and the apply step applies a plan's orientation
   kind as a RELATIVE ``Placement.transform`` — but a re-opened .kyt's blocks
   still carry the previous P&R's rotation, so every applied plan was
   double-rotated: feasible CP-SAT packs landed overlapping/off-grid on EVERY
   attempt ("blocks 'complexcostasloop' and 'gardnertimingrecovery' overlap at
   cell (6,1)"). Fix: auto_pnr canonicalizes block orientations before the sweep.

2. MANGLING ON FAILURE: the sweep clears routes per attempt; a total placement
   failure raised with the design left at ZERO routes — a shipped .kyt destroyed
   by a failed re-P&R. Fix: the full pre-sweep state (placements + routes) is
   restored verbatim before the named failure is raised.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
QPSK_KYT = REPO / "examples" / "qpsk_modem" / "qpsk_modem.kyt"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and QPSK_KYT.exists()),
    reason="chip yaml / qpsk .kyt absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _open_qpsk(catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(QPSK_KYT))
    ct = load_chip_type(str(CT_PATH))
    return ctrl, ct


def _state(ctrl):
    return ({b.name: [(c.x, c.y, c.cell_id) for c in b.placement.cells]
             for b in ctrl.project.blocks if b.placement is not None},
            {c.name: copy.deepcopy(c.route) for c in ctrl.project.connections})


def test_repnr_on_shipped_qpsk_routes_fully(qapp, catalog):
    """The user path: open the shipped (packed, previously-oriented) qpsk .kyt
    and re-run auto-P&R — every net routes and the build is clean. Before the
    canonicalization fix this raised PlacementError on every attempt."""
    ctrl, ct = _open_qpsk(catalog)
    rep = ctrl.auto_pnr({"kyttar_10x12": ct}, use_bus="always")
    assert rep.ok, [f"{r.name}:{r.reason}" for r in rep.results if not r.ok]
    assert sum(1 for r in rep.results if r.ok) == len(ctrl.project.connections)
    res = ctrl.build()
    assert res.ok, res.errors


def test_total_pnr_failure_restores_placements_and_routes(qapp, catalog, monkeypatch):
    """When EVERY sweep attempt fails placement, the design is restored VERBATIM
    (placements + routes) before the named failure raises — a failed re-P&R can
    never mangle a shipped .kyt (previously it left zero routes)."""
    from engine.errors import PlacementError

    ctrl, ct = _open_qpsk(catalog)
    before_placements, before_routes = _state(ctrl)
    assert all(r is not None for r in before_routes.values()), \
        "shipped .kyt must start fully routed"

    def _always_fail(self, *a, **k):
        raise PlacementError("forced: every attempt fails placement")
    monkeypatch.setattr(AppController, "auto_place", _always_fail)

    with pytest.raises(PlacementError):
        ctrl.auto_pnr({"kyttar_10x12": ct}, use_bus="always", time_budget_s=5.0)

    after_placements, after_routes = _state(ctrl)
    assert after_placements == before_placements, "placements not restored"
    assert after_routes == before_routes, "routes not restored"


def test_canonicalize_block_orientations_resets_shape(qapp, catalog):
    """The canonicalization helper: a rotated+mirrored block returns to its
    as-authored cell shape (same min corner) and an empty orientation history;
    an already-canonical block is untouched."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("canon", "kyttar_10x12")
    b = ctrl.place_block("GardnerTimingRecovery", 0, 2, 2,
                         library="lattrex.official", params={})
    blk = ctrl.project.block(b)
    canonical = [(c.x, c.y, c.cell_id, c.face) for c in blk.placement.cells]

    blk.placement.transform("cw")
    blk.placement.transform("mirror_h")
    assert blk.placement.orientation, "transform must record the ops"
    assert [(c.x, c.y, c.cell_id, c.face)
            for c in blk.placement.cells] != canonical

    ctrl._canonicalize_block_orientations(0)
    assert blk.placement.orientation == []
    assert [(c.x, c.y, c.cell_id, c.face)
            for c in blk.placement.cells] == canonical
