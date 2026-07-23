# SPDX-License-Identifier: GPL-3.0-or-later
"""PRODUCTION coherent 16-QAM RX — decision-directed Costas + slicer, BER 0.

The chain QAM16ComplexCostasLoop -> QAM16Slicer is placed + bus/broker-routed by the
tool and recovers the 4-bit 16-QAM symbols at BER 0 through simkyt, driven by a random
``digital.constellation_16qam()`` symbol stream with a carrier frequency offset.

16-QAM is non-constant-modulus, so the QPSK/BPSK Costas phase detectors fail — the DD
loop derotates, slices each axis to the nearest 4-PAM grid level, and forms the phase
error from the decision (the constellation_receiver_cb path). Like QPSK it keeps a
90-degree 4-fold ambiguity, so the BER check tries the 4 constellation rotations.

This mirrors ``test_qpsk_modem_ber.py`` and ships as ``examples/qam16_modem/``.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest placekyt/tests/test_qam16_modem_ber.py -q
"""
from __future__ import annotations

import math
import os
import random
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


def _rot_sym(sym, r):
    i, q = _POINTS[sym]
    for _ in range(r):
        i, q = -q, i
    return min(range(16), key=lambda j: (i - _POINTS[j][0]) ** 2 + (q - _POINTS[j][1]) ** 2)


def _qam16_ber(rx, tx, max_lag=20, guard=40):
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
    """Place QAM16 Costas -> slicer, route all nets on the bus, build. Returns
    (bres, costas_entry). Uses the validated DD loop gains + the 5x2 fold anchored at
    (0,0) so the phase landing cell abuts x16_in."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("qam16rx", "kyttar_10x12")
    lib = "lattrex.official"
    cos = ctrl.place_block("QAM16ComplexCostasLoopBlock", 0, 0, 0, library=lib,
                           params={"alpha_q15": 0x0400, "beta_q15": 0x0020})
    sli = ctrl.place_block("QAM16SlicerBlock", 0, 7, 0, library=lib)
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=cos, port="xi"), [])
    R(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=cos, port="xq"), [])
    R(BlockEndpoint(block=cos, port="yi_tap"), BlockEndpoint(block=sli, port="in_i"), [])
    R(BlockEndpoint(block=cos, port="yq_tap"), BlockEndpoint(block=sli, port="in_q"), [])
    R(BlockEndpoint(block=sli, port="out"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    entry, _ = catalog.resolved_io("QAM16ComplexCostasLoopBlock")
    return bres, entry


def _drive_ber(bres, entry, n=400, foff=0.002, seed=5):
    import simkyt
    random.seed(seed)
    tx = [random.randint(0, 15) for _ in range(n)]
    base = np.asarray([complex(*_POINTS[s]) for s in tx], dtype=np.complex64)
    k = np.arange(len(base))
    iq = (base * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for s in iq:
        chip.inject_data_physical([_fq(float(s.real))], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(s.imag))], target_hop_cnt=30, target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=200000)
        while chip.output_available("x16_out"):
            rx += [int(x) & 0xF for x in
                   chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return _qam16_ber(rx, tx), len(rx)


def test_qam16_rx_builds_all_nets(qapp, catalog, chip_type):
    """The two QAM16 blocks place, all five nets route, it builds."""
    _bres, _entry = _build_rx(catalog, chip_type)


def test_qam16_rx_ber_zero(qapp, catalog, chip_type):
    """ACCEPTANCE: a random 16-QAM burst (carrier offset) through the auto-P&R'd
    Costas -> slicer chain recovers the 4-bit symbols at BER 0 (rotation-aligned)."""
    bres, entry = _build_rx(catalog, chip_type)
    (ber, rot, lag), n_out = _drive_ber(bres, entry)
    print(f"\n16-QAM RX: BER {ber:.4f}  ({n_out} symbols out, rot={rot}, lag={lag})")
    assert ber == 0.0, f"expected BER 0, got {ber:.4f}"


@pytest.mark.skipif(not _KYT.exists(), reason="shipped .kyt absent")
def test_shipped_kyt_recovers_ber_zero(qapp, catalog, chip_type):
    """The shipped examples/qam16_modem/qam16_modem.kyt (as a user opens it) builds
    and recovers BER 0 — the exact hosted design, not a script reconstruction."""
    proj = load_project(str(_KYT))
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        proj, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    entry, _ = catalog.resolved_io("QAM16ComplexCostasLoopBlock")
    (ber, rot, lag), n_out = _drive_ber(bres, entry)
    print(f"\nshipped .kyt: BER {ber:.4f}  ({n_out} symbols out)")
    assert ber == 0.0, f"shipped .kyt expected BER 0, got {ber:.4f}"
