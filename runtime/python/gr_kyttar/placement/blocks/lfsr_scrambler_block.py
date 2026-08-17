# SPDX-License-Identifier: GPL-3.0-or-later
"""LFSRScramblerBlock — see :class:`LFSRScramblerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface


class LFSRScramblerBlock(KyttarBlock):
    """
    Additive LFSR scrambler — GNU Radio ``digital.additive_scrambler_bb``.

    XORs the input bit stream with the free-running output of a Fibonacci LFSR
    defined by ``mask`` (polynomial), ``seed`` (initial shift-register contents)
    and ``len`` (shift-register length). Because the LFSR runs INDEPENDENTLY of
    the data (the output bit is ``input XOR next_bit()``), the block is *additive*
    and *deterministic* given ``(mask, seed, len)``: feeding a scrambled stream
    through an identically-configured block descrambles it (self-inverse). This is
    the exact semantics of ``gr::digital::lfsr::next_bit()`` used by
    ``additive_scrambler_bb`` — verified BIT-EXACT against live GNU Radio.

    LFSR convention (Fibonacci, RIGHT-shifting — matches ``gr::digital::lfsr``):

        output  = shift_register & 1                      # LSB is the output
        newbit  = parity(shift_register & mask)           # XOR of masked bits
        shift_register = (shift_register >> 1) | (newbit << len)

    and the SCRAMBLED bit is ``input_bit XOR output``. This is the classic
    Fibonacci-vs-Galois convention trap: GNU Radio uses the RIGHT-shifting
    Fibonacci form above (output = LSB, feedback into bit ``len``), NOT a
    left-shifting Galois LFSR. Confirmed against live GR output (all-zeros input
    reveals the raw ``next_bit()`` sequence), not a datasheet.

    Optionally, after ``count`` items the shift register is reset to ``seed``
    (``count=0`` = never), so fixed-length vectors re-scramble identically. Since
    this fabric block is bit-serial (``bits_per_byte=1``), ``count`` is measured in
    BITS and equals GR's ``count`` items when ``bits_per_byte=1`` (the default).

    Architecture: single cell (1 cell). The block is memoryless-per-trigger apart
    from the 16-bit LFSR state (and a small reseed counter when ``count>0``); one
    input bit in, one scrambled bit out per trigger.

    Hardware deviations from digital.additive_scrambler_bb:
        - ``bits_per_byte`` must be ``1``. The Kyttar fabric processes ONE bit per
          trigger (the byte->bit unpacking GR does for hard uint8 symbols is a host
          concept); a bit-serial stream is the natural fabric representation and the
          GR default is ``bits_per_byte=1`` anyway. Any other value RAISES (INV-0:
          never silently clamp). Feed the block a bit stream (0/1 words).
        - ``len`` must be ``<= 15``. The on-chip shift register is one 16-bit word,
          so bit ``len`` (the feedback tap position) must fit: ``len`` in ``0..15``.
          GR allows ``len`` up to 63 (a 64-bit register); a register length beyond
          the 16-bit word is a genuine ISA limit and RAISES.
        - ``reset_tag_key`` (GR stream-tag reset) is not supported — the fabric has
          no per-sample tag side channel. ``count`` covers the fixed-vector reset;
          a non-empty ``reset_tag_key`` RAISES.

    Interface:
        - Entry: R1
        - Input: R0 (data bit, 0/1)
        - Output: scrambled/descrambled bit (0/1 in the LSB), one per sample.
    """
    CATEGORY = "fec"
    TAGS = ["lfsr", "scrambler", "descrambler", "additive", "fec"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    # HW-DEVIATION (INV-22): the fabric has no per-sample stream-tag side channel,
    # so ``reset_tag_key`` (GR's stream-tag reset) is NOT settable from GRC — the
    # class RAISES if it is non-empty (see __init__ / class docstring). It is
    # therefore intentionally omitted from the .block.yml binding.
    GRC_UNSUPPORTED_PARAMS = ("reset_tag_key",)

    def __init__(
        self,
        name: str,
        mask: int = 0x8A,
        seed: int = 0x7F,
        len: int = 7,
        count: int = 0,
        bits_per_byte: int = 1,
        reset_tag_key: str = "",
    ):
        """Additive LFSR scrambler.

        Args (mirror ``digital.additive_scrambler_bb`` VERBATIM):
            mask: polynomial mask (feedback tap positions).
            seed: initial shift-register contents.
            len: shift-register length (feedback bit position). Kyttar: <= 15.
            count: reset the register to ``seed`` after this many items
                (``0`` = never). Bit-serial here, so this is in bits.
            bits_per_byte: Kyttar requires ``1`` (see class docstring HW note).
            reset_tag_key: unsupported on the fabric (must be empty).
        """
        # HARDWARE DEVIATION (INV-0): bit-serial fabric -> bits_per_byte must be 1.
        if bits_per_byte != 1:
            raise ValueError(
                "LFSRScramblerBlock requires bits_per_byte=1 (the Kyttar fabric is "
                f"bit-serial; got {bits_per_byte}). Feed a 0/1 bit stream.")
        # HARDWARE DEVIATION (INV-0): 16-bit shift register -> len must be <= 15.
        if not (0 <= len <= 15):
            raise ValueError(
                "LFSRScramblerBlock requires 0 <= len <= 15 (the on-chip shift "
                f"register is one 16-bit word); got len={len}.")
        # HARDWARE DEVIATION (INV-0): no per-sample stream-tag side channel.
        if reset_tag_key:
            raise ValueError(
                "LFSRScramblerBlock does not support reset_tag_key (no stream-tag "
                "side channel on the fabric); use count= for fixed-vector reset.")
        if count < 0:
            raise ValueError(f"count must be >= 0; got {count}.")
        super().__init__(
            name,
            mask=mask,
            seed=seed,
            len=len,
            count=count,
            bits_per_byte=bits_per_byte,
            reset_tag_key=reset_tag_key,
        )
        self._mask = int(mask) & 0xFFFF
        self._seed = int(seed) & 0xFFFF
        self._len = int(len)
        self._count = int(count)
        self._bits_per_byte = int(bits_per_byte)
        # runtime LFSR state (used by process_reference; reset() restores seed).
        self._lfsr_state = self._seed
        self._counter = 0

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Bit-serial additive LFSR scrambler (Fibonacci, right-shifting), the
        exact ``gr::digital::lfsr::next_bit()`` convention:

            out    = input XOR (sr & 1)          ; scrambled bit (LSB is LFSR out)
            newbit = parity(sr & mask)           ; P flag after AND = XOR of bits
            sr     = (sr >> 1) | (newbit << len)

        The parity flag ``P`` (``AND`` sets it to the XOR of all result bits, guide
        §4.8) gives the Fibonacci feedback in one AND: ``AND sr, mask`` sets
        ``P = parity(sr & mask) = newbit``. ``BR.NP`` (branch if parity CLEAR)
        keeps ``newbit=0`` for even parity, else falls through to ``newbit=1``.

        When ``count>0`` a per-bit counter resets ``sr`` to ``seed`` after ``count``
        bits (GR fixed-vector reset). ``count=0`` omits the counter entirely.
        """
        # `febit` = 1 << len is the value OR'd into bit `len` when the Fibonacci
        # feedback bit is 1 (precomputed at construction — no per-bit SHL needed).
        data = [
            DataWord("mask", self._mask, address=1),
            DataWord("one", 1, address=2),
            DataWord("febit", (1 << self._len) & 0xFFFF, address=3),
            DataWord("zero", 0, address=4),
        ]
        state = [
            StateVar("lfsr", initial_value=self._seed),
            StateVar("outbit"),
            StateVar("fb"),
        ]

        # Reseed after `count` bits (only when count > 0). `cnt` counts DOWN from
        # `count`: each bit decrements it; when it hits 0 the register is reset to
        # `seed` and `cnt` reloaded to `count` — GR's fixed-vector reset. Counting
        # down (vs up + CMP) reuses `count` as both the compare AND the reload, so no
        # extra `zero`-reset word/instruction is needed (keeps the cell in budget).
        reseed_asm = ""
        if self._count > 0:
            data.append(DataWord("seed", self._seed, address=5))
            data.append(DataWord("count", self._count, address=6))
            state.append(StateVar("cnt", initial_value=self._count))
            reseed_asm = """\
    ; reseed (count-down): cnt -= 1; if cnt == 0: sr = seed; cnt = count
    MOVE R0, R{state:cnt}
    SUB R0, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ no_reseed
    MOVE R{state:lfsr}, R{data:seed}
    MOVE R{state:cnt}, R{data:count}
no_reseed:
"""

        # out    = input XOR (sr & 1)
        # newbit = parity(sr & mask)   (P flag after the AND)
        # sr     = (sr >> 1) | (newbit << len)
        # The feedback bit `fb` is selected via P with a forward branch — NO GOTO
        # (INV-13: a GOTO near a {write}/{jump} compiles to a stray output JUMP).
        # MOVE does NOT touch the flags (guide §4.1), so `MOVE fb,zero` after the
        # `AND ...,mask` PRESERVES P for the `BR.NP` that follows; the shift/OR is
        # then a single shared tail. `febit` OR'd unconditionally with `fb` (0 or
        # 1<<len) is the merge of both cases into one straight path.
        assembly = """\
start:
    ; save the scrambled bit: out = input XOR (sr & 1)
    MOVE R{state:outbit}, R{in:sample}
    AND R{state:lfsr}, R{data:one}
    XOR R{state:outbit}, R0
    MOVE R{state:outbit}, R0
    ; newbit = parity(sr & mask) -> P flag; select fb without clobbering P (MOVE
    ; leaves flags untouched), so BR.NP reads the AND's parity.
    AND R{state:lfsr}, R{data:mask}
    MOVE R{state:fb}, R{data:zero}
    BR.NP fb_done
    MOVE R{state:fb}, R{data:febit}
fb_done:
    ; sr = (sr >> 1) | fb
    SHR R{state:lfsr}, #1
    OR R0, R{state:fb}
    MOVE R{state:lfsr}, R0
%s\
    ; emit the scrambled bit
    MOVE R0, R{state:outbit}
    {write:out}
    {jump:out}
""" % (reseed_asm,)

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template=assembly,
        )}

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """Bit-exact reference for ``digital.additive_scrambler_bb`` (bits_per_byte=1).

        Fibonacci right-shifting LFSR (``gr::digital::lfsr::next_bit()``):
        ``out = input XOR (sr & 1)``; ``newbit = parity(sr & mask)``;
        ``sr = (sr >> 1) | (newbit << len)``; reset to ``seed`` every ``count``
        bits when ``count > 0``. Stateful across calls until :meth:`reset`.
        """
        inp = np.asarray(input_bits).astype(np.int64)
        out = np.zeros(len(inp), dtype=np.int32)
        sr = self._lfsr_state & 0xFFFF
        cnt = self._counter
        for i in range(len(inp)):
            out[i] = (int(inp[i]) ^ (sr & 1)) & 1
            newbit = bin(sr & self._mask).count("1") & 1
            sr = ((sr >> 1) | (newbit << self._len)) & 0xFFFF
            if self._count > 0:
                cnt += 1
                if cnt == self._count:
                    sr = self._seed & 0xFFFF
                    cnt = 0
        self._lfsr_state = sr
        self._counter = cnt
        return out

    def reset(self):
        """Reset the LFSR to its seed (and the reseed counter)."""
        self._lfsr_state = self._seed
        self._counter = 0
