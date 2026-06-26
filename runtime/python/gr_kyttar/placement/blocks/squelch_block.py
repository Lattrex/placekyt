"""SquelchBlock — see :class:`SquelchBlock`."""
import math
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class SquelchBlock(KyttarBlock):
    """Power squelch — mirrors GNU Radio ``analog.pwr_squelch_ff``.

    Gates (zeros) the output when the running signal POWER is below a dB threshold::

        pwr = (1 - alpha) * pwr + alpha * |x|^2     (single-pole power average)
        out = x        if pwr >= 10^(db/10)
        out = 0        otherwise   (gate=False: emit zeros below threshold)

    Params are GRC-VERBATIM (db, alpha, ramp, gate) so a ``pwr_squelch_ff``
    flowgraph ports with zero friction; the linear power threshold + Q15 internals
    are derived (the GRC-parity rule).

    NOT yet supported (documented): ``ramp`` (the sinusoidal attack/release
    envelope) — only ``ramp=0`` is implemented; and ``gate=True`` (drop samples vs
    emit zeros) — only the default ``gate=False`` (emit zeros) is implemented, since
    a chip block emits one output per input. Both raise if set to a non-default.

    Interface (defaults): entry R1, single input sample in R31.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["squelch", "gate", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, db: float = -50.0, alpha: float = 0.0001,
                 ramp: int = 0, gate: bool = False):
        """Initialize power squelch (GNU Radio ``pwr_squelch_ff`` signature).

        Args:
            name: Block name
            db: threshold in dB for power squelch (GR default -50; here we use a
                more demo-friendly default but the param is the GR one)
            alpha: gain of the power averaging filter (GR default 0.0001)
            ramp: attack/release ramp in samples — only 0 (disabled) supported
            gate: True = no output when squelched; only False (emit 0s) supported
        """
        super().__init__(name, db=db, alpha=alpha, ramp=ramp, gate=gate)
        if int(ramp) != 0:
            raise ValueError("SquelchBlock: only ramp=0 is supported "
                             "(the sinusoidal ramp envelope is not implemented)")
        if bool(gate):
            raise ValueError("SquelchBlock: only gate=False (emit zeros) is "
                             "supported; gate=True (drop samples) is not, since a "
                             "chip block emits one output per input")
        self._db = db
        self._alpha = alpha
        self._ramp = int(ramp)
        self._gate = bool(gate)

        # Derived: linear POWER threshold = 10^(db/10). Clip into Q15 [0, 1).
        self._thresh_lin = 10.0 ** (db / 10.0)
        self._thresh_q15 = float_to_q15(min(self._thresh_lin, 0.999))
        self._alpha_q15 = float_to_q15(alpha)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def db(self) -> float:
        return self._db

    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """One-cell power squelch mirroring ``pwr_squelch_ff`` (ramp=0, gate=False):

          p   = |x|^2                          (MULQ x,x)
          pwr += alpha * (p - pwr)             (single-pole average)
          out = x if pwr >= thresh else 0
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("thresh", self._thresh_q15, address=2),
                DataWord("alpha", self._alpha_q15, address=3),
            ],
            state=[
                StateVar("pwr"),
                StateVar("in_save"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    MULQ R{in:sample}, R{in:sample}
    SUB R0, R{state:pwr}
    MULQ R0, R{data:alpha}
    ADD R0, R{state:pwr}
    MOVE R{state:pwr}, R0
    CMP R{state:pwr}, R{data:thresh}
    MOVE R0, R{state:in_save}
    BR.NN emit
    MOVE R0, R{data:zero}
emit:
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Q15-EXACT reference modelling the on-chip cell (matches simKYT bit-for-bit
        and ≈ GNU Radio ``pwr_squelch_ff`` within the derived Q15 tolerance).

        p=MULQ(x,x); pwr += MULQ(alpha, p-pwr); out = x if pwr>=thresh else 0."""
        def s16(v):
            v &= 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):
            return s16((s16(a) * s16(b) + (1 << 14)) >> 15)

        thresh = self._thresh_q15
        alpha = self._alpha_q15
        pwr = 0
        out = np.zeros(len(input_samples), dtype=np.float32)
        for i, sample in enumerate(input_samples):
            x = float_to_q15(float(sample))
            p = mulq(x, x)                       # |x|^2 (x is real → x*x)
            pwr = s16(pwr + mulq(alpha, s16(p - pwr)))
            out[i] = q15_to_float(x) if pwr >= thresh else 0.0
        return out
