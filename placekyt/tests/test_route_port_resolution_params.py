# SPDX-License-Identifier: GPL-3.0-or-later
"""Drawing a route to/from a PARAM-DEPENDENT complex block must resolve the REAL
port (yi_e / xi / yi_tap …), NOT the param-less default (the real-mode ``out``).

Root cause of a real user-seen bug: ``MainWindow._on_route_completed`` resolved a
block's port via ``catalog.port_map(blk.type, library=…)`` WITHOUT the block's
params. For a ``complex=True`` GardnerTimingRecovery the param-less PortMap is the
REAL-mode block whose single output is ``out`` — a port the complex block does NOT
have (it emits ``yi_e``/``yq_e``). So drawing a wire Gardner→slicer created a phantom
net ``gardner.out -> slicer.in_i`` (a stray fly line that moved with the block), while
the real yi_e/yq_e rails stayed unrouted and the slicer got no Q → no output.

This test drives the ACTUAL GUI route-completion signal path (the thing the explicit-
port-name unit tests never exercised) and asserts the created net names the real
param-dependent port. INV-6/11: resolve ports WITH params.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_route_port_resolution_params.py -q
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _main_window(qapp):
    """A real MainWindow whose controller we drive; the window is what owns the
    ``_on_route_completed`` handler (the GUI port-resolution path under test)."""
    from ui.main_window import MainWindow
    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    win = MainWindow(controller=ctrl)
    return win


def _net_from(ctrl, src_block):
    """The (src_port, dst_block, dst_port) of the net whose source is ``src_block``."""
    for c in ctrl.project.connections:
        s = c.source
        if isinstance(s, BlockEndpoint) and s.block == src_block:
            t = c.target
            return (s.port, getattr(t, "block", None), getattr(t, "port", None))
    return None


def test_route_from_complex_gardner_resolves_yi_e_not_out(qapp):
    """Drawing a route from a complex=True Gardner's output cell to the QPSK slicer
    must create a net sourced from ``yi_e`` — NOT the phantom real-mode ``out``."""
    win = _main_window(qapp)
    ctrl = win.controller
    ctrl.new_project("t", "kyttar_10x12")
    lib = "lattrex.official"
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 6, library=lib,
                           params={"complex": True})
    sli = ctrl.place_block("QPSKSlicerBlock", 0, 6, 8, library=lib)

    # The canvas emits route_completed(source, target, points) with block NAMES for
    # a block→block route (its cells resolve to the block; the handler resolves the
    # port). Fire the handler exactly as the canvas signal would.
    win._on_route_completed(gar, sli, points=[(0, 7), (6, 8)])

    net = _net_from(ctrl, gar)
    assert net is not None, "no net created from the Gardner"
    src_port, dst_block, dst_port = net
    assert src_port != "out", (
        "PHANTOM: route resolved the complex Gardner output to the real-mode 'out' "
        "port (which the complex block does not have) — params were dropped (INV-6/11)")
    assert src_port == "yi_e", f"expected yi_e, got {src_port}"
    # and it must NOT have spawned a phantom 'out' net anywhere
    outs = [c.name for c in ctrl.project.connections
            if isinstance(c.source, BlockEndpoint)
            and c.source.block == gar and c.source.port == "out"]
    assert not outs, f"phantom gardner.out net(s) created: {outs}"


def test_route_from_order4_costas_resolves_yi_tap(qapp):
    """A route from an order-4 Costas output resolves the recovered tap ``yi_tap``
    (present in the order-4 PortMap), not a param-less default."""
    win = _main_window(qapp)
    ctrl = win.controller
    ctrl.new_project("t", "kyttar_10x12")
    lib = "lattrex.official"
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 3, library=lib,
                           params={"order": 4})
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 6, library=lib,
                           params={"complex": True})
    win._on_route_completed(cos, gar, points=[(0, 4), (0, 5)])
    net = _net_from(ctrl, cos)
    assert net is not None and net[0] == "yi_tap", \
        f"order-4 Costas output should resolve to yi_tap, got {net}"
