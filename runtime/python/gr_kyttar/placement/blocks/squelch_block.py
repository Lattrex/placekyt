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

    ``ramp`` (the raised-cosine attack/release envelope, GR-exact) IS supported: on
    the gate opening/closing the output is multiplied by a per-sample envelope
    ``0.5 - 0.5·cos(π·k/ramp)`` where the ramp counter ``k`` advances 0→ramp while
    unmuted and ramp→0 while muted (the envelope is applied at the CURRENT counter
    BEFORE it updates — bit-for-bit with ``pwr_squelch_ff``). A ``ramp`` table of
    ``ramp+1`` Q15 entries lives in the cell, so ``ramp`` is bounded by the cell
    budget (HARDWARE limit ~24, raises above — documented + loud).

    ``gate=True`` (DROP squelched samples instead of emitting zeros) is a HARDWARE
    DEVIATION — unsupported and RAISES — because a chip block emits exactly one
    output per input (it cannot drop a sample from the stream).

    Interface (defaults): entry R1, single input sample in R31.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["squelch", "gate", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # HARDWARE LIMIT: ramp uses a 2-cell pipeline (squelch cell + ramp/envelope
    # cell). The ramp cell holds a (ramp+1)-entry raised-cosine envelope table plus
    # the counter/LOAD/MULQ/emit/clamp program; that fits one 32-word cell only up
    # to ramp=4 (measured: ramp 1..4 build + match GR ≤1 LSB; ramp 5 overflows the
    # cell). A larger ramp would need a 3rd cell. Raises above MAX_RAMP, loudly.
    MAX_RAMP = 4

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
        ramp = int(ramp)
        if ramp < 0:
            raise ValueError(f"SquelchBlock: ramp must be >= 0, got {ramp}")
        # HARDWARE LIMIT: the ramp envelope table (ramp+1 Q15 entries) + the rest
        # of the cell program must fit one 32-word cell -> ramp <= MAX_RAMP. Raise
        # loudly above it (compose / use a smaller ramp) rather than mis-build.
        if ramp > self.MAX_RAMP:
            raise ValueError(
                f"HARDWARE LIMIT: SquelchBlock ramp={ramp} exceeds the max "
                f"{self.MAX_RAMP} (the ramp+1-entry raised-cosine envelope table "
                f"plus the squelch program must fit one 32-word cell).")
        if bool(gate):
            raise ValueError(
                "HARDWARE LIMIT: SquelchBlock gate=True (drop squelched samples) is "
                "unsupported; gate=False (emit zeros) only, since a chip block emits "
                "exactly one output per input and cannot drop a sample.")
        self._db = db
        self._alpha = alpha
        self._ramp = ramp
        self._gate = bool(gate)

        # Derived: linear POWER threshold = 10^(db/10). Clip into Q15 [0, 1).
        self._thresh_lin = 10.0 ** (db / 10.0)
        self._thresh_q15 = float_to_q15(min(self._thresh_lin, 0.999))
        self._alpha_q15 = float_to_q15(alpha)
        # Raised-cosine ramp envelope: env[k] = 0.5 - 0.5·cos(π·k/ramp), k=0..ramp.
        self._ramp_env_q15 = (
            [float_to_q15(0.5 - 0.5 * math.cos(math.pi * k / ramp))
             for k in range(ramp + 1)] if ramp > 0 else [])

    @property
    def cell_count(self) -> int:
        # ramp>0 needs a 2nd cell for the envelope table + counter (the squelch
        # math + the ramp logic + the table do not fit one 32-word cell).
        return 2 if self._ramp > 0 else 1

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
        """One-cell power squelch mirroring ``pwr_squelch_ff``.

          p   = |x|^2                          (MULQ x,x)
          pwr += alpha * (p - pwr)             (single-pole average)
          unmuted = pwr >= thresh
          ramp=0: out = x if unmuted else 0
          ramp>0: out = x · env[k]; then k advances toward ramp (unmuted) / 0 (muted)
        """
        if self._ramp > 0:
            return self._build_ramp_programs()
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

    def _build_ramp_programs(self) -> Dict[int, CellProgram]:
        """Power squelch WITH the raised-cosine ramp (GR pwr_squelch_ff, ramp>0) —
        a 2-cell pipeline (the squelch math + ramp logic + envelope table do not
        fit one 32-word cell):

          cell 0 (squelch): pwr += alpha·(|x|^2 - pwr); unmuted = pwr>=thresh;
                            forward (x, unmuted) to cell 1.
          cell 1 (ramp):    out = x · env[k]; then k advances toward ramp (unmuted)
                            or 0 (muted) — env applied at the CURRENT k BEFORE the
                            update, bit-for-bit with GR (envelope-then-update).
        """
        R = self._ramp
        squelch_cell = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("x_fwd"), Port("um_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1),
                  DataWord("one", 1, address=2),
                  DataWord("thresh", self._thresh_q15, address=3),
                  DataWord("alpha", self._alpha_q15, address=4)],
            state=[StateVar("pwr"), StateVar("in_save")],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    MULQ R{in:sample}, R{in:sample}
    SUB R0, R{state:pwr}
    MULQ R0, R{data:alpha}
    ADD R0, R{state:pwr}
    MOVE R{state:pwr}, R0
    MOVE R0, R{state:in_save}
    {write:x_fwd}
    CMP R{state:pwr}, R{data:thresh}
    MOVE R0, R{data:zero}
    BR.N emitum
    MOVE R0, R{data:one}
emitum:
    {write:um_fwd}
    {jump:trig}
""",
        )
        # Envelope table at addrs 1..R+1 (LOAD addr = counter + 1).
        env = [DataWord(f"e{k}", v, address=1 + k)
               for k, v in enumerate(self._ramp_env_q15)]
        rbase = 1 + len(self._ramp_env_q15)
        ramp_cell = CellProgram(
            inputs=[Port("x", register=0), Port("um", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=env + [
                DataWord("one", 1, address=rbase),
                DataWord("zero", 0, address=rbase + 1),
                DataWord("ramp", R, address=rbase + 2),
            ],
            state=[StateVar("xs"), StateVar("ums"), StateVar("cnt")],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:x}
    MOVE R{state:ums}, R{in:um}
    ADD R{state:cnt}, R{data:one}
    LOAD R0
    MULQ R0, R{state:xs}
    {write:out}
    {jump:out}
    CMP R{state:ums}, R{data:zero}
    BR.Z muted
    CMP R{state:cnt}, R{data:ramp}
    BR.NN done
    ADD R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    HALT
muted:
    CMP R{state:cnt}, R{data:zero}
    BR.NP done
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
done:
    HALT
""",
        )
        return {"0": squelch_cell, "1": ramp_cell}

    def internal_connections(self):
        if self._ramp == 0:
            return []
        return [("0", "x_fwd", "1", "x"), ("0", "um_fwd", "1", "um")]

    def internal_jumps(self):
        if self._ramp == 0:
            return []
        return [("0", "trig", "1", "default")]

    def output_cell_ids(self):
        return ["1"] if self._ramp > 0 else [0]

    def default_layout(self):
        if self._ramp == 0:
            return {0: (0, 0, "east")}
        return {"0": (0, 0, "east"), "1": (1, 0, "east")}

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
        R = self._ramp
        env = self._ramp_env_q15
        pwr = 0
        cnt = 0
        out = np.zeros(len(input_samples), dtype=np.float32)
        for i, sample in enumerate(input_samples):
            x = float_to_q15(float(sample))
            p = mulq(x, x)                       # |x|^2 (x is real → x*x)
            pwr = s16(pwr + mulq(alpha, s16(p - pwr)))
            unmuted = pwr >= thresh
            if R == 0:
                out[i] = q15_to_float(x) if unmuted else 0.0
            else:
                # envelope-then-update (bit-for-bit with pwr_squelch_ff)
                out[i] = q15_to_float(mulq(x, env[cnt]))
                if unmuted:
                    if cnt < R:
                        cnt += 1
                else:
                    if cnt > 0:
                        cnt -= 1
        return out
