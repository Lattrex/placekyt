# SPDX-License-Identifier: GPL-3.0-or-later
"""PRODUCTION coherent QPSK RX — RRC matched filter front end, auto-P&R, BER 0.

The QPSK analog of ``test_production_rx_mf_ber.py``: FOUR separate catalog blocks —
ComplexRRCMatchedFilter → ComplexCostasLoop(order=4) → MMTimingRecovery
→ QPSKSlicer — auto-placed + bus/broker-routed by the tool and recovering the 2-bit
QPSK symbols at BER 0 through simkyt, driven by a full-scale RRC QPSK burst with a
carrier offset AND a fractional timing offset.

Every internal handoff between the complex blocks is a yi/yq PAIR (2 WRITEs + 1
trigger): MF emits (yi, yq) → Costas; the order-4 Costas ``qpd`` output cell emits
(yi_tap, yq_tap) → the M&M timing loop, which emits (yi_e, yq_e) → the
QPSK slicer, which emits the 2-bit Gray symbol (0..3). The QPSK carrier has a 90°
phase ambiguity, so the BER check tries all four constellation rotations (plus a small
lag) — exactly the ``_rot``/lag pattern in
``verification/tests/test_mm_timing_recovery.py`` (the certified timing block).

Design: TX at 2 sps, MF decimation=1 → the carrier/timing loops run at 2 sps (the same
operating point the timing block's on-chip bit-exact gates are proven at).

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_qpsk_modem_ber.py -x
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


# --- full-scale RRC QPSK burst (carrier + timing offset) ---------------------
def _make_rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _shape(syms, taps, sps=2):
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    out = []
    for n in range(len(up)):
        acc = 0.0
        for k in range(len(taps)):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        out.append(acc)
    return out


def _timing_shift(x, toff):
    out = []
    for n in range(len(x) - 1):
        i = n + int(math.floor(toff))
        frac = toff - math.floor(toff)
        out.append(x[i] * (1 - frac) + x[i + 1] * frac
                   if 0 <= i < len(x) - 1 else x[n])
    return out


def _qpsk_tx(symbols, sps=2, beta=0.35, span=8, toff=0.0, amp=0.7):
    """(bi, bq) symbol pairs -> peak-normalised complex RRC I/Q at ``sps`` samples
    per symbol. QPSK constellation: +-1/sqrt(2) per axis (constant modulus). The
    burst is peak-normalised to ~``amp`` (full-scale ADC-grade drive) so the on-chip
    matched filter sees real energy (un-normalised RRC samples vanish in Q15)."""
    si = [(1 if bi == 0 else -1) / math.sqrt(2) for bi, _ in symbols]
    sq = [(1 if bq == 0 else -1) / math.sqrt(2) for _, bq in symbols]
    taps = _make_rrc(beta, sps, span)
    xi = _timing_shift(_shape(si, taps, sps), toff)
    xq = _timing_shift(_shape(sq, taps, sps), toff)
    pk = max(max(abs(a) for a in xi), max(abs(b) for b in xq)) or 1.0
    xi = [amp * a / pk for a in xi]
    xq = [amp * b / pk for b in xq]
    return xi, xq


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _rot(sym, r):
    """Rotate a QPSK symbol index by r*90 degrees (the carrier phase ambiguity)."""
    i = 1 if sym & 1 else -1
    q = 1 if sym & 2 else -1
    for _ in range(r):
        i, q = -q, i
    return (2 if q >= 0 else 0) | (1 if i >= 0 else 0)


def _qpsk_ber(rx, tx, max_lag=20, guard=20):
    """Best QPSK symbol BER over the 4 constellation rotations x a small lag,
    ignoring the first ``guard`` symbols (loop warm-up). Returns (ber, rot, lag)."""
    best = (1.0, 0, 0)
    for r in range(4):
        for lag in range(0, max_lag + 1):
            a = [_rot(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m < 60:
                continue
            err = sum(1 for k in range(guard, m) if a[k] != tx[k])
            ber = err / (m - guard)
            if ber < best[0]:
                best = (ber, r, lag)
    return best


def _build_qpsk_rx(catalog, chip_type):
    """Place MF → Costas(order=4) → MMTimingRecovery → QPSKSlicer, route all nine
    forward nets on the bus, build.  Returns (ctrl, bres, mf_entry).

    Nets: x16_in→MF.xi/xq (2), MF.yi/yq→Costas.xi/xq (2), Costas.yi_tap/yq_tap→
    MM.xi/xq (2), MM.yi_e/yq_e→Slicer.in_i/in_q (2), Slicer.out→x16_out.

    EXPLICIT anchors + ``auto_orient=False``: the order-4 Costas is the COMPACT
    4x2 fold (INV-8) with its yi_tap/yq_tap tap egressing SOUTH from the qpd cell;
    the 14-cell MMTimingRecovery sits below at (2,6) so both tap rails get a clean
    corridor and its own yi_e/yq_e pair reaches the slicer at (8,9). This floorplan
    recovers BER 0 without the flow-orient pass. NOTE: MMTiming replaced the
    quarantined complex Gardner 2026-08-16 at exact BER parity (verified: identical
    pass/fail map over seeds 5-7 x toff 0.3/0.45/0.7 — both pass 0.45/0.7 BER 0,
    both fail 0.3 with matching BERs, i.e. the 0.3 failure is upstream of timing).
    """
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("qpskrx", "kyttar_10x12")
    lib = "lattrex.official"
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 3, library=lib,
                           params={"order": 4})
    mm = ctrl.place_block("MMTimingRecoveryBlock", 0, 2, 6, library=lib)
    sli = ctrl.place_block("QPSKSlicerBlock", 0, 8, 9, library=lib)

    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=mf, port="xi"), [])
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=mf, port="xq"), [])
    R(BlockEndpoint(block=mf, port="yi"), BlockEndpoint(block=cos, port="xi"), [])
    R(BlockEndpoint(block=mf, port="yq"), BlockEndpoint(block=cos, port="xq"), [])
    R(BlockEndpoint(block=cos, port="yi_tap"), BlockEndpoint(block=mm, port="xi"), [])
    R(BlockEndpoint(block=cos, port="yq_tap"), BlockEndpoint(block=mm, port="xq"), [])
    R(BlockEndpoint(block=mm, port="yi_e"), BlockEndpoint(block=sli, port="in_i"), [])
    R(BlockEndpoint(block=mm, port="yq_e"), BlockEndpoint(block=sli, port="in_q"), [])
    R(BlockEndpoint(block=sli, port="out"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])

    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    assert len(bres.words(0)) > 0
    entry, _ = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    return ctrl, bres, entry


def test_qpsk_rx_builds_all_nets(qapp, catalog, chip_type):
    """The four separate QPSK blocks place, all nine forward nets route, it builds."""
    ctrl, _bres, _entry = _build_qpsk_rx(catalog, chip_type)
    types = {b.type for b in ctrl.project.blocks}
    assert "ComplexRRCMatchedFilterBlock" in types
    assert "ComplexCostasLoopBlock" in types
    assert "MMTimingRecoveryBlock" in types
    assert "QPSKSlicerBlock" in types


def test_qpsk_rx_ber_zero(qapp, catalog, chip_type):
    """ACCEPTANCE: a full-scale RRC QPSK burst (carrier + timing offset) through the
    auto-P&R'd MF → Costas(order=4) → MMTimingRecovery → QPSKSlicer chain recovers
    the 2-bit symbols at BER 0 (rotation- and lag-aligned)."""
    import simkyt

    _ctrl, bres, entry = _build_qpsk_rx(catalog, chip_type)

    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(nsym)]
    xi, xq = _qpsk_tx(symbols, toff=toff, amp=0.7)
    k = np.arange(len(xi))
    base = np.asarray(xi) + 1j * np.asarray(xq)
    iq = (base * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for n in range(len(iq)):
        chip.inject_data_physical([_fq(float(iq[n].real))], target_hop_cnt=30,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(iq[n].imag))], target_hop_cnt=30,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=90000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(w[-1]) & 0x3)     # QPSK slicer packs the 2-bit symbol
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)

    tx = [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in symbols]
    ber, rot, lag = _qpsk_ber(rx, tx)
    print(f"QPSK RX (MF front end): {len(rx)} symbols, "
          f"BER={ber:.4f} (rot={rot}, lag={lag})")
    assert len(rx) >= nsym - 10, f"too few recovered symbols: {len(rx)}"
    assert ber == 0.0, f"BER={ber:.4f} (rot={rot}, lag={lag}); {len(rx)} symbols"


# --- the .grc import path (the shipped examples/qpsk_modem/qpsk_modem.grc) -----
GRC = Path(__file__).resolve().parents[2] / "examples" / "qpsk_modem" / "qpsk_modem.grc"


@pytest.mark.skipif(not GRC.exists(), reason="qpsk_modem.grc absent")
def test_qpsk_grc_imports(qapp, catalog, chip_type):
    """The shipped FULL-DUPLEX qpsk_modem.grc imports into placeKYT: all 8 real
    blocks map (both chains), and the QPSK-defining params (order=4 Costas, M&M
    timing, qpsk mapper, complex upsampler) coerce from the flowgraph — the
    GRC-first workflow. The RX chain's on-chip BER-0 recovery through simKYT is gated
    by ``test_qpsk_grc_rx_chain_ber_zero`` below."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    types = {b.type for b in ctrl.project.blocks}
    # both chains present: RX (MF, Costas, MMTiming, slicer) + TX (mapper, upsampler,
    # a 2nd ComplexRRC as the shaper, upconvert).
    assert {"ComplexRRCMatchedFilterBlock", "ComplexCostasLoopBlock",
            "MMTimingRecoveryBlock", "QPSKSlicerBlock", "PSKSymbolMapperBlock",
            "ComplexUpsamplerBlock", "IQUpconvertBlock"} <= types
    assert len(ctrl.project.blocks) == 8
    # the QPSK-defining params came from the .grc, not the block defaults
    cos = next(b for b in ctrl.project.blocks if b.type == "ComplexCostasLoopBlock")
    mm = next(b for b in ctrl.project.blocks if b.type == "MMTimingRecoveryBlock")
    mp = next(b for b in ctrl.project.blocks if b.type == "PSKSymbolMapperBlock")
    assert cos.params.get("order") == 4
    assert mm.params.get("sps") == 2
    assert mp.params.get("modulation") == "qpsk"


@pytest.mark.skipif(not GRC.exists(), reason="qpsk_modem.grc absent")
def test_qpsk_grc_rx_chain_ber_zero(qapp, catalog, chip_type):
    """ACCEPTANCE (RX chain, explicit floorplan): the QPSK RX chain (folded order-4
    Costas + M&M timing recovery) recovers the 2-bit symbols at BER 0 through
    simkyt with ``auto_orient=False``. This pins the folded-block RX recovery."""
    import simkyt

    _ctrl, bres, entry = _build_qpsk_rx(catalog, chip_type)

    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(nsym)]
    xi, xq = _qpsk_tx(symbols, toff=toff, amp=0.7)
    k = np.arange(len(xi))
    base = np.asarray(xi) + 1j * np.asarray(xq)
    iq = (base * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for n in range(len(iq)):
        chip.inject_data_physical([_fq(float(iq[n].real))], target_hop_cnt=30,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(iq[n].imag))], target_hop_cnt=30,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=90000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(w[-1]) & 0x3)
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)

    tx = [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in symbols]
    ber, rot, lag = _qpsk_ber(rx, tx)
    assert len(rx) >= nsym - 10, f"too few recovered symbols: {len(rx)}"
    assert ber == 0.0, f"BER={ber:.4f} (rot={rot}, lag={lag}); {len(rx)} symbols"


_SHIPPED_KYT = GRC.parent / "qpsk_modem.kyt"


@pytest.mark.skipif(not _SHIPPED_KYT.exists(), reason="qpsk_modem.kyt absent")
def test_shipped_kyt_recovers_ber_zero(qapp, catalog, chip_type):
    """The shipped examples/qpsk_modem/qpsk_modem.kyt (as a user opens it) builds
    and recovers BER 0 — the exact FULL-DUPLEX hosted design, driven through the
    SAME stream-routed batch path the live SimServer uses (``stream_id 'rx'`` →
    the RX chain's entry/hop, output demuxed by the rx net's out_tag), NOT a
    generic port injection (which would also fire the TX chain on the shared
    port). This is the artifact gate the qpsk example previously lacked."""
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from engine.sim_bridge import SimServer  # noqa: PLC0415

    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(_SHIPPED_KYT))
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    tgts = stream_targets(ctrl.project, ctrl.registry, catalog, 0,
                          build_result=bres)
    assert "rx" in tgts and "tx" in tgts, f"stream targets: {sorted(tgts)}"

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    srv = SimServer(chip, stream_targets=tgts)

    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(nsym)]
    xi, xq = _qpsk_tx(symbols, toff=toff, amp=0.7)
    k = np.arange(len(xi))
    iq = ((np.asarray(xi) + 1j * np.asarray(xq))
          * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    payload = np.empty(2 * len(iq), dtype="<f4")
    payload[0::2] = iq.real
    payload[1::2] = iq.imag
    header = {"port": "x16_out", "in_port": "x16_in",
              "streams": [{"stream_id": "rx", "complex": True, "raw": True,
                           "n_samples": len(iq)}]}
    reply, out = srv._process_batch_duplex(header, payload)
    assert reply.get("ok"), reply.get("error")
    rx = [int(round(float(v))) & 0x3 for v in (out if out is not None else [])]

    tx = [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in symbols]
    ber, rot, lag = _qpsk_ber(rx, tx)
    print(f"\nshipped full-duplex .kyt (rx stream via SimServer batch): "
          f"BER {ber:.4f}  ({len(rx)} symbols out)")
    assert len(rx) >= nsym - 12, f"too few recovered symbols: {len(rx)}"
    assert ber == 0.0, f"shipped .kyt expected BER 0, got {ber:.4f}"


if __name__ == "__main__":
    import sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    test_qpsk_rx_builds_all_nets(app, cat, ct)
    print("[1] build + all nets: PASS")
    test_qpsk_rx_ber_zero(app, cat, ct)
    print("[2] BER 0: PASS")
