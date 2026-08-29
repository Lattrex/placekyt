# SPDX-License-Identifier: GPL-3.0-or-later
"""ChaCha20 golden reference — RFC 8439 (Nir & Langley, "ChaCha20 and Poly1305
for IETF Protocols", June 2018).

This is the AUTHORITY for :class:`ChaCha20QRBlock`: there is no stock GNU Radio
counterpart, so the gate compares the on-chip result against the published
algorithm, and the published algorithm is itself pinned by the RFC's own test
vectors (see :data:`RFC8439_QUARTERROUND_VECTOR` and
:data:`RFC8439_STATE_QUARTERROUND_VECTOR`, both reproduced verbatim from the
RFC and asserted in ``test_chacha20_qr.py``).

Everything here is exact 32-bit modular integer arithmetic — NOT Q15 DSP. A
ChaCha20 word wraps mod 2**32; it never saturates. That distinction is the
single most important property of this block and is gated explicitly.
"""

from __future__ import annotations

MASK32 = 0xFFFFFFFF
MASK16 = 0xFFFF


def rotl32(x: int, n: int) -> int:
    """Rotate a 32-bit word left by ``n`` (RFC 8439 §2.1's ``<<<``)."""
    x &= MASK32
    n &= 31
    if n == 0:
        return x
    return ((x << n) | (x >> (32 - n))) & MASK32


def quarter_round(a: int, b: int, c: int, d: int) -> tuple[int, int, int, int]:
    """The ChaCha20 quarter round, RFC 8439 §2.1, verbatim::

        a += b;  d ^= a;  d <<<= 16
        c += d;  b ^= c;  b <<<= 12
        a += b;  d ^= a;  d <<<= 8
        c += d;  b ^= c;  b <<<= 7

    All additions are mod 2**32 (wrapping, never saturating).
    """
    a, b, c, d = a & MASK32, b & MASK32, c & MASK32, d & MASK32
    a = (a + b) & MASK32
    d ^= a
    d = rotl32(d, 16)
    c = (c + d) & MASK32
    b ^= c
    b = rotl32(b, 12)
    a = (a + b) & MASK32
    d ^= a
    d = rotl32(d, 8)
    c = (c + d) & MASK32
    b ^= c
    b = rotl32(b, 7)
    return a, b, c, d


# --------------------------------------------------------------------------
# RFC 8439 test vectors, transcribed from the RFC text.
# --------------------------------------------------------------------------

#: RFC 8439 §2.1.1 ("Test Vector for the ChaCha Quarter Round").
#: ``(inputs, expected_outputs)`` as ``(a, b, c, d)`` tuples.
RFC8439_QUARTERROUND_VECTOR = (
    (0x11111111, 0x01020304, 0x9B8D6F43, 0x01234567),
    (0xEA2A92F4, 0xCB1CF8CE, 0x4581472E, 0x5881C4BB),
)

#: RFC 8439 §2.2.1 ("Test Vector for the Quarter Round on the ChaCha State") —
#: ``QUARTERROUND(2, 7, 8, 13)`` applied to a full 16-word state. An
#: INDEPENDENT check of the same primitive on different operands, so a
#: transcription slip in one vector cannot pass unnoticed.
RFC8439_STATE_QUARTERROUND_INDICES = (2, 7, 8, 13)
RFC8439_STATE_QUARTERROUND_VECTOR = (
    (
        0x879531E0, 0xC5ECF37D, 0x516461B1, 0xC9A62F8A,
        0x44C20EF3, 0x3390AF7F, 0xD9FC690B, 0x2A5F714C,
        0x53372767, 0xB00A5631, 0x974C541A, 0x359E9963,
        0x5C971061, 0x3D631689, 0x2098D9D6, 0x91DBD320,
    ),
    (
        0x879531E0, 0xC5ECF37D, 0xBDB886DC, 0xC9A62F8A,
        0x44C20EF3, 0x3390AF7F, 0xD9FC690B, 0xCFACAFD2,
        0xE46BEA80, 0xB00A5631, 0x974C541A, 0x359E9963,
        0x5C971061, 0xCCC07C79, 0x2098D9D6, 0x91DBD320,
    ),
)


# --------------------------------------------------------------------------
# The 16-bit word view — the representation the chip actually carries.
# --------------------------------------------------------------------------

#: Word order on the wire, in and out: four 32-bit values, each hi word then
#: lo word. This is the block's frame layout.
FRAME_ORDER = ("a_hi", "a_lo", "b_hi", "b_lo",
               "c_hi", "c_lo", "d_hi", "d_lo")


def words_to_frame(a: int, b: int, c: int, d: int) -> list[int]:
    """``(a, b, c, d)`` 32-bit values -> the 8-word hi/lo frame."""
    out: list[int] = []
    for v in (a, b, c, d):
        v &= MASK32
        out.append((v >> 16) & MASK16)
        out.append(v & MASK16)
    return out


def frame_to_words(frame) -> tuple[int, int, int, int]:
    """The 8-word hi/lo frame -> ``(a, b, c, d)`` 32-bit values."""
    f = [int(w) & MASK16 for w in frame]
    if len(f) != 8:
        raise ValueError(f"a ChaCha20 quarter-round frame is 8 words; got {len(f)}")
    return tuple(((f[2 * k] << 16) | f[2 * k + 1]) for k in range(4))  # type: ignore[return-value]


def quarter_round_frame(frame) -> list[int]:
    """One quarter round on an 8-word hi/lo frame -> the 8-word result frame."""
    return words_to_frame(*quarter_round(*frame_to_words(frame)))
