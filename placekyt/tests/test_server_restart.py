"""The GNURadio server can be stopped and restarted on the SAME port.

stop() used to close the socket but leave the serve thread blocked in accept(),
holding the listening port — so re-enabling "Run as GNURadio Server" failed with
"Address already in use". stop() now shuts down + joins the thread so the port is
freed before a restart.
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

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import DEMO_DIR  # noqa: E402
KYT = DEMO_DIR / "reroute_ends_on_target_cell.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and KYT.exists()), reason="chip yaml / fixture absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_server_start_stop_restart_same_port(qapp):
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(KYT)))
    sim = SimController(ctrl)
    # 3 cycles on a fixed port — each restart must succeed (port released on stop).
    PORT = 58951  # not the demo's 58950, to avoid clashing a real session
    try:
        for _ in range(3):
            bound = sim.start_gnuradio_server(port=PORT)
            assert bound == PORT, f"expected to bind {PORT}, got {bound}"
            assert sim.gr_server_running
            sim.stop_gnuradio_server()
            assert not sim.gr_server_running
    finally:
        if sim.gr_server_running:
            sim.stop_gnuradio_server()
