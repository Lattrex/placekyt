"""BPSKSlicerBlock output-packing modes: bit / byte / word.

The slicer takes a signed LLR per sample and emits a hard-decision bit. Emitting
one word per bit is maximal pressure on the output port and wasteful in a real
receiver; the ``out_mode`` parameter lets the block pack 8 ('byte') or 16 ('word',
the production default) sliced bits MSB-first into one output word, emitting only
on the group boundary. 'bit' keeps the per-sample emit (for watching a toggle).

Two levels of verification:
  1. ``process_reference`` packing math (pure Python) for every mode + round-trip.
  2. ON CHIP in the real production RX chain (MF->Costas->Gardner->Slicer, auto-P&R)
     with the slicer in each mode: unpacking the emitted words recovers the same
     bits the 'bit'-mode chain recovers, at BER 0. This drives the slicer in its
     true end-of-chain position (the standalone lead-block placement relocates the
     slicer entry, which is a harness artifact, not a slicer property).
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402
from gr_kyttar.placement.kyttar_block import BPSKSlicerBlock  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


# --- pure-reference packing (no chip) ----------------------------------------

@pytest.mark.parametrize("out_mode,bits_per", [("bit", 1), ("byte", 8), ("word", 16)])
def test_reference_packing_and_roundtrip(out_mode, bits_per):
    rng = np.random.default_rng(7)
    llrs = rng.integers(-3000, 3000, size=64).astype(np.int16)
    bits = [1 if v < 0 else 0 for v in llrs]
    words = [int(x) & 0xFFFF for x in
             BPSKSlicerBlock("r", out_mode=out_mode).process_reference(llrs)]
    assert len(words) == 64 // bits_per
    # MSB-first unpack recovers the original bits.
    unpacked = []
    for w in words:
        for k in range(bits_per - 1, -1, -1):
            unpacked.append((w >> k) & 1)
    assert unpacked == bits


def test_word_default():
    assert BPSKSlicerBlock("s").out_mode == "word"


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        BPSKSlicerBlock("s", out_mode="nibble")


# --- on-chip in the real production RX chain ---------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


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
        acc = sum(taps[k] * up[n - k] for k in range(L) if 0 <= n - k < len(up))
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
    return [amp * b / pk for b in out], syms


def _ber_with_lag(rx, tx, max_lag=24, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _build_rx(catalog, chip_type, out_mode):
    lib = "lattrex.official"
    ctrl = AppController(catalog=catalog)
    ctrl.new_project(f"rx_{out_mode}", "kyttar_10x12")
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=lib)
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 0, library=lib)
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 0, library=lib,
                           params={"out_mode": out_mode})
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xi"), [])
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yi"),
                   BlockEndpoint(block=cos, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yq"),
                   BlockEndpoint(block=cos, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=cos, port="yi_tap"),
                   BlockEndpoint(block=gar, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=gar, port="out"),
                   BlockEndpoint(block=sli, port="llr"), [])
    ctrl.add_route(BlockEndpoint(block=sli, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _ = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    return res, entry


def _run_rx(res, entry, iq):
    import simkyt
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)
    words = []
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
            words.append(int(w[-1]) & 0xFFFF)
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)
    return words


@pytest.mark.parametrize("out_mode,bits_per", [("byte", 8), ("word", 16)])
def test_packed_rx_recovers_bits_ber0(qapp, catalog, chip_type, out_mode, bits_per):
    """The production RX with a byte/word-packing slicer: unpacking the emitted
    words (MSB-first) recovers the transmitted bits at BER 0 — same as the
    per-bit chain, but with 8x/16x fewer output-port writes."""
    random.seed(5)
    nsym, foff, toff = 160, 0.008, 0.45
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = _tx_signal(bits, timing_offset=toff, amp=0.9)
    k = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)

    res, entry = _build_rx(catalog, chip_type, out_mode)
    words = _run_rx(res, entry, iq)
    assert words, f"{out_mode}: no output words emitted"

    # Unpack MSB-first to the bit stream.
    rx = []
    for w in words:
        for j in range(bits_per - 1, -1, -1):
            rx.append((w >> j) & 1)

    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    ber = (e / m) if m else 1.0
    print(f"RX out_mode={out_mode}: {len(words)} words -> {len(rx)} bits, "
          f"BER={ber:.4f} ({e}/{m}, lag={lag})")
    assert m and e == 0, f"{out_mode} BER={ber:.4f} ({e}/{m})"


if __name__ == "__main__":
    for m, n in [("bit", 1), ("byte", 8), ("word", 16)]:
        test_reference_packing_and_roundtrip(m, n)
    test_word_default()
    test_invalid_mode_rejected()
    print("reference packing + roundtrip + default + invalid: PASS")
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    for m, n in [("byte", 8), ("word", 16)]:
        test_packed_rx_recovers_bits_ber0(app, cat, ct, m, n)
        print(f"[{m}] production RX unpack == BER 0: PASS")
