# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate the Poly1305 GOLDEN and the block's arithmetic reference (RFC 8439 §2.5).

GNU Radio has no Poly1305 block, so — exactly as for ChaCha20 and LZ4 — the
golden is the published algorithm, and it is pinned by the RFC's OWN test
vectors BEFORE it is allowed to gate anything.

Scope, stated plainly (AGENTS.md §"do not claim success you have not
demonstrated"): **this file does NOT gate a built chip.** ``Poly1305MACBlock``
is not finished — its multiply ring is proven bit-exact on real silicon, but the
message packing, the carry-normalise phase, the final reduction and the tag
egress are not built yet, and the block is NOT in the catalog. What is gated
here is (a) the golden, against the RFC, and (b) the block's
``process_reference``, which models the exact limb schedule the cells run,
against that golden. The on-chip gate lands with the finished block.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_poly1305_golden.py -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for _p in (str(_RUNTIME), str(Path(__file__).parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from poly1305_golden import (  # noqa: E402
    LIMB_BITS, LIMB_MASK, N_LIMBS, P1305, REDUCTION_CONSTANT, R_CLAMP,
    RFC8439_2_5_2_KEY, RFC8439_2_5_2_MSG, RFC8439_2_5_2_R, RFC8439_2_5_2_S,
    RFC8439_2_5_2_TAG, RFC8439_A3_VECTORS, block_value, carry_normalise,
    clamp_r, from_limbs, limb_mul_mod_p, poly1305_accumulate, poly1305_mac,
    poly1305_mac_limbs, split_key, to_limbs)
from gr_kyttar.placement.blocks.poly1305_mac_block import (  # noqa: E402
    Poly1305MACBlock)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _words(msg: bytes) -> list[int]:
    """A byte message as the little-endian 16-bit word stream the block eats."""
    assert len(msg) % 2 == 0, "the word interface takes whole 16-bit words"
    return [int.from_bytes(msg[2 * i:2 * i + 2], "little")
            for i in range(len(msg) // 2)]


def _tag(out) -> bytes:
    return b"".join(int(v).to_bytes(2, "little") for v in np.asarray(out))


def _block_for(key: bytes, msg_words: int = 17) -> Poly1305MACBlock:
    """A block for ``key``. ``msg_words`` must match the driven message: the
    block consumes EXACTLY that many words (one-time-MAC semantics)."""
    return Poly1305MACBlock("p", r_key=key[:16].hex(), s_key=key[16:].hex(),
                            msg_words=msg_words)


def _even_a3():
    """The §A.3 vectors whose message is a whole number of 16-bit words.

    Three of the nine have ODD byte lengths and simply are not expressible at
    a 16-bit word interface — a real limitation of the block's input contract,
    recorded rather than hidden. They are still gated against the golden.
    """
    return [v for v in RFC8439_A3_VECTORS if len(v[2]) % 2 == 0]


# ==========================================================================
# The golden is REAL: the RFC's own vectors, before anything else uses it
# ==========================================================================

def test_golden_matches_rfc8439_section_2_5_2():
    """RFC 8439 §2.5.2 — the worked example, exact."""
    assert poly1305_mac(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY) \
        == RFC8439_2_5_2_TAG


def test_golden_reproduces_the_rfc_published_intermediates():
    """§2.5.2 prints ``r`` (clamped) and ``s`` — gate the STATE, not only the
    tag. A tag can be right for the wrong reasons; these cannot."""
    r, s = split_key(RFC8439_2_5_2_KEY)
    assert r == RFC8439_2_5_2_R
    assert s == RFC8439_2_5_2_S


@pytest.mark.parametrize("name,key,msg,exp", RFC8439_A3_VECTORS,
                         ids=[v[0] for v in RFC8439_A3_VECTORS])
def test_golden_matches_rfc8439_appendix_a3(name, key, msg, exp):
    """All NINE §A.3 edge cases: zero r, zero s, the 2**130-5 wrap, an
    all-ones block, both carry-propagation directions, and the largest
    reduced value."""
    assert poly1305_mac(msg, key) == exp


@pytest.mark.parametrize("name,key,msg,exp", RFC8439_A3_VECTORS,
                         ids=[v[0] for v in RFC8439_A3_VECTORS])
def test_limb_golden_matches_rfc8439_appendix_a3(name, key, msg, exp):
    """The LIMB model — the representation the chip computes in — is held to
    the same vectors INDEPENDENTLY, so a shared assumption cannot pass both."""
    assert poly1305_mac_limbs(msg, key) == exp


def test_limb_golden_matches_rfc8439_section_2_5_2():
    assert poly1305_mac_limbs(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY) \
        == RFC8439_2_5_2_TAG


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_the_two_golden_implementations_agree(seed):
    """Big-integer vs 13-limb radix-2**10, over random messages and keys."""
    rng = random.Random(seed)
    for _ in range(60):
        key = bytes(rng.randrange(256) for _ in range(32))
        msg = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
        assert poly1305_mac(msg, key) == poly1305_mac_limbs(msg, key)


# ==========================================================================
# The limb DECOMPOSITION is the one the ISA forces
# ==========================================================================

def test_the_radix_divides_130_exactly():
    """The ``2**130 ≡ 5`` fold is limb-aligned ONLY if the radix divides 130,
    which is what lets an overflow limb fold back with a multiply and no
    division. 10 * 13 == 130."""
    assert LIMB_BITS * N_LIMBS == 130
    assert 130 % LIMB_BITS == 0


def test_every_multiplicand_stays_under_2_to_the_15():
    """MEASURED on chip: ``MUL``/``MULHI`` are SIGNED, so the product is only
    the exact unsigned one when BOTH operands are in ``[0, 0x7FFF]``.

    This is the constraint that picked radix 2**10 over the textbook 2**26,
    and it is asserted here so a future change of radix cannot silently break
    the multiplier. Both a limb and a FOLDED coefficient must fit.
    """
    assert LIMB_MASK == 1023
    assert LIMB_MASK < 0x8000
    assert REDUCTION_CONSTANT * LIMB_MASK == 5115
    assert REDUCTION_CONSTANT * LIMB_MASK < 0x8000


def test_the_textbook_radices_would_violate_that_bound():
    """The plan's five radix-2**26 limbs, and the next candidate down, are not
    implementable on a signed multiplier — pinned so the reasoning survives."""
    # radix 2**26: a limb alone is far outside 15 bits.
    assert ((1 << 26) - 1) > 0x7FFF
    # radix 2**13: limbs fit, but the folded coefficient does not.
    assert ((1 << 13) - 1) < 0x8000
    assert REDUCTION_CONSTANT * ((1 << 13) - 1) == 0x9FFB
    assert REDUCTION_CONSTANT * ((1 << 13) - 1) > 0x7FFF


def test_the_accumulator_never_leaves_32_bits():
    """13 terms of (limb x folded coefficient) must fit the hi/lo pair."""
    worst = N_LIMBS * LIMB_MASK * (REDUCTION_CONSTANT * LIMB_MASK)
    assert worst == 68024385
    assert worst.bit_length() == 27
    assert worst < (1 << 32)


# --------------------------------------------------------------------------
# The CARRY-NORMALISE decomposition the ring runs (MEASURED bounds)
# --------------------------------------------------------------------------

MAX_ACC32 = N_LIMBS * LIMB_MASK * (REDUCTION_CONSTANT * LIMB_MASK)


def test_one_step_cannot_yield_a_10bit_limb_AND_a_one_word_carry():
    """Why the normalise is TWO stages per round, not one.

    The obvious split sends ``hi*64 + (lo >> 10)`` on one wire. For the true
    maximum accumulator that is 17 bits and does not fit — measured on chip
    via the all-maximum case, which a random sample never reaches.
    """
    assert (MAX_ACC32 >> LIMB_BITS) == 66430
    assert (MAX_ACC32 >> LIMB_BITS) > 0xFFFF          # the trap
    assert (MAX_ACC32 >> LIMB_BITS).bit_length() == 17


def _norm_round(acc):
    """The TWO-STAGE round the ring cells run.

    stage A: carry the HIGH word; the RECEIVER applies the ``*64``, so the
             wire never holds ``hi*64``.
    stage B: split the 16-bit residue at bit 10.
    Both stages ride the ring's rotation, and a carry crossing the closing
    edge takes the same ``*5`` the multiply uses (``2**130 == 5`` mod p).
    """
    def rot_add(res, cy, scale):
        return [res[k] + (REDUCTION_CONSTANT * cy[N_LIMBS - 1] if k == 0
                          else cy[k - 1]) * scale for k in range(N_LIMBS)]

    cy_a = [v >> 16 for v in acc]
    for c in cy_a:
        assert c <= 0xFFFF, ("stage A carry too wide", c)
    mid = rot_add([v & 0xFFFF for v in acc], cy_a, 64)
    cy_b = [v >> LIMB_BITS for v in mid]
    for c in cy_b:
        assert c <= 0xFFFF, ("stage B carry too wide", c)
    return rot_add([v & LIMB_MASK for v in mid], cy_b, 1)


def test_both_normalise_carries_fit_one_word_at_the_maximum():
    """The corner the on-chip case list caught: every carry must be a single
    16-bit word even when every accumulator is at its 27-bit peak."""
    acc = [MAX_ACC32] * N_LIMBS
    for _ in range(4):
        acc = _norm_round(acc)          # the asserts inside are the gate
    assert all(v <= 0xFFFF for v in acc)


def test_the_stage_a_carry_times_64_needs_MULHI():
    """``carry * 64`` is 17 bits, so ``MUL`` alone TRUNCATES it — the same
    trap as the 32-bit MAC, from the other side. Measured: 1037*64 = 66368
    became 832."""
    worst_hi = MAX_ACC32 >> 16
    assert worst_hi == 1037
    assert worst_hi * 64 == 66368
    assert (worst_hi * 64) > 0xFFFF                   # MUL alone loses bit 16
    assert (worst_hi * 64) & 0xFFFF == 832            # the wrong answer


def test_two_normalise_rounds_reproduce_every_rfc_vector():
    """The whole MAC, with the ring's two-stage normalise in place of the
    golden's carry_normalise, at the round count the block will use."""
    def poly_ring(msg, key, rounds):
        r, s = split_key(key)
        rl = to_limbs(r)
        a = [0] * N_LIMBS
        for i in range(0, len(msg), 16):
            n = to_limbs(block_value(msg[i:i + 16]))
            a = [x + y for x, y in zip(a, n)]
            for _ in range(rounds):
                a = _norm_round(a)
            a = limb_mul_mod_p(a, rl)
            for _ in range(rounds):
                a = _norm_round(a)
        return (((from_limbs(a) % P1305) + s)
                % (1 << 128)).to_bytes(16, "little")

    for rounds in (2, 3, 4):
        assert poly_ring(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY, rounds) \
            == RFC8439_2_5_2_TAG
        for _name, key, msg, exp in RFC8439_A3_VECTORS:
            assert poly_ring(msg, key, rounds) == exp


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_ring_normalise_matches_the_golden_on_random_messages(seed):
    def poly_ring(msg, key):
        r, s = split_key(key)
        rl = to_limbs(r)
        a = [0] * N_LIMBS
        for i in range(0, len(msg), 16):
            n = to_limbs(block_value(msg[i:i + 16]))
            a = [x + y for x, y in zip(a, n)]
            for _ in range(3):
                a = _norm_round(a)
            a = limb_mul_mod_p(a, rl)
            for _ in range(3):
                a = _norm_round(a)
        return (((from_limbs(a) % P1305) + s)
                % (1 << 128)).to_bytes(16, "little")

    rng = random.Random(seed)
    for _ in range(60):
        key = bytes(rng.randrange(256) for _ in range(32))
        msg = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
        assert poly_ring(msg, key) == poly1305_mac(msg, key)


def test_mutation_a_one_stage_normalise_overflows_the_carry_wire():
    """INV-4 for the two-stage split: the ONE-stage form must be provably
    unable to carry the maximum accumulator."""
    with pytest.raises(AssertionError):
        for v in [MAX_ACC32] * N_LIMBS:
            lo, hi = v & 0xFFFF, v >> 16
            c = hi * 64 + (lo >> LIMB_BITS)
            assert c <= 0xFFFF, "one-stage carry does not fit a word"


def test_limb_roundtrip():
    rng = random.Random(11)
    for _ in range(500):
        x = rng.randrange(1 << 130)
        assert from_limbs(to_limbs(x)) == x


def test_carry_normalise_folds_the_top_limb_by_five():
    """Anything carried past 2**130 re-enters at limb 0 multiplied by 5 —
    the whole reason the radix must divide 130."""
    acc = [0] * N_LIMBS
    acc[N_LIMBS - 1] = 1 << LIMB_BITS          # exactly 2**130
    out = carry_normalise(acc)
    assert all(v <= LIMB_MASK for v in out)
    assert from_limbs(out) == REDUCTION_CONSTANT
    assert (1 << 130) % P1305 == REDUCTION_CONSTANT


def test_limb_multiply_matches_bignum_modular_multiply():
    rng = random.Random(5)
    for _ in range(300):
        a = rng.randrange(1 << 130)
        r = rng.randrange(1 << 124) & R_CLAMP
        got = from_limbs(carry_normalise(
            limb_mul_mod_p(carry_normalise(to_limbs(a)), to_limbs(r)))) % P1305
        assert got == (a * r) % P1305


# ==========================================================================
# The CLAMP and the HIGH BIT — the two classic implementation bugs
# ==========================================================================

def test_clamp_clears_exactly_the_rfc_bits():
    """22 bits: the top 4 of each 32-bit word, plus the low 2 of the upper 3."""
    assert clamp_r(b"\xff" * 16) == R_CLAMP
    assert bin(R_CLAMP).count("1") == 128 - 22


def test_block_value_sets_the_bit_above_its_own_LENGTH():
    """A full block contributes 1 << 128; a short final block contributes a
    LOWER bit — that is what makes the padding injective."""
    assert block_value(b"\x00" * 16) == 1 << 128
    assert block_value(b"\x00" * 3) == 1 << 24
    assert block_value(b"\x01") == 1 + (1 << 8)


def test_a_short_block_is_not_the_same_as_a_zero_padded_full_block():
    """The high bit is what separates them. If it were dropped, these two
    messages would collide."""
    key = RFC8439_2_5_2_KEY
    assert poly1305_mac(b"\x01\x02", key) != poly1305_mac(
        b"\x01\x02" + b"\x00" * 14, key)


# ==========================================================================
# The BLOCK's reference runs the CELL schedule, and equals the golden
# ==========================================================================

def test_block_reference_matches_rfc8439_section_2_5_2():
    """The block's own reference — the systolic passes, the wrap-×5 and the
    carry sweeps — reproduces the RFC tag exactly."""
    blk = _block_for(RFC8439_2_5_2_KEY)
    out = blk.process_reference(np.array(_words(RFC8439_2_5_2_MSG),
                                         dtype=np.uint16))
    assert _tag(out) == RFC8439_2_5_2_TAG


@pytest.mark.parametrize("name,key,msg,exp", _even_a3(),
                         ids=[v[0] for v in _even_a3()])
def test_block_reference_matches_appendix_a3(name, key, msg, exp):
    blk = _block_for(key, msg_words=len(msg) // 2)
    out = blk.process_reference(np.array(_words(msg), dtype=np.uint16))
    assert _tag(out) == exp


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_block_reference_matches_golden_random(seed):
    rng = random.Random(seed)
    for _ in range(40):
        key = bytes(rng.randrange(256) for _ in range(32))
        msg = bytes(rng.randrange(256) for _ in range(2 * rng.randrange(1, 60)))
        blk = _block_for(key, msg_words=len(msg) // 2)
        out = blk.process_reference(np.array(_words(msg), dtype=np.uint16))
        assert _tag(out) == poly1305_mac(msg, key)


def test_the_block_clamps_r_even_when_the_caller_did_not():
    """The clamp is part of the algorithm, so an unclamped key still yields
    the RFC-correct tag."""
    blk = _block_for(RFC8439_2_5_2_KEY)
    assert blk._r == RFC8439_2_5_2_R
    assert blk._r == clamp_r(RFC8439_2_5_2_KEY[:16])


def test_odd_length_messages_are_a_known_interface_limit():
    """HONEST LIMIT, not a silent one: the block's input is a 16-bit word
    stream, so an odd-BYTE message cannot be expressed at that interface.
    Three §A.3 vectors are odd-length and are gated at the golden instead.

    LAYER: block interface — fixable (a byte-oriented input port, or a
    trailing-length side input), not a substrate wall.
    """
    odd = [v for v in RFC8439_A3_VECTORS if len(v[2]) % 2]
    assert len(odd) == 3
    for name, key, msg, exp in odd:
        assert poly1305_mac(msg, key) == exp        # still gated, via bytes


# ==========================================================================
# INV-4 — every gate above is PROVEN to fail on a corrupted implementation
# ==========================================================================

def _mac_no_clamp(msg, key):
    r = int.from_bytes(key[:16], "little")          # MUTANT: clamp skipped
    s = int.from_bytes(key[16:], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        acc = ((acc + block_value(msg[i:i + 16])) * r) % P1305
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _mac_no_high_bit(msg, key):
    r, s = split_key(key)
    acc = 0
    for i in range(0, len(msg), 16):
        blk = msg[i:i + 16]
        acc = ((acc + int.from_bytes(blk, "little")) * r) % P1305  # MUTANT
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _mac_wrong_reduction(msg, key, c):
    """MUTANT: fold the overflow by ``c`` instead of 5."""
    p = (1 << 130) - c
    r, s = split_key(key)
    acc = 0
    for i in range(0, len(msg), 16):
        acc = ((acc + block_value(msg[i:i + 16])) * r) % p
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _mac_no_s(msg, key):
    r, _ = split_key(key)
    return (poly1305_accumulate(msg, r) % (1 << 128)).to_bytes(16, "little")


def test_mutation_skipping_the_r_clamp_fails():
    """INV-4: the clamp is mandatory. Proven to change the tag."""
    assert _mac_no_clamp(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY) \
        != RFC8439_2_5_2_TAG


def test_mutation_dropping_the_block_high_bit_fails():
    """INV-4: without the padding bit the tag is wrong AND the padding stops
    being injective."""
    assert _mac_no_high_bit(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY) \
        != RFC8439_2_5_2_TAG
    key = RFC8439_2_5_2_KEY
    assert _mac_no_high_bit(b"\x01\x02", key) == _mac_no_high_bit(
        b"\x01\x02" + b"\x00" * 14, key)          # the collision it creates


@pytest.mark.parametrize("c", [1, 2, 3, 4, 6, 7, 10])
def test_mutation_wrong_reduction_constant_fails(c):
    """INV-4: ``2**130 ≡ 5`` and nothing else. Every neighbouring constant
    must break the RFC vector."""
    assert c != REDUCTION_CONSTANT
    assert _mac_wrong_reduction(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY, c) \
        != RFC8439_2_5_2_TAG


def test_mutation_omitting_the_final_add_s_fails():
    """INV-4: the tag is ``acc + s``, and ``s`` is what makes it a MAC rather
    than a public hash."""
    assert _mac_no_s(RFC8439_2_5_2_MSG, RFC8439_2_5_2_KEY) \
        != RFC8439_2_5_2_TAG


def test_mutation_wrong_reduction_constant_fails_in_the_limb_model_too():
    """The same mutation, applied where the CHIP would carry it: fold the top
    limb by 4 or 6 instead of 5."""
    rl = to_limbs(RFC8439_2_5_2_R)
    a = to_limbs(12345678901234567890123456789)
    good = limb_mul_mod_p(a, rl)
    for c in (4, 6):
        bad = [0] * N_LIMBS
        for i in range(N_LIMBS):
            for j in range(N_LIMBS):
                k = i + j
                if k >= N_LIMBS:
                    bad[k - N_LIMBS] += a[i] * (c * rl[j])
                else:
                    bad[k] += a[i] * rl[j]
        assert bad != good


def test_the_wrong_32bit_mac_order_keeps_the_low_word_bit_exact():
    """INV-4 for the MEASURED carry trap, and the reason it is dangerous.

    ``MULHI`` is an ALU op and sets all flags, so the six-instruction order
    ``MUL / ADD / MOVE / MULHI / ADC / MOVE`` runs its ``ADC`` against the
    flags ``MULHI`` just wrote, not the ones ``ADD`` produced. Measured on
    chip with ``a * b = 921 * 5115``, the accumulator carried a **constant**
    ``+0x10000`` error from the first accumulation onward — ``MULHI`` happened
    to leave C set on the first pass and clear thereafter, so the damage is a
    single spurious carry that never washes out:

        after 1 MAC:  0x0048E203   (correct 0x0047E203)
        after 6 MACs: 0x01B04C12   (correct 0x01AF4C12)

    and **the low word was bit-exact in every one of the six**. This pins the
    chip's own numbers: the mutant's low half is always right, its high half
    never is.
    """
    a, b = 921, 5115
    # The chip's own numbers, read back over six successive accumulations.
    MEASURED_BAD = (0x0048E203, 0x0090C406, 0x00D8A609,
                    0x0120880C, 0x01686A0F, 0x01B04C12)
    for n, bad in enumerate(MEASURED_BAD, start=1):
        good = (n * a * b) & 0xFFFFFFFF
        assert bad != good                              # the gate must fail
        assert bad - good == 0x10000                    # one lost carry
        assert (bad & 0xFFFF) == (good & 0xFFFF)        # LOW WORD SURVIVES


def test_mutation_empty_output_fails():
    blk = _block_for(RFC8439_2_5_2_KEY)
    out = blk.process_reference(np.array(_words(RFC8439_2_5_2_MSG),
                                         dtype=np.uint16))
    assert len(out) == Poly1305MACBlock.TAG_WORDS
    assert _tag(np.array([], dtype=np.uint16)) != RFC8439_2_5_2_TAG


def test_a_one_limb_gate_would_not_catch_a_wrong_limb():
    """The brief's "a value gate on ONE value is not a value gate", made
    executable — at the layer where it actually bites on THIS block.

    The TAG avalanches, so a corrupted key changes every tag byte. The
    accumulator LIMBS do not: a single wrong coefficient limb leaves several
    of the thirteen accumulators bit-identical. That is exactly the shape this
    build hit on chip twice (twelve of thirteen accumulators exact, once from
    the carry trap and once from folding the egress onto a MAC cell), so the
    on-chip gate must assert ALL THIRTEEN, never a sample.
    """
    rl = to_limbs(RFC8439_2_5_2_R)
    a = to_limbs(12345678901234567890123456789)
    good = limb_mul_mod_p(a, rl)
    rl[7] ^= 1                                  # one wrong coefficient limb
    bad = limb_mul_mod_p(a, rl)
    assert bad != good                          # the vector as a whole moves
    survivors = [k for k in range(N_LIMBS) if bad[k] == good[k]]
    assert survivors, "expected some limbs to survive a single-limb fault"
    assert len(survivors) < N_LIMBS             # ...but not all of them
