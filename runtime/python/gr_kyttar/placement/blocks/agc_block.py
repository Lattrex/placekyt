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

    Interface (defaults): entry R1, single input sample in R31.
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

          out   = in * gain                       (MULQ)
          |out| = abs(out)                        (conditional negate)
          err   = reference - |out|
          gain += rate * err                      (MULQ then ADD)
          gain  = min(gain, max_gain)             (clamp high; low floor at 0)
          emit out
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
                StateVar("out_save"),
                StateVar("abs_save"),
            ],
            assembly_template="""\
start:
    MULQ R{in:sample}, R{state:gain}
    MOVE R{state:out_save}, R0
    CMP R0, R{data:zero}
    BR.NN have_abs
    SUB R{data:zero}, R0
have_abs:
    MOVE R{state:abs_save}, R0
    MOVE R0, R{data:reference}
    SUB R0, R{state:abs_save}
    MULQ R0, R{data:rate}
    ADD R0, R{state:gain}
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
    MOVE R0, R{state:out_save}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Q15-EXACT reference modelling the on-chip cell (matches simKYT bit-for-bit
        and ≈ GNU Radio ``agc_ff`` within the derived Q15 tolerance).

        Mirrors the cell's integer math: out=MULQ(in,gain); |out|; err=ref-|out|;
        gain += MULQ(rate,err); clamp gain to [0, max_gain]. The error and gain are
        carried in Q15 integers, exactly as the datapath does."""
        def s16(v):
            v &= 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):  # Q15 * Q15 -> Q15 with round-to-nearest (matches MULQ)
            return s16((s16(a) * s16(b) + (1 << 14)) >> 15)

        ref = self._reference_q15
        rate = self._rate_q15
        gmax = self._max_gain_q15
        gain = self._gain_q15
        out = np.zeros(len(input_samples), dtype=np.float32)
        for i, sample in enumerate(input_samples):
            x = float_to_q15(float(sample))
            o = mulq(x, gain)
            out[i] = q15_to_float(o)
            ao = o if o >= 0 else s16(-o)
            err = s16(ref - ao)
            gain = s16(gain + mulq(rate, err))
            if gain > gmax:
                gain = gmax
            if gain < 0:
                gain = 0
        self._current_gain = q15_to_float(gain)
        return out

    def reset(self):
        """Reset gain to the initial value."""
        self._current_gain = self._initial_gain
