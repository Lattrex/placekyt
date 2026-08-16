# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify HammingEncoderBlock — systematic Hamming(7,4) FEC encoder.

NO GNU Radio counterpart (gr-fec has no plain Hamming(7,4) factory), so the
golden reference is the standard systematic generator matrix G = [I4 | P]
(R. W. Hamming, "Error Detecting and Error Correcting Codes", BSTJ 29(2), 1950;
systematic form per any coding-theory text, e.g. Lin & Costello, "Error Control
Coding"), implemented HERE, independently of the block's own reference:

    THE CONVENTION PIN (shared verbatim with HammingDecoderBlock):
    codeword MSB-first on the wire = d3 d2 d1 d0 p2 p1 p0, data nibble arrives
    MSB-first (d3 first), parity p2 = d3^d2^d1, p1 = d3^d2^d0, p0 = d3^d1^d0
    (even parity).

Pure bit manipulation on raw 0/1 words (NOT Q15) — the comparison is BIT-EXACT
(metric DECISION, tolerance 0). Rate-EXPANDING 4:7, so the DUT is driven with
``run_block_dut_rate`` (drains the whole 7-bit burst per emitting trigger).

Coverage: all 16 nibbles exhaustively (on-chip), random (>=3 seeds), the
input-LSB-mask edge (stray high bits, the PackKBits lesson), round-trip through
an INDEPENDENT golden syndrome decoder (clean + every single-bit error position),
golden self-checks (min distance 3), and the mandatory INV-4 mutation gates
(wrong parity equation, swapped/parity-first bit order, LSB-first data order,
dropped parity bit, +1 shift, empty) that MUST FAIL.

Run::

    cd <repo>
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_hamming_encoder.py -v
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut_rate, write_report, CompareResult, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


# --- golden reference: the standard systematic Hamming(7,4) G matrix ----------
# Written out INDEPENDENTLY of the block's process_reference (the test must not
# certify the block against itself). c = m . G (mod 2), m = [d3 d2 d1 d0]:
#
#           d3 d2 d1 d0 p2 p1 p0
#     G = [  1  0  0  0  1  1  1 ]
#         [  0  1  0  0  1  1  0 ]
#         [  0  0  1  0  1  0  1 ]
#         [  0  0  0  1  0  1  1 ]
_G = [
    [1, 0, 0, 0, 1, 1, 1],
    [0, 1, 0, 0, 1, 1, 0],
    [0, 0, 1, 0, 1, 0, 1],
    [0, 0, 0, 1, 0, 1, 1],
]


def _golden_encode_nibble(nib: int) -> list[int]:
    """[d3 d2 d1 d0 p2 p1 p0] MSB-first via the G matrix product."""
    m = [(nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1]
    return [sum(m[r] * _G[r][c] for r in range(4)) % 2 for c in range(7)]


def _golden_encode_bits(bits: list[int]) -> list[int]:
    """Bit stream (first bit of each group = d3) -> codeword bit stream.
    A trailing partial group of < 4 bits is not emitted."""
    out: list[int] = []
    for j in range(len(bits) // 4):
        d3, d2, d1, d0 = (b & 1 for b in bits[4 * j: 4 * j + 4])
        out.extend(_golden_encode_nibble((d3 << 3) | (d2 << 2) | (d1 << 1) | d0))
    return out


def _golden_decode_codeword(c: list[int]) -> int:
    """Independent golden syndrome DECODER (the round-trip pin): corrects any
    single-bit error, returns the data nibble. c = [c6..c0] MSB-first."""
    c = list(c)
    s2 = c[0] ^ c[1] ^ c[2] ^ c[4]          # d3^d2^d1 ^ p2
    s1 = c[0] ^ c[1] ^ c[3] ^ c[5]          # d3^d2^d0 ^ p1
    s0 = c[0] ^ c[2] ^ c[3] ^ c[6]          # d3^d1^d0 ^ p0
    syn = (s2 << 2) | (s1 << 1) | s0
    # syndrome -> flipped bit index (0 = MSB d3): each column of H is unique.
    flip = {0b111: 0, 0b110: 1, 0b101: 2, 0b011: 3,
            0b100: 4, 0b010: 5, 0b001: 6}
    if syn:
        c[flip[syn]] ^= 1
    return (c[0] << 3) | (c[1] << 2) | (c[2] << 1) | c[3]


# --- DUT drive ----------------------------------------------------------------

def _run(bit_words: list[int]):
    """Drive raw input words through the on-chip DUT; return the flat output
    bit list + the per-trigger burst lengths."""
    inq = [int(w) & 0xFFFF for w in bit_words]
    dut = run_block_dut_rate("HammingEncoderBlock", inq, params={},
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out")
    assert dut.ok, dut.reason
    out = [int(w) & 0xFFFF for w in dut.outputs_q15]
    return out, [len(t) for t in dut.per_trigger], dut


def _nibble_bits(nib: int) -> list[int]:
    return [(nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1]


def _errs(got: list[int], want: list[int]) -> int:
    """Bit errors over the FULL golden length (a short stream is an error)."""
    assert len(want) > 0
    return sum(1 for i in range(len(want))
               if i >= len(got) or got[i] != want[i])


# --- golden self-checks -------------------------------------------------------

def test_golden_min_distance_3():
    """The 16 golden codewords have pairwise Hamming distance >= 3 — the
    defining Hamming(7,4) property; a wrong G could not pass this."""
    cws = [_golden_encode_nibble(n) for n in range(16)]
    for a, b in itertools.combinations(range(16), 2):
        d = sum(x != y for x, y in zip(cws[a], cws[b]))
        assert d >= 3, f"d({a:04b},{b:04b}) = {d} < 3 — not a Hamming code"


def test_golden_decoder_inverts_encoder():
    """Golden decoder o golden encoder == identity, clean AND under every
    single-bit error (7*16 = 112 cases) — proves the decoder used for the
    round-trip gate honors the same convention pin."""
    for nib in range(16):
        cw = _golden_encode_nibble(nib)
        assert _golden_decode_codeword(cw) == nib
        for pos in range(7):
            bad = list(cw)
            bad[pos] ^= 1
            assert _golden_decode_codeword(bad) == nib, (nib, pos)


def test_block_reference_matches_golden():
    """The block's own process_reference_q15 == the independent G-matrix golden
    for all 16 nibbles, and it drops a trailing partial group."""
    from gr_kyttar.placement.blocks.hamming_encoder_block import (
        HammingEncoderBlock)
    blk = HammingEncoderBlock("ref")
    for nib in range(16):
        bits = _nibble_bits(nib)
        assert blk.process_reference_q15(bits) == _golden_encode_nibble(nib)
        assert HammingEncoderBlock.encode_nibble(nib) == \
            _golden_encode_nibble(nib)
    # partial trailing group (3 bits) is NOT emitted (the pack_k_bits floor)
    assert blk.process_reference_q15(_nibble_bits(0xB) + [1, 0, 1]) == \
        _golden_encode_nibble(0xB)


# --- correctness: on-chip DUT vs golden ---------------------------------------

def test_all_16_codewords_exhaustive():
    """Every data nibble 0..15 through the on-chip DUT in one stream — all 16
    codewords bit-exact, 7 bits per 4-bit group (rate 4:7, INV-20 burst)."""
    bits = [b for nib in range(16) for b in _nibble_bits(nib)]
    got, per_trigger, _ = _run(bits)
    want = _golden_encode_bits(bits)
    # rate check: every 4th trigger bursts exactly 7 bits, others 0.
    assert per_trigger == ([0, 0, 0, 7] * 16), per_trigger
    assert len(got) == 7 * 16
    e = _errs(got, want)
    print(f"\nexhaustive 16 nibbles: {e} bit errors / {len(want)}")
    assert e == 0, f"{e}/{len(want)} bit errors vs the G-matrix golden"


@pytest.mark.parametrize("seed", [1, 7, 42, 20260816])
def test_random_bit_exact(seed):
    rng = random.Random(seed)
    bits = [rng.randint(0, 1) for _ in range(48)]  # 12 nibbles
    got, _, _ = _run(bits)
    want = _golden_encode_bits(bits)
    e = _errs(got, want)
    print(f"\nrandom seed={seed}: {e} bit errors / {len(want)}")
    assert e == 0, f"seed {seed}: {e}/{len(want)} bit errors vs golden"


def test_input_lsb_mask_edge():
    """Stray high bits on the input words are ignored (only the LSB is data,
    the GR pack_k_bits convention) — the PackKBitsBlock leak lesson: a masked
    value must actually be STORED, not just computed."""
    words = [0x0003, 0x00FE, 0x8001, 0x7FFF, 0x0002, 0x0055, 0x1234, 0xFFFF]
    got, _, _ = _run(words)
    want = _golden_encode_bits([w & 1 for w in words])
    e = _errs(got, want)
    assert e == 0, f"stray-high-bit inputs leaked into the codeword: {e} errors"


def test_partial_trailing_group_not_emitted():
    """A trailing partial group (< 4 bits) produces NO codeword."""
    bits = _nibble_bits(0x9) + [1, 1]
    got, per_trigger, _ = _run(bits)
    assert per_trigger == [0, 0, 0, 7, 0, 0], per_trigger
    assert got == _golden_encode_nibble(0x9)


def test_round_trip_via_golden_decoder():
    """DUT encoder -> golden syndrome decoder == identity: clean, AND with one
    injected bit error per codeword at a rotating position (the min-distance-3
    guarantee the decoder side relies on — the convention pin, end to end)."""
    rng = random.Random(99)
    nibs = [rng.randint(0, 15) for _ in range(16)]
    bits = [b for nib in nibs for b in _nibble_bits(nib)]
    got, _, _ = _run(bits)
    assert len(got) == 7 * len(nibs)
    # clean round trip
    for j, nib in enumerate(nibs):
        assert _golden_decode_codeword(got[7 * j: 7 * j + 7]) == nib, j
    # single-bit-error round trip (error position rotates over all 7)
    for j, nib in enumerate(nibs):
        cw = got[7 * j: 7 * j + 7]
        cw[j % 7] ^= 1
        assert _golden_decode_codeword(cw) == nib, (j, j % 7)


# --- MANDATORY mutation gates (INV-4) -----------------------------------------
# Each corrupts the DUT stream (or the golden) the way a specific encoder bug
# would, and asserts the gate FAILS — a gate never shown to fail certifies
# nothing.

_MUT_BITS = [b for nib in (0x1, 0x6, 0xB, 0xE, 0x9, 0x4) for b in
             _nibble_bits(nib)]


def test_mutation_wrong_parity_equation_fails():
    """An encoder computing p2 = d2^d1^d0 (the WRONG equation) must disagree."""
    got, _, _ = _run(_MUT_BITS)
    want = _golden_encode_bits(_MUT_BITS)
    corrupt = list(got)
    for j in range(len(corrupt) // 7):
        d3, d2, d1, d0 = corrupt[7 * j: 7 * j + 4]
        corrupt[7 * j + 4] = d2 ^ d1 ^ d0   # wrong p2
    assert _errs(corrupt, want) > 0, \
        "a wrong parity equation went undetected by the gate!"


def test_mutation_parity_first_layout_fails():
    """A swapped codeword layout (p2 p1 p0 d3 d2 d1 d0 — parity first) must
    disagree with the pinned d-first layout."""
    got, _, _ = _run(_MUT_BITS)
    want = _golden_encode_bits(_MUT_BITS)
    corrupt = []
    for j in range(len(got) // 7):
        grp = got[7 * j: 7 * j + 7]
        corrupt.extend(grp[4:] + grp[:4])
    assert _errs(corrupt, want) > 0, \
        "a parity-first codeword layout went undetected!"


def test_mutation_lsb_first_data_fails():
    """An encoder reading the nibble LSB-first (d0 arrives first) must
    disagree — the arrival order (d3 first) is part of the pin."""
    reversed_bits = []
    for j in range(len(_MUT_BITS) // 4):
        reversed_bits.extend(reversed(_MUT_BITS[4 * j: 4 * j + 4]))
    got, _, _ = _run(reversed_bits)      # DUT fed LSB-first data
    want = _golden_encode_bits(_MUT_BITS)  # golden fed the pinned order
    assert _errs(got, want) > 0, "an LSB-first data order went undetected!"


def test_mutation_dropped_parity_bit_fails():
    """A 6-bit emit (p0 dropped) must fail — the output COUNT is load-bearing."""
    got, _, _ = _run(_MUT_BITS)
    want = _golden_encode_bits(_MUT_BITS)
    corrupt = [b for j in range(len(got) // 7)
               for b in got[7 * j: 7 * j + 6]]
    assert _errs(corrupt, want) > 0, "a dropped parity bit went undetected!"


def test_mutation_plus_one_shift_fails():
    """A +1-bit shift of the codeword stream must FAIL (no free lag alignment,
    INV-2: delay 0 asserted)."""
    got, _, _ = _run(_MUT_BITS)
    want = _golden_encode_bits(_MUT_BITS)
    shifted = [0] + got[:-1]
    assert _errs(shifted, want) > 0, "a +1 shift went undetected!"


def test_mutation_empty_output_fails():
    """An empty DUT output cannot certify against a non-empty golden."""
    want = _golden_encode_bits(_MUT_BITS)
    assert len(want) > 0
    assert _errs([], want) == len(want) > 0


# --- report -------------------------------------------------------------------

def test_emit_report():
    bits = [b for nib in range(16) for b in _nibble_bits(nib)]
    got, _, _ = _run(bits)
    want = _golden_encode_bits(bits)
    e = _errs(got, want)
    res = CompareResult(passed=(e == 0), metric=Metric.DECISION,
                        n_compared=len(want), bit_errors=e, delay_used=0)
    assert res.passed, res.summary()
    write_report("HammingEncoderBlock", res, coverage={
        "gr_equiv": "(none — systematic Hamming(7,4), Hamming 1950 G matrix)",
        "edge": True,  # all 16 nibbles exhaustively + stray-high-bit inputs
        "exhaustive_nibbles": 16, "random": 4, "mutation": True,
        "round_trip": "golden syndrome decoder, clean + 1-bit errors",
        "decision": "c = [d3 d2 d1 d0 p2 p1 p0] MSB-first; p2=d3^d2^d1, "
                    "p1=d3^d2^d0, p0=d3^d1^d0 (even parity)",
        "note": "2-cell rate-expanding 4:7 (pack4+p2 -> p1+p0+burst7); "
                "bit-exact vs the G-matrix golden, delay 0",
    })
