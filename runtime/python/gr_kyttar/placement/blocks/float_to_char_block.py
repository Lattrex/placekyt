# SPDX-License-Identifier: GPL-3.0-or-later
"""FloatToCharBlock — see :class:`FloatToCharBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class FloatToCharBlock(KyttarBlock):
    """Float → signed 8-bit integer, the drop-in for GNU Radio ``blocks.float_to_char``.

    GNU Radio ``float_to_char(vlen=1, scale)`` computes, per sample::

        out = saturate_int8( lrintf(in * scale) )        # int8 in [-128, 127]

    where ``lrintf`` rounds to nearest with **ties to even** (the default IEEE
    rounding mode, ``FE_TONEAREST``). ``out`` is a signed 8-bit integer.

    On this fabric the datapath is Q15: a sample word ``k`` represents the fraction
    ``in = k / 32768`` with ``k`` an integer in ``[-32768, 32767]`` (i.e.
    ``in ∈ [-1.0, +1.0)``). The block computes the SAME function exactly on that
    representation::

        P = k * scale                       # exact integer product
        q = round_half_to_even(P / 2^15)    # nearest integer, ties to even
        out = clamp(q, -128, 127)           # int8 saturation

    and emits ``out`` as a single word (a signed int8 sign-extended into the low
    byte of the 16-bit output word — the same raw-integer output convention the
    slicer blocks use; it is NOT a Q15 fraction).

    Verified BIT-EXACT vs live GNU Radio ``float_to_char`` over the whole Q15 input
    range and all supported scales, INCLUDING exact half-way ties (round-to-even)
    and the ±full-scale saturation edges (see ``test_float_to_char.py``).

    Hardware deviations from blocks.float_to_char (INV-0):
      * **scale must be a non-negative INTEGER.** GNU Radio accepts an arbitrary
        float ``scale``. A non-integer ``scale`` makes ``round(k*scale/2^15)`` land
        on rounding boundaries that the exact integer-product datapath cannot
        reproduce bit-for-bit for every ``k``, so the block would silently disagree
        with GR on some inputs. Rather than ship a "mostly right" converter, the
        block RAISES on a non-integer (or negative) ``scale``. Integer scales are
        the meaningful ones for float→byte conversion (e.g. ``127``/``128`` to map
        a normalised signal to the full int8 range); ``scale`` is still exposed with
        GR's name and default (``1.0``).
      * **Input domain is the Q15 range [-1, 1).** GR sees the true float; the
        fabric input is already Q15-quantised. The comparison drives GR with the
        SAME quantised value the chip sees, so the block is a faithful drop-in for
        any signal already living in the fabric's [-1, 1) domain (the universal
        case — everything on-chip is Q15). ``|in| >= 1`` is not representable and
        cannot be presented to the block.

    Interface:
      * Entry: R1
      * Input: R31 (single Q15 sample)
      * Output: writes the int8 result word to the target's input register
    """
    CATEGORY = "type_conversion"
    TAGS = ["float_to_char", "type_conversion", "quantize", "int8"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, scale: float = 1.0):
        """Initialise the float→char converter.

        Args:
            name: block name.
            scale: GNU Radio ``scale`` — multiplies the input before rounding. Must
                be a non-negative INTEGER (see the HARDWARE DEVIATION in the class
                docstring); a non-integer/negative value RAISES.
        """
        # HARDWARE DEVIATION: GR's scale is an arbitrary float; the Q15 exact-product
        # datapath is bit-exact only for non-negative INTEGER scale. Raise, never
        # silently clamp/approximate (INV-0).
        if scale != int(scale) or scale < 0:
            raise ValueError(
                f"FloatToCharBlock: scale must be a non-negative integer on this Q15 "
                f"fabric (HW-DEVIATION from blocks.float_to_char); got {scale!r}. "
                f"Integer scales (e.g. 1, 127, 128) map the [-1,1) fabric range to int8.")
        super().__init__(name, scale=float(scale))
        self._scale = int(scale)
        if self._scale > 0x7FFF:
            # Guard the 16-bit scale operand register.
            raise ValueError(
                f"FloatToCharBlock: scale {self._scale} exceeds the 16-bit operand "
                f"range; any scale >= 128 already saturates every input to the int8 "
                f"rails, so no useful scale needs more.")

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def scale(self) -> float:
        return float(self._scale)

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Single cell: ``out = clamp(round_half_even(k*scale / 2^15), -128, 127)``.

        ``MULQ k, scale`` yields ``q = (k*scale) >> 15`` — the arithmetic FLOOR of
        ``k*scale / 2^15`` (works for both signs). ``MUL k, scale`` yields the low
        16 bits of the product; masking the low 15 bits gives the discarded
        fraction ``r = (k*scale) mod 2^15``. Round-half-to-even then bumps ``q`` iff
        ``r > 2^14`` OR (``r == 2^14`` AND ``q`` is odd). Finally ``q`` is clamped to
        the int8 range. All branch targets are REAL instructions (never a
        ``{write}``/``{jump}`` placeholder — INV-13 build-engine gotcha), and the
        program falls through linearly to a single terminal ``{write}``/``{jump}``.
        """
        # 5 data words + <=27 instructions fit one cell (32 words total, data low).
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("scale", self._scale & 0xFFFF, address=1),
                DataWord("half", 0x4000, address=2),      # 2^14  (the half LSB)
                DataWord("one", 0x0001, address=3),
                DataWord("pos127", 0x007F, address=4),    # +127
                DataWord("neg128", 0xFF80, address=5),    # -128 (int16)
            ],
            state=[StateVar("q"), StateVar("k")],
            assembly_template="""\
start:
    MOVE R{state:k}, R{in:sample}
    MULQ R{state:k}, R{data:scale}
    MOVE R{state:q}, R0
    MUL R{state:k}, R{data:scale}
    SHL R0, #1
    SHR R0, #1
    CMP R0, R{data:half}
    BR.LT sat
    BR.NZ roundup
    AND R{state:q}, R{data:one}
    BR.Z sat
roundup:
    MOVE R0, R{state:q}
    ADD R0, R{data:one}
    MOVE R{state:q}, R0
sat:
    CMP R{state:q}, R{data:pos127}
    BR.LT chk_lo
    MOVE R{state:q}, R{data:pos127}
chk_lo:
    CMP R{state:q}, R{data:neg128}
    BR.GE emit
    MOVE R{state:q}, R{data:neg128}
emit:
    MOVE R0, R{state:q}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples) -> np.ndarray:
        """Bit-exact reference: the SAME integer-product / round-half-even / int8
        saturate the on-chip datapath computes.

        Accepts either Q15 words (uint16 ints in [0, 0xFFFF]) or floats in [-1, 1);
        floats are Q15-quantised first (matching what the fabric sees). Returns the
        int8 results as an int16 array (the raw output-word convention)."""
        out = []
        for v in np.asarray(input_samples).ravel():
            if isinstance(v, (np.integer, int)) and not isinstance(v, bool) and 0 <= int(v) <= 0xFFFF:
                k = int(v)
                k = k - 0x10000 if k >= 0x8000 else k     # signed int16
            else:
                k = float_to_q15(float(v))
                k = k - 0x10000 if k >= 0x8000 else k
            P = k * self._scale
            q = P >> 15                                   # arithmetic floor
            r = P - (q << 15)                             # in [0, 2^15)
            half = 1 << 14
            if r > half or (r == half and (q & 1)):
                q += 1
            q = max(-128, min(127, q))                    # int8 saturation
            out.append(q)
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """No cross-sample state."""
        pass
