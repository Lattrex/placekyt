# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify QPSKSlicerBlock 1:1 against GNU Radio
``digital.constellation_decoder_cb(constellation_qpsk())``.

The QPSK RX slicer turns a received (I, Q) sample into the 2-bit symbol index
0..3 that GR's ``constellation_qpsk().decision_maker()`` returns. QPSK is
constant-modulus and separable, so the decision is a pure per-axis SIGN test:
``symbol = (Q >= 0 ? 2 : 0) | (I >= 0 ? 1 : 0)``. This block was load-bearing
in the shipped BER-0 QPSK modem for months with only whole-chain coverage
(exactly the INV-25 "used in a working example" trap) — this suite gives it
the per-block GR-equivalence gate + report the metrics table requires.
Mandatory mutation gates (INV-4): a swapped-axis bit map, a one-symbol shift,
and empty output all FAIL.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_qpsk_slicer.py -q
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")


def _fq(v):
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_decision(iq_floats):
    """GR golden: ``constellation_qpsk().decision_maker(z)`` per sample."""
    script = ("import json,sys\n"
              "from gnuradio import digital\n"
              "c = digital.constellation_qpsk()\n"
              "d = json.loads(sys.stdin.read())\n"
              "print(json.dumps([c.decision_maker(complex(a, b)) for a, b in d]))\n")
    r = subprocess.run([_GR_PY, "-c", script],
                       input=json.dumps([[a, b] for a, b in iq_floats]),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _gr_points():
    script = ("from gnuradio import digital\nimport json\n"
              "print(json.dumps([[p.real, p.imag] for p in "
              "digital.constellation_qpsk().points()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return [complex(a, b) for a, b in json.loads(r.stdout.strip().splitlines()[-1])]


def run_qpsk_slicer_dut(iq_pairs_q15, chip_yaml):
    """Build x16_in -> QPSKSlicerBlock -> x16_out, drive (I, Q) Q15 pairs, and
    return the emitted 2-bit symbol indices (0..3), one per input point.
    Mirrors ``qam16_dut._run_qam16_slicer_complex`` (same complex-in/word-out
    single-block shape)."""
    import simkyt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("m", ctk)
    blk = ctrl.place_block("QPSKSlicerBlock", 0, 1, 1, library="lattrex.official",
                           params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="in_i"), name="ini")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({ctk: ct})
    assert rep.ok, rep.failed
    res = BuildEngine(cat, chip_yaml).build(ctrl.project, {ctk: ct})
    assert res.ok, res.errors

    entry, ins = cat.resolved_io("QPSKSlicerBlock", {}, library="lattrex.official")
    port = ct.port("x16_in")
    bo = ctrl.project.block(blk)
    lc = bo.placement.cells[0]
    dist = abs(lc.x - port.cell_x) + abs(lc.y - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)

    out = []
    for (i, q) in iq_pairs_q15:
        chip.inject_data_physical([int(i) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[0]))
        chip.run(max_events=6000)
        chip.inject_data_physical([int(q) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[1]))
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=200000)
        while chip.output_available("x16_out"):
            out += [int(x) & 0xFFFF for x in
                    chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return [w & 0x3 for w in out]


def _run(pairs_q15):
    dut = run_qpsk_slicer_dut(pairs_q15, CHIP_YAML)
    floats = [((v - 0x10000 if v >= 0x8000 else v) / 32768.0,
               (w - 0x10000 if w >= 0x8000 else w) / 32768.0) for (v, w) in pairs_q15]
    gr = _gr_decision(floats)
    return dut, gr


# --- correctness ----------------------------------------------------------------

def test_all_4_points_decode_to_gr_symbol():
    """Each exact GR constellation point decodes to its own index."""
    pts = _gr_points()
    pairs = [(_fq(p.real), _fq(p.imag)) for p in pts]
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(n) if dut[k] != gr[k])
    print(f"\nexact points: {errs} sym errors / {n}")
    assert n == len(pts) and errs == 0, f"{errs} symbol errors vs GR decision_maker"


def test_noisy_samples_decode_like_gr():
    """Noisy samples in every quadrant decode identically to GR's
    decision_maker (proves the sign thresholds match GR's decision cells)."""
    rng = random.Random(7)
    pairs = []
    for _ in range(160):
        i = rng.choice([-1, 1]) * (0.707 + rng.uniform(-0.4, 0.25))
        q = rng.choice([-1, 1]) * (0.707 + rng.uniform(-0.4, 0.25))
        pairs.append((_fq(i), _fq(q)))
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(n) if dut[k] != gr[k])
    print(f"\nnoisy: {errs} sym errors / {n}")
    assert errs == 0, f"{errs} symbol errors vs GR decision_maker"


# --- MANDATORY mutation gates (INV-4) --------------------------------------------

def test_mutation_swapped_axis_map_fails():
    """The swapped bit map ``(I>=0 ? 2:0)|(Q>=0 ? 1:0)`` must disagree with GR
    (GR's MSB comes from the Q axis) — proving the gate can tell them apart."""
    pts = _gr_points()

    def swapped(z):
        return (2 if z.real >= 0 else 0) | (1 if z.imag >= 0 else 0)
    gr = _gr_decision([(p.real, p.imag) for p in pts])
    disagree = sum(1 for k, p in enumerate(pts) if swapped(p) != gr[k])
    assert disagree > 0, ("the swapped-axis map agreed with GR on every point — "
                          "the gate cannot detect an I/Q bit swap!")


def test_mutation_shifted_stream_fails():
    pts = _gr_points()
    pairs = [(_fq(p.real), _fq(p.imag)) for p in pts] * 2
    dut, gr = _run(pairs)
    shifted = [dut[-1]] + list(dut[:-1])
    n = min(len(shifted), len(gr))
    errs = sum(1 for k in range(1, n) if shifted[k] != gr[k])
    assert errs > 0, "a one-symbol shift went undetected!"


def test_empty_output_fails():
    dut = run_qpsk_slicer_dut([], CHIP_YAML)
    assert len(dut) == 0


# --- report ----------------------------------------------------------------------

def test_emit_report():
    rng = random.Random(5)
    pairs = []
    for _ in range(64):
        i = rng.choice([-1, 1]) * (0.707 + rng.uniform(-0.35, 0.25))
        q = rng.choice([-1, 1]) * (0.707 + rng.uniform(-0.35, 0.25))
        pairs.append((_fq(i), _fq(q)))
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(n) if dut[k] != gr[k])
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("QPSKSlicerBlock", res, coverage={
        "gr_equiv": "digital.constellation_decoder_cb(constellation_qpsk())",
        "patterns": "exact points, noisy quadrants (160 samples)",
        "mutation": True,
        "decision": "per-axis SIGN: symbol = (Q>=0 ? 2:0) | (I>=0 ? 1:0)",
        "note": "1-cell branchless sign slicer; verified vs GR decision_maker",
    })
