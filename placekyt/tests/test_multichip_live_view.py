# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-chip LIVE VIEW: waveform traces from a hosted 2P2S run (offscreen Qt).

User-reported: a gain_2p2s server run showed NOTHING in placeKYT's waveform
panel (and no cell animation) — the MultiChipSimServer had no trace plumbing
into the GUI. Now the same pull path as the single-chip server drains EVERY
chip's trace (chip-tagged, buffers reset via re-enable_trace — verified to
clear) into chip-qualified TraceModel rows, and (animation ON) per-chip cell
overlays.
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

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and KYT_2P2S.exists()),
    reason="chip yaml / gain_2p2s.kyt absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_multichip_run_populates_waveform_traces(qapp):
    from engine.port_config import multi_chip_stream_targets

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.open_project(str(KYT_2P2S))
    r = ctrl.build()
    assert r.ok
    tg = multi_chip_stream_targets(ctrl.project, ctrl.registry, ctrl.catalog,
                                   build_result=r)
    sim = SimController(ctrl)
    port = sim.start_gnuradio_server(port=0)
    try:
        assert port is not None and sim._multi is True
        assert sim._server_pull_timer.isActive(), \
            "multi-chip server must start the live-view pull timer"

        # One real multichip batch across all four streams.
        c = socket.create_connection(("127.0.0.1", port), timeout=60)
        try:
            inputs = {s: [0.5, 0.25] for s in "ABCD"}
            payload = np.concatenate(
                [np.asarray(inputs[s], dtype=np.float32) for s in "ABCD"])
            streams = []
            for s in "ABCD":
                t = tg[s]
                streams.append({
                    "stream_id": s, "chip_id": t["chip_id"],
                    "out_chip": t["out_chip"], "entry_addr": t["entry_addr"],
                    "hop_count": t["hop_count"], "data_addrs": t["data_addrs"],
                    "out_tag": t["out_tag"], "complex": False, "raw": False,
                    "n_samples": 2})
            send_message(c, {"op": "process_batch_multichip",
                             "streams": streams}, payload)
            reply, _ = recv_message(c)
            assert reply.get("lengths") == [2, 2, 2, 2], reply
        finally:
            c.close()

        # ANIMATION smoke: with cell animation ON, the refresh emits per-chip
        # cell states (the canvas overlay) without touching single-chip-only
        # APIs. Connect BEFORE the forced refresh below so one refresh feeds
        # both the waveform assert and this one.
        got_states = []
        sim.cell_states.connect(lambda s: got_states.append(list(s)))
        sim._animate_cells = True

        sim.refresh_debug_from_chip(force=True)
        assert got_states and got_states[-1], "no cell states emitted"
        anim_chips = {c for (c, _x, _y) in got_states[-1]}
        assert len(anim_chips) >= 2, \
            f"animation states not per-chip: {sorted(anim_chips)}"

        streams_by_port = sim.trace_model.port_streams()
        chips_seen = {chip for (chip, _p) in streams_by_port}
        # Chain heads (0, 2) inject; chain tails (1, 3) capture — the waveform
        # rows must be chip-qualified and cover BOTH chains end to end.
        assert {0, 2}.issubset(chips_seen), \
            f"head-chip injections missing from the trace: {sorted(streams_by_port)}"
        assert {1, 3}.issubset(chips_seen), \
            f"tail-chip captures missing from the trace: {sorted(streams_by_port)}"
        assert all(vals for vals in streams_by_port.values())
    finally:
        sim.stop_gnuradio_server()
