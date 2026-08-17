# SPDX-License-Identifier: GPL-3.0-or-later
"""CharToFloatBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class CharToFloatBlock(KyttarBlock):
    """
    Convert an int8 (char) stream to a "float" stream — drop-in for GNU Radio
    ``blocks.char_to_float`` (``out = in / scale``, GR default ``scale = 1``).

    THE SUBSTRATE TRUTH (what "char" and "float" MEAN on this fabric).
    ---------------------------------------------------------------------------
    A Kyttar "float" sample is a **Q15** word: a signed 16-bit numerator ``n``
    interpreted as ``n / 32768`` in the range ``[-1.0, +1.0)`` (INV-0 / the Q15
    ISA range). The GNU Radio ``char_to_float`` input is a raw **int8** in
    ``[-128, +127]``; its output is the plain number ``in / scale``. For that
    output to be a valid fabric "float" it must land inside ``[-1, +1)``:

        out = in / scale ∈ [-1, +1)   ⟺   scale > |in|.

    Since ``|in| ≤ 128`` over the whole int8 domain, ``scale ≥ 128`` makes the
    ENTIRE int8 range representable as a Q15 sample. This is the int8→Q15 ADC /
    soft-bit front-end conversion: a signed byte becomes the fabric's own
    fixed-point sample. The output word emitted is

        out_word = round(in * 32768 / scale)              (clamped to Q15),

    i.e. the Q15 numerator of the float ``in / scale`` — exactly what the
    verification harness compares against ``float_to_q15(gr_out)``.

    DATAPATH (single cell, memoryless → delay 0). The input word carries the
    sign-extended int8 char ``c`` (``c & 0xFFFF``). We compute
    ``out = (c<<8) * B >> 15`` with ``B = round(128 * 32768 / scale)`` (a Q15
    factor ≤ 1 for ``scale ≥ 128``):

        (c<<8) * B >> 15 = c * 256 * B / 32768 = c * (128*32768/scale) / 128
                         = round(c * 32768 / scale).

    ``c<<8`` fits int16 for every int8 ``c`` (``-128<<8 = -0x8000``,
    ``127<<8 = 0x7F00``), so no headroom clip is needed before the MULQ. MULQ
    truncates ``>>15`` where GR's float divide rounds, so a NON-power-of-two
    ``scale`` differs by ≤1 Q15 LSB — inside the derived single-MULQ AMPLITUDE
    floor (``op_count=1`` → 2 LSB). Power-of-two ``scale`` (256, 512, …, 32768)
    is BIT-EXACT.

    Hardware deviations from blocks.char_to_float:
    ---------------------------------------------------------------------------
    HW-DEVIATION (INV-0, Q15 ISA range [-1, 1)): ``scale`` must be ``>= 128``.
    A Kyttar "float" is a Q15 value in [-1, 1); ``out = in/scale`` only fits that
    range for the full int8 input domain when ``scale >= 128``. **GR's DEFAULT
    ``scale = 1`` is therefore NOT representable on this substrate** — it asks for
    outputs up to ``±127.0``, ~127× outside Q15 — and this block RAISES on it
    (it does NOT silently wrap or clamp the semantics away). ``scale = 128`` maps
    the int8 range onto the full [-1, 1) Q15 span; larger ``scale`` shrinks it.
    An out-of-range ``scale`` raises a ``ValueError`` loudly.

    (This is a genuine ISA limit, not convenience: the fabric has no wider
    numeric type than Q15, so a byte's numeric value simply cannot be carried as
    a >1.0 "float". The useful, faithful conversion is int8 → Q15-normalized
    sample, which is exactly ``scale >= 128``.)
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["char_to_float", "type_convert", "int8", "adc", "signal_conditioning"]

    MIN_SCALE = 128.0

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, scale: float = 1.0):
        s = float(scale)
        if not (s >= self.MIN_SCALE):
            raise ValueError(
                f"HARDWARE LIMIT: scale={s} < {self.MIN_SCALE:g}. On the Q15 "
                f"fabric a 'float' sample lives in [-1, 1); char_to_float's "
                f"out=in/scale only fits that range for the full int8 domain "
                f"([-128,127]) when scale >= 128. GR's default scale=1 asks for "
                f"outputs up to +-127.0 (~127x outside Q15) and is NOT "
                f"representable here. Use scale >= 128 (128 maps int8 onto the "
                f"full [-1,1) span).")
        super().__init__(name, scale=s)
        self._scale = s
        # B = round(128 * 32768 / scale), the Q15 factor applied after (c<<8).
        b = int(round(128.0 * 32768.0 / s))
        b = max(-32768, min(32767, b))    # scale=128 -> 32768 clamps to 32767
        self._factor_q15 = b & 0xFFFF

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("factor", self._factor_q15, address=1)],
            state=[StateVar("cw")],
            assembly_template="""\
start:
    SHL R{in:sample}, #8
    MOVE R{state:cw}, R0
    MULQ R{state:cw}, R{data:factor}
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
        """Bit-exact predictor of the on-chip datapath: the input word is a
        sign-extended int8 char ``c``; emit ``clamp_q15((c<<8) * B >> 15)`` with
        ``B`` the stored Q15 factor. This models MULQ's ``>>15`` truncation, so
        it is exact vs the built cell (not the float ideal)."""
        b = self._s16(self._factor_q15)
        out = []
        for w in x_q15:
            c = self._s16(int(w) & 0xFFFF)          # the char, sign-extended
            a = self._s16((c << 8) & 0xFFFF)        # SHL #8 keeps low 16 bits
            prod = a * b                            # signed 32-bit product
            r = prod >> 15                          # MULQ arithmetic >>15
            r = max(-32768, min(32767, r))
            out.append(r & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: ``char / scale`` (GR ``char_to_float``), where the
        input word is interpreted as a sign-extended int8 char. Clipped to the
        Q15 range [-1, 1) — the drop-in claim on THIS substrate (GR float itself
        neither quantizes nor clips, but a fabric 'float' is Q15)."""
        arr = np.asarray(input_samples).astype(np.int64) & 0xFFFF
        chars = np.where(arr >= 0x8000, arr - 0x10000, arr)
        # sign-extend to int8: the char is the low byte
        chars = ((chars & 0xFF) ^ 0x80) - 0x80
        out = chars.astype(np.float64) / self._scale
        return np.clip(out, -1.0, 32767.0 / 32768.0).astype(np.float32)
