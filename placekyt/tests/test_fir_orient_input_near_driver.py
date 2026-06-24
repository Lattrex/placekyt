# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression: the FIR's input cell must stay NEAREST its driver after the FULL
place-and-route flow (auto_place THEN auto_route_all).

The flyline-minimising auto-orient (autoplace ``_orient_for``, §8/§4.3) turns the
20-tap FIR's as-authored vertical column ``ccw`` so its INPUT cell lands nearest
its driver (the gain output) and its OUTPUT nearest the consumer (x16_out). That
was correct after ``auto_place`` alone — but ``auto_route_all`` ran a SECOND,
older orient pass (``AutoRouter.orient_for_flow``) that scored against the bare
as-authored PortMap, ignoring the already-applied ``ccw``. It therefore
re-recommended ``ccw`` and applied it on top (ccw∘ccw = 180°), flipping the FIR
back to a column with its input FARTHEST from the driver.

This test runs the COMPLETE flow (place + route, as the GUI / GRC import does)
and asserts the input-near-driver invariant survives it — the existing
``test_autoplace_flyline_min`` only checked the post-place state, before the
route-pass re-orient, so it missed the regression.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
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


def _io_cells(ctrl, catalog, name):
    b = ctrl.project.block(name)
    pm = catalog.port_map(b.type, params=b.params, library=b.library)
    ins = [(b.placement.cell(p.cell_id).x, b.placement.cell(p.cell_id).y)
           for p in pm.inputs() if b.placement.cell(p.cell_id)]
    outs = [(b.placement.cell(p.cell_id).x, b.placement.cell(p.cell_id).y)
            for p in pm.outputs() if b.placement.cell(p.cell_id)]
    return ins, outs


def _man(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _run_flow(catalog, chip_type, taps, use_bus):
    """gain → FIR(``taps``-tap) → x16_out through the FULL place+route flow, with
    the router selected by ``use_bus`` (the GUI default is ``"always"`` — the
    BUS/BROKER path; ``None`` exercises the heuristic path). Returns the controller
    plus the route report so the caller can assert the input-near-driver invariant."""
    coeffs = [0.05] * taps

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("fir", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official")
    f = ctrl.place_block("FIRFilterBlock", 0, 5, 5,
                         library="lattrex.official",
                         params={"coefficients": coeffs})
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=g, port="sample"), name="in_g")
    ctrl.add_logical_connection(
        BlockEndpoint(block=g, port="out"),
        BlockEndpoint(block=f, port="sample"), name="g_f")
    ctrl.add_logical_connection(
        BlockEndpoint(block=f, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="f_out")

    ctrl.auto_place(0)
    kwargs = {"auto_orient": True}
    if use_bus is not None:
        kwargs["use_bus"] = use_bus
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, **kwargs)
    return ctrl, g, f, rep


# The GUI's GRC-import path calls auto_route_all(use_bus="always") — the BUS/BROKER
# router. The earlier regression only ran the default heuristic path, so it missed
# that the route-pass re-orient flipped the FIR on the GUI path too. Cover BOTH
# router paths AND a 20- and 40-tap FIR (the footprint/orientation differs by size).
@pytest.mark.parametrize("use_bus", [None, "always"])
@pytest.mark.parametrize("taps", [20, 40])
def test_fir_input_near_driver_after_place_and_route(qapp, catalog, chip_type,
                                                     taps, use_bus):
    """gain → FIR → x16_out: after auto_place AND auto_route_all the FIR's input
    cell is at least as near its driver (gain output) as its output cell is, and
    the net still routes — on BOTH the heuristic and the use_bus='always' (GUI)
    router paths, for a 20- and a 40-tap FIR."""
    ctrl, g, f, rep = _run_flow(catalog, chip_type, taps, use_bus)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]

    _, go = _io_cells(ctrl, catalog, g)
    fi, fo = _io_cells(ctrl, catalog, f)
    dist_in = _man(fi[0], go[0])
    dist_out = _man(fo[0], go[0])
    assert dist_in <= dist_out, (
        f"FIR({taps} taps, use_bus={use_bus!r}) input {fi[0]} must be at least as "
        f"near its driver {go[0]} as its output {fo[0]} (in={dist_in}, "
        f"out={dist_out}) AFTER place+route")


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    for _taps in (20, 40):
        for _bus in (None, "always"):
            test_fir_input_near_driver_after_place_and_route(
                app, cat, ct, _taps, _bus)
    print("FIR orient input-near-driver after place+route: PASS")
