# SPDX-License-Identifier: GPL-3.0-or-later
"""XorBlock — bitwise XOR of two byte streams. See :class:`XorBlock`."""
import numpy as np

from ..block import CellProgram, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class XorBlock(KyttarBlock):
    """
    Bitwise XOR of two byte streams — drop-in for GNU Radio ``blocks.xor_bb``.

        out[n] = a[n] ^ b[n]           (bitwise, per element)

    GNU Radio's ``xor_bb`` XORs ``num_inputs`` (2+) unsigned-char streams together.
    This block builds the canonical TWO-input case: ``out = a ^ b`` of two byte
    (uint8) streams landing in R0 (a) and R1 (b). The result is one native LOGIC
    ``XOR`` on the cell ALU (``R0 = R0 ^ R1``), which writes R0 — so the emit reads
    straight from R0. Memoryless, no group delay (delay=0).

    This is a pure BITWISE op, NOT Q15 arithmetic: there is no rounding, no
    saturation, and no overflow corner. A byte value 0..255 rides the low 8 bits of
    the 16-bit data word (high bits zero), and XOR is bit-parallel, so the on-chip
    result is BIT-EXACT vs ``xor_bb`` for every input pair — the whole 8-bit result,
    not just the LSB. (The datapath word is 16-bit; XOR of two byte-valued words is
    itself byte-valued, so nothing spills above bit 7.)

    Interface: two real byte inputs ``a`` (R0) / ``b`` (R1) via the proven complex-
    burst fan-in (``WRITE a -> R0``, ``WRITE b -> R1``, one ``JUMP``); ONE byte
    output from R0. Single cell.

    GR ``xor_bb`` takes NO params (num_inputs is the input-port count; the 2-input
    case has none the user must set), so this block — matching GR — takes none.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["xor", "xor_bb", "logic", "byte", "signal_conditioning"]

    def __init__(self, name: str):
        super().__init__(name)
        # a@R0, b@R1; the XOR result egresses from R0.
        self._interface = BlockInterface(
            entry_address=1, input_registers=[0, 1], output_registers=[0])

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # a@R0, b@R1 -> XOR (LOGIC MODE 10) writes R0 = R0 ^ R1 -> emit R0.
        lines = [
            "    XOR R{in:a}, R{in:b}",   # R0 = a ^ b (LOGIC writes R0)
            "    {write:out}",
            "    {jump:out}",
        ]
        return {0: CellProgram(
            inputs=[Port("a", register=0), Port("b", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            assembly_template="start:\n" + "\n".join(lines) + "\n",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_bytes(self, a_stream, b_stream) -> list:
        """Bit-exact predictor of the on-chip XOR: ``a ^ b`` per element, masked to
        16 bits (byte inputs stay in 0..255). Inputs are lists of ints."""
        return [(int(a) ^ int(b)) & 0xFFFF for a, b in zip(a_stream, b_stream)]

    def process_reference(self, input_samples) -> np.ndarray:
        """Reference for the harness. The two byte streams are carried as one
        complex array (real = a, imag = b); returns their bitwise XOR as float
        (byte values 0..255). (For direct byte compares use
        :meth:`process_reference_bytes`.)"""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = np.rint(arr.real).astype(np.int64)
            b = np.rint(arr.imag).astype(np.int64)
        else:
            a = np.rint(arr).astype(np.int64)
            b = np.zeros_like(a)
        return (np.bitwise_xor(a, b) & 0xFFFF).astype(np.float32)
