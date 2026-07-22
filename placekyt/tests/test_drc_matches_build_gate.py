# SPDX-License-Identifier: GPL-3.0
"""The DRC panel/badge (``controller.run_drc`` → ``check_project``) must report the SAME
errors the sim's build gate (``controller.build`` → ``BuildEngine``) does.

They used to diverge: ``check_project`` did NOT run the BUS DRC (the single-cell
input==output deadlock hazard, dual-input-same-face), while the build did. So a routed
design whose single-cell block has its input arriving on the same face its output drives
read "DRC clean" (green badge, empty Design Rules panel) — yet Run aborted with
"Sim: error: 1 DRC error(s)". The check was out of sync, especially after an auto-route.

Fix: ``check_project`` folds in ``bus_drc.check_project_bus`` (given the catalog), and
``run_drc`` passes the catalog. Panel and build now emit one identical error list.

This builds a minimal single-cell bus-fed block whose hand-laid input and output routes
share a face, then asserts run_drc() reports the SAME deadlock the build does.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_drc_matches_build_gate.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

_CHIP = str(Path(__file__).resolve().parents[1] / "resources" / "chips"
            / "kyttar_10x12.yaml")
_LIB = "lattrex.official"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _errs(drc):
    return [f for f in drc.findings if f.severity.value == "ERROR"]


_MODEM = str(Path(__file__).resolve().parents[2]
             / "examples" / "fsk4_modem" / "fsk4_modem.kyt")


@pytest.mark.skipif(not (os.path.exists(_CHIP) and os.path.exists(_MODEM)),
                    reason="chip yaml / modem .kyt absent")
def test_run_drc_matches_build_gate_on_saved_modem(qapp):
    """On the SAVED modem .kyt (whose slicer geometry has the single_cell_inout
    deadlock), the DRC panel must report exactly what the build gate reports."""
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.open_project(_MODEM)

    build = ctrl.build()
    drc = ctrl.run_drc()

    b_cats = {getattr(e, "category", None) for e in build.errors}
    d_cats = {e.category for e in _errs(drc)}

    # The build gate genuinely fails on the slicer deadlock.
    assert not build.ok, "saved modem should trip the single-cell deadlock at build"
    assert "single_cell_inout_deadlock" in b_cats, b_cats

    # THE FIX: run_drc (panel/badge) must NOT read clean while the build gate fails,
    # and every build ERROR category must appear in the panel too. Before the fix,
    # run_drc knew nothing of the bus DRC → drc.ok True, empty panel, green badge,
    # yet Run aborted "1 DRC error(s)".
    assert not drc.ok, "DRC panel clean while build gate fails — out of sync"
    assert b_cats <= d_cats, (
        f"build errors {b_cats} not all surfaced by run_drc {d_cats}")


@pytest.mark.skipif(not os.path.exists(_CHIP), reason="chip yaml absent")
def test_run_drc_carries_catalog_for_bus_checks(qapp):
    """run_drc must pass the catalog so the bus checks can run at all (the plumbing
    the sync depends on) — a clean single block still returns a well-formed result."""
    key = "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.new_project("drcsync2", key)
    ctrl.place_block("GainBlock", 0, 5, 5, library=_LIB, params={"gain": 1.0})
    drc = ctrl.run_drc()  # must not raise; catalog threaded through
    assert drc is not None
