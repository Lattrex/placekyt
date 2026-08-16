# SPDX-License-Identifier: GPL-3.0-or-later
"""SRAM-panel examples over the REAL "Run as GNURadio Server" path (GUI-reported).

The user-reported failure: running the shipped PSK31 / CW examples from the GUI
with the GRC server produced NO output and an EMPTY panel in the inspector. Root
cause: ``SimController._setup_panels`` ran only on the local-Sim path — the
server start / rehost / dirty-rebuild paths hosted the chip with NO panel
registered, so the controller's panel-protocol words left ``x1_out`` with nobody
listening (visible as x1_out pulses in the waveform, zero return traffic).

These tests drive the EXACT path the GUI uses: ``SimController.
start_gnuradio_server`` on the SHIPPED ``.kyt``, then the same ``process_batch``
RPC ``kyttar.source`` sends over a real socket. They assert:

  * the panel device is registered at server start with the ``.kyt``'s ROM image
    (what the Inspector shows) and its read-auto-increment flag;
  * the PSK31 message round-trips SAMPLE-EXACT and the CW message BIT-EXACT vs
    their goldens — through the hosted chip, per-sample (paced) injection;
  * a ``reset`` RPC (the rehost path — a fresh chip) KEEPS the panel registered
    and a second run still matches (the second-Run-goes-silent regression).
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from engine.sim_bridge import recv_message, send_message  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
PSK31_KYT = _ROOT / "examples" / "psk31_transceiver" / "psk31_transceiver.kyt"
CW_KYT = _ROOT / "examples" / "cw_transceiver" / "cw_transceiver.kyt"
sys.path.insert(0, str(_ROOT / "examples" / "psk31_transceiver"))

pytestmark = pytest.mark.skipif(
    not (PSK31_KYT.exists() and CW_KYT.exists()),
    reason="shipped example .kyt absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _serve(kyt_path, port):
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(kyt_path)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=port)
    assert bound == port
    return ctrl, sim, bound


def _batch(port, payload):
    """One process_batch RPC exactly as kyttar.source sends it (per the .grc:
    stream 'tx', float, non-raw, pipelined header — tagged streams take the
    per-sample path)."""
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    try:
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "stream_id": "tx",
                         "complex": False, "raw": False, "pipelined": True},
                     np.asarray(payload, dtype=np.float32))
        hdr, out = recv_message(c)
    finally:
        c.close()
    assert hdr.get("ok"), hdr
    return [int(round(float(v) * 32768.0)) for v in out]


def _reset(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    try:
        send_message(c, {"op": "reset"}, np.zeros(0, dtype=np.float32))
        recv_message(c)
    finally:
        c.close()


def test_psk31_server_panel_registered_and_sample_exact(qapp):
    from psk31_tx_golden import DEMO_TEXT, golden_tx_q15

    ctrl, sim, port = _serve(PSK31_KYT, 58973)
    try:
        # The Inspector's view: the panel device exists and holds the Varicode ROM.
        dev = sim.panel_device(0)
        assert dev is not None, "no panel device registered on the server chip"
        assert len(dev.mem) > 128, f"panel ROM not preloaded ({len(dev.mem)} words)"
        # The message over the real socket — sample-exact vs the golden.
        payload = [ord(ch) / 32768.0 for ch in DEMO_TEXT]
        got = _batch(port, payload)
        gold = golden_tx_q15(DEMO_TEXT, sps=8, amplitude=1.0)
        assert got == gold, (
            f"server run != golden ({len(got)} vs {len(gold)} samples)")
        # RESET RPC → the rehost path builds a FRESH chip: the panel must be
        # re-registered and a second run must still match.
        _reset(port)
        dev2 = sim.panel_device(0)
        assert dev2 is not None and len(dev2.mem) > 128, \
            "panel lost across the reset/rehost path"
        got2 = _batch(port, payload)
        assert got2 == gold, "second run (after reset) != golden"
    finally:
        if sim.gr_server_running:
            sim.stop_gnuradio_server()


def test_cw_server_panel_registered_and_bit_exact(qapp):
    """v2 standalone transmitter over the server: the payload is the MESSAGE
    CHARACTERS (as the .grc's uchar_to_float -> 1/32768 raw-scale chain injects
    them); the chip keys each one from its Morse ROM region, self-paced by the
    on-chip completion kick."""
    ctrl, sim, port = _serve(CW_KYT, 58974)
    try:
        dev = sim.panel_device(0)
        assert dev is not None, "no panel device registered on the server chip"
        assert dev.mem, "panel Morse ROM not preloaded"
        assert dev.auto_inc_read, "panel read auto-increment not applied"
        blk = next(b for b in ctrl.project.blocks if b.type == "CWKeyerBlock")
        k = ctrl.catalog.instantiate("CWKeyerBlock", "ref", blk.params,
                                     library=blk.library)
        text = "CQ CQ DE KYTTAR"
        chars = [ord(ch) if ch != " " else 0 for ch in text]
        gold = [int(v) & 0xFFFF
                for v in k.key_envelope_q15(np.asarray(chars, dtype=np.int32))]
        payload = [ord(ch) / 32768.0 for ch in text]
        got = [v & 0xFFFF for v in _batch(port, payload)]
        assert got == gold, (
            f"server run != ITU-R golden ({len(got)} vs {len(gold)} samples)")
    finally:
        if sim.gr_server_running:
            sim.stop_gnuradio_server()
