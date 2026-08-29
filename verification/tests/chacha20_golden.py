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


# --------------------------------------------------------------------------
# RFC 8439 §2.3 — the ChaCha20 BLOCK FUNCTION.
# --------------------------------------------------------------------------

#: The four ChaCha20 constant words — ASCII ``"expand 32-byte k"`` read as four
#: little-endian 32-bit words (RFC 8439 §2.3).
CHACHA20_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)

#: The state index quadruple for quarter round ``j`` of a COLUMN round
#: (RFC 8439 §2.3.1) and of a DIAGONAL round.
COLUMN_QUARTERROUNDS = ((0, 4, 8, 12), (1, 5, 9, 13),
                        (2, 6, 10, 14), (3, 7, 11, 15))
DIAGONAL_QUARTERROUNDS = ((0, 5, 10, 15), (1, 6, 11, 12),
                          (2, 7, 8, 13), (3, 4, 9, 14))


def quarterround_indices(j: int, diagonal: bool) -> tuple[int, int, int, int]:
    """The four state indices quarter round ``j`` operates on.

    Both halves of a double round collapse into ONE closed form::

        index(k) = 4*k + ((j + k*shift) mod 4)      shift = 1 if diagonal else 0

    i.e. take the ``k``-th row of the 4x4 state and step ``shift`` columns per
    row. That is why the column/diagonal permutation costs no lookup table: the
    schedule is arithmetic, not data. Verified exhaustively against the RFC's
    literal quadruples by ``test_chacha20_keystream.py``.
    """
    shift = 1 if diagonal else 0
    return tuple(4 * k + ((j + k * shift) & 3) for k in range(4))  # type: ignore[return-value]


def initial_state(key: bytes, nonce: bytes, counter: int) -> list[int]:
    """Build the 16-word ChaCha20 state (RFC 8439 §2.3).

    Layout: 4 constant words, then the 8 key words, then the block counter,
    then the 3 nonce words. ``key`` is 32 bytes and ``nonce`` 12 bytes, both
    parsed as LITTLE-ENDIAN 32-bit words exactly as the RFC specifies.
    """
    if len(key) != 32:
        raise ValueError(f"a ChaCha20 key is 32 bytes; got {len(key)}")
    if len(nonce) != 12:
        raise ValueError(f"an RFC 8439 nonce is 12 bytes; got {len(nonce)}")
    words = list(CHACHA20_CONSTANTS)
    words += [int.from_bytes(key[4 * i:4 * i + 4], "little") for i in range(8)]
    words.append(counter & MASK32)
    words += [int.from_bytes(nonce[4 * i:4 * i + 4], "little") for i in range(3)]
    return words


def double_round(state: list[int]) -> list[int]:
    """One ChaCha20 double round: 4 column quarter rounds then 4 diagonal ones."""
    s = list(state)
    for diagonal in (False, True):
        for j in range(4):
            ia, ib, ic, idx = quarterround_indices(j, diagonal)
            s[ia], s[ib], s[ic], s[idx] = quarter_round(
                s[ia], s[ib], s[ic], s[idx])
    return s


def block_function(key: bytes, nonce: bytes, counter: int) -> list[int]:
    """RFC 8439 §2.3 block function -> the 16 output words.

    20 rounds (10 double rounds), then the ORIGINAL state is added back word by
    word mod 2**32. That final addition is what makes ChaCha20 one-way; without
    it the permutation is trivially invertible, which is why it is an explicit
    mutation gate.
    """
    initial = initial_state(key, nonce, counter)
    s = list(initial)
    for _ in range(10):
        s = double_round(s)
    return [(s[i] + initial[i]) & MASK32 for i in range(16)]


def serialize(words) -> bytes:
    """The 16 state words -> 64 keystream bytes, little-endian (RFC 8439 §2.3)."""
    return b"".join(int(w & MASK32).to_bytes(4, "little") for w in words)


def keystream_block(key: bytes, nonce: bytes, counter: int) -> bytes:
    """The 64 keystream bytes for one block (RFC 8439 §2.3)."""
    return serialize(block_function(key, nonce, counter))


def keystream(key: bytes, nonce: bytes, counter: int, nbytes: int) -> bytes:
    """``nbytes`` of keystream from ``counter``, incrementing per 64-byte block.

    The counter is 32-bit and does NOT carry into the nonce (RFC 8439 §2.3).
    """
    out = bytearray()
    blk = counter & MASK32
    while len(out) < nbytes:
        out += keystream_block(key, nonce, blk)
        blk = (blk + 1) & MASK32
    return bytes(out[:nbytes])


def encrypt(key: bytes, nonce: bytes, counter: int, plaintext: bytes) -> bytes:
    """RFC 8439 §2.4 ChaCha20 encryption: plaintext XOR keystream."""
    ks = keystream(key, nonce, counter, len(plaintext))
    return bytes(p ^ k for p, k in zip(plaintext, ks))


def state_to_words16(state) -> list[int]:
    """The 16-word 32-bit state -> 32 sixteen-bit words, hi then lo per value.

    This is the wire representation the chip carries (the same hi/lo convention
    as the quarter-round frame).
    """
    out: list[int] = []
    for v in state:
        v &= MASK32
        out.append((v >> 16) & MASK16)
        out.append(v & MASK16)
    return out


def words16_to_state(words) -> list[int]:
    """32 sixteen-bit hi/lo words -> the 16-word 32-bit state."""
    w = [int(x) & MASK16 for x in words]
    if len(w) != 32:
        raise ValueError(f"a ChaCha20 state is 32 sixteen-bit words; got {len(w)}")
    return [((w[2 * i] << 16) | w[2 * i + 1]) for i in range(16)]


# --------------------------------------------------------------------------
# RFC 8439 block-function + encryption test vectors, transcribed from the RFC.
# --------------------------------------------------------------------------

#: RFC 8439 §2.3.2 ("Test Vector for the ChaCha20 Block Function").
RFC8439_BLOCK_KEY = bytes(range(32))
RFC8439_BLOCK_NONCE = bytes.fromhex("000000090000004a00000000")
RFC8439_BLOCK_COUNTER = 1
RFC8439_BLOCK_EXPECTED_STATE = (
    0xE4E7F110, 0x15593BD1, 0x1FDD0F50, 0xC47120A3,
    0xC7F4D1C7, 0x0368C033, 0x9AAA2204, 0x4E6CD4C3,
    0x466482D2, 0x09AA9F07, 0x05D7C214, 0xA2028BD9,
    0xD19C12B5, 0xB94E16DE, 0xE883D0CB, 0x4E3C50A2,
)
RFC8439_BLOCK_EXPECTED_KEYSTREAM = bytes.fromhex(
    "10f1e7e4d13b5915500fdd1fa32071c4"
    "c7d1f4c733c068030422aa9ac3d46c4e"
    "d2826446079faa0914c2d705d98b02a2"
    "b5129cd1de164eb9cbd083e8a2503c4e"
)

#: RFC 8439 §2.4.2 ("Example and Test Vector for the ChaCha20 Cipher").
RFC8439_ENCRYPT_KEY = bytes(range(32))
RFC8439_ENCRYPT_NONCE = bytes.fromhex("000000000000004a00000000")
RFC8439_ENCRYPT_COUNTER = 1
RFC8439_ENCRYPT_PLAINTEXT = (
    b"Ladies and Gentlemen of the class of '99: If I could offer you "
    b"only one tip for the future, sunscreen would be it."
)
RFC8439_ENCRYPT_CIPHERTEXT = bytes.fromhex(
    "6e2e359a2568f98041ba0728dd0d6981"
    "e97e7aec1d4360c20a27afccfd9fae0b"
    "f91b65c5524733ab8f593dabcd62b357"
    "1639d624e65152ab8f530c359f0861d8"
    "07ca0dbf500d6a6156a38e088a22b65e"
    "52bc514d16ccf806818ce91ab7793736"
    "5af90bbf74a35be6b40b8eedf2785e42"
    "874d"
)
