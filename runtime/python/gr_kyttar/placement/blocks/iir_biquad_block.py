"""IIRBiquadBlock — see :class:`IIRBiquadBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class IIRBiquadBlock(KyttarBlock):
    """
    IIR Biquad filter block — Direct Form I, GR ``filter.iir_filter_ffd`` parity.

    ``y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1*y[n-1] - a2*y[n-2]``  (a0 = 1)

    Q15 FEEDBACK-COEFFICIENT RANGE — the "half-and-double-MSUQ" trick
    ----------------------------------------------------------------
    A biquad's feedback coefficient ``a1`` is ``-2*cos(omega)``, so for any real
    filter ``|a1|`` can be up to ~2 — which is **NOT representable in Q15** (whose
    range is [-1, +1)). The OLD block clamped ``a`` coeffs to [-1, 1], silently
    turning every sharp filter (poles near the unit circle, |a1|>1) into a
    completely different — wrong — filter. That clamp is the real bug, not the
    architecture.

    The fix is the standard fixed-point-DSP move, and it needs no new ISA: store
    each feedback coefficient HALVED (``a/2``, always representable since the
    halved magnitude is < 1 for |a| < 2) and apply its multiply-subtract TWICE.
    The ISA's ``MSUQ Ra, Rb`` does ``R0 -= (Ra*Rb)>>15`` (architecture_spec_v0.11
    §4.12, MAC opcode MODE=11), and each ``(y * (a/2))>>15`` product is in range,
    so subtracting it twice equals subtracting ``a*y`` with no intermediate
    overflow:

        MSUQ R{y1}, R{a1h}    ; R0 -= (y1 * a1/2) >> 15
        MSUQ R{y1}, R{a1h}    ; R0 -= (y1 * a1/2) >> 15   == R0 -= a1*y1
        MSUQ R{y2}, R{a2h}    ; (a2 is typically < 1 but halved uniformly so the
        MSUQ R{y2}, R{a2h}    ;  same two-MSUQ form covers |a2| up to 2 as well)

    No saturating shift is needed (unlike the FIR gain restore) — the output
    ``y`` of a STABLE biquad is itself bounded (< 1 for a normalized response), so
    the Q15 accumulator stays in range through the whole Direct-Form-I sum. This
    keeps the block a single cell and bit-exactly mirrors GR's accumulation order.

    PRECISION LIMIT (a documented known limit, like the FIR's tap ceiling)
    ---------------------------------------------------------------------
    GR's ``iir_filter_ffd`` uses DOUBLE-precision feedback taps; Q15 (15
    fractional bits) is coarser, and quantization error in the recursive loop
    GROWS as the poles approach the unit circle. Measured vs GR (butterworth-2):
    normalized cutoff 0.10-0.40 → 3-16 LSB (excellent); 0.05 → ~62 LSB
    (marginal); 0.02 (poles within ~0.02 of |z|=1) → ~260 LSB. So the block is
    production-accurate for the common gentle-to-moderate filter range and carries
    a guarded known-limit test for very sharp poles — the same "ship the proven
    range, document the edge" stance as the FIR tap-count ceiling (INV-7).

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["iir", "biquad", "filter", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, b_coeffs: List[float], a_coeffs: List[float]):
        """
        Initialize IIR biquad block.

        Args:
            name: Block name
            b_coeffs: Feedforward coefficients [b0, b1, b2]
            a_coeffs: Feedback coefficients [a1, a2] (a0 is assumed to be 1)
        """
        super().__init__(name, b_coeffs=b_coeffs, a_coeffs=a_coeffs)
        self._b_coeffs = list(b_coeffs)
        self._a_coeffs = list(a_coeffs)

        # Feedforward coeffs are normally |b| <= 1 (a normalized biquad). Convert
        # directly to Q15.
        self._b_q15 = [float_to_q15(b) for b in b_coeffs]
        # Feedback coeffs can have |a| up to ~2 (a1 = -2cos(omega)), which Q15
        # cannot hold. Store them HALVED (representable) and apply each MSUQ TWICE
        # on-chip — see the class docstring. NO clamping: clamping silently
        # corrupts every sharp filter.
        self._a_half_q15 = [float_to_q15(a / 2.0) for a in a_coeffs]

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def b_coefficients(self) -> List[float]:
        return self._b_coeffs

    @property
    def a_coefficients(self) -> List[float]:
        return self._a_coeffs

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Direct-Form-I biquad: y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2.

        The feedback terms use the HALF-AND-DOUBLE-MSUQ trick (see the class
        docstring): each ``a`` is stored halved (``a1h``/``a2h``) and its
        ``MSUQ`` is emitted TWICE, so ``|a|`` up to ~2 (which Q15 can't hold)
        works without a wider accumulator. ``MSUQ Ra,Rb`` is ``R0 -= (Ra*Rb)>>15``
        (architecture_spec_v0.11 §4.12). The accumulation order matches the
        bit-exact reference :meth:`process_reference_q15`.
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("b0", self._b_q15[0], address=1),
                DataWord("b1", self._b_q15[1], address=2),
                DataWord("b2", self._b_q15[2], address=3),
                DataWord("a1h", self._a_half_q15[0], address=4),
                DataWord("a2h", self._a_half_q15[1], address=5),
            ],
            state=[
                StateVar("x1"), StateVar("x2"),
                StateVar("y1"), StateVar("y2"),
                StateVar("x_save"), StateVar("y_save"),
            ],
            assembly_template="""\
start:
    MOVE R{state:x_save}, R{in:sample}
    MULQ R{in:sample}, R{data:b0}
    MACQ R{state:x1}, R{data:b1}
    MACQ R{state:x2}, R{data:b2}
    MSUQ R{state:y1}, R{data:a1h}
    MSUQ R{state:y1}, R{data:a1h}
    MSUQ R{state:y2}, R{data:a2h}
    MSUQ R{state:y2}, R{data:a2h}
    MOVE R{state:y_save}, R0
    MOVE R{state:x2}, R{state:x1}
    MOVE R{state:x1}, R{state:x_save}
    MOVE R{state:y2}, R{state:y1}
    MOVE R{state:y1}, R{state:y_save}
    {write:out}
    {jump:out}
""",
        )}

    # --- Q15 reference (models the hardware datapath EXACTLY) -----------------

    @staticmethod
    def _to_s16(v: int) -> int:
        v &= 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    @classmethod
    def _macq(cls, acc: int, a_q15: int, b_q15: int) -> int:
        """``R0 += (a*b)>>15`` in Q15, WRAPPING in 16 bits (sign-extended) — the
        exact ALU MACQ. (MSUQ is the same with a minus.)"""
        prod = (cls._to_s16(a_q15) * cls._to_s16(b_q15)) >> 15
        return cls._to_s16((acc + prod) & 0xFFFF)

    @classmethod
    def _msuq(cls, acc: int, a_q15: int, b_q15: int) -> int:
        prod = (cls._to_s16(a_q15) * cls._to_s16(b_q15)) >> 15
        return cls._to_s16((acc - prod) & 0xFFFF)

    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 Direct-Form-I biquad, in the SAME accumulation order as
        the built datapath (MULQ b0, MACQ b1/b2, then each feedback term as TWO
        MSUQ of the halved coeff). Every op WRAPS in 16 bits exactly as the ALU
        does; a stable biquad keeps the accumulator in range. Returns one signed
        Q15 int (as a uint16 word) per input sample — the golden predictor the
        verification gate compares the DUT against bit-for-bit."""
        b0q, b1q, b2q = self._b_q15
        a1h, a2h = self._a_half_q15
        x1 = x2 = y1 = y2 = 0
        out = []
        for s in input_q15:
            xq = self._to_s16(int(s) & 0xFFFF)
            acc = (self._to_s16(xq) * self._to_s16(b0q)) >> 15   # MULQ b0
            acc = self._macq(acc, x1, b1q)                       # MACQ b1
            acc = self._macq(acc, x2, b2q)                       # MACQ b2
            acc = self._msuq(acc, y1, a1h)                       # -a1*y1 (half, x2)
            acc = self._msuq(acc, y1, a1h)
            acc = self._msuq(acc, y2, a2h)                       # -a2*y2 (half, x2)
            acc = self._msuq(acc, y2, a2h)
            yq = self._to_s16(acc & 0xFFFF)
            x2, x1 = x1, xq
            y2, y1 = y1, yq
            out.append(yq & 0xFFFF)
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference (legacy / diagnostic). For the bit-exact Q15 predictor
        used by the verification gate, see :meth:`process_reference_q15`."""
        output = np.zeros_like(input_samples, dtype=np.float64)
        x_hist = [0.0, 0.0]  # x[n-1], x[n-2]
        y_hist = [0.0, 0.0]  # y[n-1], y[n-2]

        b0, b1, b2 = self._b_coeffs
        a1, a2 = self._a_coeffs

        for i, x_n in enumerate(input_samples):
            y_n = (b0 * x_n + b1 * x_hist[0] + b2 * x_hist[1]
                   - a1 * y_hist[0] - a2 * y_hist[1])

            # Update history
            x_hist[1] = x_hist[0]
            x_hist[0] = float(x_n)
            y_hist[1] = y_hist[0]
            y_hist[0] = y_n

            output[i] = y_n

        return output.astype(np.float32)

    @classmethod
    def lowpass(cls, name: str, cutoff: float, sample_rate: float, q: float = 0.707) -> 'IIRBiquadBlock':
        """Create a lowpass biquad filter."""
        omega = 2.0 * np.pi * cutoff / sample_rate
        alpha = np.sin(omega) / (2.0 * q)

        b0 = (1.0 - np.cos(omega)) / 2.0
        b1 = 1.0 - np.cos(omega)
        b2 = (1.0 - np.cos(omega)) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        # Normalize by a0
        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])

    @classmethod
    def highpass(cls, name: str, cutoff: float, sample_rate: float, q: float = 0.707) -> 'IIRBiquadBlock':
        """Create a highpass biquad filter."""
        omega = 2.0 * np.pi * cutoff / sample_rate
        alpha = np.sin(omega) / (2.0 * q)

        b0 = (1.0 + np.cos(omega)) / 2.0
        b1 = -(1.0 + np.cos(omega))
        b2 = (1.0 + np.cos(omega)) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])

    @classmethod
    def bandpass(cls, name: str, center: float, bandwidth: float, sample_rate: float) -> 'IIRBiquadBlock':
        """Create a bandpass biquad filter."""
        omega = 2.0 * np.pi * center / sample_rate
        alpha = np.sin(omega) * np.sinh(np.log(2.0) / 2.0 * bandwidth * omega / np.sin(omega))

        b0 = alpha
        b1 = 0.0
        b2 = -alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])
