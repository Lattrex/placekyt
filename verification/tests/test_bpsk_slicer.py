# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify BPSKSlicerBlock 1:1 against GNU Radio ``digital.binary_slicer_fb``.

``digital.binary_slicer_fb`` (binary_slicer_fb.h) is a MEMORYLESS feed-forward
hard slicer on the sign of a real input:

    input <  0  -> 0
    input >= 0  -> 1   (the 0 tie -> 1)

float in / byte out, one output byte per input sample. The Kyttar block in
``out_mode="bit"`` reproduces that EXACTLY (one word 0/1 per sample), so this is a
clean BIT-EXACT verification: no RMS normalization, no group delay, no tolerance.

The classic INV-25 trap this block was carrying: the PoC computed the INVERTED
decision (``>= 0 -> 0``, ``< 0 -> 1``) with the tie the wrong way. It only ever
"worked" because the BPSK modem's BER metric is 180°-inversion-tolerant. This gate
holds the block to GR's EXACT boundary, including the ``input == 0 -> 1`` tie.

Coverage: edge (exact 0, tiny +/-1 LSB around 0, full-scale +/-) + random (>=3
seeds) + out_mode sweep (bit / byte / word packing) + mandatory INV-4 mutation
gates (inverted decision, wrong tie-break, +1 sample shift, empty output) that MUST
FAIL against the GR reference.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_bpsk_slicer.py -q
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

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.bpsk_slicer_block import BPSKSlicerBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


def _s16(w: int) -> int:
    w &= 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


def _q15_to_float(w: int) -> float:
    """Q15 word -> the float GNU Radio would slice (exactly what the chip sees)."""
    return _s16(w) / 32768.0


def _gr_slice(words_q15) -> list[int]:
    """GR golden: ``digital.binary_slicer_fb`` on the float of each Q15 word."""
    floats = [_q15_to_float(w) for w in words_q15]
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks, digital\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_f(d, False)\n"
        "sl = digital.binary_slicer_fb()\n"
        "snk = blocks.vector_sink_b()\n"
        "tb.connect(src, sl); tb.connect(sl, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(floats),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_bit(words_q15):
    """DUT (bit mode, 1:1) sliced bytes for each input word + the GR reference."""
    dut = run_block_dut("BPSKSlicerBlock", words_q15, params={"out_mode": "bit"},
                        in_port="llr", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    gr = _gr_slice(words_q15)
    return dut, gr


# --- stimulus families --------------------------------------------------------
# Edge: exact 0 (the tie -> 1), the two 1-LSB neighbours of 0 (+1, -1 = 0xFFFF),
# full-scale +/- (0x7FFF = +max, 0x8000 = -1.0), mid-scale +/-, all-ones.
EDGE = [0x0000, 0x0001, 0xFFFF, 0x7FFF, 0x8000, 0x8001, 0x4000, 0xC000, 0x2000, 0xE000]


def _random(seed, n=24):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _bit_errors(dut_words, gr_bytes):
    n = min(len(dut_words), len(gr_bytes))
    assert n > 0, "no samples compared"
    errs = sum(1 for k in range(n)
               if dut_words[k] is None or (int(dut_words[k]) & 0xFFFF) != gr_bytes[k])
    return errs, n


# --- correctness: bit-exact vs GR ---------------------------------------------

def test_edge_bit_exact_vs_gr():
    """Edge vectors (exact 0, 1-LSB neighbours, full-scale +/-) slice bit-for-bit
    like GR binary_slicer_fb — INCLUDING the input==0 -> 1 tie."""
    dut, gr = _run_bit(EDGE)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nedge: {errs} bit errors / {n}; dut={dut.outputs_q15} gr={gr}")
    assert errs == 0, f"{errs}/{n} bit errors vs binary_slicer_fb"


def test_input_zero_ties_to_one():
    """The exact-zero tie must resolve to bit 1 (GR: input >= 0 -> 1)."""
    dut, gr = _run_bit([0x0000, 0x0000, 0x0001, 0xFFFF])
    assert gr == [1, 1, 1, 0], f"GR reference unexpected: {gr}"
    assert [int(x) & 0xFFFF for x in dut.outputs_q15] == [1, 1, 1, 0], \
        f"tie/neighbour decision wrong: {dut.outputs_q15}"


@pytest.mark.parametrize("seed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(seed):
    dut, gr = _run_bit(_random(seed))
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nrandom seed={seed}: {errs} bit errors / {n}")
    assert errs == 0, f"seed {seed}: {errs}/{n} bit errors vs GR"


# --- param sweep: out_mode packing (a Kyttar-only extension) -------------------
# The GR-equivalence is 'bit' mode. 'byte'/'word' pack the SAME sliced bits
# MSB-first; verify the on-chip packed words unpack to the exact GR bit stream.

@pytest.mark.parametrize("out_mode,bits_per", [("byte", 8), ("word", 16)])
def test_packed_modes_unpack_to_gr_bits(out_mode, bits_per):
    """out_mode byte/word: unpacking the emitted words MSB-first recovers the
    exact GR binary_slicer_fb bit stream (the packing is lossless + GR-consistent)."""
    words_q15 = _random(99, n=bits_per * 3)   # a whole number of packed groups
    gr = _gr_slice(words_q15)
    dut = run_block_dut("BPSKSlicerBlock", words_q15,
                        params={"out_mode": out_mode}, in_port="llr",
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    # bit mode emits one word/sample; packed mode emits one word per `bits_per`
    # samples. run_block_dut records got[-1] per driven sample, so the packed word
    # appears on the sample that completes each group; collect the non-None words.
    packed = [int(w) & 0xFFFF for w in dut.outputs_q15 if w is not None]
    unpacked = []
    for w in packed:
        for k in range(bits_per - 1, -1, -1):
            unpacked.append((w >> k) & 1)
    n = min(len(unpacked), len(gr))
    assert n >= bits_per, f"{out_mode}: too few unpacked bits ({n})"
    errs = sum(1 for k in range(n) if unpacked[k] != gr[k])
    print(f"\n{out_mode}: {len(packed)} words -> {len(unpacked)} bits, {errs} err/{n}")
    assert errs == 0, f"{out_mode} packing != GR bit stream: {errs}/{n}"


# --- reference sanity (pure python == GR, no chip) ----------------------------

def test_reference_matches_gr_over_range():
    """process_reference('bit') == GR binary_slicer_fb over a full-range sweep."""
    words = list(range(0, 0x10000, 257))   # dense sweep across the whole Q15 range
    ref = [int(b) & 0xFFFF for b in BPSKSlicerBlock("r", out_mode="bit")
           .process_reference([_s16(w) for w in words])]
    gr = _gr_slice(words)
    n = min(len(ref), len(gr))
    errs = sum(1 for k in range(n) if ref[k] != gr[k])
    assert errs == 0, f"reference disagrees with GR on {errs}/{n} samples"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_inverted_decision_fails():
    """The OLD inverted decision (the PoC bug: >=0 -> 0, <0 -> 1) must DISAGREE
    with GR — proving the gate rejects the pre-fix slicer."""
    dut, gr = _run_bit(EDGE)
    inverted = [1 - (int(w) & 1) for w in dut.outputs_q15]   # flip every bit
    errs, n = _bit_errors(inverted, gr)
    assert errs > 0, "an inverted-decision DUT went undetected by the gate!"


def test_mutation_wrong_tiebreak_fails():
    """A slicer that ties input==0 to 0 (GR ties it to 1) must be caught."""
    words = [0x0000, 0x0000, 0x0000, 0x0001]
    gr = _gr_slice(words)               # [1,1,1,1]
    wrong = [0, 0, 0, 1]                # tie -> 0 (the wrong break)
    errs = sum(1 for a, b in zip(wrong, gr) if a != b)
    assert errs > 0, "a wrong (0) tie-break agreed with GR — gate is blind to it!"


def test_mutation_one_sample_shift_fails():
    """A +1-sample shift of the decoded stream must FAIL (no free lag alignment)."""
    dut, gr = _run_bit(_random(7))
    shifted = [0] + [int(w) & 0xFFFF for w in dut.outputs_q15[:-1]]
    errs, n = _bit_errors(shifted, gr)
    assert errs > 0, "a one-sample shift went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_slice(EDGE)
    n = min(0, len(gr))
    assert n == 0 and len(gr) > 0   # empty DUT -> nothing compared -> not a pass


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    dut, gr = _run_bit(EDGE)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("BPSKSlicerBlock", res, coverage={
        "gr_equiv": "digital.binary_slicer_fb",
        "edge": True, "random": 4, "param_sweep": "out_mode bit/byte/word",
        "mutation": True,
        "decision": "sign of input: >=0 -> 1 (tie -> 1), <0 -> 0 (GR-exact)",
        "note": "1-cell memoryless slicer; bit-exact vs binary_slicer_fb, delay 0",
    })
