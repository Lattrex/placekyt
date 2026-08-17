# SPDX-License-Identifier: GPL-3.0-or-later
"""AGCBlock — see :class:`AGCBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class AGCBlock(KyttarBlock):
    """Automatic Gain Control — mirrors GNU Radio ``analog.agc_ff`` EXACTLY.

    GNU Radio's float AGC loop (gr-analog ``agc_ff`` / ``kernel::agc``)::

        output  = input * gain
        gain   += rate * (reference - |output|)
        if max_gain > 0: gain = min(gain, max_gain)

    Power is approximated by absolute value (``|output|``), exactly as GNU Radio
    documents. Params are GRC-VERBATIM so a flowgraph using ``agc_ff`` ports with
    zero friction; the Q15 fixed-point is derived internally (the GRC-parity rule).

    GAIN UPDATE — FULL-PRECISION ERROR FEEDBACK (fixed 2026-08-16): the naive
    ``gain += MULQ(rate, err)`` STALLS at small rates — MULQ truncation zeroes
    every increment with ``|err| < 2^15/rate_q``, and at GR's DEFAULT rate=1e-4
    (rate_q=3) that is a third of full scale. The stall is DIRECTION-ASYMMETRIC
    (floor truncation zeroes only POSITIVE sub-LSB increments; a negative err
    still steps −1), so only a RISING gain froze — a falling AGC self-repaired
    and hid the bug. Fix = the RMS pair's error-feedback accumulator idiom,
    here in its cheapest exact form: a 32-bit running sum
    ``S = gain<<16 + acc`` stepped by ``prod<<1`` (``prod = rate_q*err``)::

        hi   = MULQ(rate, err)               # prod >> 15 (floor)
        frac = (MUL(rate, err) << 1) & 0xFFFF
        gain += hi;  acc += frac;  gain += carry(acc)   # via ADD / SHL / ADC

    The identity ``prod<<1 == (prod>>15)<<16 + ((lo16<<1) & 0xFFFF)`` is exact
    for MULQ's floor semantics, so NO increment is ever lost: the loop converges
    within ±1 LSB at ANY representable rate (16-bit residual — even finer than
    the RMS 15-bit masked form, and it needs no mask word, which is what lets
    the update fit the single 32-word cell).

    Interface (defaults): entry R1, single input sample in R0.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["agc", "gain", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(
        self,
        name: str,
        rate: float = 1e-4,
        reference: float = 1.0,
        gain: float = 1.0,
        max_gain: float = 0.0,
    ):
        """Initialize AGC block (GNU Radio ``analog.agc_ff`` signature).

        Args:
            name: Block name
            rate: update rate of the loop (GR default 1e-4)
            reference: reference value to adjust signal power to (GR default 1.0)
            gain: initial gain value (GR default 1.0)
            max_gain: maximum gain value; 0 means UNLIMITED (GR default 0)
        """
        super().__init__(name, rate=rate, reference=reference, gain=gain,
                         max_gain=max_gain)
        self._rate = rate
        self._reference = reference
        self._initial_gain = gain
        self._max_gain = max_gain
        self._current_gain = gain

        # Q15 fixed-point (derived; not user-facing). reference/gain/max_gain are
        # magnitudes that may exceed 1.0 in float but the on-chip datapath is Q15
        # [-1,1); clip is applied by float_to_q15. The loop runs at the scale the
        # signal lives at, which for a chip block is Q15.
        self._rate_q15 = float_to_q15(rate)
        self._reference_q15 = float_to_q15(min(reference, 0.999))
        self._gain_q15 = float_to_q15(min(gain, 0.999))
        # max_gain == 0 → unlimited; represent as the Q15 ceiling (0.999).
        self._max_gain_q15 = float_to_q15(0.999 if max_gain <= 0 else
                                          min(max_gain, 0.999))

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def reference(self) -> float:
        return self._reference

    @property
    def max_gain(self) -> float:
        return self._max_gain

    @property
    def gain(self) -> float:
        return self._current_gain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """One-cell AGC mirroring ``agc_ff``:

          out   = in * gain                       (MULQ; emitted immediately —
                                                   the update below only shapes
                                                   the NEXT sample's gain)
          |out| = abs(out)                        (conditional negate)
          err   = reference - |out|
          gain += rate * err                      (EXACT: MULQ high + MUL low
                                                   error-feedback via ADC — see
                                                   the class docstring; a bare
                                                   MULQ stalls at rate=1e-4)
          gain  = min(gain, max_gain)             (clamp high; low floor at 0)

        REGISTER BUDGET (the reclaims that make the exact update fit one cell):
        the output is emitted RIGHT AFTER the MULQ (WRITE preserves R0), which
        frees the old ``out_save``/``abs_save`` states; ``err`` is the only
        scratch state; the 16-bit frac accumulator replaces the RMS masked form
        (no ``mask`` data word, no ``hi``/``t`` scratch — the carry rides the C
        flag into an ADC). 4 data + 3 state + 23 instructions + input R0 = 31.
        ``OR R0, R0`` re-derives the sign flags of ``out`` after the WRITE (no
        reliance on WRITE preserving flags).
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("reference", self._reference_q15, address=2),
                DataWord("rate", self._rate_q15, address=3),
                DataWord("max_gain", self._max_gain_q15, address=4),
            ],
            state=[
                StateVar("gain", initial_value=self._gain_q15),
                StateVar("err"),
                StateVar("acc"),
            ],
            assembly_template="""\
start:
    MULQ R{in:sample}, R{state:gain}
    {write:out}
    OR R0, R0
    BR.NN have_abs
    SUB R{data:zero}, R0
have_abs:
    SUB R{data:reference}, R0
    MOVE R{state:err}, R0
    MULQ R{state:err}, R{data:rate}
    ADD R0, R{state:gain}
    MOVE R{state:gain}, R0
    MUL R{state:err}, R{data:rate}
    SHL R0, #1
    ADD R0, R{state:acc}
    MOVE R{state:acc}, R0
    ADC R{state:gain}, R{data:zero}
    MOVE R{state:gain}, R0
clamp_hi:
    CMP R{state:gain}, R{data:max_gain}
    BR.N clamp_lo
    MOVE R{state:gain}, R{data:max_gain}
clamp_lo:
    CMP R{state:gain}, R{data:zero}
    BR.NN output
    MOVE R{state:gain}, R{data:zero}
output:
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Q15-EXACT reference modelling the on-chip cell (matches simKYT
        bit-for-bit and ≈ GNU Radio ``agc_ff`` within the derived Q15 tolerance).

        Mirrors the cell's integer math EXACTLY: out=MULQ(in,gain) (floor
        semantics — MULQ truncates toward -inf, i.e. arithmetic >>15); |out|;
        err=ref-|out|; the full-precision error-feedback update (hi via MULQ,
        16-bit frac accumulator via MUL<<1, carry folded in via ADC); the
        high/low clamps on the N flag exactly as the branches read it."""
        def s16(v):
            v &= 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):  # Q15*Q15 -> Q15, floor (MULQ truncates toward -inf)
            return s16((s16(a) * s16(b)) >> 15)

        ref = self._reference_q15
        rate = self._rate_q15
        gmax = self._max_gain_q15
        gain = self._gain_q15
        acc = 0
        out = np.zeros(len(input_samples), dtype=np.float32)
        for i, sample in enumerate(input_samples):
            x = float_to_q15(float(sample))
            o = mulq(x, gain)
            out[i] = q15_to_float(o)
            ao = o if o >= 0 else s16(-o)
            err = s16(ref - ao)
            prod = s16(rate) * err               # rate >= 0 in Q15
            hi = prod >> 15                      # MULQ floor
            frac = ((prod & 0xFFFF) << 1) & 0xFFFF
            gain = s16(gain + hi)                # ADD (16-bit wrap)
            t = acc + frac
            acc = t & 0xFFFF
            gain = s16(gain + (t >> 16))         # ADC carry fold
            # clamp_hi: BR.N on (gain - gmax) — clamp when N clear.
            if not ((gain - gmax) & 0x8000):
                gain = gmax
            # clamp_lo: BR.NN on (gain - 0) — floor at 0 when N set.
            if (gain - 0) & 0x8000:
                gain = 0
        self._current_gain = q15_to_float(gain)
        return out

    def reset(self):
        """Reset gain to the initial value."""
        self._current_gain = self._initial_gain
