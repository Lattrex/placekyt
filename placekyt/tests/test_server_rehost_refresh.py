"""The server rebuild (on a live edit) refreshes the canvas — no phantom routing cells.

After the GNURadio server rebuilds + re-hosts the chip because the design was edited
since the run started (build_dirty), the canvas must FULL-render so the displayed cells
match the freshly-built chip. Otherwise routing cells from a route the user edited mid-
session linger as "phantom" items while the new (correct) bitstream runs underneath
(the user's blue-box artifact). The run itself was already correct; this is the display.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402
from ui.canvas.chip_canvas import ChipCanvas  # noqa: E402
from ui.canvas.cell_item import CellItem  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402
from commands import SetConnectionRouteCommand  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import DEMO_DIR  # noqa: E402
KYT = DEMO_DIR / "reroute_ends_on_target_cell.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and KYT.exists()), reason="chip yaml / fixture absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_rehost_emits_and_render_has_no_ghost_cells(qapp):
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    proj = load_project(str(KYT))
    ctrl = AppController(catalog=cat)
    ctrl.set_project(proj)
    sim = SimController(ctrl)
    fired = {"n": 0}
    sim.chip_rehosted.connect(lambda: fired.__setitem__("n", fired["n"] + 1))
    port = sim.start_gnuradio_server()
    assert port
    try:
        # Reroute the Costas->Gardner net to a different valid path → build_dirty.
        cg = next(c for c in ctrl.project.connections
                  if isinstance(c.source, BlockEndpoint)
                  and isinstance(c.target, BlockEndpoint)
                  and "costas" in c.source.block.lower()
                  and "gardner" in c.target.block.lower())
        newpath = [(8, 4), (8, 5), (7, 5), (6, 5), (5, 5), (4, 5),
                   (3, 5), (2, 5), (2, 4), (2, 3), (3, 3)]
        ctrl.commands.execute(SetConnectionRouteCommand(ctrl.project, cg.name, None))
        ctrl.commands.execute(
            SetConnectionRouteCommand(ctrl.project, cg.name, newpath))
        # The server's pre-batch rebuild fires chip_rehosted (queued → process now).
        chip, err = sim._rebuild_if_dirty_threadsafe()
        qapp.processEvents()
        assert err is None and chip is not None
        assert fired["n"] >= 1, "rehost must signal the GUI to re-render"

        # A full render of the edited project leaves NO routing cell that isn't on
        # a CURRENT route (no phantom cells from the old path).
        canvas = ChipCanvas()
        canvas.set_project(ctrl.project, {"kyttar_10x12": ct})
        canvas.render_scene()
        live = set()
        for c in ctrl.project.connections:
            if c.is_routed:
                live |= {(p.x, p.y) for p in c.route}
        ghosts = sorted({(it.cx, it.cy) for it in canvas._scene.items()
                         if isinstance(it, CellItem)
                         and getattr(it, "route_name", None) is not None
                         and (it.cx, it.cy) not in live})
        assert not ghosts, f"phantom routing cells off all current routes: {ghosts}"
    finally:
        sim.stop_gnuradio_server()
