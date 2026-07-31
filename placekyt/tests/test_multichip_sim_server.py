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


def test_multichip_server_drives_both_chains(qapp, catalog):
    import simkyt
    if not hasattr(simkyt.MultiChipSimulation.new("probe", 5.0),
                   "set_port_input_routed"):
        pytest.skip("simkyt .so predates the routed-input relay flag")

    from engine.simulator import MultiChipSimEngine
    from engine.sim_bridge import MultiChipSimServer, send_message, recv_message

    words, il = _auto_pnr_gain(catalog)
    hop, entry, a0 = il["hop"], il["entry"], il["data_addrs"][0]
    routed = tuple(il["cell"]) != (0, 0)

    ct = str(CT_PATH)
    eng = MultiChipSimEngine({i: ct for i in range(4)})
    eng.connect(0, "x16_out", 1, "x16_in")   # chain A
    eng.connect(2, "x16_out", 3, "x16_in")   # chain B
    for cid in range(4):
        eng.load(cid, words, trace=True)
        eng.configure_input_port(cid, "x16_in", entry_addr=entry,
                                 hop_count=hop, data_addr=a0, routed=routed)

    tg = {
        "chainA": {"chip_id": 0, "out_chip": 1, "entry_addr": entry,
                   "hop_count": hop, "data_addrs": [a0], "out_tag": None,
                   "routed": routed},
        "chainB": {"chip_id": 2, "out_chip": 3, "entry_addr": entry,
                   "hop_count": hop, "data_addrs": [a0], "out_tag": None,
                   "routed": routed},
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
    # 0.25x of each chain's OWN input — chain A then chain B, no crosstalk.
    assert vals[0] == pytest.approx(0.125, abs=1e-3)
    assert vals[1] == pytest.approx(0.0625, abs=1e-3)
    assert vals[2] == pytest.approx(0.1875, abs=1e-3)
    assert vals[3] == pytest.approx(0.0312, abs=2e-3)
