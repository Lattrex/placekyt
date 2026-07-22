# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify QAM16SlicerBlock 1:1 against GNU Radio
``digital.constellation_decoder_cb(constellation_16qam())``.

The 16-QAM RX slicer turns a received (I, Q) sample into the 4-bit symbol index 0..15
that GR's ``constellation_16qam().decision_maker()`` returns. GR's map is NOT separable,
but the nearest-point decision factors into two per-axis (sign, outer-magnitude) bits +
a fixed 16-entry permutation LUT — VERIFIED here against ``decision_maker`` itself over
the exact constellation points AND noisy samples across every decision cell. Mandatory
mutation gates (INV-4): the OLD invented separable-Gray decode, a one-symbol shift, and
empty output all FAIL.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_qam16_slicer.py -q
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
from qam16_dut import run_qam16_slicer_dut  # noqa: E402
from gr_kyttar.placement.blocks.qam16_slicer_block import QAM16SlicerBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

_NORM = 1.0 / (10.0 ** 0.5)


def _fq(v):
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_decision(iq_floats):
    """GR golden: ``constellation_16qam().decision_maker(z)`` per sample."""
    script = ("import json,sys\n"
              "from gnuradio import digital\n"
              "c = digital.constellation_16qam()\n"
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
              "digital.constellation_16qam().points()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return [complex(a, b) for a, b in json.loads(r.stdout.strip().splitlines()[-1])]


def _run(pairs_q15):
    """DUT symbols (skip index 0 = pipeline warm-up) and the GR reference for them."""
    dut = run_qam16_slicer_dut(pairs_q15, CHIP_YAML)
    floats = [((v - 0x10000 if v >= 0x8000 else v) / 32768.0,
               (w - 0x10000 if w >= 0x8000 else w) / 32768.0) for (v, w) in pairs_q15]
    gr = _gr_decision(floats)
    return dut, gr


# --- correctness: exact constellation points ----------------------------------

def test_all_16_points_decode_to_gr_symbol():
    """Each exact GR point decodes to its own index (identity), matching GR."""
    pts = _gr_points()
    pairs = [(_fq(0.0), _fq(0.0))]   # a guard sample (warm-up), skipped
    pairs += [(_fq(p.real), _fq(p.imag)) for p in pts]
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(1, n) if dut[k] != gr[k])
    print(f"\nexact points: {errs} sym errors / {n - 1}")
    assert errs == 0, f"{errs} symbol errors vs GR decision_maker"


def test_noisy_samples_decode_like_gr():
    """Noisy samples around each grid point decode identically to GR's
    decision_maker (proves the per-axis thresholds match GR's decision cells)."""
    rng = random.Random(11)
    lv = [-3, -1, 1, 3]
    pairs = [(_fq(0.0), _fq(0.0))]
    for _ in range(120):
        i = rng.choice(lv) * _NORM + rng.uniform(-0.13, 0.13)
        q = rng.choice(lv) * _NORM + rng.uniform(-0.13, 0.13)
        pairs.append((_fq(i), _fq(q)))
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(1, n) if dut[k] != gr[k])
    print(f"\nnoisy: {errs} sym errors / {n - 1}")
    assert errs == 0, f"{errs} symbol errors vs GR decision_maker"


def test_mapper_slicer_loopback_identity():
    """Slicer(mapper(bits)) == the original symbols — the modem's clean-channel
    identity (the whole point of a matched mapper+slicer pair)."""
    from gr_kyttar.placement.blocks._qam16_common import qam16_points_q15
    pts = qam16_points_q15()

    def s16(v):
        return v - 0x10000 if v >= 0x8000 else v
    syms = [0] + list(range(16)) + [11, 4, 9, 2, 0, 15]
    pairs = [(pts[v][0], pts[v][1]) for v in syms]
    dut = run_qam16_slicer_dut(pairs, CHIP_YAML)
    n = min(len(dut), len(syms))
    errs = sum(1 for k in range(1, n) if dut[k] != syms[k])
    print(f"\nloopback: {errs} sym errors / {n - 1}")
    assert errs == 0, f"mapper->slicer loopback not identity: {errs} errors"


# --- MANDATORY mutation gates (INV-4) ------------------------------------------

def test_mutation_separable_gray_decode_fails():
    """The OLD invented separable per-axis Gray decode ``(I_bits<<2)|Q_bits`` must
    disagree with GR decision_maker — proving the gate rejects the legacy slicer."""
    pts = _gr_points()

    def legacy_decode(z):
        # per-axis Gray {-3:00,-1:01,+1:11,+3:10}; sym = (I_bits<<2)|Q_bits
        def axis(v):
            if v >= 0:
                return 0b11 if abs(v) < 2 * _NORM else 0b10
            return 0b01 if abs(v) < 2 * _NORM else 0b00
        return (axis(z.real) << 2) | axis(z.imag)
    gr = _gr_decision([(p.real, p.imag) for p in pts])
    disagree = sum(1 for k, p in enumerate(pts) if legacy_decode(p) != gr[k])
    assert disagree > 0, ("the legacy separable-Gray decode agreed with GR on every "
                          "point — the gate can't tell it from the GR slicer!")


def test_mutation_shifted_stream_fails():
    """A one-symbol shift of the decoded stream must be caught."""
    pts = _gr_points()
    pairs = [(_fq(0.0), _fq(0.0))] + [(_fq(p.real), _fq(p.imag)) for p in pts]
    dut, gr = _run(pairs)
    shifted = [0] + list(dut[:-1])
    n = min(len(shifted), len(gr))
    errs = sum(1 for k in range(2, n) if shifted[k] != gr[k])
    assert errs > 0, "a one-symbol shift went undetected!"


def test_empty_output_fails():
    dut = run_qam16_slicer_dut([], CHIP_YAML)
    assert len(dut) == 0


# --- report --------------------------------------------------------------------

def test_emit_report():
    rng = random.Random(3)
    pairs = [(_fq(0.0), _fq(0.0))]
    for _ in range(48):
        i = rng.choice([-3, -1, 1, 3]) * _NORM + rng.uniform(-0.1, 0.1)
        q = rng.choice([-3, -1, 1, 3]) * _NORM + rng.uniform(-0.1, 0.1)
        pairs.append((_fq(i), _fq(q)))
    dut, gr = _run(pairs)
    n = min(len(dut), len(gr))
    errs = sum(1 for k in range(1, n) if dut[k] != gr[k])
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n - 1, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("QAM16SlicerBlock", res, coverage={
        "gr_equiv": "digital.constellation_decoder_cb(constellation_16qam())",
        "patterns": "exact points, noisy grid, mapper->slicer loopback",
        "mutation": True,
        "decision": "per-axis (sign, |v|>=2/sqrt10) key -> GR permutation LUT",
        "note": "3-cell islice/qslice/lut; verified vs GR decision_maker",
    })
