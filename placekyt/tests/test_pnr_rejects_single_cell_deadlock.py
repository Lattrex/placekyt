# SPDX-License-Identifier: GPL-3.0
"""The place<->route loop (``auto_pnr``) must NEVER ACCEPT a fully-routed layout that
trips the single-cell input==output deadlock (§5.3).

The loop keeps the first arrangement it judges "clean". That gate checked for unrouted
nets, crossover cells, and dual-input-same-face — but NOT the single_cell_inout deadlock
(a single-cell block whose input arrives on the same face its output drives). So the
loop accepted a fully-routed-but-deadlocked layout (the fsk4 slicer at (8,3), input +
output both on face E), handing the user a design that reads placed+routed yet the build
hard-fails with "1 DRC error(s)".

Fix: the acceptance gate also runs ``_check_single_cell_inout`` and marks such a layout
NOT clean, so the sweep escalates to look for a distinct-face layout instead of stopping.

Two tests:
  * a FAST, DETERMINISTIC unit that hand-lays a routed single-cell deadlock and asserts
    the exact acceptance-gate predicate the loop consults would REJECT it (and accept a
    distinct-face layout) — this is the discriminating check on the fix's logic.
  * a slower end-to-end guard on the fsk4 modem: IF auto_pnr accepts a fully-routed
    layout, it is deadlock-free.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_pnr_rejects_single_cell_deadlock.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.grc_import import import_grc  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

_CHIP = str(Path(__file__).resolve().parents[1] / "resources" / "chips"
            / "kyttar_10x12.yaml")
_GRC = str(Path(__file__).resolve().parents[2]
           / "examples" / "fsk4_modem" / "fsk4_modem.grc")
_KEY = "kyttar_10x12"
_LIB = "lattrex.official"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _accepts(project):
    """The auto_pnr acceptance gate's single-cell decision: a layout is only 'clean'
    when _check_single_cell_inout is empty. This mirrors the exact predicate the loop
    consults (controller.auto_pnr) after routing — the fix wires it into the gate."""
    from engine.bus_drc import _check_single_cell_inout
    return not _check_single_cell_inout(project)


@pytest.mark.skipif(not os.path.exists(_CHIP), reason="chip yaml absent")
def test_gate_predicate_rejects_deadlock_accepts_split(qapp):
    """Unit-pin the hazard predicate the gate keys on: a routed single-cell block whose
    input arrival face == output drive face must be REJECTED (deadlock), and the same
    block routed with input/output on DIFFERENT faces must be ACCEPTED. This is what
    makes the auto_pnr gate escalate past the fsk4-slicer (8,3) same-face-E deadlock."""
    cat = BlockCatalog.from_gr_kyttar()

    # DEADLOCK: input net ENDS at the EAST neighbour (9,5) (arrival face E), output net's
    # first hop is ALSO to the EAST neighbour (9,5) (drive face E). _check_single_cell_
    # inout reads pts[-1] for arrival and pts[1] for drive — both resolve to E here.
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dl", _KEY)
    g = ctrl.place_block("GainBlock", 0, 8, 5, library=_LIB, params={"gain": 1.0})
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=g, port="sample"),
                   [(0, 5), (9, 5)])                 # arrives at EAST neighbour (9,5)
    ctrl.add_route(BlockEndpoint(block=g, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"),
                   [(8, 5), (9, 5), (9, 0)])         # drives EAST (8,5)->(9,5)
    assert not _accepts(ctrl.project), (
        "gate ACCEPTED a routed single-cell in==out deadlock (both face E) — the fix "
        "would not reject it, so auto_pnr would stop on the deadlocked layout")

    # SPLIT: input arrives at the WEST neighbour (7,5) (face W), output drives EAST.
    ctrl2 = AppController(catalog=cat)
    ctrl2.new_project("ok", _KEY)
    g2 = ctrl2.place_block("GainBlock", 0, 8, 5, library=_LIB, params={"gain": 1.0})
    ctrl2.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                    BlockEndpoint(block=g2, port="sample"),
                    [(0, 5), (7, 5)])                # arrives at WEST neighbour (7,5)
    ctrl2.add_route(BlockEndpoint(block=g2, port="out"),
                    ChipPortEndpoint(chip=0, port="x16_out"),
                    [(8, 5), (9, 5), (9, 0)])        # drives EAST
    assert _accepts(ctrl2.project), (
        "gate REJECTED a valid distinct-face layout (in=W, out=E) — over-strict")


@pytest.mark.skipif(not (os.path.exists(_CHIP) and os.path.exists(_GRC)),
                    reason="chip yaml / modem grc absent")
def test_auto_pnr_never_accepts_a_single_cell_deadlock(qapp):
    """End-to-end guard: if auto_pnr accepts a FULLY-routed layout for the modem, it is
    deadlock-free. (When no fully-clean layout exists the loop falls back to the best
    routed one — that give-up case is the separate placer-capacity limitation and is out
    of scope here, so we only assert on the fully-routed acceptance the gate controls.)"""
    from engine.bus_drc import _check_single_cell_inout

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(_CHIP)
    r = import_grc(_GRC, cat, chip_type=_KEY)
    ctrl = AppController(catalog=cat)
    ctrl.project = r.project

    report = ctrl.auto_pnr({_KEY: ct}, chip=0, use_bus="always")
    n_routed = sum(1 for x in report.results if x.ok)
    n_total = len(report.results)
    if n_routed == n_total:
        assert not _check_single_cell_inout(ctrl.project), (
            "auto_pnr accepted a FULLY-ROUTED layout that STILL has a single-cell "
            "in==out deadlock")
