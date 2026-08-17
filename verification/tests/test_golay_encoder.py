# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify GolayEncoderBlock — extended binary Golay (24,12) systematic encoder.

NO GNU Radio counterpart (gr-fec has no Golay factory), so the golden reference
is the standard generator matrix G = [I12 | B] with the STANDARD B matrix
(F. J. MacWilliams & N. J. A. Sloane, "The Theory of Error-Correcting Codes",
North-Holland 1977, Ch. 2 §6, bordered reverse-circulant form; the same G in
Lin & Costello, "Error Control Coding"), implemented HERE, independently of the
block's own reference:

    THE CONVENTION PIN (shared verbatim with GolayDecoderBlock):
    codeword MSB-first on the wire = d11 .. d0 p11 .. p0; the 12 data bits
    arrive MSB-first (first bit = d11); with m = [d11 .. d0] the parity bits
    are p11..p0 = m . B (mod 2), B column 0 -> p11 (first parity on the wire).

Pure bit manipulation on raw 0/1 words (NOT Q15) — the comparison is BIT-EXACT
(metric DECISION, tolerance 0). Rate-EXPANDING 12:24, so the DUT is driven with
``run_block_dut_rate`` (drains the whole 24-bit burst per emitting trigger).

Coverage: golden structural self-checks (EXHAUSTIVE weight distribution
1/759/2576/759/1 at weights 0/8/12/16/24 — the decisive extended-Golay
fingerprint — plus sampled pairwise min distance 8 and the all-zero/all-one
anchors), a sampled 12-word codeword sweep + random (>=3 seeds) on-chip, the
input-LSB-mask edge (stray high bits), trailing-partial drop, round-trip
through an INDEPENDENT brute-force nearest-codeword golden decoder (clean and
with 1, 2, and 3 injected bit errors per codeword), and the mandatory INV-4
mutation gates (wrong B row, parity-first layout, LSB-first data order,
dropped parity bit, +1 shift, empty) that MUST FAIL.

Run::

    cd <repo>
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_golay_encoder.py -v
"""
from __future__ import annotations

import functools
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_rate, write_report, CompareResult, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


# --- golden reference: the standard extended Golay G = [I12 | B] --------------
# Written out INDEPENDENTLY of the block's process_reference (the test must not
# certify the block against itself). MacWilliams & Sloane 1977 Ch.2 §6:
# B[0][0]=0, first row/column otherwise all-ones, 11x11 core = the reverse
# circulant c[(i+j-2) mod 11] over the indicator of {0} u QR(11) = {0,1,3,4,5,9}.
# B is SYMMETRIC. Row i is the parity contribution of d(11-i); column j -> p(11-j).
_B = [
    [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0],
    [1, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1],
    [1, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0],
    [1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1],
    [1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 1],
    [1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1],
    [1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0],
    [1, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0, 0],
    [1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0],
    [1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0, 1],
]


def _word_bits(w: int) -> list[int]:
    """12 data bits MSB-first (bit 11 = d11 = first on the wire)."""
    return [(w >> (11 - i)) & 1 for i in range(12)]


def _golden_encode_word(w: int) -> list[int]:
    """[d11..d0 p11..p0] MSB-first via the G = [I12 | B] matrix product."""
    m = _word_bits(w)
    return m + [sum(m[i] * _B[i][j] for i in range(12)) % 2 for j in range(12)]


def _golden_encode_bits(bits: list[int]) -> list[int]:
    """Bit stream (first bit of each group = d11) -> codeword bit stream.
    A trailing partial group of < 12 bits is not emitted."""
    out: list[int] = []
    for j in range(len(bits) // 12):
        grp = [b & 1 for b in bits[12 * j: 12 * j + 12]]
        w = 0
        for b in grp:
            w = (w << 1) | b
        out.extend(_golden_encode_word(w))
    return out


@functools.lru_cache(maxsize=1)
def _codebook() -> list[tuple[int, ...]]:
    return [tuple(_golden_encode_word(w)) for w in range(4096)]


def _golden_decode_codeword(c: list[int]) -> int | None:
    """Independent golden DECODER (the round-trip pin): brute-force
    nearest-codeword over the full 4096-word codebook — unambiguous, no
    algorithmic subtlety. Corrects any error pattern of weight <= 3 (min
    distance 8). Returns the 12-bit data word, or None if no codeword lies
    within distance 3 (weight-4+ patterns are not uniquely decodable)."""
    best_w, best_d = None, 4
    for w, cw in enumerate(_codebook()):
        d = sum(x != y for x, y in zip(cw, c))
        if d < best_d:
            best_w, best_d = w, d
    return best_w


# --- DUT drive ----------------------------------------------------------------

def _run(bit_words: list[int]):
    """Drive raw input words through the on-chip DUT; return the flat output
    bit list + the per-trigger burst lengths."""
    inq = [int(w) & 0xFFFF for w in bit_words]
    dut = run_block_dut_rate("GolayEncoderBlock", inq, params={},
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out")
    assert dut.ok, dut.reason
    out = [int(w) & 0xFFFF for w in dut.outputs_q15]
    return out, [len(t) for t in dut.per_trigger], dut


@functools.lru_cache(maxsize=8)
def _run_cached(bit_words: tuple[int, ...]):
    out, per_trigger, _ = _run(list(bit_words))
    return out, per_trigger


def _errs(got: list[int], want: list[int]) -> int:
    """Bit errors over the FULL golden length (a short stream is an error)."""
    assert len(want) > 0
    return sum(1 for i in range(len(want))
               if i >= len(got) or got[i] != want[i])


# --- golden structural self-checks --------------------------------------------

def test_golden_weight_distribution_exhaustive():
    """All 4096 codewords have the extended-Golay weight distribution
    1/759/2576/759/1 at weights 0/8/12/16/24 — the DECISIVE fingerprint: no
    wrong B (wrong row, transposed core, shifted circulant) can reproduce it."""
    from collections import Counter
    wd = Counter(sum(cw) for cw in _codebook())
    assert dict(wd) == {0: 1, 8: 759, 12: 2576, 16: 759, 24: 1}, dict(wd)


def test_golden_min_distance_8_sampled_pairs():
    """Pairwise distance >= 8 on sampled codeword pairs (the code is linear,
    so this follows from the weight distribution — checked independently)."""
    rng = random.Random(2412)
    words = rng.sample(range(4096), 64)
    cws = [_golden_encode_word(w) for w in words]
    for a, b in itertools.combinations(range(len(words)), 2):
        d = sum(x != y for x, y in zip(cws[a], cws[b]))
        assert d >= 8, f"d({words[a]:03x},{words[b]:03x}) = {d} < 8"


def test_golden_anchors_and_structure():
    """All-zero -> all-zero; all-one -> all-one (every B column has odd
    weight); B is symmetric with B.B^T = I (self-dual code)."""
    assert _golden_encode_word(0x000) == [0] * 24
    assert _golden_encode_word(0xFFF) == [1] * 24
    for i in range(12):
        for j in range(12):
            assert _B[i][j] == _B[j][i], (i, j)
            dot = sum(_B[i][k] * _B[j][k] for k in range(12)) % 2
            assert dot == (1 if i == j else 0), (i, j)


def test_block_reference_matches_golden():
    """The block's own process_reference_q15 / encode_word == the independent
    G-matrix golden over sampled words + both anchors, and it drops a
    trailing partial group."""
    from gr_kyttar.placement.blocks.golay_encoder_block import (
        GolayEncoderBlock)
    blk = GolayEncoderBlock("ref")
    rng = random.Random(7)
    for w in [0x000, 0xFFF] + [rng.randrange(4096) for _ in range(64)]:
        assert GolayEncoderBlock.encode_word(w) == _golden_encode_word(w), w
        assert blk.process_reference_q15(_word_bits(w)) == \
            _golden_encode_word(w), w
    # partial trailing group (5 bits) is NOT emitted (the pack_k_bits floor)
    assert blk.process_reference_q15(_word_bits(0xB71) + [1, 0, 1, 1, 0]) == \
        _golden_encode_word(0xB71)


# --- correctness: on-chip DUT vs golden ---------------------------------------

_SWEEP_WORDS = [0x000, 0xFFF, 0x001, 0x800, 0xAAA, 0x555,
                0x2F2, 0x123, 0x7FF, 0x900, 0x0F0, 0xC3C]


def test_sampled_codeword_sweep():
    """A structured 12-word sweep (anchors, single bits, alternating and
    block patterns) through the on-chip DUT in one stream — bit-exact, 24
    bits per 12-bit group (rate 12:24, INV-20 burst)."""
    bits = [b for w in _SWEEP_WORDS for b in _word_bits(w)]
    got, per_trigger, _ = _run(bits)
    want = _golden_encode_bits(bits)
    # rate check: every 12th trigger bursts exactly 24 bits, others 0.
    assert per_trigger == ([0] * 11 + [24]) * len(_SWEEP_WORDS), per_trigger
    assert len(got) == 24 * len(_SWEEP_WORDS)
    e = _errs(got, want)
    print(f"\nsweep {len(_SWEEP_WORDS)} words: {e} bit errors / {len(want)}")
    assert e == 0, f"{e}/{len(want)} bit errors vs the G-matrix golden"


@pytest.mark.parametrize("seed", [1, 7, 42, 20260816])
def test_random_bit_exact(seed):
    rng = random.Random(seed)
    bits = [rng.randint(0, 1) for _ in range(48)]  # 4 groups
    got, _, _ = _run(bits)
    want = _golden_encode_bits(bits)
    e = _errs(got, want)
    print(f"\nrandom seed={seed}: {e} bit errors / {len(want)}")
    assert e == 0, f"seed {seed}: {e}/{len(want)} bit errors vs golden"


def test_input_lsb_mask_edge():
    """Stray high bits on the input words are ignored (only the LSB is data,
    the GR pack_k_bits convention) — the PackKBits leak lesson."""
    rng = random.Random(31)
    lsbs = [rng.randint(0, 1) for _ in range(24)]
    words = [b | (rng.randint(0, 0x7FFF) << 1) for b in lsbs]
    got, _, _ = _run(words)
    want = _golden_encode_bits(lsbs)
    e = _errs(got, want)
    assert e == 0, f"stray-high-bit inputs leaked into the codeword: {e} errors"


def test_partial_trailing_group_not_emitted():
    """A trailing partial group (< 12 bits) produces NO codeword."""
    bits = _word_bits(0x94D) + [1, 1, 0, 1, 0]
    got, per_trigger, _ = _run(bits)
    assert per_trigger == [0] * 11 + [24] + [0] * 5, per_trigger
    assert got == _golden_encode_word(0x94D)


def test_round_trip_via_golden_decoder():
    """DUT encoder -> golden nearest-codeword decoder == identity: clean AND
    with 1, 2, and 3 injected bit errors per codeword at rotating positions —
    the min-distance-8 guarantee the decoder side relies on (the convention
    pin, end to end)."""
    rng = random.Random(99)
    words = [rng.randrange(4096) for _ in range(8)]
    bits = [b for w in words for b in _word_bits(w)]
    got, _, _ = _run(bits)
    assert len(got) == 24 * len(words)
    for j, w in enumerate(words):
        cw = got[24 * j: 24 * j + 24]
        assert _golden_decode_codeword(cw) == w, ("clean", j)
        for nerr in (1, 2, 3):
            bad = list(cw)
            for k in range(nerr):
                bad[(5 * j + 7 * k) % 24] ^= 1
            assert _golden_decode_codeword(bad) == w, (nerr, j)


# --- MANDATORY mutation gates (INV-4) -----------------------------------------
# Each corrupts the DUT stream (or its stimulus) the way a specific encoder bug
# would, and asserts the gate FAILS — a gate never shown to fail certifies
# nothing.

_MUT_WORDS = (0x2F2, 0x94D, 0xB71, 0x5A5)  # 0x5A5: d10=1, d9=0 — makes the
# wrong-B-row mutation (row 1 <- row 2) observable (a word with d10=0 never
# exercises row 1).
_MUT_BITS = tuple(b for w in _MUT_WORDS for b in _word_bits(w))


def _mut_run():
    got, _ = _run_cached(_MUT_BITS)
    return list(got), _golden_encode_bits(list(_MUT_BITS))


def test_mutation_wrong_b_row_fails():
    """An encoder whose B has row 1 replaced by row 2 (a single wrong row)
    must disagree with the golden."""
    # sensitivity precondition: at least one word exercises row 1 differently
    # from row 2 (d10 xor d9 set), else the mutation is a no-op by algebra.
    assert any(((w >> 10) ^ (w >> 9)) & 1 for w in _MUT_WORDS)
    got, want = _mut_run()
    bad_b = [list(r) for r in _B]
    bad_b[1] = list(_B[2])
    corrupt = []
    for j in range(len(got) // 24):
        m = got[24 * j: 24 * j + 12]     # DUT data half (bit-exact)
        par = [sum(m[i] * bad_b[i][c] for i in range(12)) % 2
               for c in range(12)]
        corrupt.extend(m + par)
    assert _errs(corrupt, want) > 0, "a wrong B row went undetected!"


def test_mutation_parity_first_layout_fails():
    """A swapped codeword layout (p11..p0 d11..d0 — parity first) must
    disagree with the pinned data-first layout."""
    got, want = _mut_run()
    corrupt = []
    for j in range(len(got) // 24):
        grp = got[24 * j: 24 * j + 24]
        corrupt.extend(grp[12:] + grp[:12])
    assert _errs(corrupt, want) > 0, "a parity-first layout went undetected!"


def test_mutation_lsb_first_data_fails():
    """An encoder reading the data word LSB-first (d0 arrives first) must
    disagree — the arrival order (d11 first) is part of the pin."""
    reversed_bits = []
    for j in range(len(_MUT_BITS) // 12):
        reversed_bits.extend(reversed(_MUT_BITS[12 * j: 12 * j + 12]))
    got, _ = _run_cached(tuple(reversed_bits))  # DUT fed LSB-first data
    want = _golden_encode_bits(list(_MUT_BITS))  # golden fed the pinned order
    assert _errs(list(got), want) > 0, "an LSB-first data order went undetected!"


def test_mutation_dropped_parity_bit_fails():
    """A 23-bit emit (p0 dropped) must fail — the output COUNT is
    load-bearing."""
    got, want = _mut_run()
    corrupt = [b for j in range(len(got) // 24)
               for b in got[24 * j: 24 * j + 23]]
    assert _errs(corrupt, want) > 0, "a dropped parity bit went undetected!"


def test_mutation_plus_one_shift_fails():
    """A +1-bit shift of the codeword stream must FAIL (no free lag
    alignment, INV-2: delay 0 asserted)."""
    got, want = _mut_run()
    shifted = [0] + got[:-1]
    assert _errs(shifted, want) > 0, "a +1 shift went undetected!"


def test_mutation_empty_output_fails():
    """An empty DUT output cannot certify against a non-empty golden."""
    want = _golden_encode_bits(list(_MUT_BITS))
    assert len(want) > 0
    assert _errs([], want) == len(want) > 0


# --- report -------------------------------------------------------------------

def test_emit_report():
    bits = [b for w in _SWEEP_WORDS for b in _word_bits(w)]
    got, _, _ = _run(bits)
    want = _golden_encode_bits(bits)
    e = _errs(got, want)
    res = CompareResult(passed=(e == 0), metric=Metric.DECISION,
                        n_compared=len(want), bit_errors=e, delay_used=0)
    assert res.passed, res.summary()
    write_report("GolayEncoderBlock", res, coverage={
        "gr_equiv": "(none — extended Golay (24,12), MacWilliams & Sloane "
                    "G = [I12 | B])",
        "edge": True,  # anchors + single-bit + alternating + stray-high-bit
        "sweep_words": len(_SWEEP_WORDS), "random": 4, "mutation": True,
        "round_trip": "brute-force nearest-codeword golden decoder, clean "
                      "+ 1/2/3-bit errors",
        "decision": "c = [d11..d0 p11..p0] MSB-first; p = m.B, B = the "
                    "MacWilliams-Sloane bordered reverse circulant "
                    "(symmetric, B.B^T = I); weight dist 1/759/2576/759/1",
        "note": "4-cell rate-expanding 12:24 (pack12 -> par7 -> par5 -> "
                "burst24, LOAD-table parity); bit-exact vs the G-matrix "
                "golden, delay 0",
    })
