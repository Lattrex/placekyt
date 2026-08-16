# SPDX-License-Identifier: GPL-3.0-or-later
"""MultiplyBlock — see :class:`MultiplyBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class MultiplyBlock(KyttarBlock):
    """
    N-stream multiply — drop-in for GNU Radio ``blocks.multiply_ff``: the
    element-wise product of ``num_inputs`` real input streams.

        out[n] = a0[n] · a1[n] · … · a(N-1)[n]

    GNU Radio's ``multiply_xx`` exposes ``num_inputs`` (the number of streams to
    multiply, default 2), so this block mirrors it: ``num_inputs`` real inputs land
    in R0..R(N-1); the product is a chain of ``MULQ`` (``out = ((a0·a1)>>15·a2)>>15
    …``) — each ``MULQ`` is a Q15 product. Memoryless, no group delay (delay=0).

    Interface: ``num_inputs`` real inputs ``a0..a(N-1)`` in R0..R(N-1) (the proven
    complex-burst fan-in delivers a0/a1; N>2 needs an N-operand driver), ONE real
    output. ``num_inputs`` is bounded by the cell's register budget (HW limit ~24);
    raises above.

    Q15 note: the only 2-input product that overflows is the exact
    ``(-1.0)·(-1.0)=+1.0`` corner — the MULQ datapath WRAPS (not saturates); the
    bit-exact reference models that, and the GR-equivalence stimulus stays off the
    simultaneous full-scale-negative corner.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["multiply", "product", "multiply_ff", "signal_conditioning"]

    MAX_INPUTS = 24

    def __init__(self, name: str, num_inputs: int = 2):
        n = int(num_inputs)
        if n < 2:
            raise ValueError(f"num_inputs must be >= 2, got {n}")
        if n > self.MAX_INPUTS:
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} exceeds {self.MAX_INPUTS} "
                f"(the N input registers + chained MULQ program must fit one "
                f"32-word cell).")
        super().__init__(name, num_inputs=n)
        self._num_inputs = n
        # N real inputs in R0..R(N-1); the product egresses from R0.
        self._interface = BlockInterface(
            entry_address=1, input_registers=list(range(n)), output_registers=[0])

    @property
    def num_inputs(self) -> int:
        return self._num_inputs

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        n = self._num_inputs
        ports = [Port(f"a{i}", register=i) for i in range(n)]
        # MULQ a0,a1 -> R0; then MULQ R0,a2; ... chain across all N inputs.
        lines = ["    MULQ R{in:a0}, R{in:a1}"]
        for i in range(2, n):
            lines.append(f"    MULQ R0, R{{in:a{i}}}")
        lines += ["    {write:out}", "    {jump:out}"]
        # ``join``: the COUNTING-JOIN entry (see AddBlock) — every arm JUMPs
        # here; a toggle counter fires the product on the SECOND arrival, in
        # ANY arm order (no depth election, no stale-operand race). The tail
        # PRECEDES the compute body labelled ``default`` and falls through on
        # the second arrival (see AddBlock for the label/GOTO rationale).
        # ``sink``: the legacy DATA-ONLY entry, kept for saved designs.
        # R0 is BOTH the ALU result register AND input a0 — the counter must
        # save/restore it or the toggle arithmetic destroys the delivered a0
        # (the sum then reads 1-jcnt as its first operand).
        # The counting tail costs 8 program words + jcnt/jsav + 'one' — it
        # exists ONLY for the 2-input form (the dataflow-join arity every GRC
        # join uses; N>=3 combiners are driven as single N-operand bursts and
        # would overflow the 32-word cell with the tail).
        join_tail = "" if n != 2 else (
            "join:\n"
            "    MOVE R{state:jsav}, R0\n"
            "    MOVE R0, R{data:one}\n"
            "    SUB R0, R{state:jcnt}\n"
            "    BR.Z +3\n"
            "    MOVE R{state:jcnt}, R0\n"
            "    MOVE R0, R{state:jsav}\n"
            "    HALT\n"
            "    MOVE R{state:jcnt}, R0\n"
            "    MOVE R0, R{state:jsav}\n")
        return {0: CellProgram(
            inputs=ports,
            outputs=[Port("out")],
            entries=([EntryPoint("default"), EntryPoint("sink")]
                     + ([EntryPoint("join")] if n == 2 else [])),
            data=([DataWord("one", 1, address=n)] if n == 2 else []),
            state=([StateVar("jcnt"), StateVar("jsav")] if n == 2 else []),
            assembly_template=(join_tail + "default:\n" + "\n".join(lines)
                               + "\nsink:\n    HALT\n"),
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, *streams) -> list:
        """Bit-exact predictor of the on-chip chained MULQ: ``((a0·a1)>>15·a2)>>15
        …`` with the Q15 datapath's wrapping overflow. Each ``streams[i]`` is a list
        of uint16 Q15 words (one per input; 2 for the default 2-input block)."""
        out = []
        for sample in zip(*streams):
            acc = self._s16(sample[0])
            for s in sample[1:]:
                acc = (acc * self._s16(s)) >> 15      # one Q15 MULQ (arith floor)
                acc = self._s16(acc & 0xFFFF)         # wrap to Q15 (datapath wraps)
            out.append(acc & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference. The two streams are carried as one complex array
        (real = a, imag = b); returns their real product."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = arr.real.astype(np.float64)
            b = arr.imag.astype(np.float64)
        else:
            a = arr.astype(np.float64)
            b = arr.astype(np.float64)
        return (a * b).astype(np.float32)
