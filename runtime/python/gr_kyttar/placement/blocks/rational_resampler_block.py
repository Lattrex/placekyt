# SPDX-License-Identifier: GPL-3.0-or-later
"""RationalResamplerBlock — see :class:`RationalResamplerBlock`."""
import math
from typing import Dict, List

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import KyttarBlock, float_to_q15
from .fir_filter_block import FIRFilterBlock


class RationalResamplerBlock(FIRFilterBlock):
    """Rational resampler — GNU Radio ``filter.rational_resampler_fff``.

    Resamples the input by the rational factor ``interpolation/decimation``
    (L/M) through one polyphase FIR: conceptually the input is zero-stuffed by
    L, filtered by ``taps``, and every M-th full-rate output is emitted. This
    is the block GRC places as **Rational Resampler**.

    GR-verbatim parameters (names, defaults, semantics all pinned against LIVE
    GR 3.10 ``rational_resampler_fff``):

    * ``interpolation`` (L) / ``decimation`` (M) — must both be >= 1.
    * ``taps`` — the FIR taps. When EMPTY, GR designs an anti-image low-pass
      (``firdes.low_pass(gain=L', Fs=L', mid, trans, KAISER, beta=7)``) after
      first REDUCING (L, M) by their gcd; the reduced values become the
      operative rates (GR's ``interpolation()``/``decimation()`` getters return
      the reduced values — mirrored here). With user taps NO reduction happens.
      GR zero-pads the tap set to a multiple of L (its ``taps()`` getter shows
      the padded set); padding never changes the computed output.
    * ``fractional_bw`` — only used when ``taps`` is empty. ``<= 0`` selects
      GR's default 0.4; ``>= 0.5`` raises (GR-verbatim). The design arithmetic
      (rate/transition/mid-band) is done in float32 exactly as GR's C++, so the
      designed taps are float-bit-exact with GR (verified live; the hardware
      gate is Q15-exact tap parity per INV-16).

    OUTPUT ALIGNMENT (pinned live — do NOT "fix" to phase 0): with
    ``y_full`` = the zero-stuffed convolution (zero initial history) and
    ``K = ceil(len(taps)/L)`` taps per polyphase arm, GR emits

        ``y[k] = y_full[D + k*M]``,   ``D = L*(K-1)``

    i.e. the polyphase filter's history alignment starts the output D full-rate
    samples in. For K == 1 (len(taps) <= L) this is plain phase 0; for deeper
    filters GR's first output already spans x[0..K-1]. This differs from
    ``fir_filter_fff``/``interp_fir_filter_fff`` (both phase 0), so
    ``rational_resampler_fff(1, M, taps)`` is NOT sample-aligned with
    ``fir_filter_fff(M, taps)`` — verified live with impulse probes. The
    on-chip mod-M gate counter is seeded so the first emitted output is exactly
    ``y_full[D]``.

    OUTPUT COUNT: the chip emits every ``j >= D`` with ``j ≡ D (mod M)`` over
    the ``n*L`` full-rate indices — ``ceil((n*L - D)/M)`` outputs for ``n``
    inputs (deterministic). GR's scheduler emits a PREFIX of the same sequence
    on a finite stream (it under-runs the tail by up to a couple of outputs
    while forecasting input needs); on a continuous stream the sequences are
    identical. The verification gate pins the DUT count formula exactly and
    asserts GR's output equals the DUT prefix.

    ON-CHIP FORM (single cell, polyphase): the cell keeps a K-deep INPUT-rate
    delay line (shifted ONCE per input — never L times), and per input runs the
    L polyphase arms unrolled. Each arm p: a 4-word countdown mod-M gate
    (SUB/MOVE/BR.NZ/reload — flags survive MOVE per the ISA), then, only when
    the gate fires, the arm's MAC chain ``Σ_m h[p+mL]·x[n-m]`` (oldest-first,
    exactly the zero-stuff accumulation order with the zero terms skipped —
    bit-identical, since a wrapping add of an exact 0 is the identity) and the
    WRITE+JUMP emit. Skipped arms cost only the 4 gate words. Polyphase (vs the
    unrolled zero-stuff of the FIR interp path) is what makes the combo fit a
    cell at all: the MAC count per input is N, not N*L.

    Hardware deviations from filter.rational_resampler_fff (all RAISE loudly):

    * HW-DEVIATION: ``interpolation`` (after gcd reduction when auto-designing)
      is capped at 3, and the tap count is capped per L by the measured 32-word
      cell budget: L=1 -> 5 taps, L=2 -> 4, L=3 -> 3 (program
      ``K + N + 6L + 1`` words + N+2 data words + K+1 state registers <= the
      cell). Larger configurations raise with the composed
      ``UpsamplerBlock(sps=L) -> FIRFilterBlock(taps, decimation=M)``
      workaround (note: that chain is phase-0 aligned, i.e. GR's output delayed
      by D full-rate samples).
    * HW-DEVIATION: ``sum(|taps|) <= 1`` is required (Q15 accumulator
      headroom): the per-arm emit cannot also carry the saturating gain-restore
      in the remaining cell budget. Auto-designed taps have gain L' (> 1 for
      any real resampling ratio) AND >= ~17 taps, so the GR-verbatim empty-taps
      default always raises on-chip — supply small normalized taps and apply
      gain in a separate stage.
    * HW-DEVIATION: ``decimation`` <= 32767 (the mod-M reload constant is one
      signed 16-bit data word).
    """

    CATEGORY = "filtering"
    TAGS = ["resampler", "rational", "polyphase", "filter", "rate"]

    # Measured single-cell tap ceilings per interpolation L (probed against the
    # real resolver/build: the largest N that BUILDS and COMPUTES; above these
    # the state block no longer fits beside the unrolled arm programs).
    # L >= 4 fits nothing: the L*(gate+emit) fixed cost alone (>= 28 words)
    # exhausts the cell before a single tap.
    _RR_TAP_CAP = {1: 5, 2: 4, 3: 3}

    def __init__(self, name: str, interpolation: int = 1, decimation: int = 1,
                 taps: List[float] = (), fractional_bw: float = 0.0):
        interpolation = int(interpolation)
        decimation = int(decimation)
        if interpolation < 1:
            raise ValueError(
                f"interpolation must be > 0, got {interpolation}")
        if decimation < 1:
            raise ValueError(f"decimation must be > 0, got {decimation}")

        taps = list(taps or [])
        auto_designed = not taps
        if not taps:
            # GR auto-design path: reduce (L, M) by their gcd — the reduced
            # values are the OPERATIVE rates (GR's getters return them) — then
            # design the anti-image low-pass. fractional_bw is validated (and
            # defaulted) ONLY here, exactly as GR does.
            g = math.gcd(interpolation, decimation)
            interpolation //= g
            decimation //= g
            taps = self.design_filter(interpolation, decimation, fractional_bw)

        KyttarBlock.__init__(self, name, interpolation=interpolation,
                             decimation=decimation, taps=list(taps),
                             fractional_bw=fractional_bw)

        self._interpolation = interpolation
        self._decimation = decimation
        self._fractional_bw = fractional_bw
        self._coefficients = list(taps)
        self._num_taps = len(taps)

        L, M, N = interpolation, decimation, self._num_taps
        # K = taps per polyphase arm = the input-rate delay depth. GR pads the
        # tap set to a multiple of L (zeros), which never changes K or any
        # output value, so the pad is left virtual here.
        self._arm_depth = (N + L - 1) // L
        # GR's polyphase history alignment: first output = y_full[D].
        self._phase_offset = L * (self._arm_depth - 1)

        sum_abs = sum(abs(c) for c in taps)
        if sum_abs > 1.0:
            # HARDWARE DEVIATION: no room for the saturating gain-restore
            # (INV-13 headroom) beside the per-arm mod-M gate + emit. Raise
            # loudly rather than wrap on overload (never silently clamp).
            auto_note = f", auto-designed gain={L}" if auto_designed else ""
            raise ValueError(
                f"HARDWARE LIMIT: RationalResamplerBlock requires "
                f"sum(|taps|) <= 1 (got {sum_abs:.4f}{auto_note}"
                f"): the Q15 accumulator has no headroom-restore room beside "
                f"the per-arm mod-M gate. Scale the taps to sum(|taps|) <= 1 "
                f"and apply the gain in a separate stage (e.g. GainBlock), or "
                f"compose UpsamplerBlock(sps={L}) -> FIRFilterBlock(taps, "
                f"decimation={M}) (phase-0 aligned: GR's rational_resampler "
                f"output delayed by {self._phase_offset} full-rate samples).")
        self._head_shift = 0                       # S = 0 by construction
        self._coeff_q15 = [float_to_q15(c) for c in taps]

        if M > 0x7FFF:
            raise ValueError(
                f"HARDWARE LIMIT: decimation must be <= 32767 (one signed "
                f"16-bit mod-M reload word), got {M}.")
        cap = self._RR_TAP_CAP.get(L)
        if cap is None:
            raise ValueError(
                f"HARDWARE LIMIT: RationalResamplerBlock supports "
                f"interpolation <= 3 on this cell (measured budget: the L "
                f"unrolled polyphase arms cost 6 words each in gate+emit "
                f"alone; L={L} needs >= {6 * L + 2} program words before any "
                f"tap). Compose UpsamplerBlock(sps={L}) -> FIRFilterBlock("
                f"taps, decimation={M}) instead (phase-0 aligned: GR's "
                f"rational_resampler output delayed by {self._phase_offset} "
                f"full-rate samples).")
        if N > cap:
            auto_note = (" (auto-designed — the GR default Kaiser design "
                         "never fits a cell)" if auto_designed else "")
            raise ValueError(
                f"HARDWARE LIMIT: RationalResamplerBlock with interpolation="
                f"{L} supports at most {cap} taps in the single-cell budget "
                f"(measured: program K+N+6L+1 = "
                f"{self._arm_depth + N + 6 * L + 1} words + {N + 2} data "
                f"words + {self._arm_depth + 1} state registers for N={N} "
                f"overflows the 32-word cell), got {N} taps{auto_note}. "
                f"Compose UpsamplerBlock(sps={L}) -> FIRFilterBlock(taps, "
                f"decimation={M}) instead (phase-0 aligned: GR's "
                f"rational_resampler output delayed by {self._phase_offset} "
                f"full-rate samples).")

        self._delay_line = [0.0] * self._num_taps

    # ------------------------------------------------------------------ design
    @staticmethod
    def design_filter(interpolation: int, decimation: int,
                      fractional_bw: float) -> List[float]:
        """GR's ``rational_resampler`` anti-image low-pass design, replicated
        with the SAME float32 parameter arithmetic as the C++ (a float64
        replica lands one tap off at some ratios — the transition width crosses
        an ntaps rounding boundary). ``fractional_bw <= 0`` selects GR's
        default 0.4; ``>= 0.5`` raises. Returns the UNPADDED design (GR's
        ``taps()`` getter additionally zero-pads to a multiple of
        ``interpolation``; the pad never changes any output). Verified
        float-bit-exact against live GR across (L, M, fractional_bw); the
        hardware gate is Q15-exact tap parity (INV-16)."""
        if fractional_bw >= 0.5:
            raise ValueError(
                f"Invalid fractional_bandwidth {fractional_bw:.2f}, "
                f"must be in (0, 0.5)")
        if fractional_bw <= 0:
            fractional_bw = 0.4
        from . import _firdes

        beta = 7.0
        fbw = np.float32(fractional_bw)
        halfband = np.float32(0.5)
        rate = np.float32(interpolation) / np.float32(decimation)
        if rate >= 1.0:
            trans_width = np.float32(halfband - fbw)
            mid_transition_band = np.float32(
                halfband - trans_width / np.float32(2.0))
        else:
            trans_width = np.float32(rate * (halfband - fbw))
            mid_transition_band = np.float32(
                rate * halfband - trans_width / np.float32(2.0))
        return [float(t) for t in _firdes.low_pass(
            float(interpolation),        # gain
            float(interpolation),        # Fs
            float(mid_transition_band),  # transition mid point
            float(trans_width),          # transition width
            "kaiser", beta)]

    # -------------------------------------------------------------- geometry
    def _single_cell_max(self) -> int:
        # Always single-cell (the __init__ caps enforce N <= cap); this also
        # steers the inherited bit-exact reference machinery down its
        # single-cell accumulation-order branch, which is the order the
        # polyphase arms reproduce (zero terms skipped).
        return self._RR_TAP_CAP.get(self._interpolation, self._num_taps)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def fractional_bw(self) -> float:
        return self._fractional_bw

    # ------------------------------------------------------------- datapath
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """One cell: shift the K-deep input-rate delay line once, then run the
        L polyphase arms unrolled, each behind its own 4-word countdown mod-M
        gate. See the class docstring for the derivation; the arm MAC order is
        oldest-first (descending tap index), matching the inherited zero-stuff
        reference bit-for-bit."""
        L, M, N, K = (self._interpolation, self._decimation, self._num_taps,
                      self._arm_depth)
        data = [DataWord(f"c{i}", self._coeff_q15[i], address=i + 1)
                for i in range(N)]
        data.append(DataWord("decim", M, address=N + 1))
        data.append(DataWord("one", 1, address=N + 2))
        # Countdown gate: decrement per arm, emit when it hits 0, reload M.
        # Seeded at D+1 so the FIRST emit is full-rate index D — GR's polyphase
        # history alignment (y[0] = y_full[D]), pinned live.
        state = [StateVar(f"d{i}") for i in range(K)]
        state.append(StateVar("counter",
                              initial_value=self._phase_offset + 1))

        lines = []
        for i in range(K - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i + 1}}}")
        lines.append(f"    MOVE R{{state:d{K - 1}}}, R{{in:sample}}")
        for p in range(L):
            skip = f"_rr_skip_{p}"
            lines += [
                "    SUB R{state:counter}, R{data:one}",
                "    MOVE R{state:counter}, R0",       # flags survive MOVE
                f"    BR.NZ {skip}",
                "    MOVE R{state:counter}, R{data:decim}",
            ]
            arm = [p + m * L for m in range(K) if p + m * L < N]
            if arm:
                # Oldest-first: tap p+mL pairs with x[n-m] = d[K-1-m]; run m
                # from high to low so the accumulation order equals the
                # zero-stuff reference (its skipped terms are exact zeros).
                first = True
                for m in range(len(arm) - 1, -1, -1):
                    op = "MULQ" if first else "MACQ"
                    lines.append(
                        f"    {op} R{{state:d{K - 1 - m}}}, "
                        f"R{{data:c{p + m * L}}}")
                    first = False
            else:
                # Virtual zero-pad arm (N < multiple of L): the output is an
                # exact 0 (all its stuffed inputs are zeros).
                lines.append("    XOR R{state:d0}, R{state:d0}")
            lines += ["    {write:out}", "    {jump:out}"]
            lines.append(f"{skip}:")
        lines.append("    HALT")

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template="start:\n" + "\n".join(lines) + "\n",
        )}

    # ------------------------------------------------------------ references
    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 predictor: zero-stuff by L, run the inherited
        single-cell-order FIR model, emit ``full[D::M]`` (GR's polyphase
        alignment). One uint16 Q15 word per OUTPUT sample."""
        L, M = self._interpolation, self._decimation
        stuffed = []
        for s in input_q15:
            stuffed.append(int(s) & 0xFFFF)
            stuffed.extend([0] * (L - 1))
        full = self._full_fir_q15(stuffed)
        return full[self._phase_offset::M]

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference: zero-stuff, convolve, emit ``full[D::M]``."""
        L, M = self._interpolation, self._decimation
        x = np.asarray(input_samples, dtype=np.float32)
        stuffed = np.zeros(len(x) * L, dtype=np.float32)
        stuffed[::L] = x
        full = np.zeros(len(stuffed), dtype=np.float32)
        delay = [0.0] * self._num_taps
        for i, sample in enumerate(stuffed):
            delay = [float(sample)] + delay[:-1]
            full[i] = sum(c * d for c, d in zip(self._coefficients, delay))
        return full[self._phase_offset::M]

    def expected_output_count(self, n_inputs: int) -> int:
        """Deterministic on-chip output count for ``n_inputs`` inputs:
        the emitted full-rate indices are ``D, D+M, ...`` below ``n*L``."""
        span = n_inputs * self._interpolation - self._phase_offset
        if span <= 0:
            return 0
        return -(-span // self._decimation)

    def reset(self):
        self._delay_line = [0.0] * self._num_taps
