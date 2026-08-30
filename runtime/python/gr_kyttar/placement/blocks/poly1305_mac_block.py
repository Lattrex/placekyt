# SPDX-License-Identifier: GPL-3.0-or-later
"""Poly1305MACBlock — see :class:`Poly1305MACBlock`."""
from typing import List

import numpy as np

from ._base import BlockInterface, KyttarBlock

MASK16 = 0xFFFF

#: The Poly1305 prime, ``2**130 - 5`` (RFC 8439 §2.5).
P1305 = (1 << 130) - 5

#: RFC 8439 §2.5 clamp on the low 16 key bytes.
R_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF

#: ``2**130 mod p`` — the reduction constant. NOT a tunable.
REDUCTION_CONSTANT = 5

#: Limb radix. See the class docstring for why it is 10 and not 26.
LIMB_BITS = 10
#: ``130 == 10 * 13`` exactly, so the ``2**130 ≡ 5`` fold is limb-aligned.
N_LIMBS = 13
LIMB_MASK = (1 << LIMB_BITS) - 1

#: Words in the emitted tag (16 bytes, little-endian 16-bit words).
TAG_WORDS = 8
#: Words in one full message block (16 bytes).
BLOCK_WORDS = 8


def _clamp_r(r_bytes: bytes) -> int:
    """RFC 8439 §2.5 clamp — mandatory, never optional."""
    return int.from_bytes(bytes(r_bytes), "little") & R_CLAMP


def _to_limbs(x: int) -> List[int]:
    return [(x >> (LIMB_BITS * i)) & LIMB_MASK for i in range(N_LIMBS)]


def _from_limbs(limbs) -> int:
    return sum(int(v) << (LIMB_BITS * i) for i, v in enumerate(limbs))


class Poly1305MACBlock(KyttarBlock):
    """
    **Poly1305** one-time authenticator (RFC 8439 §2.5) — a placeKYT-native
    ([Kyttar]) cryptographic primitive with **no stock GNU Radio counterpart**.
    The golden reference is the published algorithm in
    ``verification/tests/poly1305_golden.py``, itself pinned by the RFC's own
    §2.5.2 worked example and the nine §A.3 edge-case vectors.

    .. warning::

       **THIS BLOCK IS NOT FINISHED, and is deliberately NOT registered in
       ``_modmap.py``** — it has no ``build_cell_programs`` and cannot be
       placed. What exists and is gated is the arithmetic DESIGN plus this
       ``process_reference``, which is exact against the golden on every RFC
       vector. The field multiply below has been proven bit-exact on a real
       placed + routed + built chip (all thirteen accumulators, eleven cases
       including the all-maximum corner); the message packing, the
       carry-normalise phase, the final reduction, the ``+ s`` and the tag
       egress are NOT built. Registering it before those exist would drag an
       unbuildable block into the GRC-binding, placement and orientation
       gates. See the ``Poly1305MACBlock`` entries in ``manifest.json`` and
       ``KNOWLEDGE_BASE/lessons_log.md`` for exactly what was measured.

    Per 16-byte message block, over the field ``GF(2**130 - 5)``::

        acc = ((acc + block_with_high_bit) * r) mod (2**130 - 5)
        tag = (acc + s) mod 2**128

    This is **exact modular integer arithmetic, not Q15 DSP**: nothing
    saturates, every carry is real, and the Q15 idioms (INV-13) must NOT be
    inherited.

    Why radix 2**10 and not the textbook 2**26
    ==========================================

    The radix must **divide 130 exactly** for the ``2**130 ≡ 5`` fold to be
    limb-aligned, which allows only 2, 5, 10, 13 and 26. Choosing among those
    is settled by a property of this ISA that was **MEASURED on a real chip**,
    not read out of a table:

    ``MUL`` (low 16) and ``MULHI`` (high 16) are **SIGNED**. Multiplying
    ``0x0002 * 0xFFFF`` returns ``0xFFFFFFFE``, not ``0x0001FFFE`` — i.e. the
    high word is the *signed* product's. An exact **unsigned** 16×16→32 product
    therefore requires **both operands to lie in ``[0, 0x7FFF]``**, which was
    confirmed exact over the full range.

    That single fact eliminates the usual choice:

    * radix ``2**26`` — limbs reach 26 bits, far outside 15. Dead.
    * radix ``2**13`` — limbs fit, but the folded coefficient ``5*r[j]``
      reaches ``0x9FFB``. Dead.
    * radix ``2**10`` — limbs ``<= 1023`` and ``5*r[j] <= 5115``, both inside
      15 bits. **This one.** Its accumulators peak at a measured 25 bits, well
      inside the 32 the hi/lo pair carries.

    So the plan's "five radix-2**26 limbs" is not implementable on this
    multiplier; thirteen radix-2**10 limbs is the nearest shape that is.

    The 32-bit MAC, and the carry trap inside it
    ============================================

    One partial product accumulated into a 32-bit ``(hi, lo)`` pair costs
    **seven instructions**, and their ORDER is load-bearing::

        MULHI c, a        ; hi half FIRST
        MOVE  t, R0       ; park it
        MUL   c, a        ; lo half
        ADD   R0, lo      ; sets C
        MOVE  lo, R0      ; MOVE is flag-preserving, the carry survives
        ADC   t, hi       ; the carry rides into the high half
        MOVE  hi, R0

    The obvious six-instruction order — ``MUL / ADD / MOVE / MULHI / ADC /
    MOVE`` — is **WRONG**, and wrong in the most dangerous way. ``MULHI`` is an
    ALU op and sets all flags, so it **destroys the carry** the ``ADD`` just
    produced. **Measured on chip:** the accumulator carries a constant
    ``+0x10000`` error from the first accumulation onward while **the low word
    stays bit-exact** in every one of six successive MACs — a defect no
    single-word gate would ever see (INV-54's lesson, in a new place). Computing
    the high half first and parking it in ``t`` keeps ``ADD``→``ADC`` adjacent
    in flag terms, which is the only ordering that is correct.

    Datapath — a 13-cell systolic ring
    ==================================

    The binding constraint is transport, not arithmetic (INV-45): a 13-limb
    accumulator is 13 words and a 13-limb product is 26, so ANY shape that
    *moves* the accumulator pays ``2W+1`` instructions per hop and dies. The
    escape is to keep the accumulator **resident and never move it**, which
    forces one cell per output limb.

    Cell ``k`` owns output limb ``k`` permanently. Both operand vectors stream
    instead:

    * the **a-line rotates** — each MAC cell forwards its resident limb to its
      successor, so after ``i`` passes cell ``k`` holds ``a[(k-i) mod 13]``;
    * the **coefficient is broadcast** — on pass ``i`` every cell receives the
      same ``r[i]`` (a compile-time constant, since ``r`` is a block param).

    With the a-line rotating, cell ``k`` on pass ``i`` holds ``a[m]`` with
    ``m = (k-i) mod 13`` and needs the coefficient ``r[j]`` where ``m + j ≡ k``,
    i.e. ``j = i`` — **the same index for every cell**, which is exactly what
    makes a broadcast sufficient. Verified against the plain dot-product form
    over 400 random limb pairs.

    **The ×5 rides the a-line, not the coefficient.** A partial product whose
    limb index reaches 13 folds back with a factor of 5. In the rotating form
    that is precisely the limb which has **wrapped past the end of the ring**,
    and each limb wraps at most once in 13 passes — so one ``MUL`` by 5, applied
    once at the single wrap edge (``mac12 → mac0``), delivers the entire
    reduction. No division, no per-cell flag, no second broadcast. ``5 * 1023 =
    5115`` is still inside the 15-bit multiplicand rule, so the folded limb is
    a legal operand on the very next pass.

    Per-cell budget (INV-33): 4 state (``a``, ``hi``, ``lo``, ``t``) + 2 inputs
    (rotated limb, broadcast coefficient) + ~10 instructions = **16 of 31** —
    deliberately generous, because neither operand vector is resident.

    Three scheduling rules the ring obeys, each established by a wrong answer
    on chip (and promoted to invariants):

    1. **ADOPT and MAC are SEPARATE ENTRIES.** Fusing *adopt / compute /
       forward* into one entry is wrong in all six step orders and both
       trigger orders — a cell entry is atomic, so the second cell to run in a
       pass already sees the first cell's forward and one value sweeps the
       whole ring.
    2. **Both sweeps fire in REVERSE ring order.** Forward order left twelve
       of thirteen accumulators exact and the wrap cell exactly one pass
       stale: ``JUMP``\\ s are issued in program order but delivery is
       asynchronous, so "later in the entry" is not "later in time" at a
       distant cell.
    3. **The ×5 rides the last cell's FORWARD**, not a cell of its own — a
       separate wrap cell cannot be ordered between the two sweeps by any
       trigger placement. Neither the egress nor the collector may ride a
       compute cell or sit on the broadcast walk.

    **Measured, on a real placed + routed + built chip:** with those in place,
    all thirteen accumulators are bit-exact over eleven cases — all-zero,
    ``a=max r=max`` (every accumulator at its 27-bit peak, exercising the full
    carry chain), ``a=max r=1``, a single limb, the ``r=1`` identity, and six
    random limb pairs.

    Interface:
        - Input: the message as 16-bit little-endian words.
        - Output: the 16-byte tag as ``TAG_WORDS`` little-endian words.

    Hardware deviations from the plan's sketch:
        - The plan specified five radix-2**26 limbs. That is not implementable
          on a SIGNED 16×16 multiplier (measured); thirteen radix-2**10 limbs
          is the nearest limb-aligned shape whose multiplicands stay under
          2**15. The externally visible behaviour — the RFC 8439 tag — is
          unchanged.
    """

    CATEGORY = "fec"
    TAGS = ["poly1305", "crypto", "mac", "authenticator", "rfc8439",
            "multi-word", "130-bit"]

    _interface = BlockInterface(
        entry_address=16, input_registers=[1], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    #: Words per emitted tag.
    TAG_WORDS = TAG_WORDS
    #: Limb count / radix, exported for the tests.
    N_LIMBS = N_LIMBS
    LIMB_BITS = LIMB_BITS
    REDUCTION_CONSTANT = REDUCTION_CONSTANT

    def __init__(self, name: str,
                 r_key: str = "85d6be7857556d337f4452fe42d506a8",
                 s_key: str = "0103808afb0db2fd4abff6af4149f51b"):
        """Poly1305 over a 32-byte one-time key, split into ``r`` and ``s``.

        Both halves are 16 bytes given as lowercase hex, little-endian, exactly
        as RFC 8439 §2.5.2 prints them. ``r_key`` is **clamped** on the way in
        (§2.5) — the clamp is part of the algorithm, so a caller that passes an
        unclamped value still gets the RFC-correct tag.
        """
        super().__init__(name)
        self.r_key = r_key
        self.s_key = s_key
        self._r = _clamp_r(bytes.fromhex(r_key))
        self._s = int.from_bytes(bytes.fromhex(s_key), "little")
        self._r_limbs = _to_limbs(self._r)

    # ------------------------------------------------------------- structure
    @property
    def cell_count(self) -> int:
        return N_LIMBS + 1          # 13 MAC cells + the sequencer

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        return "seq"

    # ------------------------------------------------------------- reference
    def process_reference(self, input_words) -> np.ndarray:
        """Bit-exact reference: the RFC 8439 §2.5 tag of the input word stream.

        ``input_words`` is the message as raw little-endian 16-bit words (NOT
        Q15). The output is the 16-byte tag as ``TAG_WORDS`` little-endian
        words. This models the SAME limb schedule the cells run, so it is a
        statement about the datapath, not merely about the algorithm.
        """
        w = [int(v) & MASK16 for v in np.asarray(input_words).ravel()]
        msg = b"".join(int(v).to_bytes(2, "little") for v in w)

        rl = self._r_limbs
        a = [0] * N_LIMBS
        for off in range(0, len(msg), 16):
            blk = msg[off:off + 16]
            n = _to_limbs(int.from_bytes(blk, "little") + (1 << (8 * len(blk))))
            a = [a[k] + n[k] for k in range(N_LIMBS)]
            a = self._normalise(a)

            # 13 systolic passes: broadcast r[i], rotate the a-line, x5 at wrap.
            acc = [0] * N_LIMBS
            line = list(a)
            for i in range(N_LIMBS):
                for k in range(N_LIMBS):
                    acc[k] += rl[i] * line[k]
                line = [(REDUCTION_CONSTANT * line[N_LIMBS - 1]) if k == 0
                        else line[k - 1] for k in range(N_LIMBS)]

            # reduce the 32-bit accumulators back to a limb line
            a = [0] * N_LIMBS
            carry = 0
            for k in range(N_LIMBS):
                v = acc[k] + carry
                a[k] = v & LIMB_MASK
                carry = v >> LIMB_BITS
            a[0] += REDUCTION_CONSTANT * carry
            a = self._normalise(a, sweeps=1)

        tag = ((_from_limbs(a) % P1305) + self._s) % (1 << 128)
        b = tag.to_bytes(16, "little")
        return np.array([int.from_bytes(b[2 * i:2 * i + 2], "little")
                         for i in range(TAG_WORDS)], dtype=np.uint16)

    @staticmethod
    def _normalise(a, sweeps: int = 2):
        """Carry-normalise a limb line, folding the top overflow by ×5.

        ``130 == 10 * 13`` exactly, so anything carried out of the top limb has
        weight ``2**130`` and folds straight onto limb 0 with a multiply by
        :data:`REDUCTION_CONSTANT`. **No division anywhere.**
        """
        a = list(a)
        for _ in range(sweeps):
            c = 0
            for i in range(N_LIMBS):
                a[i] += c
                c = a[i] >> LIMB_BITS
                a[i] &= LIMB_MASK
            a[0] += REDUCTION_CONSTANT * c
        return a

    def reset(self):
        """No cross-call state: the accumulator re-arms per message."""
        pass
