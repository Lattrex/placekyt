# SPDX-License-Identifier: GPL-3.0-or-later
"""FSK4SlicerBlock — see :class:`FSK4SlicerBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface, float_to_q15


class FSK4SlicerBlock(KyttarBlock):
    """
    M17 4FSK hard-decision slicer (1 cell) — the 1-D PAM analog of the QPSK slicer.

    The receiver's final decision stage of an **M17 4-level FSK** modem: a recovered
    FM-discriminator LEVEL (the symbol-center value out of the timing loop) maps to
    the 2-bit **dibit** it came from. It is the exact inverse of
    :class:`FSK4SymbolMapperBlock` in the same **LSB-first** convention, so a TX→RX
    loopback recovers the original bitstream unchanged.

    Decision (nearest of the four PAM levels {+3, +1, −1, −3})
    =========================================================
    Thresholds at 0 and ±2 on the {±1, ±3} PAM grid — i.e. the input is expected
    NORMALISED so the outer ``±3`` symbols sit at ``±1.0`` (the same normalisation
    :class:`FSK4SymbolMapperBlock` emits), which puts the ``±2``-on-the-grid
    thresholds at ``±2/3`` and the inner/outer split at ``0``::

        y ≥ +2/3        →  +3  →  d = 1
        0 ≤ y < +2/3    →  +1  →  d = 0
        −2/3 ≤ y < 0    →  −1  →  d = 2
        y < −2/3        →  −3  →  d = 3

    This is the inverse Gray map of the mapper (RULE #0, LSB-first): the level→dibit
    table is ``+3→1, +1→0, −1→2, −3→3``. The recovered dibit ``d`` is emitted as TWO
    output words, **b0 (the LSB) first, then b1 (the MSB)** — ``b0 = d & 1``,
    ``b1 = (d >> 1) & 1`` — matching the mapper's LSB-first bit order, so a clean
    channel gives bit-for-bit identity.

    Decision logic (branchless-ish sign tests)
    ------------------------------------------
    Two sign tests decide the dibit. ``s = (y < 0)`` selects the sign half (inner
    vs outer flips: for ``y ≥ 0`` the OUTER symbol is +3=d1, for ``y < 0`` the outer
    is −3=d3). ``m = (|y| ≥ 2/3)`` selects inner (±1) vs outer (±3). The four
    ``(s, m)`` cases map straight to ``d``:

        s=0 m=0 → +1 → d=0     s=0 m=1 → +3 → d=1
        s=1 m=0 → −1 → d=2     s=1 m=1 → −3 → d=3

    Reading off the two output bits directly from the table above, ``b0 = d & 1``
    is exactly the magnitude flag ``m`` and ``b1 = (d >> 1) & 1`` is exactly the
    sign flag ``s``. So the slicer needs NO dibit lookup table at all — it emits::

        b0 (LSB) = (|y| ≥ 2/3)      the magnitude bit
        b1 (MSB) = (y < 0)          the sign bit

    Interface:
        - Entry: R1 (cell 0)
        - Input: R0 (the discriminator symbol level, signed Q15)
        - Output: R0 — two words per input, ``b0`` (LSB) then ``b1`` (MSB)

    Single cell: a sign compare (``y < 0``) and a magnitude compare (``|y| ≥ thr``)
    yield the two output bits directly. A remote JUMP does not halt local execution,
    so both bit WRITEs + the trigger ride one program.
    """
    CATEGORY = "demodulation"
    TAGS = ["fsk", "4fsk", "c4fm", "m17", "slicer", "hard_decision", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # Decision threshold on |y|: midpoint between the +1 (1/3) and +3 (1.0) levels
    # in the mapper's normalisation = 2/3. (== level "2" on the {1,3} grid / 3.)
    _THR = 2.0 / 3.0

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def threshold_q15(self) -> int:
        """The Q15 magnitude threshold (``2/3``) between the inner and outer levels."""
        return float_to_q15(self._THR)

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Slice a signed level to a dibit and emit its two bits LSB-first.

        The dibit's two bits ARE the two decision flags (see class docstring):
        ``b0 = (|y| ≥ thr)`` (magnitude) and ``b1 = (y < 0)`` (sign). ``mag`` holds
        ``|y|`` (negate when ``y < 0``, tracked by ``sign``). Emit ``b0`` then
        ``b1`` — no dibit lookup table. Single cell, single output face."""
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0x0000, address=1),
                DataWord("one", 0x0001, address=2),
                DataWord("thr", self.threshold_q15, address=3),
            ],
            state=[
                StateVar("y_save"),
                StateVar("mag"),   # |y|
                StateVar("sign"),  # b1 = (y < 0)
            ],
            assembly_template="""\
start:
    MOVE R{state:y_save}, R{in:sample}
    ; --- sign bit b1 = (y < 0); build |y| in mag ---
    MOVE R{state:sign}, R{data:zero}
    MOVE R{state:mag}, R{state:y_save}
    CMP R{state:y_save}, R{data:zero}
    BR.NN nonneg
    MOVE R{state:sign}, R{data:one}
    MOVE R0, R{data:zero}
    SUB R0, R{state:y_save}
    MOVE R{state:mag}, R0
nonneg:
    ; --- b0 = (|y| >= thr): emit LSB first ---
    MOVE R0, R{data:zero}
    CMP R{state:mag}, R{data:thr}
    BR.N emit_b0
    MOVE R0, R{data:one}
emit_b0:
    {write:out}
    ; --- b1 = sign: emit MSB ---
    MOVE R0, R{state:sign}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples) -> np.ndarray:
        """Reference: signed Q15 level -> two bits (b0 LSB, then b1 MSB) per input,
        matching the on-chip inverse Gray map (LSB-first). Returns a flat bit list
        of length ``2·len(input)``."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        thr = s16(self.threshold_q15)
        out = []
        for v in np.asarray(input_samples).reshape(-1):
            y = s16(v)
            sign = 1 if y < 0 else 0
            mag = -y if y < 0 else y
            magflag = 1 if mag >= thr else 0
            # b0 (LSB) = magnitude flag, b1 (MSB) = sign flag (see class docstring).
            out.append(magflag)   # b0 (LSB) first
            out.append(sign)      # b1 (MSB)
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """No state carried across samples."""
        pass
