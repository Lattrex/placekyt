# SPDX-License-Identifier: GPL-3.0-or-later
"""RMSBlock / RMSCFBlock — see the class docstrings.

The two GNU Radio RMS blocks (``blocks.rms_ff`` and ``blocks.rms_cf``) share one
datapath: a single-pole IIR power average followed by a square root. This module
holds the SHARED core (the IIR cell tail + the 3-cell sqrt pipeline: normalize ->
quartic polynomial -> denormalize) and the two thin fronts (x**2 for the real
block, re**2 + im**2 for the complex block).

GNU RADIO SEMANTICS (pinned against LIVE GR, 2026-08-16):
    avg = (1 - alpha)*avg + alpha*|x[n]|**2      (avg starts at 0)
    out[n] = sqrt(avg)                            (sqrt AFTER the update)
so the first output is ``sqrt(alpha * |x[0]|**2)``. Verified to ~1e-8 against
``blocks.rms_ff`` / ``blocks.rms_cf`` on live GNU Radio.
"""
import math

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


# ---------------------------------------------------------------------------
# The sqrt(mantissa) polynomial.
#
# The power word Y (0..32767) is normalized by counting left shifts s until the
# mantissa m' lands in [0x4000, 0x7FFF] (value [0.5, 1)); then
#
#     sqrt(Y/2^15) = sqrt(m'/2^15) * 2^(-s/2)
#
# and the fractional part f = (m'/2^15 - 0.5)*2 in [0, 1) drives a QUARTIC
# least-squares fit of sqrt(0.5 + f/2):
#
#     sqrt(0.5 + f/2) ~= c4*f^4 + c3*f^3 + c2*f^2 + c1*f + c0.
#
# Every coefficient is sub-unity (Q15-representable, no INV-15 handling); the
# peak fit error is 1.61e-5 (0.53 Q15 LSB). The denormalize step shifts right by
# floor(s/2) and multiplies by 1/sqrt(2) when s is odd. Exhaustively verified
# over ALL 32768 power words: on-chip sqrt error in [-4.5, +0.6] LSB.
#
# The coefficients below are round(c * 32768) of the numpy LSQ fit over 20001
# points of f in [0, 1] (frozen so the block is deterministic without numpy's
# polyfit at import time).
# ---------------------------------------------------------------------------
_SQRT_C4 = -238
_SQRT_C3 = 1031
_SQRT_C2 = -2765
_SQRT_C1 = 11568
_SQRT_C0 = 23171
_INV_SQRT2_Q15 = 23170          # round(2**-0.5 * 32768)

# The norm cell forwards s = _S_ZERO_SENTINEL for a zero power word: parity even,
# floor(s/2) = 15 right-shifts turn any polynomial output (< 2^15) into 0, so the
# downstream cells need NO dedicated zero branch.
_S_ZERO_SENTINEL = 30


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class _RMSCoreBlock(KyttarBlock):
    """Shared core of RMSBlock / RMSCFBlock (not registered as a catalog block —
    subclasses provide the power front; this class is abstract via the missing
    ``_front_*`` hooks on direct use).

    DATAPATH (4 cells, feed-forward chain, folded 2x2 per INV-8/14):

    Cell ``pwr`` (input cell) — the per-sample power + the IIR average:
      1. FRONT (subclass): compute the instantaneous power p = |x|^2 >= 0 in R0,
         saturating the Q15-unrepresentable corner (|x|^2 >= 1) to 0x7FFF.
      2. IIR with FULL-PRECISION ERROR FEEDBACK. The GR update
         ``avg += alpha*(p - avg)`` dies in bare Q15 for small alpha: with
         ``MULQ(alpha_q, d)`` truncation the increment is 0 for every
         ``|d| < 2^15/alpha_q`` LSB, so the average stalls up to 10923 LSB short
         at GR's DEFAULT alpha=1e-4 (alpha_q=3). The fix is a 30-bit accumulator
         S = y*2^15 + acc_lo kept as two 16-bit words:
             d      = p - y                       (16-bit, always in range)
             hi     = (alpha_q*d) >> 15           (MULQ, truncates toward -inf)
             lo15   = (alpha_q*d) & 0x7FFF        (MUL low half, masked)
             t      = acc_lo + lo15               (<= 0xFFFE, no wrap)
             y     += hi + (t >> 15);  acc_lo = t & 0x7FFF
         Because alpha_q*d = (hi << 15) + lo15 EXACTLY (floor-division identity),
         no increment is ever lost: the average converges to the true mean power
         within +-1 LSB at ANY representable alpha. y stays in [0, 32767] by
         construction (the update is a convex step toward p, proven in the module
         tests' exhaustive bounds).
      3. Forward y to the sqrt pipeline.

    Cell ``norm`` — left-shift y until >= 0x4000 counting s (y=0 forwards f=0
    with the s=30 sentinel; a zero word would otherwise shift forever). Forwards
    f = (m' - 0x4000) << 1 and s.

    Cell ``poly`` — quartic Horner on the frozen coefficients (all MULQ/ADD, all
    intermediates in range). Forwards (p, s).

    Cell ``denorm`` (output cell) — multiplies by 1/sqrt(2) when s is odd, then
    shifts right floor(s/2) times with a SHR-#1 counter loop (shift counts are
    immediate fields — INV-34; a data-dependent count MUST loop). Emits the RMS
    word.

    LAYOUT: 2x2 serpentine fold (INV-14 even-column): pwr(0,0) -> norm(0,1) ->
    poly(1,1) -> denorm(1,0). Input cell (0,0) and output cell (1,0) co-locate on
    the top edge (INV-8). Feed-forward only — no feedback corridor, no
    reconvergent fan-in — so no serialize-LOCK is needed (INV-19/20 N/A) and the
    block is freely orientable (faces come from default_layout only).

    Hardware deviations from blocks.rms_ff / blocks.rms_cf:
    -------------------------------------------------------------------------
    HW-DEVIATION (INV-0, Q15 ISA):
      1. ALPHA IS QUANTIZED TO Q15: the on-chip filter runs at
         alpha_eff = round(alpha*32768)/32768. GR's default alpha=1e-4 becomes
         3/32768 ~= 9.155e-5 (8.4% off). The SETTLED RMS value is
         alpha-independent (it is the mean power), so the settled output still
         equals GR's; only the transient time constant shifts by the same ~8%.
         alpha with round(alpha*32768) == 0 (alpha < ~1.5e-5) cannot run the
         filter at all and RAISES; alpha outside (0, 1] RAISES (alpha=1.0 clips
         to 32767/32768, ~3e-5 short of GR's no-memory limit).
      2. Q15 RANGE: instantaneous power >= 1.0 (|x| = 1, or |z|^2 >= 1 for the
         complex block, up to 2.0) saturates to 0x7FFF, so a full-scale input
         settles to sqrt(32767/32768) (word ~32765) where GR reports values up
         to sqrt(2). GR-equivalence stimulus therefore stays inside |z| < 1;
         the saturating corner is pinned bit-exact against the block's own
         reference.

    DERIVED TOLERANCE (settled-tail vs live GR, NOT tuned — see test_rms.py):
      sqrt path (fit + Q15 Horner + denorm, exhaustive bound)   <= 4.5 LSB
      settled power error (x^2 MULQ truncation bias + error-
        feedback dither + input quantization)                   <= 2.5 LSB
      amplified through d(sqrt)/dY = 90.5/sqrt(Y): stimulus
        RMS >= 0.18 => Y >= 1062 => factor <= 2.78              <= 7.0 LSB
      warm-up residual (n_warm = ceil(10/alpha_eff), e^-10)     <= 4.0 LSB
      TOTAL -> 16 Q15 LSB (measured peaks: 4.4 at RMS 0.4, 7.7 at RMS 0.18).
    """
    CATEGORY = "signal_conditioning"

    def __init__(self, name: str, alpha: float = 0.0001):
        super().__init__(name, alpha=alpha)
        # HARDWARE DEVIATION: alpha is quantized to Q15 (alpha_eff =
        # round(alpha*32768)/32768). alpha < ~1.5e-5 quantizes to ZERO (the
        # filter would never update) and alpha outside (0, 1] is not
        # representable/meaningful on the Q15 datapath -> RAISE, never clamp
        # silently (INV-0).
        alpha = float(alpha)
        if not (0.0 < alpha <= 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: rms alpha={alpha} outside (0, 1] — the Q15 "
                f"datapath cannot represent it (GR accepts any float; on this "
                f"fabric alpha is a Q15 coefficient).")
        aq = int(round(alpha * 32768.0))
        if aq <= 0:
            raise ValueError(
                f"HARDWARE LIMIT: rms alpha={alpha} quantizes to 0 in Q15 "
                f"(alpha < ~1.5e-5) — the averager would never update. "
                f"Smallest representable alpha is 1/65536-rounding, ~1.53e-5.")
        self._alpha = alpha
        self._alpha_q15 = min(aq, 32767)

    # ------------------------------------------------------------------ props
    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def alpha_q15(self) -> int:
        """The Q15 coefficient the chip runs (HW-DEVIATION: quantized alpha)."""
        return self._alpha_q15

    @property
    def cell_count(self) -> int:
        return 4

    # ---------------------------------------------------------- front hooks
    def _front_inputs(self):
        raise NotImplementedError

    def _front_asm(self) -> str:
        """Assembly that leaves the saturated instantaneous power in R0 and
        ends by falling through to the shared IIR tail label ``_p``."""
        raise NotImplementedError

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        n_in = len(self._front_inputs())
        d0 = n_in                       # first data address, above the inputs
        cells = {}

        # (1) pwr — front (power in R0, saturated) + error-feedback IIR.
        cells["pwr"] = CellProgram(
            inputs=self._front_inputs(),
            outputs=[Port("yout"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("satpos", 0x7FFF, address=d0),
                DataWord("alpha", self._alpha_q15, address=d0 + 1),
                DataWord("mask", 0x7FFF, address=d0 + 2),
            ],
            state=[
                StateVar("y", register=d0 + 3),
                StateVar("d", register=d0 + 4),
                StateVar("hi", register=d0 + 5),
                StateVar("acclo", register=d0 + 6),
            ],
            assembly_template=self._front_asm() + """\
_p:
    SUB R0, R{state:y}
    MOVE R{state:d}, R0
    MULQ R{state:d}, R{data:alpha}
    MOVE R{state:hi}, R0
    MUL R{state:d}, R{data:alpha}
    AND R0, R{data:mask}
    ADD R0, R{state:acclo}
    MOVE R{state:d}, R0
    AND R0, R{data:mask}
    MOVE R{state:acclo}, R0
    SHR R{state:d}, #15
    ADD R0, R{state:hi}
    ADD R0, R{state:y}
    MOVE R{state:y}, R0
    {write:yout}
    {jump:trig}
""",
        )

        # (2) norm — count left shifts to [0x4000, 0x7FFF]; y=0 -> the s=30
        # sentinel (even parity, 15 denorm shifts -> exact 0).
        cells["norm"] = CellProgram(
            inputs=[Port("y", register=0)],
            outputs=[Port("f"), Port("s"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("c4000", 0x4000, address=2),
                DataWord("one", 1, address=3),
                DataWord("szero", _S_ZERO_SENTINEL, address=4),
            ],
            state=[
                StateVar("ys", register=5),
                StateVar("s", register=6),
            ],
            assembly_template="""\
start:
    MOVE R{state:ys}, R{in:y}
    CMP R{state:ys}, R{data:zero}
    BR.NZ _nz
    MOVE R0, R{data:zero}
    {write:f}
    MOVE R0, R{data:szero}
    {write:s}
    {jump:trig}
    HALT
_nz:
    MOVE R{state:s}, R{data:zero}
_lp:
    CMP R{state:ys}, R{data:c4000}
    BR.GE _dn
    SHL R{state:ys}, #1
    MOVE R{state:ys}, R0
    ADD R{state:s}, R{data:one}
    MOVE R{state:s}, R0
    GOTO _lp
_dn:
    SUB R{state:ys}, R{data:c4000}
    SHL R0, #1
    {write:f}
    MOVE R0, R{state:s}
    {write:s}
    {jump:trig}
""",
        )

        # (3) poly — quartic Horner on f (all coeffs sub-unity, all
        # intermediates in range); forwards (p, s).
        cells["poly"] = CellProgram(
            inputs=[Port("f", register=0), Port("s", register=1)],
            outputs=[Port("p"), Port("s"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("c4", _SQRT_C4 & 0xFFFF, address=2),
                DataWord("c3", _SQRT_C3 & 0xFFFF, address=3),
                DataWord("c2", _SQRT_C2 & 0xFFFF, address=4),
                DataWord("c1", _SQRT_C1 & 0xFFFF, address=5),
                DataWord("c0", _SQRT_C0 & 0xFFFF, address=6),
            ],
            state=[
                StateVar("fr", register=7),
                StateVar("ss", register=8),
            ],
            assembly_template="""\
start:
    MOVE R{state:fr}, R{in:f}
    MOVE R{state:ss}, R{in:s}
    MOVE R0, R{data:c4}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c3}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c2}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c1}
    MULQ R0, R{state:fr}
    ADD R0, R{data:c0}
    {write:p}
    MOVE R0, R{state:ss}
    {write:s}
    {jump:trig}
""",
        )

        # (4) denorm — 1/sqrt(2) on odd s, then floor(s/2) SHR-#1 loop
        # (immediate-count shifts only — INV-34). EXIT-CELL RULE: NO GOTO here —
        # a GOTO assembles to a local hop-31 JUMP, and the build's output-handoff
        # pass rewrites JUMPs in the block's EXIT cell to the external output
        # trigger (verified: the GOTO at the loop tail became a second external
        # JUMP and the loop ran exactly once — the s>=4 outputs came out one
        # shift short, exactly 2x). CONDITIONAL branches survive the pass, so the
        # loop is a do-while on SUB's Z flag with a pre-test for k==0 (SHR sets Z).
        cells["denorm"] = CellProgram(
            inputs=[Port("p", register=0), Port("s", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=2),
                DataWord("invsqrt2", _INV_SQRT2_Q15, address=3),
            ],
            state=[
                StateVar("ps", register=4),
                StateVar("ks", register=5),
            ],
            assembly_template="""\
start:
    MOVE R{state:ps}, R{in:p}
    MOVE R{state:ks}, R{in:s}
    AND R{state:ks}, R{data:one}
    BR.Z _even
    MULQ R{state:ps}, R{data:invsqrt2}
    MOVE R{state:ps}, R0
_even:
    SHR R{state:ks}, #1
    MOVE R{state:ks}, R0
    BR.Z _dn
_lp:
    SHR R{state:ps}, #1
    MOVE R{state:ps}, R0
    SUB R{state:ks}, R{data:one}
    MOVE R{state:ks}, R0
    BR.NZ _lp
_dn:
    MOVE R0, R{state:ps}
    {write:out}
    {jump:trig}
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def internal_connections(self):
        return [("pwr", "yout", "norm", "y"),
                ("norm", "f", "poly", "f"),
                ("norm", "s", "poly", "s"),
                ("poly", "p", "denorm", "p"),
                ("poly", "s", "denorm", "s")]

    def internal_jumps(self):
        return [("pwr", "trig", "norm", "default"),
                ("norm", "trig", "poly", "default"),
                ("poly", "trig", "denorm", "default")]

    def output_cell_ids(self):
        return ["denorm"]

    def default_layout(self):
        # 2x2 serpentine fold (INV-14 even-column): input cell (0,0) and output
        # cell (1,0) co-locate on the top edge (INV-8).
        return {"pwr": (0, 0, "south"),
                "norm": (0, 1, "east"),
                "poly": (1, 1, "north"),
                "denorm": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @classmethod
    def _sqrt_q15(cls, y: int) -> int:
        """Bit-exact model of the norm -> poly -> denorm pipeline for one power
        word y in [0, 32767]."""
        if y == 0:
            return 0
        ys, s = y, 0
        while ys < 0x4000:
            ys = (ys << 1) & 0xFFFF
            s += 1
        fw = ((ys - 0x4000) << 1) & 0xFFFF
        p = _SQRT_C4
        for c in (_SQRT_C3, _SQRT_C2, _SQRT_C1, _SQRT_C0):
            p = _s16(((p * _s16(fw)) >> 15) + c)
        if s & 1:
            p = (p * _INV_SQRT2_Q15) >> 15
        return p >> (s >> 1)

    def _iir_sqrt_q15(self, power_words) -> list:
        """Bit-exact IIR (error feedback) + sqrt over a saturated power stream."""
        aq = self._alpha_q15
        y = acclo = 0
        out = []
        for p in power_words:
            d = p - y
            inc = aq * d
            hi = inc >> 15                    # MULQ truncation toward -inf
            lo = inc & 0x7FFF
            t = acclo + lo
            y = y + hi + (t >> 15)
            acclo = t & 0x7FFF
            out.append(self._sqrt_q15(y) & 0xFFFF)
        return out

    def _reference_avg_sqrt(self, powers: np.ndarray) -> np.ndarray:
        """Float GR model: avg=(1-a)avg+a*p then sqrt, clipped to Q15."""
        avg = 0.0
        out = np.empty(len(powers), dtype=np.float64)
        for i, p in enumerate(powers):
            avg = (1.0 - self._alpha) * avg + self._alpha * float(p)
            out[i] = math.sqrt(avg)
        return np.clip(out, 0.0, 32767.0 / 32768.0).astype(np.float32)


class RMSBlock(_RMSCoreBlock):
    """RMS of a real stream — drop-in for GNU Radio ``blocks.rms_ff``
    (param ``alpha``, GR default 0.0001):

        avg = (1 - alpha)*avg + alpha*x[n]^2;   out[n] = sqrt(avg)

    (sqrt AFTER the update — first output sqrt(alpha*x[0]^2); pinned against
    live GR). See :class:`_RMSCoreBlock` for the shared datapath, the
    HW-DEVIATIONS (Q15-quantized alpha; power saturation at |x|=1) and the
    derived tolerance.
    """
    TAGS = ["rms", "rms_ff", "power", "average", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _front_inputs(self):
        return [Port("sample", register=0)]

    def _front_asm(self) -> str:
        # x^2 via MULQ x,x; the single unrepresentable corner is x = -1.0
        # (0x8000): (2^15)^2 >> 15 = 0x8000 reads negative -> saturate 0x7FFF.
        return """\
start:
    MULQ R{in:sample}, R{in:sample}
    BR.NN _p
    MOVE R0, R{data:satpos}
"""

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact predictor of the on-chip datapath (front + IIR + sqrt)."""
        powers = []
        for w in x_q15:
            x = _s16(w)
            p = (x * x) >> 15
            powers.append(min(0x7FFF, p))
        return self._iir_sqrt_q15(powers)

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference matching GR ``rms_ff`` (clipped to Q15)."""
        arr = np.asarray(input_samples).astype(np.float64)
        return self._reference_avg_sqrt(arr * arr)


class RMSCFBlock(_RMSCoreBlock):
    """RMS of a complex stream — drop-in for GNU Radio ``blocks.rms_cf``
    (param ``alpha``, GR default 0.0001): the same averager run on
    ``|z|^2 = re^2 + im^2`` with a REAL scalar output:

        avg = (1 - alpha)*avg + alpha*(re^2 + im^2);   out[n] = sqrt(avg)

    Front = the proven ComplexToMagSquared form (MULQ re,re + MACQ im,im).
    Power >= 0, so overflow shows as bit 15 — but re = -1.0 makes the FIRST
    product alone 0x8000, and 0x8000 + 0x8000 WRAPS TO ZERO with N clear, so the
    guard checks N after EACH accumulation step (a single end-check misses the
    re = im = -1.0 corner). See :class:`_RMSCoreBlock` for the shared datapath,
    HW-DEVIATIONS, and the derived tolerance.
    """
    TAGS = ["rms", "rms_cf", "power", "average", "complex",
            "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0])

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _front_inputs(self):
        return [Port("re", register=0), Port("im", register=1)]

    def _front_asm(self) -> str:
        return """\
start:
    MULQ R{in:re}, R{in:re}
    BR.N _sat
    MACQ R{in:im}, R{in:im}
    BR.NN _p
_sat:
    MOVE R0, R{data:satpos}
"""

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, re_q15, im_q15) -> list:
        """Bit-exact predictor of the on-chip datapath (front + IIR + sqrt)."""
        powers = []
        for rw, iw in zip(re_q15, im_q15):
            r, i = _s16(rw), _s16(iw)
            p = (r * r) >> 15
            if p >= 0x8000 or (p & 0x8000):
                p = 0x7FFF
            else:
                p = _s16((p + ((i * i) >> 15)) & 0xFFFF)
                if p < 0:
                    p = 0x7FFF
            powers.append(min(0x7FFF, p))
        return self._iir_sqrt_q15(powers)

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference matching GR ``rms_cf`` (clipped to Q15)."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            powers = (arr.real.astype(np.float64) ** 2
                      + arr.imag.astype(np.float64) ** 2)
        else:
            powers = np.asarray(arr, dtype=np.float64) ** 2
        return self._reference_avg_sqrt(powers)
