# SPDX-License-Identifier: GPL-3.0-or-later
"""PRODUCTION coherent 16-QAM RX — industry-standard M&M cascade, BER 0.

The full receiver chain

    ComplexRRCMatchedFilter -> ComplexGain(2.4) -> MMTimingRecovery
        -> QAM16ComplexCostasLoop -> QAM16Slicer

is placed + bus/broker-routed by the tool and recovers the 4-bit 16-QAM symbols at
BER 0 through simkyt, driven by a random RRC-shaped ``constellation_16qam()`` symbol
stream at 2 samples/symbol. This is the step up from the old 2-block Costas->slicer
stub (rejected as "not a modem").

16-QAM is non-constant-modulus, so the QPSK/BPSK receiver fails: raw Gardner leaves
~3% jitter on the 4-level axes (M&M decision-directed timing replaces it), the PSK
Costas orders don't apply (a decision-directed carrier loop replaces them), and the
decision-directed loops need the constellation at its nominal scale (the ComplexGain
restores the 0.949 outer level the matched filter's Q15 headroom pre-scaling
compressed). Like QPSK it keeps a 90-degree four-fold ambiguity, so the BER check
tries the four constellation rotations.

No carrier frequency offset: the hosted .kyt runs TX and RX on the SAME chip / SAME
clock, so foff = 0 by construction (the decision-directed M&M TED, before the Costas,
needs foff = 0).

This mirrors ``test_qpsk_modem_ber.py`` and ships as ``examples/qam16_modem/``.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest placekyt/tests/test_qam16_modem_ber.py -q
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

_KYT = _ROOT / "examples" / "qam16_modem" / "qam16_modem.kyt"

# GNU Radio constellation_16qam() points (index 0..15 -> (I, Q)), units {-1,-3}/sqrt10.
_NORM = 1.0 / math.sqrt(10.0)
_LEVELS = [(+1, -1), (-1, -1), (+3, -3), (-3, -3), (-3, -1), (+3, -1), (-1, -3),
           (+1, -3), (-3, +3), (+3, +3), (-1, +1), (+1, +1), (+1, +3), (-1, +3),
           (+3, +1), (-3, +1)]
_POINTS = [(i * _NORM, q * _NORM) for (i, q) in _LEVELS]

_BETA, _SPS, _SPAN = 0.35, 2, 8
_GAIN = 2.4


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _rrc(beta, sps, span):
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
            v = (math.sin(math.pi * t * (1 - beta))
                 + 4 * beta * t * math.cos(math.pi * t * (1 + beta))) / (
                     math.pi * t * (1 - (4 * beta * t) ** 2))
        taps.append(v)
    e = math.sqrt(sum(x * x for x in taps))
    return np.array([x / e for x in taps])


def _qam16_burst(n, seed=5, amp=0.9):
    """A random RRC-shaped 16-QAM baseband stream at 2 sps, peak-scaled to ``amp``
    (no carrier offset). Returns (iq complex64, tx symbol indices)."""
    rng = np.random.RandomState(seed)
    tx = rng.randint(0, 16, n)
    base = np.array([complex(*_POINTS[s]) for s in tx], dtype=np.complex128)
    up = np.zeros(n * _SPS, dtype=np.complex128)
    up[::_SPS] = base
    shaped = np.convolve(up, _rrc(_BETA, _SPS, _SPAN))
    shaped = shaped / (np.max(np.abs(shaped)) + 1e-12) * amp
    return shaped.astype(np.complex64), tx.tolist()


def _rot_sym(sym, r):
    i, q = _POINTS[sym]
    for _ in range(r):
        i, q = -q, i
    return min(range(16), key=lambda j: (i - _POINTS[j][0]) ** 2 + (q - _POINTS[j][1]) ** 2)


def _qam16_ber(rx, tx, max_lag=25, guard=60):
    best = (1.0, 0, 0)
    for r in range(4):
        for lag in range(0, max_lag + 1):
            a = [_rot_sym(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m - guard < 80:
                continue
            err = sum(1 for k in range(guard, m) if a[k] != tx[k])
            ber = err / (m - guard)
            if ber < best[0]:
                best = (ber, r, lag)
    return best


def _build_rx(catalog, chip_type):
    """Place MF -> ComplexGain(2.4) -> MMTimingRecovery -> QAM16 Costas -> slicer,
    route all nets on the bus, build. Returns (bres, mf_entry). Uses the proven
    RX floorplan (auto_orient=False): MF(0,0), gain(0,3), MM(2,4) -- MM's counter
    input must NOT sit at chip column 0 -- Costas(1,8), slicer(6,8)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("qam16rx", "kyttar_10x12")
    lib = "lattrex.official"
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cg = ctrl.place_block("ComplexGainBlock", 0, 0, 3, library=lib,
                          params={"gain": _GAIN})
    mm = ctrl.place_block("MMTimingRecoveryBlock", 0, 2, 4, library=lib)
    cos = ctrl.place_block("QAM16ComplexCostasLoopBlock", 0, 1, 8, library=lib,
                           params={"alpha_q15": 0x0400, "beta_q15": 0x0020})
    sli = ctrl.place_block("QAM16SlicerBlock", 0, 6, 8, library=lib)
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=mf, port="xi"), [])
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=mf, port="xq"), [])
    R(BlockEndpoint(block=mf, port="yi"), BlockEndpoint(block=cg, port="xi"), [])
    R(BlockEndpoint(block=mf, port="yq"), BlockEndpoint(block=cg, port="xq"), [])
    R(BlockEndpoint(block=cg, port="yi"), BlockEndpoint(block=mm, port="xi"), [])
    R(BlockEndpoint(block=cg, port="yq"), BlockEndpoint(block=mm, port="xq"), [])
    R(BlockEndpoint(block=mm, port="yi_e"), BlockEndpoint(block=cos, port="xi"), [])
    R(BlockEndpoint(block=mm, port="yq_e"), BlockEndpoint(block=cos, port="xq"), [])
    R(BlockEndpoint(block=cos, port="yi_tap"), BlockEndpoint(block=sli, port="in_i"), [])
    R(BlockEndpoint(block=cos, port="yq_tap"), BlockEndpoint(block=sli, port="in_q"), [])
    R(BlockEndpoint(block=sli, port="out"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    entry, _ = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    return bres, entry


def _drive_ber(bres, entry, n=400, seed=5, hop=30):
    """Inject xi then xq then a jump per sample, read the recovered 4-bit symbols
    from x16_out (the M&M cascade at 2 sps). ``hop`` is the injection hop count to
    the RX chain's landing cell — 30 (the port edge) for the RX-only auto-P&R
    build, or the RX stream's resolved ``hop_count`` for the full-duplex .kyt
    (where x16_in fans to BOTH the RX matched filter and the TX mapper, so a
    generic port injection would corrupt the RX — the burst must land at the RX
    stream's specific block entry/hop)."""
    import simkyt
    iq, tx = _qam16_burst(n, seed=seed)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for s in iq:
        chip.inject_data_physical([_fq(float(s.real))], target_hop_cnt=hop, target_addr=0)
        chip.run(max_events=8000)
        chip.inject_data_physical([_fq(float(s.imag))], target_hop_cnt=hop, target_addr=1)
        chip.run(max_events=8000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=300000)
        while chip.output_available("x16_out"):
            rx += [int(x) & 0xF for x in
                   chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return _qam16_ber(rx, tx), len(rx), tx


def test_qam16_rx_builds_all_nets(qapp, catalog, chip_type):
    """The five RX blocks place, all eleven nets route, it builds."""
    _bres, _entry = _build_rx(catalog, chip_type)


def test_qam16_rx_ber_zero(qapp, catalog, chip_type):
    """ACCEPTANCE: a random RRC-shaped 16-QAM burst through the auto-P&R'd
    MF -> gain -> M&M -> Costas -> slicer chain recovers the 4-bit symbols at
    BER 0 (rotation-aligned)."""
    bres, entry = _build_rx(catalog, chip_type)
    (ber, rot, lag), n_out, tx = _drive_ber(bres, entry)
    print(f"\n16-QAM RX: BER {ber:.4f}  ({n_out} symbols out, rot={rot}, lag={lag})")
    assert n_out >= len(tx) - 10, f"too few recovered symbols: {n_out}"
    assert ber == 0.0, f"expected BER 0, got {ber:.4f}"


@pytest.mark.skipif(not _KYT.exists(), reason="shipped .kyt absent")
def test_shipped_kyt_recovers_ber_zero(qapp, catalog, chip_type):
    """The shipped examples/qam16_modem/qam16_modem.kyt (as a user opens it) builds
    and recovers BER 0 -- the exact FULL-DUPLEX hosted design, not a script
    reconstruction. x16_in fans to BOTH the RX matched filter and the TX mapper,
    so the RX burst is driven through the SAME stream-routed batch path the live
    SimServer / batch_check.py use (``stream_id 'rx'`` → the RX chain's entry/hop,
    output demuxed by the rx net's out_tag) — NOT a generic port injection (which
    would corrupt the RX by also firing the TX chain on the shared port)."""
    import simkyt  # noqa: PLC0415
    from engine.port_config import stream_targets  # noqa: PLC0415
    from engine.sim_bridge import SimServer  # noqa: PLC0415

    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(_KYT))
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    tgts = stream_targets(ctrl.project, ctrl.registry, catalog, 0, build_result=bres)
    assert "rx" in tgts and "tx" in tgts, f"stream targets: {sorted(tgts)}"

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    srv = SimServer(chip, stream_targets=tgts)

    n = 400
    iq, tx = _qam16_burst(n, seed=5)
    payload = np.empty(2 * len(iq), dtype="<f4")
    payload[0::2] = iq.real
    payload[1::2] = iq.imag
    header = {"port": "x16_out", "in_port": "x16_in",
              "streams": [{"stream_id": "rx", "complex": True, "raw": True,
                           "n_samples": len(iq)}]}
    reply, out = srv._process_batch_duplex(header, payload)
    assert reply.get("ok"), reply.get("error")
    rx = [int(round(float(v))) & 0xF for v in (out if out is not None else [])]

    (ber, rot, lag) = _qam16_ber(rx, tx)
    print(f"\nshipped full-duplex .kyt (rx stream via SimServer batch): "
          f"BER {ber:.4f}  ({len(rx)} symbols out)")
    assert ber == 0.0, f"shipped .kyt expected BER 0, got {ber:.4f}"
