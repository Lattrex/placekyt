# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify HammingDecoderBlock — systematic Hamming(7,4) hard-decision syndrome
decoder (NO GNU Radio counterpart; gr-fec ships no plain Hamming(7,4) factory).

GOLDEN: the standard textbook syndrome decoder (R. W. Hamming, "Error Detecting
and Error Correcting Codes", Bell System Technical Journal 29(2):147-160, 1950;
Lin & Costello, "Error Control Coding", 2nd ed., §3.3 syndrome decoding), on THE
PINNED CONVENTION shared verbatim with the sibling HammingEncoderBlock:

    codeword MSB-first on the wire:  c = d3 d2 d1 d0 p2 p1 p0
    data nibble MSB-first (d3 first), EVEN parity:
        p2 = d3 ^ d2 ^ d1
        p1 = d3 ^ d2 ^ d0
        p0 = d3 ^ d1 ^ d0

H's columns follow directly (syndrome s = s2 s1 s0, sK = received pK XOR its
recomputed parity): d3->7, d2->6, d1->5, d0->3, p2->4, p1->2, p0->1 — all
distinct and non-zero, the single-error-correcting property. The syndrome ->
flip-bit LUT over the 7-bit word (bit6=d3 .. bit0=p0) is therefore
[0, 1, 2, 8, 4, 16, 32, 64] for s = 0..7. The GOLDEN ENCODER below is written
from EXACTLY these equations — the round-trip gate is the convention pin.

Coverage (the dispatch bar):
  * all 16 clean codewords decode to their data nibble;
  * EVERY single-bit error position on every codeword corrected — 7*16 = 112
    cases, EXHAUSTIVE, on-chip;
  * double-bit errors are UNCORRECTABLE (Hamming distance 3) — documented as a
    known limit and GATED (every one of the 16*21 double-error words decodes to
    a WRONG nibble, and the DUT reproduces the golden's deterministic
    miscorrection — not hidden);
  * random streams, >= 3 seeds;
  * round-trip golden-encoder -> DUT-decoder == identity under 0 and 1 injected
    errors per codeword;
  * INV-4 mutations (no-correction passthrough, wrong syndrome LUT, swapped bit
    order, +1 shift, empty) all FAIL the gate.

Bit streams are RAW words (one 0/1 bit per sample, LSB used — the Pack/Unpack
convention), not Q15 (the XorBlock lesson). Rate-REDUCING 7:4, so the DUT runs
through ``run_block_dut_rate`` (drains the whole 4-bit burst each 7th trigger).

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_hamming_decoder.py -q
"""
from __future__ import annotations

import itertools
import os
import random
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

from kyttar_verify import (  # noqa: E402
    run_block_dut_rate, write_report, CompareResult, Metric)
from gr_kyttar.placement.blocks.hamming_decoder_block import (  # noqa: E402
    HammingDecoderBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


# --- the pinned convention, written out INDEPENDENTLY of the block ------------

def golden_encode(nib: int) -> int:
    """GOLDEN ENCODER — the pinned convention equations VERBATIM (the sibling
    HammingEncoderBlock is built from these same lines). nibble MSB-first."""
    d3, d2, d1, d0 = (nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1
    p2 = d3 ^ d2 ^ d1
    p1 = d3 ^ d2 ^ d0
    p0 = d3 ^ d1 ^ d0
    return (d3 << 6) | (d2 << 5) | (d1 << 4) | (d0 << 3) | (p2 << 2) | (p1 << 1) | p0


# H columns in wire order (derived from the equations above; see module docstring)
_COLS = (7, 6, 5, 3, 4, 2, 1)
_FLIP = (0, 1, 2, 8, 4, 16, 32, 64)


def golden_decode(word7: int) -> int:
    """GOLDEN DECODER — the standard syndrome decode (Hamming 1950; Lin &
    Costello §3.3): syndrome = XOR of the H columns of the set received bits;
    flip the LUT-indicated bit; emit d3 d2 d1 d0."""
    w = int(word7) & 0x7F
    s = 0
    for j in range(7):                     # j=0 -> d3 (bit 6) ... j=6 -> p0 (bit 0)
        if (w >> (6 - j)) & 1:
            s ^= _COLS[j]
    c = w ^ _FLIP[s]
    return (c >> 3) & 0xF


def bits_of(word7: int) -> list[int]:
    """7-bit word -> wire bits, MSB (d3) first."""
    return [(word7 >> k) & 1 for k in range(6, -1, -1)]


def nib_bits(nib: int) -> list[int]:
    """4-bit nibble -> bits MSB (d3) first."""
    return [(nib >> k) & 1 for k in range(3, -1, -1)]


def golden_stream(bit_stream) -> list[int]:
    """Golden over a raw bit stream: group by 7 (MSB-first), syndrome-decode,
    emit 4 bits MSB-first; trailing partial group dropped."""
    bits = [int(b) & 1 for b in bit_stream]
    out: list[int] = []
    for g in range(len(bits) // 7):
        w = 0
        for b in bits[g * 7:(g + 1) * 7]:
            w = (w << 1) | b
        out.extend(nib_bits(golden_decode(w)))
    return out


def _run_dut(bit_stream):
    dut = run_block_dut_rate("HammingDecoderBlock",
                             [int(b) & 0xFFFF for b in bit_stream],
                             in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _bit_errors(got, ref):
    n = min(len(got), len(ref))
    assert n > 0, "no bits compared"
    errs = sum(1 for j in range(n) if (int(got[j]) & 1) != (int(ref[j]) & 1))
    errs += abs(len(got) - len(ref))       # a length mismatch is itself an error
    return errs, n


# --- golden self-checks: the code IS a (7,4) Hamming code ---------------------

def test_golden_code_properties():
    """The pinned convention really is a distance-3 single-error-correcting
    (7,4) code: 16 codewords, every codeword has syndrome 0, minimum nonzero
    codeword weight 3, and H's columns are distinct and non-zero."""
    cws = [golden_encode(n) for n in range(16)]
    assert len(set(cws)) == 16
    assert sorted(set(_COLS)) == [1, 2, 3, 4, 5, 6, 7]      # distinct, non-zero
    for n, c in enumerate(cws):
        # syndrome of a clean codeword is 0 and it decodes to its own nibble
        s = 0
        for j in range(7):
            if (c >> (6 - j)) & 1:
                s ^= _COLS[j]
        assert s == 0, f"codeword {c:07b} has non-zero syndrome {s}"
        assert golden_decode(c) == n
    # linear code: min distance = min nonzero weight = 3 (Hamming bound met)
    wmin = min(bin(c).count("1") for c in cws if c != 0)
    assert wmin == 3, f"minimum codeword weight {wmin} != 3"


def test_golden_matches_block_reference_exhaustively():
    """The block's process_reference == the independent golden over ALL 128
    possible 7-bit received words (covers clean + every 1..7-bit error pattern
    — the fused on-chip algebra is pinned to the standard decoder everywhere)."""
    for w in range(128):
        ref = list(HammingDecoderBlock("r").process_reference(bits_of(w)))
        gold = golden_stream(bits_of(w))
        assert ref == gold, f"word {w:07b}: block ref {ref} != golden {gold}"


# --- on-chip correctness ------------------------------------------------------

def test_all_16_clean_codewords_on_chip():
    """All 16 clean codewords, one stream, on-chip: decoded nibbles == data."""
    stream, want = [], []
    for nib in range(16):
        stream.extend(bits_of(golden_encode(nib)))
        want.extend(nib_bits(nib))
    dut = _run_dut(stream)
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    assert errs == 0, f"clean codewords: {errs}/{n} bit errors (got={got})"


def test_every_single_bit_error_corrected_exhaustive_on_chip():
    """EXHAUSTIVE single-error correction: all 7 error positions on all 16
    codewords (7*16 = 112 corrupted words, 784 bits) in one on-chip stream —
    every one must decode to the ORIGINAL data nibble."""
    stream, want = [], []
    for nib in range(16):
        c = golden_encode(nib)
        for pos in range(7):
            stream.extend(bits_of(c ^ (1 << pos)))
            want.extend(nib_bits(nib))
    dut = _run_dut(stream)
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    assert n == 112 * 4
    assert errs == 0, f"single-error exhaustive: {errs}/{n} bit errors"


def test_rate_7_to_4_burst_framing():
    """The block is rate 7:4: six accumulating triggers emit nothing, the 7th
    emits exactly the 4-bit burst (per-trigger drain proves no dropped or
    duplicated words — the INV-19/20 output-count bar, per-sample side)."""
    stream = bits_of(golden_encode(0xB)) + bits_of(golden_encode(0x4) ^ 0x10)
    dut = _run_dut(stream)
    lens = [len(t) for t in dut.per_trigger]
    assert lens == [0, 0, 0, 0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 4], lens
    assert [int(w) & 1 for w in dut.outputs_q15] == nib_bits(0xB) + nib_bits(0x4)


def test_trailing_partial_group_not_emitted():
    """A trailing partial group (< 7 bits) is never emitted (the pack_k_bits
    streaming convention)."""
    stream = bits_of(golden_encode(0x9)) + [1, 0, 1]       # 7 + 3 bits
    dut = _run_dut(stream)
    assert [int(w) & 1 for w in dut.outputs_q15] == nib_bits(0x9)
    assert all(len(t) == 0 for t in dut.per_trigger[7:])


@pytest.mark.parametrize("rseed", [1, 7, 42])
def test_random_streams_match_golden(rseed):
    """Random RAW bit streams (arbitrary 7-bit words, not just codewords, >= 3
    seeds): the DUT equals the standard syndrome decoder bit-for-bit."""
    rng = random.Random(rseed)
    stream = [rng.randint(0, 1) for _ in range(7 * 12)]
    dut = _run_dut(stream)
    want = golden_stream(stream)
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    assert errs == 0, f"seed {rseed}: {errs}/{n} bit errors vs golden"


def test_input_lsb_masked():
    """Only the LSB of each input word is a bit (the GR pack_k_bits input
    convention): stray high bits must be ignored."""
    nib = 0x6
    clean = bits_of(golden_encode(nib))
    noisy = [b | (rng_high << 1) for b, rng_high in
             zip(clean, [1, 0, 3, 0, 1, 2, 0])]            # add high garbage
    dut = _run_dut(noisy)
    assert [int(w) & 1 for w in dut.outputs_q15] == nib_bits(nib)


# --- round trip: golden encoder -> DUT decoder (the convention pin) -----------

def test_round_trip_zero_errors_identity():
    """golden_encode (the pinned equations) -> DUT decode == identity for a
    random data-nibble stream with NO injected errors."""
    rng = random.Random(101)
    nibs = [rng.randrange(16) for _ in range(12)]
    stream = [b for n in nibs for b in bits_of(golden_encode(n))]
    dut = _run_dut(stream)
    want = [b for n in nibs for b in nib_bits(n)]
    errs, n = _bit_errors([int(w) & 1 for w in dut.outputs_q15], want)
    assert errs == 0, f"0-error round trip broken: {errs}/{n}"


def test_round_trip_one_error_per_codeword_identity():
    """golden_encode -> ONE random injected bit error per codeword -> DUT
    decode == identity (the whole point of the code)."""
    rng = random.Random(202)
    nibs = [rng.randrange(16) for _ in range(12)]
    stream = []
    for n in nibs:
        stream.extend(bits_of(golden_encode(n) ^ (1 << rng.randrange(7))))
    dut = _run_dut(stream)
    want = [b for n in nibs for b in nib_bits(n)]
    errs, ncmp = _bit_errors([int(w) & 1 for w in dut.outputs_q15], want)
    assert errs == 0, f"1-error round trip broken: {errs}/{ncmp}"


# --- KNOWN LIMIT (gated, not hidden): double-bit errors are uncorrectable -----

def test_double_bit_errors_uncorrectable_known_limit():
    """Hamming(7,4) has distance 3: it corrects 1 error and CANNOT correct 2.
    EVERY double-bit error pattern (all 21 position pairs on all 16 codewords)
    mis-decodes — the syndrome equals the XOR of two H columns, which is the
    column of a THIRD bit, so the decoder flips that bit and lands on a wrong
    codeword. This is the standard, documented limit of the code (Lin &
    Costello §3.3), asserted here so it can never be silently glossed over."""
    for nib in range(16):
        c = golden_encode(nib)
        for i, j in itertools.combinations(range(7), 2):
            assert golden_decode(c ^ (1 << i) ^ (1 << j)) != nib, \
                f"double error ({i},{j}) on nibble {nib:x} unexpectedly corrected"


def test_double_bit_errors_dut_matches_goldens_miscorrection():
    """The DUT reproduces the golden's DETERMINISTIC double-error miscorrection
    (all 21 pairs on two codewords, on-chip): same wrong nibble, bit-for-bit —
    the known limit is deterministic, not noise."""
    stream, want = [], []
    for nib in (0x5, 0xC):
        c = golden_encode(nib)
        for i, j in itertools.combinations(range(7), 2):
            r = c ^ (1 << i) ^ (1 << j)
            stream.extend(bits_of(r))
            want.extend(nib_bits(golden_decode(r)))
    dut = _run_dut(stream)
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    assert errs == 0, f"double-error miscorrection diverges from golden: {errs}/{n}"


# --- MANDATORY mutation gates (INV-4): each corrupted decoder MUST FAIL -------

def _single_error_suite():
    """The stimulus the mutants are judged on: every single-bit error position
    on every codeword (the decoder's whole job)."""
    stream, want = [], []
    for nib in range(16):
        c = golden_encode(nib)
        for pos in range(7):
            stream.extend(bits_of(c ^ (1 << pos)))
            want.extend(nib_bits(nib))
    return stream, want


def test_mutation_no_correction_passthrough_fails():
    """A decoder that IGNORES the syndrome (extracts d3..d0 verbatim — the
    no-correction passthrough mutant) must FAIL on the single-error suite."""
    stream, want = _single_error_suite()
    mut = []
    for g in range(len(stream) // 7):
        w = 0
        for b in stream[g * 7:(g + 1) * 7]:
            w = (w << 1) | (b & 1)
        mut.extend(nib_bits((w >> 3) & 0xF))               # no correction
    errs, n = _bit_errors(mut, want)
    assert errs > 0, "a no-correction passthrough decoder went undetected!"


def test_mutation_wrong_syndrome_lut_fails():
    """A decoder with a WRONG syndrome->flip LUT (the positional-Hamming LUT
    flip[s] = 1 << (s-1), correct only if the code used position-value columns)
    must FAIL — it differs from the pinned convention exactly at the d0/p2
    column swap (s=3 and s=4)."""
    wrong_flip = (0, 1, 2, 4, 8, 16, 32, 64)
    assert wrong_flip != _FLIP
    stream, want = _single_error_suite()
    mut = []
    for g in range(len(stream) // 7):
        w = 0
        for b in stream[g * 7:(g + 1) * 7]:
            w = (w << 1) | (b & 1)
        s = 0
        for j in range(7):
            if (w >> (6 - j)) & 1:
                s ^= _COLS[j]
        mut.extend(nib_bits(((w ^ wrong_flip[s]) >> 3) & 0xF))
    errs, n = _bit_errors(mut, want)
    assert errs > 0, "a wrong-syndrome-LUT decoder went undetected!"


def test_mutation_swapped_bit_order_fails():
    """A decoder that reads the codeword LSB-first (swapped wire bit order —
    parity bits first) must FAIL the gate."""
    stream, want = _single_error_suite()
    mut = []
    for g in range(len(stream) // 7):
        w = 0
        for b in reversed(stream[g * 7:(g + 1) * 7]):      # swapped order
            w = (w << 1) | (b & 1)
        mut.extend(nib_bits(golden_decode(w)))
    errs, n = _bit_errors(mut, want)
    assert errs > 0, "a swapped-bit-order decoder went undetected!"


def test_mutation_one_bit_shift_fails():
    """A +1-bit shift of the decoded stream must FAIL (no free lag alignment,
    INV-2)."""
    rng = random.Random(31)
    nibs = [rng.randrange(16) for _ in range(10)]
    stream = [b for n in nibs for b in bits_of(golden_encode(n))]
    want = [b for n in nibs for b in nib_bits(n)]
    good = golden_stream(stream)
    shifted = [0] + good[:-1]
    errs, n = _bit_errors(shifted, want)
    assert errs > 0, "a one-bit output shift went undetected!"


def test_mutation_empty_output_fails():
    """An empty output cannot be certified against a non-empty reference."""
    stream, want = _single_error_suite()
    errs, n = _bit_errors([0], want)                       # length gap counted
    assert errs > 0, "an (near-)empty output went undetected!"


def test_mutation_misframed_stream_fails_on_chip():
    """SUBSTRATE-level framing sensitivity: dropping the stream's FIRST bit
    (mis-framing every subsequent group) must diverge from the aligned golden."""
    rng = random.Random(77)
    nibs = [rng.randrange(16) for _ in range(8)]
    stream = [b for n in nibs for b in bits_of(golden_encode(n))]
    dut = _run_dut(stream[1:])                             # dropped first bit
    want = [b for n in nibs for b in nib_bits(n)]
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    assert errs > 0, "a misframed (dropped-bit) stream went undetected!"


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Dashboard report: the exhaustive single-error suite, on-chip, bit-exact."""
    stream, want = _single_error_suite()
    dut = _run_dut(stream)
    got = [int(w) & 1 for w in dut.outputs_q15]
    errs, n = _bit_errors(got, want)
    res = CompareResult(passed=(errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("HammingDecoderBlock", res, coverage={
        "gr_equiv": "none (no stock GR Hamming(7,4); golden = standard syndrome "
                    "decoder, Hamming 1950 / Lin & Costello §3.3)",
        "convention": "c = d3 d2 d1 d0 p2 p1 p0 MSB-first; p2=d3^d2^d1, "
                      "p1=d3^d2^d0, p0=d3^d1^d0 (pinned, shared with "
                      "HammingEncoderBlock)",
        "edge": "all 16 clean codewords; 7:4 burst framing; trailing partial "
                "dropped; input LSB masked",
        "exhaustive": "112/112 single-bit errors corrected ON-CHIP; all 128 "
                      "7-bit words block-ref == golden",
        "random": 3,
        "round_trip": "golden-encoder -> DUT under 0 and 1 injected errors == identity",
        "known_limit": "double-bit errors uncorrectable (distance 3): all 16*21 "
                       "patterns mis-decode (gated), DUT == golden's "
                       "deterministic miscorrection on-chip",
        "mutation": "no-correction passthrough / wrong syndrome LUT / swapped "
                    "bit order / +1 shift / empty / misframed stream",
        "decision": "rate 7:4, raw 0/1 words, BIT-EXACT, delay 0, tol 0",
        "note": "2-cell pipeline: fused pack+syndrome accumulator (pre-shifted "
                "H columns) -> 8-entry LUT correct + MSB-first 4-bit burst",
    })
