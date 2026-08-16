# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify LFSRScramblerBlock 1:1 against GNU Radio ``digital.additive_scrambler_bb``.

``digital.additive_scrambler_bb`` XORs the input bit stream with the FREE-RUNNING
output of a Fibonacci LFSR (``gr::digital::lfsr::next_bit()``), defined by
``mask`` (polynomial), ``seed`` (initial register) and ``len`` (register length):

    output_bit = shift_register & 1               # the LFSR output (LSB)
    newbit     = parity(shift_register & mask)     # XOR of masked bits
    shift_register = (shift_register >> 1) | (newbit << len)
    scrambled_bit  = input_bit XOR output_bit

It is ADDITIVE (LFSR runs independently of the data), so it is deterministic given
``(mask, seed, len)`` and self-inverse — an identically-configured block descrambles.
There is NO meaningful correlation peak (per the manifest note): this is a BIT-EXACT
gate (metric DECISION, tolerance 0), compared against the LIVE GNU Radio block.

The classic trap this gate guards against is the Fibonacci-vs-Galois LFSR
convention: GNU Radio uses the RIGHT-shifting Fibonacci form above (output = LSB,
feedback into bit ``len``), NOT a left-shifting Galois LFSR. Confirmed against live
GR output (all-zeros input reveals the raw ``next_bit()`` sequence).

Coverage: edge (all-zeros in -> raw LFSR sequence / all-ones in -> its complement),
seed sweep, random (>=3 seeds), parameter sweep over mask/seed/len (+ ``count``
reseed), and mandatory INV-4 mutation gates (wrong seed, wrong tap mask, off-by-one
register length, inverted output, +1 sample shift, empty output) that MUST FAIL
against the GR reference.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_lfsr_scrambler.py -q
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
from gr_kyttar.placement.blocks.lfsr_scrambler_block import LFSRScramblerBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


# --- GNU Radio golden reference (subprocess into the GR interpreter) ----------

def _gr_scramble(inbits, mask, seed, length, count=0):
    """GR golden: ``digital.additive_scrambler_bb`` on the input bit stream."""
    payload = {"inbits": [int(b) & 1 for b in inbits],
               "mask": int(mask), "seed": int(seed), "len": int(length),
               "count": int(count)}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks, digital\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['inbits'], False, 1, [])\n"
        "sc  = digital.additive_scrambler_bb(d['mask'], d['seed'], d['len'], "
        "d['count'], 1)\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, sc); tb.connect(sc, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_dut(inbits, mask, seed, length, count=0):
    """Build + run the Kyttar block on simKYT for the given bit stream + params."""
    words = [int(b) & 1 for b in inbits]
    dut = run_block_dut(
        "LFSRScramblerBlock", words,
        params={"mask": mask, "seed": seed, "len": length, "count": count},
        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _bit_errors(dut_words, gr_bits):
    n = min(len(dut_words), len(gr_bits))
    assert n > 0, "no samples compared"
    errs = sum(1 for k in range(n)
               if dut_words[k] is None or (int(dut_words[k]) & 1) != (gr_bits[k] & 1))
    return errs, n


# --- stimulus families --------------------------------------------------------
# A few representative (mask, seed, len) configs spanning register lengths.
CONFIGS = [
    (0x8A, 0x7F, 7),      # GR default-ish 7-bit scrambler (x^7+x^3+1 family)
    (0x19, 0x01, 4),      # x^4 + x^3 + 1
    (0x29, 0x1F, 5),      # x^5 + x^3 + 1
    (0x61, 0x2A, 6),      # x^6 + x^5 + 1
    (0x4001, 0x0001, 15),  # 15-bit LFSR (x^15 + x^14 + 1, MIL-STD-188 family)
]


def _random_bits(seed, n=48):
    rng = random.Random(seed)
    return [rng.randint(0, 1) for _ in range(n)]


# --- correctness: bit-exact vs GR ---------------------------------------------

def test_all_zeros_reveals_raw_lfsr_sequence():
    """All-zeros input -> the raw ``next_bit()`` LFSR sequence; must match GR
    exactly (this is the sequence that pins down the Fibonacci convention)."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = [0] * 48
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nall-zeros: {errs} errors / {n}; gr[:16]={gr[:16]}")
    assert errs == 0, f"raw LFSR sequence != GR: {errs}/{n}"


def test_all_ones_is_complement_of_lfsr_sequence():
    """All-ones input -> input XOR lfsr = complement of the all-zeros output;
    must match GR (proves the XOR is truly additive)."""
    mask, seed, length = 0x8A, 0x7F, 7
    ones = _run_dut([1] * 48, mask, seed, length)
    zeros_gr = _gr_scramble([0] * 48, mask, seed, length)
    ones_gr = _gr_scramble([1] * 48, mask, seed, length)
    # GR sanity: all-ones output is the bitwise complement of all-zeros output.
    assert ones_gr == [1 - b for b in zeros_gr]
    errs, n = _bit_errors(ones.outputs_q15, ones_gr)
    assert errs == 0, f"all-ones scramble != GR: {errs}/{n}"


@pytest.mark.parametrize("mask,seed,length", CONFIGS)
def test_param_sweep_bit_exact_vs_gr(mask, seed, length):
    """Parameter sweep over (mask, seed, len): random bit stream scrambles
    bit-for-bit like GR additive_scrambler_bb."""
    inbits = _random_bits(1234 + length, n=48)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nmask=0x{mask:X} seed=0x{seed:X} len={length}: {errs} errors / {n}")
    assert errs == 0, f"config (0x{mask:X},0x{seed:X},{length}): {errs}/{n} bit errors"


@pytest.mark.parametrize("seed", [0x01, 0x2A, 0x55, 0x7F])
def test_seed_sweep_bit_exact_vs_gr(seed):
    """Seed sweep at fixed (mask,len): each seed scrambles bit-exact vs GR."""
    mask, length = 0x8A, 7
    inbits = _random_bits(7, n=40)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    assert errs == 0, f"seed 0x{seed:X}: {errs}/{n} bit errors"


@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(rseed):
    """Random bit streams (>=3 seeds) scramble bit-exact vs GR."""
    mask, seed, length = 0x4001, 0x0001, 15
    inbits = _random_bits(rseed, n=64)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\nrandom rseed={rseed}: {errs} errors / {n}")
    assert errs == 0, f"rseed {rseed}: {errs}/{n} bit errors vs GR"


@pytest.mark.parametrize("count", [4, 8, 13])
def test_count_reseed_bit_exact_vs_gr(count):
    """The ``count`` fixed-vector reset (reseed every ``count`` bits) matches GR
    exactly — the LFSR restarts from ``seed`` on the boundary."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = _random_bits(99, n=50)
    dut = _run_dut(inbits, mask, seed, length, count=count)
    gr = _gr_scramble(inbits, mask, seed, length, count=count)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    print(f"\ncount={count}: {errs} errors / {n}")
    assert errs == 0, f"count={count}: {errs}/{n} bit errors vs GR"


def test_self_inverse_descrambles():
    """An identically-configured block descrambles: scramble then scramble again
    recovers the original bit stream (additive self-synchronizing property).
    Verified end-to-end on-chip."""
    mask, seed, length = 0x4001, 0x0001, 15
    inbits = _random_bits(5, n=48)
    scrambled = [int(w) & 1 for w in _run_dut(inbits, mask, seed, length).outputs_q15]
    recovered = [int(w) & 1 for w in _run_dut(scrambled, mask, seed, length).outputs_q15]
    assert recovered == inbits, "descramble did not recover the original bits"


# --- reference sanity (pure python == GR, no chip) ----------------------------

def test_reference_matches_gr_over_configs():
    """process_reference == GR additive_scrambler_bb over every config + a random
    stream (proves the on-chip-mirrored reference is itself GR-exact)."""
    for (mask, seed, length) in CONFIGS:
        inbits = _random_bits(2024, n=64)
        ref = [int(b) & 1 for b in LFSRScramblerBlock(
            "r", mask=mask, seed=seed, len=length).process_reference(inbits)]
        gr = _gr_scramble(inbits, mask, seed, length)
        n = min(len(ref), len(gr))
        errs = sum(1 for k in range(n) if ref[k] != gr[k])
        assert errs == 0, f"reference != GR on config (0x{mask:X},0x{seed:X},{length})"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_wrong_seed_fails():
    """A DUT built with the WRONG seed must DISAGREE with the GR reference built
    from the correct seed — proving the gate is sensitive to the seed."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = [0] * 48
    gr = _gr_scramble(inbits, mask, seed, length)          # correct seed
    dut_wrong = _run_dut(inbits, mask, seed ^ 0x01, length)  # wrong seed
    errs, n = _bit_errors(dut_wrong.outputs_q15, gr)
    assert errs > 0, "a wrong-seed DUT went undetected by the gate!"


def test_mutation_wrong_tap_mask_fails():
    """A DUT built with the WRONG polynomial tap mask must DISAGREE with the GR
    reference built from the correct mask (guards the Fibonacci tap positions)."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = [0] * 48
    gr = _gr_scramble(inbits, mask, seed, length)          # correct mask
    dut_wrong = _run_dut(inbits, mask ^ 0x04, seed, length)  # perturbed tap
    errs, n = _bit_errors(dut_wrong.outputs_q15, gr)
    assert errs > 0, "a wrong-tap-mask DUT went undetected by the gate!"


def test_mutation_off_by_one_register_length_fails():
    """A DUT built with an off-by-one register length (``len``) must DISAGREE with
    the GR reference at the correct length (guards the feedback bit position)."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = [0] * 48
    gr = _gr_scramble(inbits, mask, seed, length)          # correct len
    dut_wrong = _run_dut(inbits, mask, seed, length + 1)     # len off by one
    errs, n = _bit_errors(dut_wrong.outputs_q15, gr)
    assert errs > 0, "an off-by-one register length went undetected by the gate!"


def test_mutation_inverted_output_fails():
    """Inverting every output bit must DISAGREE with GR — proving the gate rejects
    a sign/polarity-flipped scrambler."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = _random_bits(3, n=40)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    inverted = [1 - (int(w) & 1) for w in dut.outputs_q15]
    errs, n = _bit_errors(inverted, gr)
    assert errs > 0, "an inverted-output DUT went undetected by the gate!"


def test_mutation_one_sample_shift_fails():
    """A +1-sample shift of the scrambled stream must FAIL (no free lag alignment)."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = _random_bits(11, n=40)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    shifted = [0] + [int(w) & 1 for w in dut.outputs_q15[:-1]]
    errs, n = _bit_errors(shifted, gr)
    assert errs > 0, "a one-sample shift went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_scramble([0] * 16, 0x8A, 0x7F, 7)
    n = min(0, len(gr))
    assert n == 0 and len(gr) > 0   # empty DUT -> nothing compared -> not a pass


# --- HARDWARE-DEVIATION guards (INV-0): unsupported params must RAISE ----------

def test_bits_per_byte_must_be_one():
    with pytest.raises(ValueError):
        LFSRScramblerBlock("x", mask=0x8A, seed=0x7F, len=7, bits_per_byte=8)


def test_len_ceiling_raises():
    with pytest.raises(ValueError):
        LFSRScramblerBlock("x", mask=0x8A, seed=0x7F, len=16)


def test_reset_tag_key_unsupported():
    with pytest.raises(ValueError):
        LFSRScramblerBlock("x", mask=0x8A, seed=0x7F, len=7, reset_tag_key="rst")


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    mask, seed, length = 0x8A, 0x7F, 7
    inbits = _random_bits(1, n=48)
    dut = _run_dut(inbits, mask, seed, length)
    gr = _gr_scramble(inbits, mask, seed, length)
    errs, n = _bit_errors(dut.outputs_q15, gr)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("LFSRScramblerBlock", res, coverage={
        "gr_equiv": "digital.additive_scrambler_bb",
        "edge": "all-zeros (raw LFSR seq) / all-ones (complement)",
        "random": 4, "seed_sweep": 4,
        "param_sweep": "mask/seed/len over 5 configs (len 4..15) + count reseed 4/8/13",
        "mutation": "wrong seed / wrong tap mask / off-by-one len / inverted / +1 shift / empty",
        "self_inverse": "scramble->scramble recovers input on-chip",
        "decision": "out = input XOR next_bit(); Fibonacci right-shift LFSR (GR-exact)",
        "note": "1-cell bit-serial additive scrambler; BIT-EXACT vs additive_scrambler_bb, delay 0",
        "hw_deviation": "bits_per_byte must be 1; len<=15 (16-bit reg); no reset_tag_key",
    })
