# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end proof for the FM TRANSCEIVER demo (examples/fm_transceiver).

The demo is a REAL transceiver: a SEPARATE transmit chain and a SEPARATE receive
chain share ONE chip, demuxed by ``stream_id`` (the AM-transceiver / BPSK-modem
pattern):

    TX (stream 'tx'):  audio -> tx_src -> frequency_modulator -> tx_sink  ==> FM passband
    RX (stream 'rx'):  fm_iq -> rx_src -> quadrature_demod    -> rx_sink  ==> recovered audio

These tests prove the whole path an end user runs:

  * the .grc IMPORTS into placeKYT with exactly the two chains' DSP cells and both
    streams wired to the shared x16_in/x16_out (TX real audio in, RX complex I/Q in),
  * it auto-P&Rs + builds on one chip,
  * and each stream RUNS LIVE over the SimServer batch bridge (the GUI
    "Run as GNURadio Server -> Execute" path): the TX stream produces the FM passband
    and the RX stream independently recovers the transmitted audio.

The complex FM passband is streamed into the RX the PROVEN way (``complex_in='complex'``,
interleaved xi/xq per sample — the coherent-BPSK-RX ingress path).

Run:
    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_fm_transceiver_grc.py -q
"""
from __future__ import annotations

import math
import os
import socket
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

GRC = _ROOT / "examples" / "fm_transceiver" / "fm_transceiver.grc"
CHIP = "kyttar_10x12"
CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

FS = 32000.0
FDEV = 1500.0
SENS = 2.0 * math.pi * FDEV / FS       # sensitivity (rad/sample) — must match gen_grc
GAIN = 1.0 / SENS                       # discriminator gain


# ---- reference DSP (mirrors gr-kyttar/python/kyttar/fm_demo_stim.py) --------
def _audio(n):
    t = np.arange(n)
    return (0.6 * np.cos(2 * np.pi * 300.0 * t / FS)
            + 0.3 * np.cos(2 * np.pi * 800.0 * t / FS)).astype(np.float64)


def _fm_phase(n):
    """Integrated FM phase phi[k] = sum_{i<=k} sensitivity*audio[i] (VCO pre-inc)."""
    return np.cumsum(SENS * _audio(n))


def _fm_iq_interleaved(n):
    """Complex FM passband exp(j*phi) as interleaved [I0,Q0,I1,Q1,...] floats — the
    payload the RX source streams (complex_in='complex')."""
    phi = _fm_phase(n)
    iq = np.empty(2 * n, dtype=np.float64)
    iq[0::2] = np.cos(phi)
    iq[1::2] = np.sin(phi)
    return iq.astype(np.float32)


def _fm_real(n):
    """Re part of the passband cos(phi) — what the TX chain emits on x16_out."""
    return np.cos(_fm_phase(n)).astype(np.float32)


def _import():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    cat = BlockCatalog.from_gr_kyttar()
    return cat, import_grc(str(GRC), cat, chip_type=CHIP)


def _best_corr(out, ref, max_lag=8):
    """|corr| of ``out`` vs ``ref`` maximised over a small integer sample lag (the
    quadrature discriminator's 1-sample memory + any egress alignment)."""
    o = np.asarray(out, dtype=float)
    r = np.asarray(ref, dtype=float)
    best = 0.0
    for lag in range(max_lag):
        a = o[lag:len(r)]
        b = r[:len(a)]
        if len(a) > 16 and a.std() > 0 and b.std() > 0:
            best = max(best, abs(float(np.corrcoef(a, b)[0, 1])))
    return best


def test_file_exists():
    assert GRC.is_file(), f"missing demo .grc: {GRC}"


def test_imports_two_stream_transceiver():
    """The importer keeps exactly the two chains' DSP cells and wires BOTH streams
    to the shared input/output ports (a true duplex transceiver, not a loopback).
    TX net is real (audio), RX net is complex (I/Q FM)."""
    cat, res = _import()
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    assert "FrequencyModulatorBlock" in types, types
    assert "QuadratureDemodBlock" in types, types
    sids = {getattr(c, "stream_id", None) for c in res.project.connections}
    assert "tx" in sids and "rx" in sids, sids
    # TX audio net is REAL, RX FM net is COMPLEX (drives data_addrs sizing).
    by_sid = {getattr(c, "stream_id", None): c for c in res.project.connections}
    assert by_sid["tx"].src_complex is False, "TX audio net must be real"
    assert by_sid["rx"].src_complex is True, "RX FM net must be complex I/Q"


def _host_chip():
    """Import -> auto-P&R -> build -> host the chip on a SimServer with BOTH stream
    targets resolved from the build. Returns (srv, port, stream_targets)."""
    import simkyt
    from engine.io.chip_type_io import load_chip_type
    from engine.port_config import stream_targets, batch_reset_writes
    from engine.sim_bridge import SimServer
    from ui.controller import AppController

    cat, res = _import()
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({CHIP: ct}).ok, "auto-P&R failed"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    tgts = stream_targets(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                          build_result=bres)
    assert "tx" in tgts and "rx" in tgts, tgts

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.chips[0].words)
    srv = SimServer(chip, stream_targets=tgts,
                    batch_reset_writes=batch_reset_writes(bres, 0))
    return srv, srv.start(), tgts


def _run_stream(port, stream_id, payload, is_complex):
    from engine.sim_bridge import send_message, recv_message
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    try:
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "complex": is_complex, "raw": False,
                         "stream_id": stream_id}, payload)
        _hdr, out = recv_message(c)
    finally:
        c.close()
    return out


def test_tx_stream_produces_fm_passband():
    """LIVE TX: drive audio into the 'tx' stream; the chip's VCO emits the COMPLEX FM
    passband ``exp(j*phi)`` (phi integrating sensitivity*audio). The VCO is a
    complex-output block, so x16_out carries the I and Q rails INTERLEAVED
    [I0,Q0,I1,Q1,...] (256 samples -> 512 words) — the same complex egress a genuine
    I/Q chain produces. We de-interleave and check BOTH rails against cos/sin(phi)."""
    srv, port, _ = _host_chip()
    try:
        N = 256
        audio = _audio(N).astype(np.float32)
        out = _run_stream(port, "tx", audio, is_complex=False)
    finally:
        srv.stop()
    # Complex output: 2 words/sample, I/Q interleaved.
    assert out is not None and len(out) >= 2 * N, (
        f"TX stream produced no complex egress ({0 if out is None else len(out)} words)")
    out = np.asarray(out, dtype=float)
    i_rail = out[0:2 * N:2]
    q_rail = out[1:2 * N:2]
    phi = _fm_phase(N)
    i_corr = _best_corr(i_rail, np.cos(phi))
    q_corr = _best_corr(q_rail, np.sin(phi))
    assert i_corr > 0.90 and q_corr > 0.90, (
        f"TX FM passband I/Q rails do not match exp(j*phi) "
        f"(|corr_I|={i_corr:.4f}, |corr_Q|={q_corr:.4f})")


def test_rx_stream_recovers_audio():
    """LIVE RX: drive the complex FM passband into the 'rx' stream; the quadrature
    discriminator recovers the transmitted audio."""
    srv, port, _ = _host_chip()
    try:
        N = 256
        iq = _fm_iq_interleaved(N)
        out = _run_stream(port, "rx", iq, is_complex=True)
    finally:
        srv.stop()
    assert out is not None and len(out) >= N - 1, (
        f"RX stream produced no egress ({0 if out is None else len(out)} words)")
    # Discriminator output aligns to audio[1:] (one-sample memory).
    ref = _audio(N)[1:]
    corr = _best_corr(out[:len(ref)], ref)
    assert corr > 0.90, (
        f"RX chain does not recover the transmitted audio (|corr|={corr:.4f})")


def test_full_duplex_both_streams_on_shared_chip():
    """Both streams run on the SAME hosted chip (shared x16_in/x16_out, demuxed by
    stream_id): TX yields the FM passband AND RX recovers the audio, back to back."""
    srv, port, _ = _host_chip()
    try:
        N = 256
        tx_out = _run_stream(port, "tx", _audio(N).astype(np.float32), is_complex=False)
        rx_out = _run_stream(port, "rx", _fm_iq_interleaved(N), is_complex=True)
    finally:
        srv.stop()
    assert tx_out is not None and len(tx_out) >= 2 * N   # complex I/Q egress
    assert rx_out is not None and len(rx_out) >= N - 1
    tx_i = np.asarray(tx_out, dtype=float)[0:2 * N:2]
    tx_corr = _best_corr(tx_i, np.cos(_fm_phase(N)))
    ref = _audio(N)[1:]
    rx_corr = _best_corr(rx_out[:len(ref)], ref)
    assert tx_corr > 0.90 and rx_corr > 0.90, (tx_corr, rx_corr)
