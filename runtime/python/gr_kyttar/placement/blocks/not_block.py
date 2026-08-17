# SPDX-License-Identifier: GPL-3.0-or-later
"""NotBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class NotBlock(KyttarBlock):
    """
    Bitwise NOT of a byte stream — drop-in for GNU Radio ``blocks.not_bb``:
    ``out[n] = (~in[n]) & 0xFF``.

    ``not_bb`` complements each byte over the FULL 8-bit width, i.e. the output is
    ``~in`` masked to a byte, NOT a per-bit toggle of only the low bit. Verified
    against LIVE GNU Radio ``blocks.not_bb``: ``0x00 -> 0xFF``, ``0xFF -> 0x00``,
    ``0xAA -> 0x55``, ``0x0F -> 0xF0``. (GR's C++ is literally ``out = ~in`` on a
    ``uint8``, so the top-bit complement is retained and the byte wraps around.)

    Single memoryless feed-forward cell: the on-chip ``NOT`` op complements the whole
    16-bit register, so the block masks the result back to 8 bits with an ``AND
    0x00FF`` to reproduce ``not_bb``'s exact byte width. This ``& 0xFF`` is
    LOAD-BEARING — without it a 16-bit NOT would set the high byte (``0x00 ->
    0xFF00`` instead of ``0x00FF``) and disagree with GR. Memoryless -> delay = 0.
    One byte input, one byte output; no parameters (mirrors ``not_bb``, which takes
    none).

    Byte-stream block (integer 0..255 words), NOT a Q15 amplitude block: there is no
    fixed-point quantization and no saturation — the comparison against GR is
    BIT-EXACT (metric DECISION, tolerance 0).

    Interface:
        - Entry: R1
        - Input: R0 (data byte, 0..255)
        - Output: (~byte) & 0xFF, one per sample.
    """
    CATEGORY = "logic"
    TAGS = ["not", "not_bb", "bitwise", "complement", "logic"]

    BYTE_MASK = 0x00FF

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str):
        # not_bb takes NO parameters (INV-0: mirror GR verbatim).
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # out = (~byte) & 0xFF. NOT complements the whole 16-bit register, so the
        # AND with 0x00FF restores GR not_bb's exact byte width (see class docstring).
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("byte_mask", self.BYTE_MASK, address=1),
            ],
            state=[],
            assembly_template="""\
start:
    NOT R{in:sample}
    AND R0, R{data:byte_mask}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples) -> np.ndarray:
        """Bit-exact reference for ``blocks.not_bb``: ``out = (~in) & 0xFF`` over the
        full 8-bit width. Byte semantics — inputs are uint8, output is uint8."""
        arr = np.asarray(input_samples).astype(np.int64)
        return ((~arr) & 0xFF).astype(np.int32)
