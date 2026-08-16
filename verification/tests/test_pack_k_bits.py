# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify PackKBitsBlock 1:1 against GNU Radio ``blocks.pack_k_bits_bb``.

``blocks.pack_k_bits_bb(k)`` consumes ``k`` input bytes (each carrying one bit in
its LSB) and packs them **MSB-first** (GR's fixed convention) into one output byte::

    byte = (b[0] << (k-1)) | (b[1] << (k-2)) | ... | (b[k-1] << 0)

i.e. per input bit ``acc = (acc << 1) | (bit & 1)`` and a byte is emitted every
``k`` bits. It is RATE-REDUCING (``k`` in -> 1 out) and drops a trailing partial
group of < k bits (GR emits ``floor(nin/k)`` bytes). GR reads only the LOW bit of
each input item (``d_bits[i] & 1``).

This is a BIT-EXACT gate (metric DECISION, tolerance 0), compared against the LIVE
GNU Radio block over k = 2..8, edge (all-zeros / all-ones), random (>= 3 seeds), a
k sweep, and the mandatory INV-4 mutation gates (LSB-first instead of MSB-first,
wrong k, a dropped bit, inverted output, +1 shift, empty) that MUST FAIL.

The on-chip block is rate-reducing: ``run_block_dut`` records ``got[-1]`` per driven
sample, so a packed byte appears on the sample that completes each k-bit group and
the k-1 accumulating samples read ``None``. The comparison collects the non-None
words (the packed byte stream) and asserts it equals GR bit-for-bit.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_pack_k_bits.py -q
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
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.pack_k_bits_block import PackKBitsBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


# --- GNU Radio golden reference (subprocess into the GR interpreter) ----------

def _gr_pack(inbits, k):
    """GR golden: ``blocks.pack_k_bits_bb(k)`` on the input bit stream."""
    payload = {"inbits": [int(b) & 0xFF for b in inbits], "k": int(k)}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['inbits'], False, 1, [])\n"
        "pk  = blocks.pack_k_bits_bb(d['k'])\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, pk); tb.connect(pk, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_dut(inbits, k):
    """Build + run PackKBitsBlock on simKYT for the given bit stream + k."""
    words = [int(b) & 0xFFFF for b in inbits]
    dut = run_block_dut("PackKBitsBlock", words, params={"k": k},
                        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _packed(dut):
    """The on-chip packed byte stream: the non-None words (a byte lands on the
    sample completing each k-bit group; accumulating samples read None)."""
    return [int(w) & 0xFFFF for w in dut.outputs_q15 if w is not None]


def _byte_errors(dut_bytes, gr_bytes):
    n = min(len(dut_bytes), len(gr_bytes))
    assert n > 0, "no bytes compared"
    errs = sum(1 for j in range(n) if (dut_bytes[j] & 0xFF) != (gr_bytes[j] & 0xFF))
    # a length mismatch is itself an error (dropped/extra group)
    errs += abs(len(dut_bytes) - len(gr_bytes))
    return errs, n


# --- stimulus families --------------------------------------------------------

def _random_bits(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 1) for _ in range(n)]


K_VALUES = [2, 3, 4, 5, 6, 7, 8]


# --- correctness: bit-exact vs GR ---------------------------------------------

@pytest.mark.parametrize("k", K_VALUES)
def test_k_sweep_bit_exact_vs_gr(k):
    """k sweep 2..8: a random bit stream (whole number of k-bit groups) packs
    byte-for-byte like GR pack_k_bits_bb."""
    inbits = _random_bits(1000 + k, n=k * 6)   # 6 full groups
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    errs, n = _byte_errors(got, gr)
    print(f"\nk={k}: dut={got} gr={gr}; {errs} err / {n}")
    assert errs == 0, f"k={k}: {errs}/{n} byte errors (dut={got} gr={gr})"


@pytest.mark.parametrize("k", [2, 4, 8])
def test_all_zeros_packs_to_zero(k):
    """All-zeros input -> all-zero bytes; must match GR exactly."""
    inbits = [0] * (k * 4)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    errs, n = _byte_errors(got, gr)
    assert errs == 0 and all(b == 0 for b in got), f"k={k}: all-zeros != GR: {got}"


@pytest.mark.parametrize("k", [2, 4, 8])
def test_all_ones_packs_to_full_mask(k):
    """All-ones input -> each byte is (2**k - 1); must match GR exactly (MSB-first
    packing of all 1s is the k-bit all-ones mask)."""
    inbits = [1] * (k * 4)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    errs, n = _byte_errors(got, gr)
    assert errs == 0 and all(b == (1 << k) - 1 for b in got), \
        f"k={k}: all-ones != GR: {got} (expect {(1 << k) - 1})"


def test_msb_first_ordering_is_gr():
    """The FIRST input bit is the MOST significant bit of the output byte — the
    single fact that pins the MSB-first convention (a known GR-vs-LSB trap)."""
    # k=8, bits 1,0,0,0,0,0,0,0 -> 0x80 = 128 if MSB-first (GR), 1 if LSB-first.
    inbits = [1, 0, 0, 0, 0, 0, 0, 0]
    dut = _run_dut(inbits, 8)
    gr = _gr_pack(inbits, 8)
    got = _packed(dut)
    assert gr == [128], f"GR sanity: MSB-first expected 128, got {gr}"
    assert got == [128], f"DUT not MSB-first: {got} (expect [128])"


@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_bit_exact_vs_gr(rseed):
    """Random bit streams (>= 3 seeds) pack bit-exact vs GR at k=8."""
    k = 8
    inbits = _random_bits(rseed, n=k * 8)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    errs, n = _byte_errors(got, gr)
    print(f"\nrandom rseed={rseed}: {errs} err / {n}")
    assert errs == 0, f"rseed {rseed}: {errs}/{n} byte errors vs GR"


@pytest.mark.parametrize("k", [3, 5, 7])
def test_trailing_partial_group_dropped_like_gr(k):
    """A trailing partial group (< k bits) is NOT emitted — GR produces exactly
    floor(nin/k) bytes; the DUT must match that count and those bytes."""
    inbits = _random_bits(55, n=k * 3 + (k - 1))   # 3 full groups + a partial
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    assert len(gr) == 3, f"GR sanity: expected 3 full bytes, got {gr}"
    errs, n = _byte_errors(got, gr)
    assert errs == 0 and len(got) == len(gr), \
        f"k={k}: partial-group handling != GR (dut={got} gr={gr})"


def test_input_lsb_masked_like_gr():
    """GR reads only the LSB of each input item (d_bits[i] & 1); a stray high bit
    must be ignored identically by the DUT."""
    k = 4
    # 0/1 pattern but with high bits set on some items (2,3,5 -> LSB 0,1,1).
    inbits = [3, 0, 5, 2,  2, 3, 1, 0]  # LSBs: 1,0,1,0, 0,1,1,0 -> 0xA, 0x6
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    assert gr == [0xA, 0x6], f"GR sanity: LSB-mask expected [10,6], got {gr}"
    errs, n = _byte_errors(got, gr)
    assert errs == 0, f"LSB masking != GR: dut={got} gr={gr}"


# --- reference sanity (pure python == GR, no chip) ----------------------------

def test_reference_matches_gr_over_k_sweep():
    """process_reference == GR pack_k_bits_bb over the whole k sweep + a random
    stream (proves the on-chip-mirrored reference is itself GR-exact)."""
    for k in K_VALUES:
        inbits = _random_bits(2024, n=k * 6)
        ref = [int(b) & 0xFF for b in PackKBitsBlock("r", k=k)
               .process_reference(inbits)]
        gr = _gr_pack(inbits, k)
        assert ref == gr, f"reference != GR at k={k}: ref={ref} gr={gr}"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def test_mutation_lsb_first_instead_of_msb_first_fails():
    """Packing LSB-first (the wrong bit order) must DISAGREE with the MSB-first GR
    reference — the core convention the gate must be sensitive to."""
    k = 8
    inbits = _random_bits(3, n=k * 4)
    gr = _gr_pack(inbits, k)                    # correct MSB-first
    # Build the LSB-first mutant of the SAME bits by hand.
    mut = []
    for j in range(len(inbits) // k):
        byte = 0
        for i in range(k):
            byte |= (int(inbits[j * k + i]) & 1) << i   # LSB-first (wrong)
        mut.append(byte)
    errs, n = _byte_errors(mut, gr)
    assert errs > 0, "an LSB-first packer went undetected by the gate!"


def test_mutation_wrong_k_fails():
    """A DUT built at the WRONG k must DISAGREE with the GR reference built at the
    correct k (proves the gate is sensitive to k)."""
    inbits = _random_bits(9, n=8 * 4)          # 32 bits
    gr = _gr_pack(inbits, 8)                    # correct k
    dut_wrong = _run_dut(inbits, 4)            # wrong k -> different byte stream
    got = _packed(dut_wrong)
    errs, n = _byte_errors(got, gr)
    assert errs > 0, "a wrong-k DUT went undetected by the gate!"


def test_mutation_dropped_bit_fails():
    """Dropping one input bit (a mis-counted group) must shift every subsequent
    byte and FAIL against GR — guards the bit counter / group boundary."""
    k = 8
    inbits = _random_bits(11, n=k * 4)
    gr = _gr_pack(inbits, k)
    dropped = _gr_pack(inbits[1:], k)          # same bits, one dropped at the front
    errs, n = _byte_errors(dropped, gr)
    assert errs > 0, "a dropped input bit went undetected by the gate!"


def test_mutation_inverted_output_fails():
    """Inverting every output byte must DISAGREE with GR (polarity flip)."""
    k = 8
    inbits = _random_bits(13, n=k * 4)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    inverted = [(~b) & 0xFF for b in _packed(dut)]
    errs, n = _byte_errors(inverted, gr)
    assert errs > 0, "an inverted-output DUT went undetected by the gate!"


def test_mutation_one_sample_shift_fails():
    """A +1-byte shift of the packed stream must FAIL (no free lag alignment)."""
    k = 8
    inbits = _random_bits(17, n=k * 5)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    got = _packed(dut)
    shifted = [0] + got[:-1]
    errs, n = _byte_errors(shifted, gr)
    assert errs > 0, "a one-byte shift went undetected!"


def test_empty_output_fails():
    """An empty DUT output cannot be certified against a non-empty reference."""
    gr = _gr_pack([1, 0] * 8, 2)
    assert len(gr) > 0
    # comparing an empty packed stream to a non-empty GR ref is all-error.
    errs, n = _byte_errors([0], gr)   # n>=1 forced; a length gap is counted
    assert errs > 0, "an (near-)empty output went undetected!"


# --- HARDWARE / RANGE guards (INV-0): unsupported k must RAISE ------------------

@pytest.mark.parametrize("bad_k", [0, -1, 9, 16])
def test_out_of_range_k_raises(bad_k):
    """k must be 1..8 (GR packs into one byte); out-of-range RAISES, never clamps."""
    with pytest.raises(ValueError):
        PackKBitsBlock("x", k=bad_k)


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    k = 8
    inbits = _random_bits(1, n=k * 6)
    dut = _run_dut(inbits, k)
    gr = _gr_pack(inbits, k)
    errs, n = _byte_errors(_packed(dut), gr)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("PackKBitsBlock", res, coverage={
        "gr_equiv": "blocks.pack_k_bits_bb",
        "edge": "all-zeros (0 bytes) / all-ones ((2^k-1) mask) / MSB-first ordering",
        "random": 4,
        "k_sweep": "2..8 bit-exact vs GR (whole groups) + trailing partial dropped (k=3/5/7)",
        "mutation": "LSB-first / wrong k / dropped bit / inverted / +1 shift / empty",
        "lsb_mask": "input LSB only (GR d_bits & 1), stray high bits ignored",
        "decision": "byte = MSB-first pack of k input LSBs; floor(nin/k) bytes (GR-exact)",
        "note": "1-cell bit-serial packer; BIT-EXACT vs pack_k_bits_bb, delay 0, tol 0",
        "hw_range": "k in 1..8 (packs into one byte); out-of-range RAISES",
    })
