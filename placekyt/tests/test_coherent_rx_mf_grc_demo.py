"""GRC-first PRODUCTION coherent BPSK RX (RRC matched filter front end), BER 0.

Imports ``coherent_bpsk_rx_mf_demo.grc`` — the customer-facing demo flowgraph with
the RRC matched filter added ahead of Costas, and an input-vs-recovered-bits view
in the QT waveform sinks — into placeKYT, auto-places + bus-routes the four blocks
(ComplexRRCMatchedFilter -> ComplexCostasLoop -> GardnerTimingRecovery -> BPSKSlicer),
builds, and drives the proven RRC BPSK burst (carrier + timing offset) through
simkyt to recover bits at BER 0.

This proves the .grc ITSELF round-trips through the tool (not just a hand-built
chain): the importer maps the named xi/xq/yi/yq ports of the new MF block onto the
catalog PortMap, producing the same seven nets as the manual recipe in
``test_production_rx_mf_ber.py``.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_coherent_rx_mf_grc_demo.py -x
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

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC = EXAMPLES_DIR / "coherent_bpsk_rx_mf_demo.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC.exists()), reason="chip yaml / .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


# --- full-scale RRC BPSK burst (carrier + timing offset) — same as the MF test ---
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


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0, amp=0.9):
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    pk = max(abs(b) for b in out) or 1.0
    out = [amp * b / pk for b in out]      # full-scale ADC-grade drive
    return out, syms


def _ber_with_lag(rx, tx, max_lag=24, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)        # inversion tolerant (BPSK 180° ambiguity)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _autopnr_from_grc(catalog, chip_type):
    """Import the demo .grc → auto-place → auto-route (bus). Returns ctrl + report."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    return ctrl, rep


def test_mf_demo_imports_all_four_blocks(qapp, catalog, chip_type):
    """The demo .grc imports as the four SEPARATE catalog blocks (incl. the new MF),
    auto-places, and bus-routes ALL SEVEN nets (I/Q ingress to MF.xi/xq, MF.yi/yq to
    Costas, the forward chain, and egress)."""
    ctrl, rep = _autopnr_from_grc(catalog, chip_type)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    types = {b.type for b in ctrl.project.blocks}
    assert "ComplexRRCMatchedFilterBlock" in types
    assert "ComplexCostasLoopBlock" in types
    assert "GardnerTimingRecovery" in types
    assert "BPSKSlicerBlock" in types
    routed = {r.name for r in rep.routed}
    assert {f"net{i}" for i in range(1, 8)} <= routed, \
        f"all seven nets must route, got {sorted(routed)}"
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    assert len(bres.words(0)) > 0


def test_mf_demo_ber_zero(qapp, catalog, chip_type):
    """ACCEPTANCE: the IMPORTED demo .grc recovers bits at BER 0 — full-scale RRC
    BPSK burst (carrier+timing offset) through the auto-P&R'd MF->Costas->Gardner->
    Slicer chain. Same operating point (nsym=160) as the flagship."""
    import simkyt

    ctrl, rep = _autopnr_from_grc(catalog, chip_type)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    entry, _ins = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = _tx_signal(bits, timing_offset=toff, amp=0.9)
    k = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    rx = []
    for n in range(len(sig)):
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
            rx.append(int(w[-1]) & 1)
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)

    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    ber = (e / m) if m else 1.0
    print(f"MF GRC demo RX: {len(rx)} bits, BER={ber:.4f} ({e}/{m}, lag={lag})")
    assert m and e == 0, f"BER={ber:.4f} ({e}/{m}, lag={lag}); {len(rx)} bits"


if __name__ == "__main__":
    import sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    test_mf_demo_imports_all_four_blocks(app, cat, ct)
    print("[1] import + all nets + build: PASS")
    test_mf_demo_ber_zero(app, cat, ct)
    print("[2] BER 0: PASS")
