# SPDX-License-Identifier: GPL-3.0-or-later
"""KeepOneInNBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class KeepOneInNBlock(KyttarBlock):
    """
    Decimate-by-drop — drop-in for GNU Radio ``blocks.keep_one_in_n``: keep one
    sample out of every ``n`` (no filtering, just drop). Output rate = input / n.

    Unlike the DecimatorBlock (an anti-alias FIR + emit-every-n), this is a pure
    pass-through gated by a modulo-``n`` counter — the decimator's emit gate without
    the filter. GR's keep_one_in_n keeps the LAST sample of each group of ``n``
    (verified: ``keep_one_in_n(3)`` of 0..11 → 2,5,8,11, i.e. phase n−1), so the
    counter emits when it reaches ``n`` then resets. Single cell.

    Params mirror GR: ``n`` (the keep factor; GRC ``n``). Exact pass-through (no Q15
    arithmetic), so emitted samples equal the input bit-for-bit. The emit happens on
    input indices n−1, 2n−1, … — ``run_block_dut`` records None on the dropped
    triggers, and the emitted stream is ``outputs[n-1::n]`` (delay 0).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["keep_one_in_n", "downsample", "decimate", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, n: int = 2):
        if int(n) < 1:
            raise ValueError(f"n must be >= 1, got {n}")
        super().__init__(name, n=n)
        self._n = int(n)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def n(self) -> int:
        return self._n

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("n", self._n, address=1),
                  DataWord("one", 1, address=2)],
            state=[StateVar("xs"), StateVar("counter", initial_value=0)],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:x}
    ADD R{state:counter}, R{data:one}
    MOVE R{state:counter}, R0
    CMP R{state:counter}, R{data:n}
    BR.NZ _skip
    XOR R{state:counter}, R{state:counter}
    MOVE R{state:counter}, R0
    MOVE R0, R{state:xs}
    {write:out}
    {jump:out}
    HALT
_skip:
    HALT
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """The emitted (kept) stream: every n-th input at phase n−1 (GR's keep)."""
        return [int(w) & 0xFFFF for w in x_q15[self._n - 1::self._n]]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: keep one in n (phase n−1)."""
        return np.asarray(input_samples)[self._n - 1::self._n].astype(np.float32)
