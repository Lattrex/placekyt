"""IIRBiquadBlock — see :class:`IIRBiquadBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Optional
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class IIRBiquadBlock(KyttarBlock):
    """Direct-Form-I IIR filter — GR ``filter.iir_filter_ffd`` parity (ARBITRARY
    ORDER).

    Mirrors GNU Radio's ``iir_filter_ffd(fftaps, fbtaps, oldstyle)`` VERBATIM::

        y[n] = Σ_k  fftaps[k]·x[n-k]   (±)   Σ_{j>=1} fbtaps[j]·y[n-j]

    where the feedback sign is ``+`` when ``oldstyle=True`` (the default; GR's
    legacy convention stored the feedback taps already-negated) and ``−`` when
    ``oldstyle=False``. **``fbtaps[0]`` is IGNORED in both styles** (it is not a
    normalizer — confirmed against GR ``iir_filter_ffd``). The feed-forward order
    is ``len(fftaps)-1`` and the feedback order is ``len(fbtaps)-1``; both may be
    any length (the classic biquad is just ``fftaps=[b0,b1,b2]``,
    ``fbtaps=[1,a1,a2]``).

    Q15 FEEDBACK-COEFFICIENT RANGE — the "half-and-double-MSUQ" trick
    ----------------------------------------------------------------
    A feedback coefficient can have ``|coef|`` up to ~2 (e.g. a biquad's
    ``a1 = -2cos(omega)``), which Q15 (range [-1, +1)) cannot hold. Rather than
    clamp (which silently corrupts every sharp filter), each feedback coefficient
    is stored HALVED (always representable for ``|coef| < 2``) and its multiply-
    subtract/add is applied TWICE: ``MSUQ Ra,Rb`` does ``R0 -= (Ra*Rb)>>15``
    (architecture_spec_v0.11 §4.12), and two halved MSUQs equal one full one with
    no intermediate overflow. Feed-forward taps (``|b| <= 1`` for a normalized
    response) convert directly. A STABLE filter's output is bounded, so the Q15
    accumulator stays in range through the Direct-Form-I sum (no saturating
    shift), and the accumulation order matches GR's bit-for-bit.

    HARDWARE LIMIT (documented + LOUD)
    ----------------------------------
    The filter is a SINGLE cell, so the two delay lines (x and y history) + the tap
    data + the program must fit 32 words. That bounds the combined order:
    ``len(fftaps) + len(fbtaps) <= MAX_TAPS_TOTAL``. A larger filter would need a
    multi-cell wavefront (a Tier-2 block); the block RAISES above the limit rather
    than mis-build. (The common 2nd-order biquad is well inside.)

    PRECISION LIMIT (a documented known limit, like the FIR's tap ceiling)
    ---------------------------------------------------------------------
    GR's ``iir_filter_ffd`` uses DOUBLE-precision taps; Q15 is coarser, and
    quantization error in the recursive loop GROWS as the poles approach the unit
    circle. The block is production-accurate for the common gentle-to-moderate
    range and carries a guarded known-limit test for very sharp poles — the same
    "ship the proven range, document the edge" stance as the FIR tap ceiling.

    Interface (defaults): entry R1, single input sample in R31.
    """
    CATEGORY = "filtering"
    TAGS = ["iir", "biquad", "filter", "iir_filter_ffd", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str,
                 b_coeffs: Optional[List[float]] = None,
                 a_coeffs: Optional[List[float]] = None,
                 *,
                 fftaps: Optional[List[float]] = None,
                 fbtaps: Optional[List[float]] = None,
                 oldstyle: bool = True):
        """Initialize the IIR filter — GR ``iir_filter_ffd`` parity, arbitrary order.

        Two equivalent ways to specify the filter:

        * GR-native (keyword): ``fftaps`` (feed-forward ``[b0, b1, ...]``),
          ``fbtaps`` (feedback ``[fb0(ignored), a1, a2, ...]``), ``oldstyle`` (the
          GR tap-sign convention — feedback ADDS when True (default), SUBTRACTS
          when False). Any order.
        * BACK-COMPAT biquad (positional): ``b_coeffs=[b0,b1,b2]``,
          ``a_coeffs=[a1,a2]`` reproduces the old block EXACTLY — it maps to
          ``fftaps=b_coeffs``, ``fbtaps=[1.0]+a_coeffs``, ``oldstyle=False`` (i.e.
          ``y = Σb·x − Σa·y``). The positional signature ``(name, b, a)`` is
          preserved so existing callers and the convenience constructors are
          unchanged.
        """
        # Back-compat: the old signature was (name, b_coeffs, a_coeffs) with
        # y = b0x + b1x1 + b2x2 - a1y1 - a2y2 (subtract). Map to GR newstyle.
        if fftaps is None and b_coeffs is not None:
            fftaps = list(b_coeffs)
            fbtaps = [1.0] + list(a_coeffs or [])
            oldstyle = False
        if fftaps is None:
            # No coefficients given (e.g. a freshly-placed block before the user
            # edits the Inspector): default to an identity passthrough so the
            # block is always placeable/buildable. y[n] = x[n].
            fftaps = [1.0]
        if fbtaps is None:
            fbtaps = [1.0]

        super().__init__(name, fftaps=fftaps, fbtaps=fbtaps, oldstyle=oldstyle,
                         b_coeffs=b_coeffs, a_coeffs=a_coeffs)
        self._fftaps = [float(t) for t in fftaps]
        self._fbtaps = [float(t) for t in fbtaps]
        self._oldstyle = bool(oldstyle)
        # Feedback sign applied to the stored taps: GR oldstyle ADDS fb·y; newstyle
        # SUBTRACTS. We fold the style sign into a per-tap effective coefficient so
        # the on-chip MSUQ always SUBTRACTS coef·y (i.e. coef = -fb for oldstyle,
        # +fb for newstyle). fbtaps[0] is ignored.
        fb_sign = -1.0 if self._oldstyle else +1.0
        self._fb_eff = [fb_sign * t for t in self._fbtaps[1:]]

        # The whole filter is one 32-word cell. Compute the exact footprint and
        # raise if it won't fit (rather than mis-build). Words used:
        #   data:  nff (ff taps) + nfb (halved feedback coeffs)
        #   state: (nff-1) x-history + nfb y-history + 2 (x_save, y_save)
        #   prog:  1 (save x) + nff (MULQ/MACQ) + 2*nfb (double MSUQ) + 1 (save y)
        #          + (nff-1) x-shift + nfb y-shift + 2 (write/jump)
        #   + R0 (accumulator).
        nff = len(self._fftaps)
        nfb = len(self._fb_eff)
        data_words = nff + nfb
        state_words = (nff - 1) + nfb + 2
        prog_words = 1 + nff + 2 * nfb + 1 + (nff - 1) + nfb + 2
        total_words = 1 + data_words + state_words + prog_words  # +1 for R0
        # 31, not 32: the top address is reserved (R32 auto-HALT; WRITE/JUMP banned
        # at R31), so the usable program+data+state envelope is 31 words.
        if total_words > 31:
            raise ValueError(
                f"HARDWARE LIMIT: this filter needs {total_words} cell words "
                f"(ff order {nff - 1}, fb order {nfb}) but a cell has 32. The "
                f"x+y delay lines + taps + program don't fit one cell. Use a "
                f"lower-order section or a multi-cell filter. (Feedback taps cost "
                f"more: each needs a y-history word + a double MSUQ.)")

        # Feed-forward taps: |b| <= 1 normally -> direct Q15.
        self._ff_q15 = [float_to_q15(b) for b in self._fftaps]
        # Feedback effective coeffs: stored HALVED (covers |coef| up to ~2),
        # applied via TWO MSUQs each. (coef already carries the subtract sign.)
        self._fb_half_q15 = [float_to_q15(c / 2.0) for c in self._fb_eff]

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def fftaps(self) -> List[float]:
        return list(self._fftaps)

    @property
    def fbtaps(self) -> List[float]:
        return list(self._fbtaps)

    @property
    def oldstyle(self) -> bool:
        return self._oldstyle

    # Back-compat accessors (the old biquad API).
    @property
    def b_coefficients(self) -> List[float]:
        return list(self._fftaps)

    @property
    def a_coefficients(self) -> List[float]:
        # The OLD biquad ``a`` coeffs (the values SUBTRACTED in y = Σb·x − Σa·y).
        # On-chip we subtract ``_fb_eff·y``, so the subtracted coeffs ARE _fb_eff.
        return list(self._fb_eff)

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Direct-Form-I IIR: y = Σ ff·x[n-k]  (subtract) Σ fb_eff·y[n-j].

        Feed-forward: MULQ b0·x, then MACQ for each later ff tap over the x delay
        line. Feedback: each effective coeff is stored HALVED and emitted as TWO
        ``MSUQ`` (the half-and-double trick) over the y delay line. Then the delay
        lines shift. The accumulation order matches :meth:`process_reference_q15`.
        """
        nff = len(self._ff_q15)
        nfb = len(self._fb_half_q15)
        # --- data words: ff taps, then fb half taps ---
        data = []
        addr = 1
        for i, v in enumerate(self._ff_q15):
            data.append(DataWord(f"b{i}", v, address=addr)); addr += 1
        for j, v in enumerate(self._fb_half_q15):
            data.append(DataWord(f"a{j}h", v, address=addr)); addr += 1

        # --- state: x delay line x1..x(nff-1), y delay line y1..y(nfb),
        #     plus x_save / y_save staging (no feedback in a latch data path) ---
        state = []
        for k in range(1, nff):
            state.append(StateVar(f"x{k}"))
        for j in range(1, nfb + 1):
            state.append(StateVar(f"y{j}"))
        state.append(StateVar("x_save"))
        state.append(StateVar("y_save"))

        # --- program ---
        lines = ["    MOVE R{state:x_save}, R{in:sample}"]
        # Feed-forward: MULQ b0*x ; MACQ bk*x(k).
        lines.append("    MULQ R{in:sample}, R{data:b0}")
        for k in range(1, nff):
            lines.append(f"    MACQ R{{state:x{k}}}, R{{data:b{k}}}")
        # Feedback: each effective coeff -> two MSUQ of the halved coeff over y(j+1).
        for j in range(nfb):
            yreg = j + 1
            lines.append(f"    MSUQ R{{state:y{yreg}}}, R{{data:a{j}h}}")
            lines.append(f"    MSUQ R{{state:y{yreg}}}, R{{data:a{j}h}}")
        lines.append("    MOVE R{state:y_save}, R0")
        # Shift x delay line (high index first).
        for k in range(nff - 1, 1, -1):
            lines.append(f"    MOVE R{{state:x{k}}}, R{{state:x{k - 1}}}")
        if nff > 1:
            lines.append("    MOVE R{state:x1}, R{state:x_save}")
        # Shift y delay line (high index first).
        for j in range(nfb, 1, -1):
            lines.append(f"    MOVE R{{state:y{j}}}, R{{state:y{j - 1}}}")
        if nfb >= 1:
            lines.append("    MOVE R{state:y1}, R{state:y_save}")
        lines.append("    {write:out}")
        lines.append("    {jump:out}")

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template="start:\n" + "\n".join(lines) + "\n",
        )}

    # --- Q15 reference (models the hardware datapath EXACTLY) -----------------

    @staticmethod
    def _to_s16(v: int) -> int:
        v &= 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    @classmethod
    def _macq(cls, acc: int, a_q15: int, b_q15: int) -> int:
        prod = (cls._to_s16(a_q15) * cls._to_s16(b_q15)) >> 15
        return cls._to_s16((acc + prod) & 0xFFFF)

    @classmethod
    def _msuq(cls, acc: int, a_q15: int, b_q15: int) -> int:
        prod = (cls._to_s16(a_q15) * cls._to_s16(b_q15)) >> 15
        return cls._to_s16((acc - prod) & 0xFFFF)

    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 Direct-Form-I IIR, in the SAME accumulation order as the
        built datapath (MULQ b0, MACQ for the rest of ff over the x delay line,
        then each feedback term as TWO MSUQ of the halved effective coeff over the
        y delay line). Every op WRAPS in 16 bits exactly as the ALU does. Returns
        one signed Q15 word per input sample."""
        nff = len(self._ff_q15)
        nfb = len(self._fb_half_q15)
        x_hist = [0] * max(0, nff - 1)     # x1..x(nff-1)
        y_hist = [0] * nfb                 # y1..y(nfb)
        out = []
        for s in input_q15:
            xq = self._to_s16(int(s) & 0xFFFF)
            acc = (self._to_s16(xq) * self._to_s16(self._ff_q15[0])) >> 15  # MULQ b0
            acc = self._to_s16(acc & 0xFFFF)
            for k in range(1, nff):
                acc = self._macq(acc, x_hist[k - 1], self._ff_q15[k])
            for j in range(nfb):
                acc = self._msuq(acc, y_hist[j], self._fb_half_q15[j])
                acc = self._msuq(acc, y_hist[j], self._fb_half_q15[j])
            yq = self._to_s16(acc & 0xFFFF)
            if nff > 1:
                x_hist = [xq] + x_hist[:-1]
            if nfb > 0:
                y_hist = [yq] + y_hist[:-1]
            out.append(yq & 0xFFFF)
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference (legacy / diagnostic) of GR's arbitrary-order IIR:
        y = Σ ff·x[n-k]  (±)  Σ fb[j]·y[n-j], sign per oldstyle, fb[0] ignored."""
        output = np.zeros_like(input_samples, dtype=np.float64)
        nff = len(self._fftaps)
        x_hist = [0.0] * max(0, nff - 1)
        y_hist = [0.0] * len(self._fb_eff)
        for i, x_n in enumerate(input_samples):
            acc = self._fftaps[0] * float(x_n)
            for k in range(1, nff):
                acc += self._fftaps[k] * x_hist[k - 1]
            for j, c in enumerate(self._fb_eff):
                acc -= c * y_hist[j]
            if nff > 1:
                x_hist = [float(x_n)] + x_hist[:-1]
            if self._fb_eff:
                y_hist = [acc] + y_hist[:-1]
            output[i] = acc
        return output.astype(np.float32)

    # --- biquad convenience constructors (RBJ cookbook), unchanged behaviour ---
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
        return cls(name, b_coeffs=[b0 / a0, b1 / a0, b2 / a0],
                   a_coeffs=[a1 / a0, a2 / a0])

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
        return cls(name, b_coeffs=[b0 / a0, b1 / a0, b2 / a0],
                   a_coeffs=[a1 / a0, a2 / a0])

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
        return cls(name, b_coeffs=[b0 / a0, b1 / a0, b2 / a0],
                   a_coeffs=[a1 / a0, a2 / a0])
