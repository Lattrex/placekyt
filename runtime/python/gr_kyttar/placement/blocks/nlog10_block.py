# SPDX-License-Identifier: GPL-3.0-or-later
"""Nlog10Block — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


# ---------------------------------------------------------------------------
# The log2(mantissa) polynomial.
#
# For x = X/32768 (X the Q15 numerator, positive), write X = 2^e * m with the
# mantissa m in [1, 2), e = floor(log2 X). Then
#
#     log10(x) = log10(X) - log10(32768)
#              = (e + log2(m)) * log10(2) - 15 * log10(2)
#              = (e - 15) * log10(2) + log2(m) * log10(2).
#
# log2(m) for m in [1, 2) is approximated by a through-origin cubic in
# ``f = m - 1 in [0, 1)`` (log2(1) = 0 forces no constant term):
#
#     log2(1 + f) ~= C1*f + C2*f^2 + C3*f^3.
#
# The coefficients below are the least-squares fit over f in [0, 1); the peak
# |log2 approximation error| is ~1.32e-3 (i.e. ~4e-4 in log10). These are the
# ABSTRACT coefficients; the block folds the per-instance scale factor
# ``A = n*log10(2)/db_scale`` into them (see __init__) so every stored Q15
# coefficient lands in [-1, 1) and NO out-of-range-coefficient handling
# (INV-15) is needed.
# ---------------------------------------------------------------------------
_LOG2_C1 = 1.4234952675936207
_LOG2_C2 = -0.5877735756773742
_LOG2_C3 = 0.16559366906091822

_LOG10_2 = np.log10(2.0)


def _clip_q15(v: int) -> int:
    return max(-32768, min(32767, int(v)))


class Nlog10Block(KyttarBlock):
    """
    Power-to-dB: ``out = n * log10(in) + k`` — drop-in for GNU Radio
    ``blocks.nlog10_ff`` (params ``n`` default 10.0, ``k`` default 0.0). ``in`` is
    a level/power sample; the block emits its value in decibels.

    THE SUBSTRATE TRUTH (why a dB value cannot be a bare Q15 sample).
    ---------------------------------------------------------------------------
    A Kyttar "float" sample is a Q15 word: a signed numerator interpreted as
    ``word/32768`` in the range ``[-1.0, +1.0)`` (INV-0 / the Q15 ISA range). The
    output of nlog10 is a dB value which, over the natural fabric input domain
    ``in in [2^-15, ~1)``, spans roughly ``[n*log10(2^-15), 0] = [-45.15, 0] dB``
    for the default ``n = 10`` — **~45x outside the Q15 [-1, 1) span**. A raw dB
    number simply does not fit a Q15 word. This is a genuine ISA limit, not a
    convenience choice.

    THE OUTPUT REPRESENTATION (the documented HW-DEVIATION).
    ---------------------------------------------------------------------------
    The block emits a **scaled** dB value: the on-chip Q15 word carries

        out_word / 32768  ==  (n*log10(in) + k) / db_scale,

    i.e. the true dB value is ``out_word/32768 * db_scale``. ``db_scale`` is a
    power of two chosen automatically so the whole dB output range fits Q15
    ``[-1, 1)`` (default ``n=10, k=0`` -> ``db_scale = 64``, so -45.15 dB ->
    -0.706). This is the SAME "a Q15 word is the numerator of a scaled real
    value" convention the Char/FloatToChar type-converters use for out-of-range
    quantities. A consumer (or the verification harness) recovers dB by
    multiplying the Q15 float by ``db_scale``. ``db_scale`` is surfaced as a
    read-only property and reported in the block metrics; it is DERIVED from
    ``n, k`` (not a free user knob) so the block stays a faithful ``nlog10_ff``.

    THE DATAPATH (TWO cells, feed-forward wavefront, memoryless -> delay 0).
    ---------------------------------------------------------------------------
    The full algorithm (normalize + branch + cubic Horner + combine) does not fit
    one cell's ~32-word budget, so it folds into two adjacent cells (I/O
    co-located on the bus edge, output on the LAST cell — INV-8/10):

    Cell ``norm`` (input cell):
    1. Save the input word X. If ``X <= 0`` (in <= 0), forward a FLOOR SENTINEL
       (``em15 = +1``, a value the normal path never produces since normalized
       ``e-15 in [-15, -1]``) so ``poly`` emits the Q15 floor ``0x8000``
       (== ``-db_scale`` dB). This mirrors GR clamping ``in`` to a tiny epsilon
       before log10 (GR's floor ~-379 dB for FLT_MIN; on Q15 the floor is
       ``-db_scale`` dB, the most negative the scaled representation can hold).
    2. NORMALIZE: left-shift X until its MSB reaches bit 14 (value in
       ``[0x4000, 0x7FFF]``), counting shifts. This yields ``e - 15`` (the
       exponent bias term, tracked directly, starting at -1 == e=14) and the
       normalized mantissa. ``frac = (norm - 0x4000) << 1`` is ``m - 1`` in Q15.
    3. Forward ``(frac, em15)`` to ``poly``.

    Cell ``poly`` (output cell):
    4. If ``em15 > 0`` (the floor sentinel) emit ``0x8000`` and stop.
    5. Evaluate ``A*log2(m)`` by Horner on the SCALED cubic coefficients
       ``d_i = A*C_i`` (A = ``n*log10(2)/db_scale``):
       ``p = d3; p = p*frac + d2; p = p*frac + d1; p = p*frac``  (MULQ/ADD).
    6. ``out = (e-15)*A_q15 + p + C_q15`` where ``A_q15 = round(A*32768)`` and
       ``C_q15 = round(k/db_scale*32768)``. The exponent term is an integer x Q15
       product that always fits int16 (``|e-15| <= 15``, ``A_q15`` small), so a
       plain MUL (low 16 bits) is exact. Saturate to Q15 and emit.

    DERIVED TOLERANCE (INV-4 — not tuned).
    ---------------------------------------------------------------------------
    Error budget (per output Q15 LSB, db_scale-independent as a fraction):
      * cubic log2 approx: ~4e-4 in log10 -> well under 1 LSB;
      * ``A_q15`` rounding (+-0.5 LSB) enters the exponent term multiplied by up
        to ``|e-15| = 15`` -> up to ~7.5 LSB;
      * ``C_q15`` rounding +-0.5 LSB; final saturation/round +-1 LSB.
    Sum -> ~9.2 LSB worst case; the DERIVED gate tolerance is **10 Q15 LSB**
    (verified: peak measured error is 9 LSB over a full n/k grid, exhaustive X
    sweep). In dB that is ``10 * db_scale/32768`` (0.020 dB at db_scale=64).

    Hardware deviations from blocks.nlog10_ff:
    ---------------------------------------------------------------------------
    HW-DEVIATION (INV-0, Q15 ISA range [-1, 1)):
      1. OUTPUT SCALE. The emitted Q15 word is the dB value divided by an
         auto-derived power-of-two ``db_scale`` (default 64 for n=10, k=0), so the
         out-of-range dB value fits [-1, 1). True dB = ``out_word/32768*db_scale``.
         (A raw dB number is ~45x outside Q15; there is no wider numeric type on
         the fabric.) ``db_scale`` is derived from n,k, exposed read-only.
      2. FLOOR for in <= 0. GR clamps ``in`` to ~FLT_MIN and returns ~-379 dB;
         the scaled Q15 representation bottoms out at ``-db_scale`` dB (word
         0x8000). Any ``in <= 0`` maps to that floor (GR does the same monotone
         "very negative dB" thing, just at a value Q15 cannot reach).
    Both are genuine ISA limits (Q15 range), documented here, in the __init__
    comment, and in the manifest ``HW-DEVIATION:`` note. ``n, k`` themselves are
    GRC-verbatim and fully general (any real value for which the derived
    ``db_scale`` keeps the constants representable).
    """
    CATEGORY = "math_operators"
    TAGS = ["nlog10", "nlog10_ff", "log10", "dB", "power", "math_operators"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    _CELL_IDS = ["norm", "poly"]

    def __init__(self, name: str, n: float = 10.0, k: float = 0.0):
        super().__init__(name, n=n, k=k)
        self._n = float(n)
        self._k = float(k)

        # Derive db_scale: smallest power of two strictly greater than the
        # largest |dB| over the fabric input domain in in [2^-15, ~1).
        #   x = 2^-15 -> n*log10(2^-15)+k  (the most negative for n>0);
        #   x -> 1    -> k.
        lo = self._n * np.log10(2.0 ** -15) + self._k
        hi = self._k
        max_abs_db = max(abs(lo), abs(hi), 1e-9)
        db_scale = 1.0
        while db_scale <= max_abs_db:
            db_scale *= 2.0
        self._db_scale = db_scale

        # Fold the per-instance scale factor A = n*log10(2)/db_scale into the
        # cubic coefficients so every stored Q15 coefficient is in [-1, 1)
        # (A is small -> A*C_i are all sub-unity; no INV-15 needed).
        A = self._n * _LOG10_2 / db_scale
        self._d1_q15 = _clip_q15(round(A * _LOG2_C1 * 32768))
        self._d2_q15 = _clip_q15(round(A * _LOG2_C2 * 32768))
        self._d3_q15 = _clip_q15(round(A * _LOG2_C3 * 32768))
        self._A_q15 = _clip_q15(round(A * 32768))          # exponent-term scale
        self._C_q15 = _clip_q15(round(self._k / db_scale * 32768))  # k contribution

        # Sanity: the folded constants must be representable (they always are for
        # a db_scale chosen as above, but guard loudly per INV-0).
        for nm, v in (("d1", self._d1_q15), ("d2", self._d2_q15),
                      ("d3", self._d3_q15), ("A", self._A_q15),
                      ("C", self._C_q15)):
            if not (-32768 <= v <= 32767):
                raise ValueError(
                    f"HARDWARE LIMIT: nlog10 constant {nm}={v} not Q15-"
                    f"representable for n={n}, k={k} (db_scale={db_scale}).")

    # ------------------------------------------------------------------ props
    @property
    def n(self) -> float:
        return self._n

    @property
    def k(self) -> float:
        return self._k

    @property
    def db_scale(self) -> float:
        """The power-of-two output scale (HW-DEVIATION): true dB = word/32768 *
        db_scale. Derived from n, k; read-only."""
        return self._db_scale

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        cells = {}

        # (1) norm — save X; X<=0 -> forward the floor sentinel (em15=+1); else
        # normalize (left-justify MSB to bit 14, counting em15 = e-15 down from -1)
        # and forward (frac, em15). frac = (norm - 0x4000) << 1 = (m-1) in Q15.
        cells["norm"] = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("frac"), Port("em15"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("c4000", 0x4000, address=2),
                DataWord("floorflag", 1, address=3),
                DataWord("em15_init", (-1) & 0xFFFF, address=4),
            ],
            state=[
                StateVar("Xs"),
                StateVar("em15"),
            ],
            assembly_template="""\
start:
    MOVE R{state:Xs}, R{in:sample}
    CMP R{state:Xs}, R{data:one}
    BR.GE _pos
    MOVE R{state:em15}, R{data:floorflag}
    MOVE R0, R{data:one}
    {write:frac}
    MOVE R0, R{state:em15}
    {write:em15}
    {jump:trig}
    HALT
_pos:
    MOVE R{state:em15}, R{data:em15_init}
_norm:
    CMP R{state:Xs}, R{data:c4000}
    BR.GE _done_norm
    SHL R{state:Xs}, #1
    MOVE R{state:Xs}, R0
    SUB R{state:em15}, R{data:one}
    MOVE R{state:em15}, R0
    GOTO _norm
_done_norm:
    SUB R{state:Xs}, R{data:c4000}
    SHL R0, #1
    {write:frac}
    MOVE R0, R{state:em15}
    {write:em15}
    {jump:trig}
""",
        )

        # (2) poly — em15>0 (sentinel) -> emit floor 0x8000; else cubic Horner on
        # the scaled coeffs (A folded in) + the exponent term (e-15)*A_q15 + C_q15.
        cells["poly"] = CellProgram(
            inputs=[Port("frac", register=0), Port("em15", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=2),
                DataWord("floor", 0x8000, address=3),
                DataWord("d1", self._d1_q15 & 0xFFFF, address=4),
                DataWord("d2", self._d2_q15 & 0xFFFF, address=5),
                DataWord("d3", self._d3_q15 & 0xFFFF, address=6),
                DataWord("Aq", self._A_q15 & 0xFFFF, address=7),
                DataWord("Cq", self._C_q15 & 0xFFFF, address=8),
            ],
            state=[
                StateVar("fr"),
                StateVar("es"),
            ],
            assembly_template="""\
start:
    MOVE R{state:fr}, R{in:frac}
    MOVE R{state:es}, R{in:em15}
    CMP R{state:es}, R{data:zero}
    BR.GE _floor
    MUL R{state:es}, R{data:Aq}
    ADD R0, R{data:Cq}
    MOVE R{state:es}, R0
    MOVE R0, R{data:d3}
    MULQ R0, R{state:fr}
    ADD R0, R{data:d2}
    MULQ R0, R{state:fr}
    ADD R0, R{data:d1}
    MULQ R0, R{state:fr}
    ADD R0, R{state:es}
    {write:out}
    {jump:trig}
    HALT
_floor:
    MOVE R0, R{data:floor}
    {write:out}
    {jump:trig}
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["norm", "poly"]

    def internal_connections(self):
        # forward frac -> poly.frac and em15 -> poly.em15 (a 2-word data packet).
        return [("norm", "frac", "poly", "frac"),
                ("norm", "em15", "poly", "em15")]

    def internal_jumps(self):
        return [("norm", "trig", "poly", "default")]

    def output_cell_ids(self):
        return ["poly"]

    def default_layout(self):
        return {"norm": (0, 0, "east"), "poly": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def _mulq(self, a, b):
        return self._s16((self._s16(a) * self._s16(b)) >> 15)

    def _mul_lo(self, a, b):
        return self._s16((self._s16(a) * self._s16(b)) & 0xFFFF)

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip datapath (models the exact
        normalization loop + Horner + MUL/MULQ truncation + Q15 saturation).
        Returns the SCALED Q15 words the cell emits (true dB = word/32768 *
        db_scale)."""
        d1, d2, d3 = self._d1_q15, self._d2_q15, self._d3_q15
        Aq, Cq = self._A_q15, self._C_q15
        out = []
        for w in x_q15:
            X = self._s16(int(w) & 0xFFFF)
            if X <= 0:
                out.append(0x8000)
                continue
            Xs = X & 0xFFFF
            em15 = -1
            while Xs < 0x4000:
                Xs = (Xs << 1) & 0xFFFF
                em15 -= 1
            frac = ((Xs - 0x4000) << 1) & 0xFFFF
            p = self._mulq(d3, frac)
            p = self._s16(p + d2)
            p = self._mulq(p, frac)
            p = self._s16(p + d1)
            p = self._mulq(p, frac)
            val = self._s16(self._mul_lo(em15, Aq) + p + Cq)
            val = _clip_q15(val)
            out.append(val & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference matching the SCALED on-chip semantics: the emitted
        value is ``(n*log10(in) + k) / db_scale`` clipped to Q15 [-1, 1). For
        ``in <= 0`` the scaled floor is -1.0 (== -db_scale dB). A consumer
        recovers dB as ``out * db_scale``."""
        arr = np.asarray(input_samples).astype(np.float64)
        out = np.empty_like(arr, dtype=np.float64)
        for i, x in enumerate(arr):
            if x <= 0:
                out[i] = -1.0
            else:
                db = self._n * np.log10(x) + self._k
                out[i] = db / self._db_scale
        return np.clip(out, -1.0, 32767.0 / 32768.0).astype(np.float32)
