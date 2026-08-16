# SPDX-License-Identifier: GPL-3.0-or-later
"""AndConstBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class AndConstBlock(KyttarBlock):
    """
    Bitwise AND with an immediate — drop-in for GNU Radio ``blocks.and_const_bb``:
    ``out[n] = in[n] & constant``.

    A byte-stream masking primitive: ``&1`` takes the LSB, ``&0x0F`` the low
    nibble, ``&0xFF`` is identity, ``&0`` zeros the stream. Both operands are
    unsigned bytes (0..255) exactly as GNU Radio's ``and_const_bb`` (``unsigned
    char`` I/O, ``constant`` mirrored VERBATIM).

    On chip: one ``LOGIC.AND`` of the input register against a baked immediate
    (the mask), the same single-cell-op-with-a-baked-constant shape as
    ``AddConstBlock`` / ``GainBlock``. Memoryless → delay=0. Single real input,
    single real output.

    BIT-EXACT: the AND is a pure bitwise op — there is no Q15 rounding path — so
    the DUT matches GR ``and_const_bb`` bit-for-bit over the whole 0..255 byte
    range. The mask is stored as ``constant & 0xFF`` so a caller passing GR's
    signed-``char`` constant (e.g. ``0xFF`` == ``-1``) lands the same 8-bit mask
    GR applies.
    """
    CATEGORY = "byte_operators"
    TAGS = ["and_const", "and_const_bb", "bitwise", "mask", "byte_operators"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, constant: int = 1):
        """
        Args:
            name: Block instance name.
            constant: the immediate AND mask, mirrored VERBATIM from GR
                ``and_const_bb``. Applied as an 8-bit byte mask (``& 0xFF``).
        """
        super().__init__(name, constant=constant)
        self._constant = int(constant)
        # GR and_const_bb's constant is a signed char applied to unsigned-char
        # I/O; the effective mask is the low 8 bits.
        self._mask = self._constant & 0xFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def constant(self) -> int:
        return self._constant

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("mask", self._mask, address=1)],
            assembly_template="""\
start:
    AND R{in:sample}, R{data:mask}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor: ``(byte & mask) & 0xFF`` over the byte stream.

        Inputs are byte values (0..255) delivered as raw words; the AND is a pure
        bitwise mask, so the result is exactly GR ``and_const_bb``'s output byte.
        """
        return [(int(x) & self._mask) & 0xFF for x in x_q15]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: ``byte & constant`` as a byte stream.

        ``input_samples`` are byte values (GR ``and_const_bb`` I/O is unsigned
        char); the output is the masked byte, returned as float for the harness.
        """
        x = np.asarray(input_samples).astype(np.int64)
        return ((x & self._mask) & 0xFF).astype(np.float32)
