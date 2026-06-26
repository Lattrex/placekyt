# SPDX-License-Identifier: GPL-3.0-or-later
"""Complex channel selectors — :class:`ComplexToRealBlock`,
:class:`ComplexToImagBlock`."""
import numpy as np

from ..block import CellProgram, EntryPoint, Port
from ._base import BlockInterface, KyttarBlock


class _ComplexSelect(KyttarBlock):
    """Shared single-cell complex channel selector.

    A complex sample is carried as a two-operand (re@R0, im@R1) pair; this block
    forwards ONE of the two as its single real output. ``complex_to_real`` selects
    re, ``complex_to_imag`` selects im. (A subset of complex_to_float, but the two
    are separate GR blocks people wire directly to grab one rail.) Pure data
    movement → EXACT (zero Q15 error). No params. Memoryless → delay=0.
    """
    _SEL = "re"             # "re" (real) or "im" (imag) — set by the subclass

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("re", register=0), Port("im", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            assembly_template=f"""\
start:
    MOVE R0, R{{in:{self._SEL}}}
    {{write:out}}
    {{jump:out}}
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, re_q15, im_q15) -> list:
        """Bit-exact predictor: the selected channel, unchanged."""
        sel = re_q15 if self._SEL == "re" else im_q15
        return [int(w) & 0xFFFF for w in sel]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: the selected channel as a real stream."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            chan = arr.real if self._SEL == "re" else arr.imag
            return chan.astype(np.float32)
        return arr.astype(np.float32)


class ComplexToRealBlock(_ComplexSelect):
    """Real part of a complex stream — drop-in for GNU Radio
    ``blocks.complex_to_real`` (``out = real(in)``). Exact."""
    CATEGORY = "signal_conditioning"
    TAGS = ["complex_to_real", "real", "select", "signal_conditioning"]
    _SEL = "re"


class ComplexToImagBlock(_ComplexSelect):
    """Imaginary part of a complex stream — drop-in for GNU Radio
    ``blocks.complex_to_imag`` (``out = imag(in)``). Exact."""
    CATEGORY = "signal_conditioning"
    TAGS = ["complex_to_imag", "imag", "select", "signal_conditioning"]
    _SEL = "im"
