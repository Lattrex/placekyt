# SPDX-License-Identifier: GPL-3.0-or-later
"""N-stream Add / Subtract — :class:`AddBlock`, :class:`SubtractBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class _NStreamAddSub(KyttarBlock):
    """Shared single-cell SATURATING N-stream combiner.

        out[n] = a0[n] (+|-) a1[n] (+|-) … (+|-) a(N-1)[n]   (Q15-clamped each step)

    GNU Radio's ``add_xx`` / ``sub_xx`` expose ``num_inputs`` (N streams: add sums
    all N, sub computes ``a0 - a1 - … - a(N-1)``), so these blocks MIRROR that with
    a ``num_inputs`` param (default 2). ``num_inputs`` real inputs land in
    R0..R(N-1) and the op is chained pairwise across them. Each pairwise ADD/SUB
    SATURATES to the Q15 range [-1.0, +0.99997] (the production fixed-point
    behaviour shared by the FIR / dc-blocker datapaths) — where GR's pure-float
    combiner would simply keep accumulating; the DSP equivalence is asserted on
    IN-RANGE stimulus where the running result stays representable. OUT of range
    the bare Q15 ALU would WRAP (a sign flip — 0.6+0.6 = -0.8), a production
    footgun this block avoids by saturating.

    Per step the SATURATING add/sub reuses the proven restore (the same one the
    2-input block and the FIR use): save the running accumulator, do the ADD/SUB
    (which sets ``V``), and on overflow (``BR.V``... here a FORWARD ``BR.NV`` SKIP
    of the restore, the FIR's ``BR.NV +N`` idiom — a back-jump / GOTO onto a
    ``{write}``/``{jump}`` placeholder is miscompiled, INV-13) restore the rail
    from the saved accumulator's sign with the ``SHR #15; ADD satpos`` trick
    (→ +0x7FFF / -0x8000). The chain stays straight-line (forward skips only), so
    no labels into the middle of the chain are needed.

    Interface: ``num_inputs`` real inputs ``a0..a(N-1)`` in R0..R(N-1) (the proven
    complex-burst fan-in delivers a0/a1; N>2 needs an N-operand driver), ONE real
    output. Memoryless → delay=0. ``num_inputs`` is bounded by the cell budget
    (HW limit; raises above).
    """
    _OP = "ADD"            # overridden to "SUB" by SubtractBlock
    _SIGN = +1             # +1 for add, -1 for subtract (reference)
    SAT_POS_Q15 = 0x7FFF
    # Each chain step is 6 words (MOVE save, OP, BR.NV +4, MOVE restore, SHR, ADD);
    # N-1 steps + setup + emit must fit one 32-word cell. N<=6 fits comfortably.
    MAX_INPUTS = 6

    def __init__(self, name: str, num_inputs: int = 2):
        n = int(num_inputs)
        if n < 2:
            raise ValueError(f"num_inputs must be >= 2, got {n}")
        if n > self.MAX_INPUTS:
            raise ValueError(
                f"HARDWARE LIMIT: num_inputs={n} exceeds {self.MAX_INPUTS} "
                f"(the N input registers + per-step saturating add/sub chain must "
                f"fit one 32-word cell).")
        super().__init__(name, num_inputs=n)
        self._num_inputs = n
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
        # Data placed PAST the highest input register so it never collides with an
        # explicit input reg (NCO state-allocation-gap gotcha).
        satpos_addr = n
        op = self._OP
        # acc starts as a0 (in R0); chain each remaining input with a saturating op.
        lines = ["    MOVE R0, R{in:a0}"]
        for i in range(1, n):
            lines += [
                "    MOVE R{state:asav}, R0",      # save running accumulator
                f"    {op} R0, R{{in:a%d}}" % i,   # acc (op)= a_i, sets V
                "    BR.NV +3",                    # no overflow → skip the 3-word restore
                "    MOVE R0, R{state:asav}",      # overflow: rail from saved acc sign
                "    SHR R0, #15",
                "    ADD R0, R{data:satpos}",
            ]
        lines += ["    {write:out}", "    {jump:out}"]
        return {0: CellProgram(
            inputs=ports,
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", self.SAT_POS_Q15, address=satpos_addr)],
            state=[StateVar("asav")],
            assembly_template="start:\n" + "\n".join(lines) + "\n",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, *streams) -> list:
        """Bit-exact predictor of the on-chip chained SATURATING add/sub. Each
        ``streams[i]`` is a list of uint16 Q15 words (one per input; 2 for the
        default 2-input block)."""
        out = []
        for sample in zip(*streams):
            acc = self._s16(sample[0])
            for s in sample[1:]:
                acc = acc + self._SIGN * self._s16(s)
                acc = max(-32768, min(32767, acc))   # saturate after each step
            out.append(acc & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference for the DEFAULT 2-input block. The two streams are
        carried as one complex array (real = a, imag = b); returns a (+|-) b,
        clamped to the Q15 range. (For N>2 use :meth:`process_reference_q15`.)"""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = arr.real.astype(np.float64)
            b = arr.imag.astype(np.float64)
        else:
            a = arr.astype(np.float64)
            b = np.zeros_like(a)
        s = a + self._SIGN * b
        return np.clip(s, -1.0, 32767.0 / 32768.0).astype(np.float32)


class AddBlock(_NStreamAddSub):
    """N-stream adder — drop-in for GNU Radio ``blocks.add_ff``
    (``out = a0 + a1 + … + a(N-1)``). Saturating Q15. See :class:`_NStreamAddSub`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["add", "sum", "add_ff", "signal_conditioning"]
    _OP = "ADD"
    _SIGN = +1


class SubtractBlock(_NStreamAddSub):
    """N-stream subtractor — drop-in for GNU Radio ``blocks.sub_ff``
    (``out = a0 - a1 - … - a(N-1)``). Saturating Q15. See :class:`_NStreamAddSub`."""
    CATEGORY = "signal_conditioning"
    TAGS = ["subtract", "difference", "sub_ff", "signal_conditioning"]
    _OP = "SUB"
    _SIGN = -1
