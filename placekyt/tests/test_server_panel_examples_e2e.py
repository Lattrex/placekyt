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


# ---------------------------------------------------------------------------
# LZ4 over the server: the waveform must POPULATE (the "traces don't show up
# automatically" report).
#
# examples/lz4_stream chains its two streams through the CLIENT: the `cmp`
# source is fed by the compressed bytes the `raw` stream produced ON the chip,
# so it cannot rendezvous into the leader's collect window — ONE GRC Run reaches
# the server as TWO sequential ``process_batch_duplex`` RPCs.
#
# _process_batch_duplex used to fire on_new_run + _clear_chip_trace on EVERY
# RPC, so the second RPC wiped the first stream's finished trace and the last
# RPC's reset left the pane empty for the whole Run (seeded rows reading
# "Analog: —" over a collapsed ~0..1 ns ruler). Measured before the fix on the
# real chip: x1_out tag2=320 tag5=455 after `raw`, then 0/0 (0 ports) after the
# `cmp` RPC. This drives the REAL hosted server over a REAL socket and asserts
# the TraceModel still holds both streams at the end of the Run.
# ---------------------------------------------------------------------------
LZ4_KYT = _ROOT / "examples" / "lz4_stream" / "lz4_stream.kyt"


def _duplex(port, streams, payload):
    """One process_batch_duplex RPC, exactly as the GR rendezvous dispatches."""
    c = socket.socket()
    c.settimeout(600.0)
    c.connect(("127.0.0.1", port))
    try:
        send_message(c, {"op": "process_batch_duplex", "port": "x16_out",
                         "in_port": "x16_in", "streams": streams,
                         "schedule": "interleaved"},
                     np.asarray(payload, dtype=np.float32))
        hdr, out = recv_message(c)
    finally:
        c.close()
    assert hdr.get("ok"), hdr
    return hdr, out


@pytest.mark.skipif(not LZ4_KYT.exists(), reason="lz4_stream .kyt absent")
def test_lz4_duplex_run_keeps_both_streams_in_the_waveform(qapp):
    """Two data-dependent duplex RPCs = ONE Run: the waveform TraceModel must
    end the Run holding BOTH streams' port samples, not an empty pane."""
    ctrl, sim, port = _serve(LZ4_KYT, 58975)
    try:
        assert sim.panel_device(0) is not None, "no panel on the server chip"

        # RPC 1 — the `raw` (encode) stream. A short payload keeps the gate fast;
        # the trace population is what is under test, not the compression ratio.
        raw = [b / 32768.0 for b in list(b"KYTTAR LZ4 STREAM! " * 4)] \
            + [256 / 32768.0]
        _duplex(port, [{"stream_id": "raw", "complex": False, "raw": False,
                        "n_samples": len(raw)}], raw)
        sim.refresh_debug_from_chip(force=True, full_capture=True)
        after_raw = sim.trace_model.port_streams_by_tag()
        raw_ports = {k: len(v) for k, v in after_raw.items() if v}
        assert raw_ports, "the raw stream produced NO port samples at all"

        # RPC 2 — the `cmp` (decode) stream of the SAME Run. Before the fix this
        # reset the trace and the pane went empty.
        cmp_payload = [b / 32768.0 for b in (1, 2, 3, 4)]
        _duplex(port, [{"stream_id": "cmp", "complex": False, "raw": False,
                        "n_samples": len(cmp_payload)}], cmp_payload)
        sim.refresh_debug_from_chip(force=True, full_capture=True)
        after_cmp = sim.trace_model.port_streams_by_tag()

        assert after_cmp, (
            "the waveform TraceModel is EMPTY at the end of the Run — the "
            "second duplex RPC wiped the Run's trace (the reported bug)")
        for key, n in raw_ports.items():
            assert len(after_cmp.get(key, [])) >= n, (
                f"stream {key} LOST samples across the second duplex RPC "
                f"({len(after_cmp.get(key, []))} < {n}) — the Run-boundary "
                f"reset fired inside a single Run")
    finally:
        if sim.gr_server_running:
            sim.stop_gnuradio_server()
