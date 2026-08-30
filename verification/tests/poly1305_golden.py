# SPDX-License-Identifier: GPL-3.0-or-later
"""GOLDEN model of the **Poly1305** one-time authenticator (RFC 8439 §2.5).

There is NO stock GNU Radio block for Poly1305, so — exactly as for
``chacha20_golden.py`` and ``lz4_golden.py`` — the golden is the published
algorithm, transcribed straight from the RFC and pinned by the RFC's OWN test
vectors before it is allowed to gate anything.

The algorithm
=============

Poly1305 evaluates a polynomial over the prime field ``GF(2**130 - 5)``. The
32-byte one-time key splits into two halves: ``r`` (the polynomial coefficient,
first 16 bytes) and ``s`` (the final additive blind, last 16 bytes). Both are
little-endian.

``r`` is **clamped** before use (RFC 8439 §2.5): the top four bits of each of
its four 32-bit words are cleared, and the bottom two bits of the upper three
words are cleared. That is the constant :data:`R_CLAMP`. Clamping is what
bounds ``r`` so the reference implementations' carry analyses hold — it is NOT
optional, and skipping it produces a plausible-looking but wrong tag.

The message is processed in 16-byte blocks. Each block is read as a
little-endian integer and gets **one extra bit set immediately above its own
length** (``1 << (8 * len(block))``) — for a full block that is the 129th bit;
for the final short block it is lower. That high bit is what makes the
padding injective, and dropping it is the classic implementation bug::

    acc = 0
    for each block:
        n   = le_int(block) + (1 << (8 * len(block)))
        acc = ((acc + n) * r) mod (2**130 - 5)
    tag = (acc + s) mod 2**128

The tag is the low 128 bits, emitted **little-endian**.

Two independent implementations live here on purpose
====================================================

* :func:`poly1305_mac` is the plain big-integer transcription — the shortest
  honest statement of the RFC.
* :func:`poly1305_mac_limbs` is the **limb** model: 13 limbs of radix ``2**10``,
  which is the representation the Kyttar block actually computes in. It exists
  so the block's arithmetic decomposition is gated against the RFC *directly*,
  rather than against a second copy of its own assumptions.

They are asserted equal over random messages/keys in the test suite; both are
pinned by the RFC vectors here.

Why radix 2**10 and not the 2**26 the textbooks use
---------------------------------------------------
Radix must divide 130 exactly for the ``2**130 ≡ 5`` fold to be limb-aligned,
which allows only 2, 5, 10, 13 and 26. **MEASURED on the real chip**, this
ISA's ``MUL``/``MULHI`` pair is *signed*, so an exact unsigned 16×16→32 product
requires both operands to lie in ``[0, 0x7FFF]``. That rules out radix 2**26
(limbs far exceed 15 bits) and radix 2**13 (``5*r[j]`` reaches ``0x9FFB``).
Radix 2**10 leaves ``5*r[j] <= 5115`` and every limb ``<= 1023`` — both inside
15 bits — and its accumulators peak at 27 bits, comfortably inside 32.
"""
from __future__ import annotations

#: The Poly1305 prime, ``2**130 - 5``.
P1305 = (1 << 130) - 5

#: RFC 8439 §2.5 clamp applied to the low 16 bytes of the key before use.
#: Clears the top 4 bits of each 32-bit word and the low 2 bits of the upper 3.
R_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF

#: ``2**130 mod (2**130 - 5)`` — the reduction constant. Folding an overflow
#: limb back into the bottom is a multiply by exactly this and nothing else.
REDUCTION_CONSTANT = 5

#: Limb radix used by the on-chip decomposition (see the module docstring).
LIMB_BITS = 10
#: Number of limbs: ``130 == 10 * 13`` exactly, so the fold is limb-aligned.
N_LIMBS = 13
#: Mask of one limb.
LIMB_MASK = (1 << LIMB_BITS) - 1


# --------------------------------------------------------------------------
# RFC 8439 test vectors
# --------------------------------------------------------------------------

#: RFC 8439 §2.5.2 — the worked example. ``(key, message, expected tag)``.
RFC8439_2_5_2_KEY = bytes.fromhex(
    "85d6be7857556d337f4452fe42d506a8"
    "0103808afb0db2fd4abff6af4149f51b")
RFC8439_2_5_2_MSG = b"Cryptographic Forum Research Group"
RFC8439_2_5_2_TAG = bytes.fromhex("a8061dc1305136c6c22b8baf0c0127a9")

#: RFC 8439 §2.5.2's own intermediate values, so the block's *state* — not just
#: its final word — can be gated. ``r`` and ``s`` after the clamp.
RFC8439_2_5_2_R = 0x806d5400e52447c036d555408bed685
RFC8439_2_5_2_S = 0x1bf54941aff6bf4afdb20dfb8a800301

#: RFC 8439 §A.3 edge-case vectors: ``(name, key, message, expected tag)``.
#: These are the cases that separate a correct implementation from one that
#: merely passes §2.5.2 — an all-zero ``r`` (the tag is just ``s``), an
#: all-ones block driving the carry chain, and the ``2**130-5`` wrap itself.
RFC8439_A3_VECTORS = (
    (
        # A.3 #1 — r and s both zero: the tag must be all zeros.
        "zero_r_zero_s",
        bytes(32),
        bytes(64),
        bytes(16),
    ),
    (
        # A.3 #2 — r is zero, so every message term vanishes and tag == s.
        "zero_r_tag_is_s",
        bytes.fromhex("00000000000000000000000000000000"
                      "36e5f6b5c5e06070f0efca96227a863e"),
        (b"Any submission to the IETF intended by the Contributor for "
         b"publication as all or part of an IETF Internet-Draft or RFC and "
         b"any statement made within the context of an IETF activity is "
         b"considered an \"IETF Contribution\". Such statements include "
         b"oral statements in IETF sessions, as well as written and "
         b"electronic communications made at any time or place, which are "
         b"addressed to"),
        bytes.fromhex("36e5f6b5c5e06070f0efca96227a863e"),
    ),
    (
        # A.3 #3 — s is zero; the tag is the raw polynomial value.
        "zero_s",
        bytes.fromhex("36e5f6b5c5e06070f0efca96227a863e"
                      "00000000000000000000000000000000"),
        (b"Any submission to the IETF intended by the Contributor for "
         b"publication as all or part of an IETF Internet-Draft or RFC and "
         b"any statement made within the context of an IETF activity is "
         b"considered an \"IETF Contribution\". Such statements include "
         b"oral statements in IETF sessions, as well as written and "
         b"electronic communications made at any time or place, which are "
         b"addressed to"),
        bytes.fromhex("f3477e7cd95417af89a6b8794c310cf0"),
    ),
    (
        # A.3 #4 — the "2^130-5" wrap: a block that pushes the accumulator
        # exactly across the modulus so the fold-by-5 must fire.
        "wrap_2_130_minus_5",
        bytes.fromhex("1c9240a5eb55d38af333888604f6b5f0"
                      "473917c1402b80099dca5cbc207075c0"),
        (b"'Twas brillig, and the slithy toves\n"
         b"Did gyre and gimble in the wabe:\n"
         b"All mimsy were the borogoves,\n"
         b"And the mome raths outgrabe."),
        bytes.fromhex("4541669a7eaaee61e708dc7cbcc5eb62"),
    ),
    (
        # A.3 #5 — the total must be reduced: a single all-ones block with
        # r = 2 exercises the top-limb fold on its own.
        "all_ones_block",
        bytes.fromhex("02000000000000000000000000000000"
                      "00000000000000000000000000000000"),
        bytes.fromhex("ffffffffffffffffffffffffffffffff"),
        bytes.fromhex("03000000000000000000000000000000"),
    ),
    (
        # A.3 #6 — the second-to-last block wraps and the addition of s
        # carries out of 128 bits (the result must be truncated, not widened).
        "s_addition_carries_out",
        bytes.fromhex("02000000000000000000000000000000"
                      "ffffffffffffffffffffffffffffffff"),
        bytes.fromhex("02000000000000000000000000000000"),
        bytes.fromhex("03000000000000000000000000000000"),
    ),
    (
        # A.3 #7 — limb-carry propagation all the way up.
        "carry_propagation_up",
        bytes.fromhex("01000000000000000000000000000000"
                      "00000000000000000000000000000000"),
        bytes.fromhex("ffffffffffffffffffffffffffffffff"
                      "f0ffffffffffffffffffffffffffffff"
                      "11000000000000000000000000000000"),
        bytes.fromhex("05000000000000000000000000000000"),
    ),
    (
        # A.3 #8 — carry propagation the other way (borrow-ish path).
        "carry_propagation_down",
        bytes.fromhex("01000000000000000000000000000000"
                      "00000000000000000000000000000000"),
        bytes.fromhex("ffffffffffffffffffffffffffffffff"
                      "fbfefefefefefefefefefefefefefefe"
                      "01010101010101010101010101010101"),
        bytes.fromhex("00000000000000000000000000000000"),
    ),
    (
        # A.3 #9 — a single block equal to 2^130-6, the largest reduced value.
        "largest_reduced_value",
        bytes.fromhex("02000000000000000000000000000000"
                      "00000000000000000000000000000000"),
        bytes.fromhex("fdffffffffffffffffffffffffffffff"),
        bytes.fromhex("faffffffffffffffffffffffffffffff"),
    ),
)


# --------------------------------------------------------------------------
# clamping and key handling
# --------------------------------------------------------------------------

def clamp_r(r_bytes) -> int:
    """RFC 8439 §2.5: read the low 16 key bytes little-endian and CLAMP them.

    Clearing those 22 bits is mandatory. An implementation that skips it
    computes a different polynomial and yields a wrong tag for essentially
    every key — see the ``skip the r clamp`` mutation gate.
    """
    r_bytes = bytes(r_bytes)
    if len(r_bytes) != 16:
        raise ValueError(f"r must be 16 bytes, got {len(r_bytes)}")
    return int.from_bytes(r_bytes, "little") & R_CLAMP


def split_key(key) -> tuple[int, int]:
    """Split a 32-byte one-time key into ``(clamped r, s)``."""
    key = bytes(key)
    if len(key) != 32:
        raise ValueError(f"key must be 32 bytes, got {len(key)}")
    return clamp_r(key[:16]), int.from_bytes(key[16:], "little")


def block_value(block) -> int:
    """One message block as its field element: little-endian, plus the HIGH BIT.

    The extra bit sits immediately above the block's own byte length, so a full
    16-byte block contributes ``1 << 128`` and a 3-byte final block contributes
    ``1 << 24``. Omitting it makes the padding non-injective (two different
    messages can then collide) — the ``drop the high bit`` mutation gate.
    """
    block = bytes(block)
    if not 1 <= len(block) <= 16:
        raise ValueError(f"block must be 1..16 bytes, got {len(block)}")
    return int.from_bytes(block, "little") + (1 << (8 * len(block)))


# --------------------------------------------------------------------------
# the big-integer reference
# --------------------------------------------------------------------------

def poly1305_accumulate(msg, r: int, acc: int = 0) -> int:
    """Run the polynomial accumulation and return ``acc`` (no ``+ s`` yet).

    Exposed separately so a test can gate the *intermediate* state, not only
    the final tag (a one-value gate is not a gate).
    """
    msg = bytes(msg)
    for i in range(0, len(msg), 16):
        acc = ((acc + block_value(msg[i:i + 16])) * r) % P1305
    return acc


def poly1305_mac(msg, key) -> bytes:
    """GOLDEN: the Poly1305 tag of ``msg`` under the 32-byte one-time ``key``.

    Plain big-integer transcription of RFC 8439 §2.5. Returns 16 bytes,
    little-endian.
    """
    r, s = split_key(key)
    acc = poly1305_accumulate(msg, r)
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


# --------------------------------------------------------------------------
# the LIMB reference — the representation the chip computes in
# --------------------------------------------------------------------------

def to_limbs(x: int, n: int = N_LIMBS) -> list[int]:
    """Split a non-negative integer into ``n`` radix-``2**10`` limbs."""
    if x < 0:
        raise ValueError("limb decomposition is for non-negative values")
    return [(x >> (LIMB_BITS * i)) & LIMB_MASK for i in range(n)]


def from_limbs(limbs) -> int:
    """Recombine radix-``2**10`` limbs (which need not be normalised)."""
    return sum(int(v) << (LIMB_BITS * i) for i, v in enumerate(limbs))


def carry_normalise(acc) -> list[int]:
    """Propagate carries so every limb is ``< 2**10``, folding the overflow.

    The whole reason radix 2**10 was chosen: ``130 == 10 * 13`` exactly, so
    anything carried out of the top limb has weight ``2**130``, which is
    ``REDUCTION_CONSTANT`` (5) modulo the prime. It therefore folds straight
    back onto limb 0 with a multiply by 5 — **no division anywhere**.

    Two sweeps suffice: the value re-injected at limb 0 is at most
    ``5 * (acc >> 130)``, which cannot itself overflow the top limb again.
    """
    acc = [int(v) for v in acc]
    for _ in range(2):
        c = 0
        for i in range(N_LIMBS):
            acc[i] += c
            c = acc[i] >> LIMB_BITS
            acc[i] &= LIMB_MASK
        acc[0] += REDUCTION_CONSTANT * c
    return acc


def limb_mul_mod_p(a, r) -> list[int]:
    """``a * r`` (mod ``2**130 - 5``) as 13 un-normalised limb accumulators.

    Because ``2**130 ≡ 5``, a partial product whose limb index reaches or
    exceeds 13 folds back onto index ``i + j - 13`` with a factor of 5. The
    fold is applied to the COEFFICIENT (``5 * r[j]``), never to the running
    accumulator, so every multiplicand stays under ``2**15`` and the signed
    multiplier is exact — see the module docstring.
    """
    a = [int(v) for v in a]
    r = [int(v) for v in r]
    out = [0] * N_LIMBS
    for i in range(N_LIMBS):
        for j in range(N_LIMBS):
            k = i + j
            if k >= N_LIMBS:
                out[k - N_LIMBS] += a[i] * (REDUCTION_CONSTANT * r[j])
            else:
                out[k] += a[i] * r[j]
    return out


def poly1305_mac_limbs(msg, key) -> bytes:
    """GOLDEN (limb form): the same tag, computed the way the chip computes it.

    Kept deliberately separate from :func:`poly1305_mac` so that the block's
    arithmetic decomposition — radix, fold, carry discipline — is gated against
    the RFC rather than against a restatement of its own assumptions.
    """
    r, s = split_key(key)
    rl = to_limbs(r)
    acc = [0] * N_LIMBS
    msg = bytes(msg)
    for i in range(0, len(msg), 16):
        n = to_limbs(block_value(msg[i:i + 16]))
        acc = carry_normalise([x + y for x, y in zip(acc, n)])
        acc = carry_normalise(limb_mul_mod_p(acc, rl))
    # A normalised limb vector can still be >= p (it is only < 2**130); the
    # final conditional subtraction is folded into the modulo here.
    return (((from_limbs(acc) % P1305) + s) % (1 << 128)).to_bytes(16, "little")
