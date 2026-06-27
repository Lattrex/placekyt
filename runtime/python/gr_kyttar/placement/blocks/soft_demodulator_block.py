"""SoftDemodulatorBlock — see :class:`SoftDemodulatorBlock`."""
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import (BlockInterface, KyttarBlock, assemble_to_words,
                    float_to_q15, q15_to_float)


# --- standard GNU Radio constellations (points + index->symbol map) ------------
# Each entry: (points, symbol_map). points[i] is the complex constellation point
# for index i; symbol_map[i] is the symbol VALUE whose bits label that point (GR
# scans the symbol value LSB-first and stores the LLR MSB-first). For the stock GR
# constellations the symbol_map is the identity (symbol value == index), matching
# digital.constellation_bpsk()/qpsk(). Points use GR's exact amplitudes.
_QA = math.sqrt(2.0)              # GR constellation_qpsk() axis amplitude (±1.4142,
                                  # so each point has unit... energy 2). The exact
                                  # amplitude is load-bearing for the soft-decision
                                  # MAGNITUDE to match GR (the golden), so we use
                                  # GR's raw value here.

_CONSTELLATIONS = {
    # BPSK: points -1, +1 ; index 0 -> -1 (bit 0), index 1 -> +1 (bit 1).
    "bpsk": ([complex(-1.0, 0.0), complex(1.0, 0.0)], [0, 1]),
    # QPSK (GR constellation_qpsk index map): MSB=imag-sign, LSB=real-sign.
    #   00 -> -1.41-1.41j  01 -> +1.41-1.41j  10 -> -1.41+1.41j  11 -> +1.41+1.41j
    "qpsk": ([complex(-_QA, -_QA), complex(_QA, -_QA),
              complex(-_QA, _QA), complex(_QA, _QA)], [0, 1, 2, 3]),
}


class SoftDemodulatorBlock(KyttarBlock):
    """Constellation soft-decision demapper — mirrors GNU Radio
    ``digital.constellation_soft_decoder_cf(constellation, npwr)``.

    For each received complex sample ``z`` it emits ``bits_per_symbol`` soft
    Log-Likelihood Ratios, one per bit of the symbol, using GNU Radio's exact
    soft-decision rule::

        LLR(b) = log Σ_{s: bit_b(s)=1} exp(-|z-s|²/npwr)
               - log Σ_{s: bit_b(s)=0} exp(-|z-s|²/npwr)

    GR's sign convention is POSITIVE LLR ⇒ bit = 1 (the opposite of the textbook
    "positive = bit 0"); the returned LLRs are MSB-first (``llr[0]`` = the most
    significant bit of the symbol index). For the Gray-mapped square
    constellations (BPSK, QPSK) the full-log rule above is numerically identical
    to the cheaper max-log form ``(min_{bit=0}|z-s|² - min_{bit=1}|z-s|²)/npwr``,
    which is separable per axis — exactly what the chip computes.

    Params mirror the GRC block VERBATIM:
      * ``constellation`` — the constellation. Accepts a name ('bpsk'|'qpsk'), or
        a ``(points, symbol_map)`` pair (points = list of complex, symbol_map =
        index→symbol value, matching ``digital.constellation_calcdist``). Default
        'bpsk'.
      * ``npwr`` — noise power. GR's default ``-1`` resolves to ``1.0`` (GR stores
        ``d_npwr=1.0``; npwr<0 means "use the stored value"). npwr is a pure linear
        scale on the squared distance.

    HARDWARE DEVIATION (documented + LOUD): the chip computes the **max-log**
    approximation of the soft decision, and only for **separable Gray** square
    constellations (BPSK, QPSK), where each LLR is an affine function of one axis
    (a single ``MULQ`` by a precomputed slope). For BPSK this is bit-identical to
    today's ``LLR = coeff·I``. A non-separable / non-Gray constellation (e.g.
    arbitrary ``constellation_calcdist`` points) cannot be reduced to per-axis
    MULQs in a single cell and RAISES — it would need a full per-symbol distance
    LUT (a Tier-2 multi-cell block). The Q15 LLR is scaled by an internal
    ``out_scale`` so a full-scale axis input maps near half-scale Q15 (headroom for
    a downstream FEC accumulator); verification aligns the two LLR scales.

    Interface: complex input (I @R0, Q @R1); ``bits_per_symbol`` LLRs out, MSB
    first. (BPSK uses only I; Q is ignored.)
    """
    CATEGORY = "demodulation"
    TAGS = ["soft_demod", "llr", "constellation_soft_decoder", "demodulation"]

    # Production output scale: a full-scale axis input (|axis|=1.0) maps to this
    # Q15 magnitude, leaving headroom for a downstream soft-FEC accumulator.
    OUT_SCALE = 0.5

    def __init__(self, name: str, constellation="bpsk", npwr: float = -1.0,
                 noise_variance: Optional[float] = None,
                 llr_scale: float = 1.0):
        """Initialize the constellation soft demapper.

        Args:
            name: block name.
            constellation: 'bpsk' | 'qpsk', or a (points, symbol_map) pair.
            npwr: noise power (GR param). <0 → GR's stored default 1.0.
            noise_variance: DEPRECATED BPSK-only alias kept for back-compat — if
                given (and npwr left at default) it is used as npwr (σ²).
            llr_scale: legacy extra scale on the BPSK coefficient (kept so old
                callers behave identically); folded into the slope.
        """
        # Back-compat: the old signature was (name, noise_variance, llr_scale)
        # with a BPSK-only LLR = min(0.5, 2/σ²·llr_scale)·I. Preserve it: if a
        # caller passed noise_variance and left npwr at the -1 sentinel, treat
        # noise_variance as σ² and reproduce the exact old coefficient.
        self._legacy_bpsk = (noise_variance is not None and npwr < 0
                             and (isinstance(constellation, str)
                                  and constellation == "bpsk"))
        super().__init__(name, constellation=constellation, npwr=npwr,
                         noise_variance=noise_variance, llr_scale=llr_scale)

        pts, smap = self._resolve_constellation(constellation)
        self._points = pts
        self._symbol_map = smap
        self._bits = int(round(math.log2(len(pts))))
        if 2 ** self._bits != len(pts):
            raise ValueError(
                f"constellation size {len(pts)} is not a power of two")

        # npwr: GR default -1 -> 1.0 (d_npwr). A linear scale on squared distance.
        self._npwr = 1.0 if npwr < 0 else float(npwr)
        self._llr_scale = float(llr_scale)

        # Reduce to separable per-axis affine LLRs (max-log). Raises if the
        # constellation is not separable Gray square (the documented HW limit).
        self._axis_llrs = self._derive_separable_axis_llrs()

        if self._legacy_bpsk:
            # Reproduce the OLD coefficient EXACTLY: coeff = min(0.5, 2/σ²·scale).
            sigma2 = max(0.001, float(noise_variance))
            two_inv = (2.0 / sigma2) * self._llr_scale
            coeff = min(self.OUT_SCALE, two_inv)
            self._axis_llrs = [("I", +1, float_to_q15(coeff), 0)]
            self._llr_coeff_q15 = float_to_q15(coeff)
        else:
            # BPSK convenience accessor (slope of the single I-axis LLR).
            self._llr_coeff_q15 = (self._axis_llrs[0][2]
                                   if self._bits == 1 else 0)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _resolve_constellation(c) -> Tuple[List[complex], List[int]]:
        if isinstance(c, str):
            key = c.lower()
            if key not in _CONSTELLATIONS:
                raise ValueError(
                    f"unknown constellation '{c}'; known: "
                    f"{sorted(_CONSTELLATIONS)} or a (points, symbol_map) pair")
            pts, smap = _CONSTELLATIONS[key]
            return list(pts), list(smap)
        # (points, symbol_map) pair.
        pts, smap = c
        pts = [complex(p) for p in pts]
        smap = [int(s) for s in smap]
        if len(pts) != len(smap):
            raise ValueError("points and symbol_map must be the same length")
        return pts, smap

    def _derive_separable_axis_llrs(self):
        """Reduce the constellation's max-log soft decision to per-axis affine
        LLRs, or RAISE if it is not separable Gray square.

        For a separable Gray square constellation each bit depends on exactly ONE
        axis and is an AFFINE function of that axis's value (MSB = sign, inner
        bits = distance to a Gray threshold). We detect this and return a list of
        (axis, sign, slope_q15, thresh_q15) tuples in MSB-first bit order. We only
        need to support the two stock cases the chip can run; richer maps raise.
        """
        n = self._bits
        # Normalise points to unit average energy so the per-axis slope is a clean
        # function of the axis value in [-1, 1].
        if n == 1:
            # BPSK: LLR for the single bit. GR: index1=+1 is bit 1, index0=-1 is
            # bit 0; positive LLR -> bit 1 -> matches sign(I). max-log:
            #   LLR = (|z-(-1)|^2 - |z-(+1)|^2)/npwr = (4·I)/npwr   (Q=0)
            # We map a full-scale axis (|I|=1) to OUT_SCALE so it fits Q15.
            slope = self.OUT_SCALE  # full-scale I -> +-OUT_SCALE LLR
            return [("I", +1, float_to_q15(slope), 0)]
        if n == 2 and self._is_qpsk_graymap():
            # QPSK: MSB depends on imag sign, LSB on real sign (GR qpsk index map).
            #   LLR(MSB) = (4·Q)/npwr ;  LLR(LSB) = (4·I)/npwr   (separable)
            slope = self.OUT_SCALE
            return [("Q", +1, float_to_q15(slope), 0),   # MSB (bit 1 = +imag)
                    ("I", +1, float_to_q15(slope), 0)]   # LSB (bit 0 = +real)
        raise ValueError(
            "HARDWARE LIMIT: SoftDemodulatorBlock supports only separable Gray "
            "square constellations (bpsk, qpsk) in one cell. The given "
            "constellation is not separable per-axis; a full per-symbol distance "
            "LUT (a Tier-2 multi-cell block) is required. Use 'bpsk'/'qpsk' or "
            "compose distance cells.")

    def _is_qpsk_graymap(self) -> bool:
        """True iff points/symbol_map match GR's constellation_qpsk index map
        (MSB = imag sign, LSB = real sign, identity symbol_map)."""
        if self._symbol_map != [0, 1, 2, 3]:
            return False
        for i, p in enumerate(self._points):
            want_real = +1 if (i & 1) else -1
            want_imag = +1 if (i & 2) else -1
            if (p.real > 0) != (want_real > 0) or (p.imag > 0) != (want_imag > 0):
                return False
        return True

    # ---------------------------------------------------------------- props
    @property
    def cell_count(self) -> int:
        return 1

    @property
    def bits_per_symbol(self) -> int:
        return self._bits

    @property
    def npwr(self) -> float:
        return self._npwr

    @property
    def interface(self) -> BlockInterface:
        # Complex input (I@R0, Q@R1); LLR(s) out from R0.
        regs = [0] if self._bits == 1 else [0, 1]
        return BlockInterface(entry_address=1, input_registers=regs,
                              output_registers=[0])

    # --- back-compat accessors (the old BPSK API) ---------------------------
    @property
    def noise_variance(self) -> float:
        nv = self.params.get("noise_variance")
        return self._npwr if nv is None else max(0.001, float(nv))

    @property
    def llr_coeff_q15(self) -> int:
        """Signed Q15 slope of the (BPSK) I-axis LLR — kept for the old tests."""
        c = self._llr_coeff_q15
        return c - 65536 if c > 32767 else c

    # ---------------------------------------------------------------- golden
    def calc_soft_dec_float(self, z: complex) -> List[float]:
        """GNU Radio's EXACT (full-log) soft decision for one complex sample, in
        GR's native LLR scale (NOT the block's Q15 out-scale). Returns
        ``bits_per_symbol`` LLRs MSB-first, sign = positive⇒bit 1. Bit-comparable
        with ``digital.constellation_soft_decoder_cf`` (matches to ~1e-5)."""
        npwr = self._npwr
        SMALL = 1e-45
        acc0 = [0.0] * self._bits
        acc1 = [0.0] * self._bits
        for i, p in enumerate(self._points):
            d = abs(z - p) ** 2
            arg = -d / npwr
            di = (3.84745e-36 / (-arg)) if arg < -86.0 else math.exp(arg)
            v = self._symbol_map[i]
            for j in range(self._bits):
                if (v >> j) & 1:
                    acc1[j] += di
                else:
                    acc0[j] += di
        out = [0.0] * self._bits
        for i in range(self._bits):
            one = max(acc1[i], SMALL)
            zero = max(acc0[i], SMALL)
            out[self._bits - 1 - i] = math.log(one) - math.log(zero)
        return out

    def _axis_value(self, z: complex, axis: str) -> float:
        return z.real if axis == "I" else z.imag

    def process_reference_q15(self, input_samples) -> list:
        """Bit-exact predictor of the on-chip datapath: per output bit a single
        ``MULQ`` of the relevant axis by the per-axis slope (max-log, the chip's
        separable form). ``input_samples`` is a flat list of Q15 words: ONE word
        per sample for BPSK (I), TWO interleaved (I, Q) for QPSK. Returns the LLR
        words flat, ``bits_per_symbol`` per sample, MSB-first."""
        out = []
        wps_in = 1 if self._bits == 1 else 2
        n = len(input_samples) // wps_in
        for k in range(n):
            i_q = self._s16(input_samples[k * wps_in])
            q_q = (self._s16(input_samples[k * wps_in + 1])
                   if wps_in == 2 else 0)
            for (axis, sign, slope_q15, _t) in self._axis_llrs:
                v = i_q if axis == "I" else q_q
                slope = slope_q15 - 65536 if slope_q15 > 32767 else slope_q15
                llr = (sign * v * slope) >> 15
                llr = max(-32768, min(32767, llr))
                out.append(llr & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference, modelling the on-chip Q15 datapath exactly. Accepts a
        complex array (I=real, Q=imag) or, for BPSK, a real I array / Q15 ints.
        Returns the LLRs as float32 in the block's [-OUT_SCALE, OUT_SCALE) scale,
        flat (bits_per_symbol per sample, MSB-first)."""
        arr = np.asarray(input_samples)
        out = []
        for s in arr:
            if np.iscomplexobj(arr):
                i_q = float_to_q15(float(s.real))
                q_q = float_to_q15(float(s.imag))
            else:
                i_q = (float_to_q15(float(s))
                       if isinstance(s, (float, np.floating))
                       else int(s) & 0xFFFF)
                i_q = self._s16(i_q)
                q_q = 0
            i_q = self._s16(i_q)
            for (axis, sign, slope_q15, _t) in self._axis_llrs:
                v = i_q if axis == "I" else q_q
                slope = slope_q15 - 65536 if slope_q15 > 32767 else slope_q15
                llr = (sign * v * slope) >> 15
                llr = max(-32768, min(32767, llr))
                out.append(llr / 32768.0)
        return np.asarray(out, dtype=np.float32)

    @staticmethod
    def _s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    # ---------------------------------------------------------------- build
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Single-cell soft demapper: one ``MULQ`` per output bit (the separable
        max-log per-axis affine LLR), emitted MSB-first. A remote JUMP does not
        halt the issuer, so the N LLRs are emitted as N (WRITE, JUMP) pairs in one
        program."""
        if self._bits == 1:
            # BPSK: LLR = slope·I, identical to the original single-MULQ block.
            slope = self._axis_llrs[0][2]
            return {0: CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("llr")],
                entries=[EntryPoint("default")],
                data=[DataWord("coeff", slope, address=1)],
                assembly_template="""\
start:
    MULQ R{in:sample}, R{data:coeff}
    {write:llr}
    {jump:llr}
""",
            )}
        # QPSK: two LLRs (MSB from Q, LSB from I), each a MULQ by the slope.
        # Inputs land I@R0, Q@R1; save them first (MULQ clobbers R0).
        slope = self._axis_llrs[0][2]   # same slope for both axes
        data = [DataWord("slope", slope, address=2)]
        return {0: CellProgram(
            inputs=[Port("i_in", register=0), Port("q_in", register=1)],
            outputs=[Port("llr")],
            entries=[EntryPoint("default")],
            data=data,
            state=[StateVar("isav"), StateVar("qsav")],
            assembly_template="""\
start:
    MOVE R{state:isav}, R{in:i_in}
    MOVE R{state:qsav}, R{in:q_in}
    MOVE R0, R{state:qsav}
    MULQ R0, R{data:slope}
    {write:llr}
    {jump:llr}
    MOVE R0, R{state:isav}
    MULQ R0, R{data:slope}
    {write:llr}
    {jump:llr}
""",
        )}

    def reset(self):
        pass
