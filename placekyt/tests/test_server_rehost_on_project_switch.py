# SPDX-License-Identifier: GPL-3.0-or-later
"""The GRC server must re-host when the placeKYT project changes (offscreen Qt).

User-reported (2026-08-13): after serving gain_2p2s (multi-chip), opening the
single-chip gain/bpsk .kyt and re-running their flowgraphs hit the STALE
MultiChipSimServer still bound to 58950 — the gain example died with
"unknown op 'process_batch' (multichip)" and the BPSK duplex RPC recovered 0
words (streams resolved against the wrong design). Two fixes guarded here:

  1. ``SimController.start_gnuradio_server`` is idempotent ONLY for the same
     project; a DIFFERENT project restarts the server (same port kept) — a
     single<->multi chip switch needs a different server CLASS, which the
     single-chip per-batch rebuild can never provide.
  2. ``MainWindow._after_project_loaded`` restarts a running server for the
     newly-loaded project, so File > Open transparently re-hosts.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.sim_bridge import send_message, recv_message  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
KYT_2P2S = REPO / "examples" / "gain_2p2s" / "gain_2p2s.kyt"
KYT_GAIN = REPO / "examples" / "gain" / "gain.kyt"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and KYT_2P2S.exists() and KYT_GAIN.exists()),
    reason="chip yaml / example .kyts absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _rpc(port, header, payload=None):
    c = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        send_message(c, header, payload)
        return recv_message(c)
    finally:
        c.close()


def test_start_server_rehosts_after_project_switch(qapp, catalog):
    """multi -> single -> multi: each start after a project switch replaces the
    server (same port), and the SINGLE-chip server actually serves the
    single-stream ``process_batch`` the stale multichip one rejected."""
    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(KYT_2P2S))
    sim = SimController(ctrl)
    port = sim.start_gnuradio_server(port=0)
    try:
        assert port is not None and sim._multi is True
        reply, _ = _rpc(port, {"op": "ping"})
        assert reply.get("multichip") is True

        # Same project, second call: idempotent — same server, same port.
        assert sim.start_gnuradio_server(port=0) == port
        assert sim._multi is True

        # Switch to the single-chip gain project: the next start must REPLACE
        # the server (multichip class cannot serve it), keeping the port.
        ctrl.open_project(str(KYT_GAIN))
        port2 = sim.start_gnuradio_server(port=0)
        assert port2 == port, "port must be preserved across the re-host"
        assert sim._multi is False
        reply, _ = _rpc(port2, {"op": "ping"})
        assert not reply.get("multichip"), reply
        # The op the stale server rejected now WORKS — real data through the
        # single-chip gain design (0.5x).
        reply, out = _rpc(port2, {"op": "process_batch", "port": "x16_out",
                                  "in_port": "x16_in", "complex": False,
                                  "raw": False, "data_addrs": [0]},
                          np.asarray([0.5, -0.5], dtype="<f4"))
        assert reply.get("ok"), reply
        vals = np.asarray(out, dtype=np.float32)
        assert len(vals) == 2 and abs(float(vals[0]) - 0.25) < 2e-3, vals

        # And back to multi-chip.
        ctrl.open_project(str(KYT_2P2S))
        port3 = sim.start_gnuradio_server(port=0)
        assert port3 == port and sim._multi is True
        reply, _ = _rpc(port3, {"op": "ping"})
        assert reply.get("multichip") is True
    finally:
        sim.stop_gnuradio_server()


def test_project_load_stops_server_before_clearing_traces(qapp, catalog, monkeypatch):
    """ORDER gate: stop_gnuradio_server's final trace drain must land BEFORE the
    waveform clear — the reverse order repopulated the panel with the previous
    project's residual traces (user-reported: BPSK traces survived opening
    gain_2p2s)."""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win.controller.open_project(str(KYT_GAIN))
        win._after_project_loaded()
        assert win.sim.start_gnuradio_server(port=0) is not None

        calls = []
        real_stop = win.sim.stop_gnuradio_server
        real_clear = win.waveform_panel.clear_traces
        monkeypatch.setattr(win.sim, "stop_gnuradio_server",
                            lambda: (calls.append("stop"), real_stop())[1])
        monkeypatch.setattr(win.waveform_panel, "clear_traces",
                            lambda: (calls.append("clear"), real_clear())[1])

        win.controller.open_project(str(KYT_2P2S))
        win._after_project_loaded()
        assert calls[:2] == ["stop", "clear"], calls
    finally:
        win.sim.stop_gnuradio_server()
        win.close()


def test_open_project_hook_rehosts_running_server(qapp, catalog):
    """The GUI hook: with a server running, loading a new project through
    ``MainWindow._after_project_loaded`` re-hosts it for the new design."""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win.controller.open_project(str(KYT_2P2S))
        win._after_project_loaded()
        port = win.sim.start_gnuradio_server(port=0)
        assert port is not None and win.sim._multi is True

        win.controller.open_project(str(KYT_GAIN))
        win._after_project_loaded()          # must transparently re-host
        assert win.sim._gr_server is not None, "server must stay up"
        assert win.sim._gr_server.bound_port == port
        assert win.sim._multi is False
        reply, _ = _rpc(port, {"op": "ping"})
        assert not reply.get("multichip"), reply
    finally:
        win.sim.stop_gnuradio_server()
        win.close()
