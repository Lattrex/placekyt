# SPDX-License-Identifier: GPL-3.0-or-later
"""MultiChipSimServer — the GRC live bridge for a MULTI-CHIP (2P2S) design.

Step 5 of the parallel-chains work: a GRC flowgraph drives all chains over the
socket protocol, each stream addressed to WHICH chip (chain) it feeds via chip_id.
This proves the multi-chip front-end to the (already proven) MultiChipSimEngine
plumbing, as a SEPARATE server class that leaves the single-chip SimServer — the
one every shipped modem depends on — untouched.

The design: 4 chips, two parallel daisy-chains (A: 0->1, B: 2->3), each chip a
ROUTED gain tap (auto-P&R'd off the input port). Two 0.5x gains in series ->
0.25x at each tail. Both chains driven in one RPC with distinct stimulus; each
recovers its OWN input, demuxed by chain.

Requires the routed-input .so (skips on an older binary).

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_multichip_sim_server.py -q
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
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

_GRC = Path(__file__).resolve().parents[2] / "examples" / "gain" / "gain.grc"

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _auto_pnr_gain(catalog):
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    res = import_grc(str(_GRC), catalog, chip_type="kyttar_10x12")
    ctrl = AppController(catalog=catalog)
    ctrl.project = res.project
    ct = load_chip_type(str(CT_PATH))
    assert ctrl.auto_pnr({"kyttar_10x12": ct}).ok
    r = ctrl.build()
    assert r.ok
    il = list(r.chips[0].input_landings.values())[0]
    return r.chips[0].words, il


def _build_gain_on_bus_2p2s(catalog):
    """4-chip 2P2S, GAIN-ON-BUS @(1,0) per chip. Chain A head = chip0, chain B head
    = chip2. A head stream taps its gain (0.5x) and its output crosses the transparent
    inter-chip wire (hop composed to the tail) to x16_out -> 0.5x. Returns
    (build_result, gain_names)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("gob2p2s", "kyttar_10x12")
    for _ in range(3):
        ctrl.add_chip()
    gns = {}
    for chip in range(4):
        ctrl.place_block("GainBlock", chip, 1, 0, library="lattrex.official")
        gns[chip] = [b.name for b in ctrl.project.blocks
                     if b.placement and b.placement.chip == chip][-1]
        ctrl.add_route(BlockEndpoint(gns[chip], "out"),
                       ChipPortEndpoint(chip, "x16_out"),
                       [(x, 0) for x in range(1, 10)])
    ctrl.add_route(ChipPortEndpoint(0, "x16_in"),
                   BlockEndpoint(gns[0], "sample"), [(0, 0)])   # chain A head
    ctrl.add_route(ChipPortEndpoint(2, "x16_in"),
                   BlockEndpoint(gns[2], "sample"), [(0, 0)])   # chain B head
    ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
    ctrl.add_inter_chip(2, "x16_out", 3, "x16_in")
    r = ctrl.build()
    assert r.ok, [getattr(e, "category", None) for e in r.errors]
    return r, gns


def test_multichip_server_drives_both_chains(qapp, catalog):
    import simkyt
    if not hasattr(simkyt.MultiChipSimulation.new("probe", 5.0),
                   "set_port_input_routed"):
        pytest.skip("simkyt .so predates the routed-input relay flag")

    from engine.simulator import MultiChipSimEngine
    from engine.sim_bridge import MultiChipSimServer, send_message, recv_message

    r, gns = _build_gain_on_bus_2p2s(catalog)
    ct = str(CT_PATH)
    eng = MultiChipSimEngine({i: ct for i in range(4)})
    eng.connect(0, "x16_out", 1, "x16_in")   # chain A
    eng.connect(2, "x16_out", 3, "x16_in")   # chain B
    lands = {cid: (list(r.chips[cid].input_landings.values())[0]
                   if r.chips[cid].input_landings
                   else {"entry": 28, "hop": 29, "data_addrs": [0]})
             for cid in range(4)}
    for cid in range(4):
        eng.load(cid, r.words(cid), trace=True)
        il = lands[cid]
        eng.configure_input_port(cid, "x16_in", entry_addr=il["entry"],
                                 hop_count=il["hop"],
                                 data_addr=il["data_addrs"][0], routed=True)

    hopA, hopB = lands[0], lands[2]   # chain heads: chip0, chip2
    tg = {
        "chainA": {"chip_id": 0, "out_chip": 1, "entry_addr": hopA["entry"],
                   "hop_count": hopA["hop"], "data_addrs": hopA["data_addrs"],
                   "out_tag": None, "routed": True},
        "chainB": {"chip_id": 2, "out_chip": 3, "entry_addr": hopB["entry"],
                   "hop_count": hopB["hop"], "data_addrs": hopB["data_addrs"],
                   "out_tag": None, "routed": True},
    }
    srv = MultiChipSimServer(eng, tg)
    port = srv.start()
    try:
        c = socket.socket()
        c.connect(("127.0.0.1", port))
        send_message(c, {"op": "ping"})
        ping, _ = recv_message(c)
        assert ping.get("multichip") is True and ping.get("mode") == "batch"

        payload = np.array([0.5, 0.25, 0.75, 0.125], dtype=np.float32)
        send_message(c, {"op": "process_batch_multichip", "streams": [
            {"stream_id": "chainA", "chip_id": 0, "out_chip": 1,
             "complex": False, "raw": False, "n_samples": 2},
            {"stream_id": "chainB", "chip_id": 2, "out_chip": 3,
             "complex": False, "raw": False, "n_samples": 2}]}, payload)
        rh, out = recv_message(c)
        c.close()
    finally:
        srv.stop()

    assert rh["stream_ids"] == ["chainA", "chainB"]
    assert rh["chip_ids"] == [0, 2]
    assert rh["lengths"] == [2, 2]
    vals = [round(float(v), 4) for v in out]
    # 0.5x of each chain's OWN input (tap head gain, transit tail chip), no crosstalk.
    assert vals[0] == pytest.approx(0.25, abs=1e-3)
    assert vals[1] == pytest.approx(0.125, abs=1e-3)
    assert vals[2] == pytest.approx(0.375, abs=1e-3)
    assert vals[3] == pytest.approx(0.0625, abs=2e-3)


_2P2S_KYT = (Path(__file__).resolve().parents[2] / "examples" / "gain_2p2s"
             / "gain_2p2s.kyt")


@pytest.mark.skipif(not _2P2S_KYT.exists(), reason="gain_2p2s.kyt absent")
def test_sim_controller_hosts_multichip_server(qapp, catalog):
    """The GUI path: SimController.start_gnuradio_server on a 4-chip 2P2S project
    auto-selects MultiChipSimServer (was a hard 'single-chip only' error), and a
    client drives both chains addressed by chip_id, each recovering its own input.
    The gain-on-bus gain_2p2s.kyt (gain@(1,0), chain heads chip0/chip2) — a stream
    taps its head gain and its output crosses the transparent wire to the tail (0.5x).
    The header carries the resolved landing (hop 29) since the nets carry no
    stream_id."""
    from ui.sim_controller import SimController
    from engine.sim_bridge import send_message, recv_message

    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(_2P2S_KYT))
    assert len(ctrl.project.chips) == 4
    assert len(ctrl.project.inter_chip_connections) == 2
    # Resolve the chain-head landing (hop/entry) from the build (gain-on-bus @(1,0)).
    r = ctrl.build()
    ilA = list(r.chips[0].input_landings.values())[0]

    sim = SimController(ctrl)
    port = sim.start_gnuradio_server(port=0)
    try:
        assert port is not None, "multi-chip server failed to start"
        assert sim._multi is True
        assert type(sim._gr_server).__name__ == "MultiChipSimServer"
        c = socket.socket()
        c.connect(("127.0.0.1", port))
        send_message(c, {"op": "ping"})
        assert recv_message(c)[0].get("multichip") is True
        payload = np.array([0.5, 0.25, 0.75, 0.125], dtype=np.float32)
        head = {"entry_addr": ilA["entry"], "hop_count": ilA["hop"],
                "data_addrs": ilA["data_addrs"]}
        send_message(c, {"op": "process_batch_multichip", "streams": [
            {"stream_id": "A", "chip_id": 0, "out_chip": 1, **head,
             "complex": False, "raw": False, "n_samples": 2},
            {"stream_id": "B", "chip_id": 2, "out_chip": 3, **head,
             "complex": False, "raw": False, "n_samples": 2}]}, payload)
        _rh, out = recv_message(c)
        c.close()
    finally:
        sim.stop_gnuradio_server()

    vals = [round(float(v), 4) for v in out]
    assert vals[0] == pytest.approx(0.25, abs=1e-3), vals   # chain A 0.5x
    assert vals[2] == pytest.approx(0.375, abs=1e-3), vals  # chain B 0.5x, no crosstalk
    assert sim._gr_server is None  # stopped cleanly
