# SPDX-License-Identifier: GPL-3.0-or-later
"""AddConstBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15


class AddConstBlock(KyttarBlock):
    """
    Add a real constant — drop-in for GNU Radio ``blocks.add_const_ff``:
    ``out[n] = in[n] + const``.

    Single memoryless feed-forward add of an immediate: the constant is baked in as
    a Q15 data word and one ``ADD`` folds it into the sample. The GR parameter name
    is mirrored VERBATIM (``const``). Memoryless → delay=0. One real input, one real
    output.

    Q15 SATURATION (INV-13 / the ComplexGainBlock INV-25 wrap-bug analogue): a Q15
    ``ADD`` WRAPS on overflow (``0.9 + 0.5 = 1.4`` would fold to a sign-flipped
    ``-0.6``), so this block SATURATES instead — an out-of-range sum PINS to
    ±full-scale (``+0x7FFF`` / ``-0x8000``), never wraps. The ADD sets ``V``; on
    overflow the result is rebuilt from the saved operand's sign via the proven
    ``SHR #15; ADD satpos`` restore (the same one AddBlock / the FIR use), reached
    by a FORWARD ``BR.NV`` skip (a back-jump / GOTO onto a ``{write}``/``{jump}``
    placeholder is miscompiled — INV-13).

    GR is pure float and does NOT clip; the drop-in claim is therefore the Q15
    SATURATED output. The DSP-equivalence stimulus keeps ``|in + const| < 1`` (where
    the Q15 result is representable so saturate ≡ the true float sum); the bit-exact
    reference + a dedicated saturation test cover the overflow rails.

    HARDWARE DEVIATION from blocks.add_const_ff: the constant is a Q15 immediate in
    [-1.0, +0.99997], and the output saturates to the Q15 range on overflow (GR
    float neither quantizes nor clips). This is the Q15 ISA limit ([-1, 1)), not a
    convenience — an out-of-range ``const`` raises loudly.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["add_const", "add_const_ff", "bias", "offset", "signal_conditioning"]

    SAT_POS_Q15 = 0x7FFF

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, const: float = 0.0):
        c = float(const)
        if not (-1.0 <= c < 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: const={c} outside the Q15 range [-1.0, +1.0). "
                f"blocks.add_const_ff's constant must fit the Q15 immediate.")
        super().__init__(name, const=c)
        self._const = c
        self._const_q15 = float_to_q15(c) & 0xFFFF

    @property
    def const(self) -> float:
        return self._const

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # const data word placed PAST the input register (R0); satpos past that.
        return {0: CellProgram(
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("const", self._const_q15, address=1),
                DataWord("satpos", self.SAT_POS_Q15, address=2),
            ],
            state=[StateVar("asav")],
            assembly_template="""\
start:
    MOVE R0, R{in:x}
    MOVE R{state:asav}, R0
    ADD R0, R{data:const}
    BR.NV +3
    MOVE R0, R{state:asav}
    SHR R0, #15
    ADD R0, R{data:satpos}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip SATURATING add: ``x + const`` clamped
        to the Q15 range [-32768, 32767]."""
        c = self._s16(self._const_q15)
        out = []
        for x in x_q15:
            acc = self._s16(x) + c
            acc = max(-32768, min(32767, acc))    # saturate (no wrap)
            out.append(acc & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: ``x + const``, clamped to the Q15 range (the drop-in
        claim — GR float itself does not clip)."""
        arr = np.asarray(input_samples).astype(np.float64)
        return np.clip(arr + self._const, -1.0, 32767.0 / 32768.0).astype(np.float32)
