# SPDX-License-Identifier: GPL-3.0-or-later
"""FLLBandEdgeBlock — see :class:`FLLBandEdgeBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15


def _u16(v: int) -> int:
    return v & 0xFFFF


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _mq(a: int, b: int) -> int:
    """MULQ: (a*b) >> 15, truncating toward -inf (arithmetic shift)."""
    return _u16((_s16(a) * _s16(b)) >> 15)


class FLLBandEdgeBlock(KyttarBlock):
    """Band-edge FLL coarse frequency recovery = GNU Radio ``digital.fll_band_edge_cc``.

    The missing coarse-frequency stage of the industry RX cascade
    (MF -> **coarse FLL** -> timing -> fine DD carrier): pulls a large carrier
    offset (far beyond a Costas loop's pull-in) down to a residual the
    downstream Costas can capture. Parameters mirror
    ``digital.fll_band_edge_cc(samps_per_sym, rolloff, filter_size, bandwidth)``
    VERBATIM.

    Algorithm (pinned STRUCTURE-EXACT against live GR 3.10.12 — the pure-Python
    float model reproduces GR's own freq/phase/error output streams to float32
    rounding, max |Δfreq| ~2.5e-7 rad/sample over a 3000-sample acquisition):

    1. **Band-edge tap design** (pure Python, the ``_firdes`` pattern)::

           M  = rint(filter_size / sps);  k_i = -M + i*(2/sps)
           bb_i = sinc(rolloff*k_i - 0.5) + sinc(rolloff*k_i + 0.5)
           power = sum(bb_i^2)                       # GR 3.10 power-normalization
           t_i = bb_i / power;  kk_i = (i - (fs-1)//2) * 0.5/sps
           taps_lower[fs-1-i] = t_i * exp(-j*2pi*(1+rolloff)*kk_i)
           taps_upper = conj(taps_lower)             # element-wise

    2. **Per sample** (GR ``work``, newest-first dot products)::

           y[n]  = x[n] * exp(+j*phase)              # NCO de-rotation
           U     = sum_k taps_lower[k] * y[n-k]      # (GR's crossed naming)
           L     = sum_k taps_upper[k] * y[n-k]
           error = |L|^2 - |U|^2
           freq += beta*error;  phase += freq + alpha*error   # control_loop
           (alpha/beta from bandwidth with damping sqrt(2)/2, GR defaults)

    3. **The on-chip reduction.** With ``a = Re(taps_lower)``, ``b =
       Im(taps_lower)`` (real tap sets) and the four REAL dot products
       ``Ar = sum a*yi``, ``Aq = sum a*yq``, ``Br = sum b*yi``, ``Bq = sum b*yq``::

           error = 4*(Ar*Bq - Aq*Br)

       so the two complex band-edge correlators collapse to FOUR real-tap FIRs
       sharing TWO delay lines (the yi and yq histories) — half the MACs of the
       literal two-complex-filter form. The x4 (and the radians->phase-word unit
       change) is folded into the stored loop gains: ``ah = 4*alpha/pi``,
       ``bh = 4*beta/pi`` with phase as a 16-bit accumulator (65536 = one turn),
       matching GR's radian-domain loop dynamics.

    Cells (9 + 2*ceil(filter_size/3), laid out as a compact SERPENTINE fold —
    head row + boustrophedon column pairs, NO enclosed interior — whose last
    cell (``pi``) lands at/near (1,1) so the loop feedback returns through a
    short traced corridor into ``phase``'s SOUTH face; see
    :meth:`default_layout`):

      * ``phase``      — NCO phase accumulator; ``phase += dphase`` (feedback);
                         emits sin/cos phase words; forwards xi/xq; INV-19
                         serialize-LOCK after each launch.
      * ``sin_fold``/``cos_fold``/``table_sin``/``table_cos`` — the proven
        quarter-wave NCO (identical to ComplexCostasLoopBlock).
      * ``rotate``     — complex multiply ``y = x * exp(+j*phase)`` (the Costas
                         order-4 rotate cell with sinv = +sin).
      * ``fanout``     — dual-face output cell: forwards yi -> I-chain head and
                         yq -> Q-chain head (internal), taps the corrected
                         complex pair (``yi_tap``, ``yq_tap``) out (external).
      * ``ci0..``/``cq0..`` — the band-edge correlator chains: each cell holds a
        3-tap segment of ONE delay line (yi or yq history) with BOTH real tap
        sets (a, b) and forwards two partial sums — the FIRFilterBlock systolic
        wavefront with two coefficient sets over one delay line.
      * ``berr``       — ``err = MULQ(Ar,Bq) - MULQ(Aq,Br)`` (+ the INV-13
                         saturating gain restore when the tap sets carry
                         coefficient headroom).
      * ``pi``         — the loop filter: ``freq += bh*err`` with a
                         FULL-PRECISION ERROR-FEEDBACK accumulator (the RMSBlock
                         idiom — a bare MULQ truncates to zero for small errors
                         and stalls the integrator), ``dphase = freq + ah*err``,
                         feedback WRITE to ``phase`` + the INV-19 lock-clear
                         ``WRITE.CFG``.

    Execution is FULLY SERIAL per sample (one trigger chain along the fold), so
    there is no reconvergent-fan-in race (INV-20); the ``phase`` arbiter LOCK +
    ``pi`` lock-clear (INV-19) serializes samples under saturated drive.

    Hardware deviations from ``digital.fll_band_edge_cc`` (all Q15/ISA-forced):

    * **filter_size <= 27** (# HARDWARE DEVIATION): each correlator chain cell
      holds 3 taps and the serpentine fold keeps both footprint dimensions
      <= 7 (INV-9 sharpened by the <=7-wide big-block routability lesson), so
      ceil(filter_size/3) <= 9 — the verified envelope. Larger sizes RAISE.
      (GR commonly runs fine at such sizes; the band-edge S-curve only
      sharpens with more taps.)
    * **bandwidth range** (# HARDWARE DEVIATION): the folded Q15 loop gains
      ``4*alpha/pi`` and ``4*beta/pi`` must be < 1 (Q15-representable);
      bandwidth beyond ~0.55 RAISES. Practical FLL bandwidths are << that.
    * **control_loop frequency_limit is not enforced** (# HARDWARE DEVIATION):
      GR clamps freq to +-2pi*(2/sps) rad/sample; the 16-bit frequency word
      inherently bounds |freq| <= 0.5 cycles/sample, which for sps <= 4 is
      tighter than or equal to GR's limit (vacuous), and the limit only ever
      engages in runaway scenarios far outside the verified acquisition
      envelope (GR's own freq stream stays orders of magnitude below it).
    * **error clipping**: the Q15 error word saturates at +-1.0 where GR 3.10
      float does not clip. |error| > 1 occurs only in extreme transients;
      GR <= 3.8 clipped at exactly +-1.0 as well.
    * Only the corrected complex stream is output (GR's optional freq/phase/
      error debug streams are not brought out).
    """

    CATEGORY = "recovery"
    TAGS = ["fll", "band_edge", "frequency_recovery", "coarse", "complex",
            "recovery"]

    QUARTER_SIZE = 17          # quarter-wave sine table (as ComplexCostasLoop/NCO)
    TAPS_PER_CELL = 3          # per correlator-chain cell (2 tap sets, 1 delay line)
    MAX_CHAIN_CELLS = 9        # fold ceiling: the verified envelope (fs <= 27)
    MAX_FILTER_SIZE = TAPS_PER_CELL * MAX_CHAIN_CELLS
    SAT_POS_Q15 = 0x7FFF

    # INV-22: ``pipeline_lock`` is a substrate hint (the INV-19 serialize-LOCK),
    # not a GR DSP parameter — intentionally not exposed in GRC.
    GRC_UNSUPPORTED_PARAMS = ("pipeline_lock",)

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    def __init__(
        self,
        name: str,
        samps_per_sym: float = 2.0,
        rolloff: float = 0.35,
        filter_size: int = 17,
        bandwidth: float = 0.06,
        pipeline_lock: bool = True,
    ):
        """
        Args:
            name: Block name.
            samps_per_sym: Number of samples per symbol (GR verbatim; > 0).
            rolloff: Rolloff (excess bandwidth) of the signal's pulse shaping
                filter, in [0, 1] (GR verbatim).
            filter_size: Number of band-edge prototype filter taps (GR
                verbatim). HARDWARE DEVIATION: <= 27 on this chip (see class
                docstring); larger raises.
            bandwidth: Loop bandwidth (GR verbatim). HARDWARE DEVIATION: must
                keep the folded Q15 loop gains < 1 (bandwidth beyond ~0.55
                raises; see class docstring).
            pipeline_lock: When True (default) the ``phase`` cell LOCKs its
                arbiter to the feedback face after each launch and ``pi`` clears
                it per sample (INV-19) so the loop survives saturated drive.
        """
        # GR's own constructor validation, mirrored.
        if not samps_per_sym > 0:
            raise ValueError(
                f"fll_band_edge: invalid number of sps. Must be > 0, got {samps_per_sym}")
        if rolloff < 0 or rolloff > 1.0:
            raise ValueError(
                f"fll_band_edge: invalid rolloff factor. Must be in [0,1], got {rolloff}")
        if int(filter_size) <= 0:
            raise ValueError(
                f"fll_band_edge: invalid filter size. Must be > 0, got {filter_size}")
        # HARDWARE DEVIATION: chain fold ceiling (see class docstring).
        if int(filter_size) > self.MAX_FILTER_SIZE:
            raise ValueError(
                f"FLLBandEdgeBlock: filter_size {filter_size} exceeds the chip "
                f"ceiling {self.MAX_FILTER_SIZE} (ceil(fs/{self.TAPS_PER_CELL}) "
                f"correlator cells per chain must fold <= {self.MAX_CHAIN_CELLS} "
                f"on this array, INV-9). HW-DEVIATION — use a smaller prototype.")
        if not bandwidth > 0:
            raise ValueError(
                f"fll_band_edge: invalid bandwidth. Must be > 0, got {bandwidth}")

        super().__init__(name, samps_per_sym=float(samps_per_sym),
                         rolloff=float(rolloff), filter_size=int(filter_size),
                         bandwidth=float(bandwidth))
        self._sps = float(samps_per_sym)
        self._rolloff = float(rolloff)
        self._filter_size = int(filter_size)
        self._bandwidth = float(bandwidth)
        self._pipeline_lock = bool(pipeline_lock)

        # --- band-edge tap design (float, GR-structure-exact) ---
        a, b = self.design_band_edge_taps(self._sps, self._rolloff,
                                          self._filter_size)
        self._taps_a_float = a
        self._taps_b_float = b

        # --- INV-13 coefficient headroom on the tap sets ---
        # Guarantees (i) each running dot product |A|,|B| <= 1 (no mid-chain
        # wrap) and (ii) |P1 - P2| <= 1 in berr's SUB (no error wrap). The
        # scaled-down error is restored with a saturating << 2S in berr.
        S = 0
        while S <= 4:
            g = 2.0 ** -S
            sa = float(np.sum(np.abs(a))) * g
            sb = float(np.sum(np.abs(b))) * g
            if max(sa, sb) <= 1.0 and 2.0 * sa * sb <= 1.0:
                break
            S += 1
        else:
            raise ValueError(
                f"FLLBandEdgeBlock: band-edge tap sums too large to headroom-scale "
                f"(sum|a|={np.sum(np.abs(a)):.3f}, sum|b|={np.sum(np.abs(b)):.3f})")
        self._head_shift = S
        self._aq = [float_to_q15(v * (2.0 ** -S)) for v in a]
        self._bq = [float_to_q15(v * (2.0 ** -S)) for v in b]

        # --- loop gains (control_loop, damping = sqrt(2)/2 = GR default) ---
        damping = math.sqrt(2.0) / 2.0
        denom = 1.0 + 2.0 * damping * bandwidth + bandwidth * bandwidth
        self._alpha = (4.0 * damping * bandwidth) / denom
        self._beta = (4.0 * bandwidth * bandwidth) / denom
        ah = 4.0 * self._alpha / math.pi
        bh = 4.0 * self._beta / math.pi
        # HARDWARE DEVIATION: gains must be Q15-representable (see class docstring).
        if ah >= 1.0 or bh >= 1.0:
            raise ValueError(
                f"FLLBandEdgeBlock: bandwidth {bandwidth} maps to a Q15 loop gain "
                f">= 1 (ah={ah:.3f}, bh={bh:.3f}). HW-DEVIATION — use a smaller "
                f"loop bandwidth.")
        self._ah_q15 = float_to_q15(ah)
        self._bh_q15 = float_to_q15(bh)

        # Correlator chain segmentation.
        self._n_chain = (self._filter_size + self.TAPS_PER_CELL - 1) \
            // self.TAPS_PER_CELL
        offs = list(range(0, self._filter_size, self.TAPS_PER_CELL))
        self._seg_offsets = offs + [self._filter_size]

    # ------------------------------------------------------------------ design
    @staticmethod
    def design_band_edge_taps(sps: float, rolloff: float,
                              filter_size: int) -> Tuple[np.ndarray, np.ndarray]:
        """The GR ``design_filter`` reproduced in pure Python (float64).

        Returns ``(a, b)`` — the real and imaginary parts of ``taps_lower`` in
        GR's stored (time-reversed) order, which is NEWEST-FIRST for the
        per-sample dot product ``sum_k taps[k] * y[n-k]``. Structure-exact vs
        live GR 3.10.12 (proven transitively: the float model built on these
        taps reproduces GR's own error/freq output streams to float32 rounding,
        and every tap agrees with GR's ``print_taps`` to its printed precision).
        """
        fs = int(filter_size)
        M = int(np.rint(float(fs) / sps))
        k = -M + np.arange(fs) * (2.0 / sps)
        pos = rolloff * k
        bb = np.sinc(pos - 0.5) + np.sinc(pos + 0.5)
        power = float(np.sum(bb * bb))
        N = (fs - 1) // 2
        kk = (np.arange(fs) - N) * (0.5 / sps)
        lower = (bb / power) * np.exp(-1j * 2.0 * np.pi * (1.0 + rolloff) * kk)
        lower = lower[::-1]
        return np.real(lower).copy(), np.imag(lower).copy()

    @property
    def cell_count(self) -> int:
        return 9 + 2 * self._n_chain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _quarter_wave_table(self) -> List[int]:
        return [
            int(round(math.sin(k / 16 * math.pi / 2) * 32767)) & 0xFFFF
            for k in range(self.QUARTER_SIZE)
        ]

    # ------------------------------------------------------------ cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        qt = self._quarter_wave_table()

        # --- phase cell: phase += dphase (feedback); ph_sin = phase (+sin —
        # the FLL rotates by exp(+j*phase), unlike the Costas' -phase);
        # ph_cos = phase + pi/2; forwards xi/xq; INV-19 lock tail. ---
        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("dphase", register=2)],
            outputs=[Port("ph_sin"), Port("ph_cos"),
                     Port("xi_fwd"), Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=([DataWord("quarter", 16384, address=3)]
                  + ([DataWord("lock_face", 0, address=4, is_face=True),
                      DataWord("one", 1, address=5)]
                     if self._pipeline_lock else [])),
            # LOOP MEMORY: the NCO phase accumulator — cold-start per packet.
            state=[StateVar("phase", register=6, reset_per_batch=True),
                   StateVar("xis", register=7), StateVar("xqs", register=8)],
            assembly_template=("""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    ADD R{state:phase}, R{in:dphase}
    MOVE R{state:phase}, R0
    {write:ph_sin}
    ADD R0, R{data:quarter}
    {write:ph_cos}
    MOVE R0, R{state:xis}
    {write:xi_fwd}
    MOVE R0, R{state:xqs}
    {write:xq_fwd}
    {jump:trig}
""" + ("""\
    MOVE R0, R{data:lock_face}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
""" if self._pipeline_lock else "")),
        )

        # --- quarter-wave fold + table cells: verbatim ComplexCostasLoop NCO ---
        def _fold_cell():
            return CellProgram(
                inputs=[Port("phase", register=0)],
                outputs=[Port("neg"), Port("idx"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("thirtytwo", 32, address=1),
                      DataWord("fifteen", 15, address=2),
                      DataWord("sixteen", 16, address=3),
                      DataWord("zero", 0, address=4)],
                state=[StateVar("ph", register=5), StateVar("fidx", register=6),
                       StateVar("loc", register=7)],
                assembly_template="""\
start:
    MOVE R{state:ph}, R{in:phase}
    SHR R{state:ph}, #10
    MOVE R{state:fidx}, R0
    AND R{state:fidx}, R{data:thirtytwo}
    {write:neg}
    AND R{state:fidx}, R{data:fifteen}
    MOVE R{state:loc}, R0
    AND R{state:fidx}, R{data:sixteen}
    CMP R0, R{data:zero}
    BR.Z nomir
    SUB R{data:sixteen}, R{state:loc}
    MOVE R{state:loc}, R0
nomir:
    MOVE R0, R{state:loc}
    {write:idx}
    {jump:trig}
""",
            )

        def _table_cell():
            data = [DataWord(f"qt{i}", v, address=2 + i)
                    for i, v in enumerate(qt)]
            data += [DataWord("tbase", 2, address=19),
                     DataWord("zero", 0, address=20)]
            return CellProgram(
                inputs=[Port("idx", register=0), Port("neg", register=1)],
                outputs=[Port("val"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data, state=[StateVar("v", register=21)],
                assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    MOVE R{state:v}, R0
    CMP R{in:neg}, R{data:zero}
    BR.Z out
    SUB R{data:zero}, R{state:v}
out:
    {write:val}
    {jump:trig}
""",
            )

        # --- rotate: y = x * exp(+j*phase); plain forward complex multiply
        # (the proven ComplexCostasLoop order-4 rotate — sinv here is +sin). ---
        rotate_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)],
            state=[StateVar("xis", register=5), StateVar("xqs", register=6),
                   StateVar("sv", register=7), StateVar("cv", register=8),
                   StateVar("acc", register=9)],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    MOVE R{state:sv}, R{in:sinv}
    MOVE R{state:cv}, R{in:cosv}
    MULQ R{state:xis}, R{state:cv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:sv}
    SUB R{state:acc}, R0
    {write:yi}
    MULQ R{state:xis}, R{state:sv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:cv}
    ADD R{state:acc}, R0
    {write:yq}
    {jump:trig}
""",
        )

        # --- fanout: dual-face output cell. INTERNAL: forwards yi -> I-chain
        # head and yq -> Q-chain head (the yq WRITE transits the I chain on the
        # fold's forward faces), one trigger to the I head. EXTERNAL: taps the
        # corrected complex pair out (the LAST TWO WRITEs + tap_trig, so the
        # build's 2-rail port-egress patch steers both rails; the qpd idiom). ---
        fanout_cell = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("yi_fwd"), Port("yq_fwd"), Port("trig"),
                     Port("yi_tap"), Port("yq_tap"), Port("tap_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_internal", 1, address=2, is_face=True),
                  DataWord("face_tap", 1, address=3, is_face=True)],
            state=[StateVar("yis", register=4), StateVar("yqs", register=5)],
            assembly_template="""\
start:
    MOVE R{state:yis}, R{in:yi}
    MOVE R{state:yqs}, R{in:yq}
    MOVE [FACE], R{data:face_internal}
    MOVE R0, R{state:yis}
    {write:yi_fwd}
    MOVE R0, R{state:yqs}
    {write:yq_fwd}
    {jump:trig}
    MOVE [FACE], R{data:face_tap}
    MOVE R0, R{state:yis}
    {write:yi_tap}
    MOVE R0, R{state:yqs}
    {write:yq_tap}
    {jump:tap_trig}
""",
        )

        # --- correlator chain cells (ci* over yi history, cq* over yq history):
        # one delay-line segment, TWO tap sets (a, b), two partial sums — the
        # FIR systolic wavefront with a doubled MAC chain. ---
        def _chain_cell(seg_a: List[int], seg_b: List[int],
                        is_first: bool, is_last: bool) -> CellProgram:
            L = len(seg_a)
            # d{j} holds the OLDER samples at low j; taps stored reversed so
            # d{j} pairs with tap index start + (L-1-j) — the complex-FIR idiom.
            ra = list(reversed(seg_a))
            rb = list(reversed(seg_b))
            data = [DataWord(f"a{i}", ra[i], address=1 + i) for i in range(L)]
            data += [DataWord(f"b{i}", rb[i], address=1 + L + i)
                     for i in range(L)]
            dbase = 1 + 2 * L
            state = [StateVar(f"d{i}", register=dbase + i, reset_per_batch=True)
                     for i in range(L)]
            if not is_last:
                state.append(StateVar("osave", register=dbase + L))
            inputs = [Port("x", register=0)]
            if not is_first:
                inputs += [Port("pa"), Port("pb")]
            if is_last:
                outputs = [Port("pa_out"), Port("pb_out"), Port("fwd")]
            else:
                outputs = [Port("pa_out"), Port("pb_out"), Port("x_out"),
                           Port("fwd")]
            lines: List[str] = []
            if not is_last:
                lines.append("    MOVE R{state:osave}, R{state:d0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i + 1}}}")
            lines.append(f"    MOVE R{{state:d{L - 1}}}, R{{in:x}}")
            lines.append("    MULQ R{state:d0}, R{data:a0}")
            for i in range(1, L):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:a{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:pa}")
            lines.append("    {write:pa_out}")
            lines.append("    MULQ R{state:d0}, R{data:b0}")
            for i in range(1, L):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:b{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:pb}")
            lines.append("    {write:pb_out}")
            if not is_last:
                lines.append("    MOVE R0, R{state:osave}")
                lines.append("    {write:x_out}")
            lines.append("    {jump:fwd}")
            return CellProgram(
                inputs=inputs, outputs=outputs,
                entries=[EntryPoint("default")],
                data=data, state=state,
                assembly_template="start:\n" + "\n".join(lines) + "\n",
            )

        # --- berr: err = MULQ(Ar,Bq) - MULQ(Aq,Br), with the INV-13 saturating
        # << 2S gain restore when the tap sets are headroom-scaled. ---
        S2 = 2 * self._head_shift
        berr_data = []
        berr_state = [StateVar("p1", register=4)]
        if S2 > 0:
            berr_data = [DataWord("bias", 1 << (15 - S2), address=5),
                         DataWord("satpos", self.SAT_POS_Q15, address=6)]
            berr_state.append(StateVar("accs", register=7))
            berr_tail = f"""\
    MOVE R{{state:accs}}, R0
    ADD R{{state:accs}}, R{{data:bias}}
    SHR R0, #{16 - S2}
    BR.NZ besat
    SHL R{{state:accs}}, #{S2}
    {{write:err}}
    {{jump:trig}}
    HALT
besat:
    SHR R{{state:accs}}, #15
    ADD R0, R{{data:satpos}}
    {{write:err}}
    {{jump:trig}}
"""
        else:
            berr_tail = """\
    {write:err}
    {jump:trig}
"""
        berr_cell = CellProgram(
            inputs=[Port("Ar", register=0), Port("Bq", register=1),
                    Port("Aq", register=2), Port("Br", register=3)],
            outputs=[Port("err"), Port("trig")],
            entries=[EntryPoint("default")],
            data=berr_data, state=berr_state,
            assembly_template="""\
start:
    MULQ R{in:Ar}, R{in:Bq}
    MOVE R{state:p1}, R0
    MULQ R{in:Aq}, R{in:Br}
    SUB R{state:p1}, R0
""" + berr_tail,
        )

        # --- pi: freq integrator with FULL-PRECISION ERROR FEEDBACK (the
        # RMSBlock idiom: bh*err = (MULQ<<15) + (MUL & 0x7FFF) exactly, the
        # fractional part accumulates in facc so no increment is ever lost —
        # a bare MULQ stalls the integrator below |err*bh| < 2^15), then
        # dphase = freq + MULQ(ah, err), the INV-19 lock-clear WRITE.CFG, and
        # the dphase feedback WRITE to the phase cell. ---
        pi_cell = CellProgram(
            inputs=[Port("err", register=0)],
            outputs=[Port("dphase"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("bh", self._bh_q15, address=1),
                  DataWord("ah", self._ah_q15, address=2),
                  DataWord("mask", 0x7FFF, address=3)],
            # LOOP MEMORY: freq (the tracked offset) + facc (its fractional
            # accumulator) cold-start per packet.
            state=[StateVar("errs", register=4),
                   StateVar("hi", register=5),
                   StateVar("t", register=6),
                   StateVar("facc", register=7, reset_per_batch=True),
                   StateVar("freq", register=8, reset_per_batch=True),
                   StateVar("dph", register=9)],
            assembly_template=("""\
start:
    MOVE R{state:errs}, R{in:err}
    MULQ R{state:errs}, R{data:bh}
    MOVE R{state:hi}, R0
    MUL R{state:errs}, R{data:bh}
    AND R0, R{data:mask}
    ADD R0, R{state:facc}
    MOVE R{state:t}, R0
    AND R0, R{data:mask}
    MOVE R{state:facc}, R0
    SHR R{state:t}, #15
    ADD R0, R{state:hi}
    ADD R0, R{state:freq}
    MOVE R{state:freq}, R0
    MULQ R{state:errs}, R{data:ah}
    ADD R0, R{state:freq}
""" + ("""\
    MOVE R{state:dph}, R0
    SUB R{state:errs}, R{state:errs}
    WRITE.CFG @1, 4
    MOVE R0, R{state:dph}
    {write:dphase}
    {jump:trig}
""" if self._pipeline_lock else """\
    {write:dphase}
    {jump:trig}
""")),
        )

        cells: Dict[str, CellProgram] = {
            "phase": phase_cell,
            "sin_fold": _fold_cell(),
            "cos_fold": _fold_cell(),
            "table_sin": _table_cell(),
            "table_cos": _table_cell(),
            "rotate": rotate_cell,
            "fanout": fanout_cell,
        }
        offs = self._seg_offsets
        for m in range(self._n_chain):
            s, e = offs[m], offs[m + 1]
            cells[f"ci{m}"] = _chain_cell(
                self._aq[s:e], self._bq[s:e],
                is_first=(m == 0), is_last=(m == self._n_chain - 1))
        for m in range(self._n_chain):
            s, e = offs[m], offs[m + 1]
            cells[f"cq{m}"] = _chain_cell(
                self._aq[s:e], self._bq[s:e],
                is_first=(m == 0), is_last=(m == self._n_chain - 1))
        cells["berr"] = berr_cell
        cells["pi"] = pi_cell
        return cells

    # ------------------------------------------------------------- connections
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        n = self._n_chain
        conns: List[Tuple[Any, str, Any, str]] = [
            ("phase", "ph_sin", "sin_fold", "phase"),
            ("phase", "ph_cos", "cos_fold", "phase"),
            ("phase", "xi_fwd", "rotate", "xi"),
            ("phase", "xq_fwd", "rotate", "xq"),
            ("sin_fold", "idx", "table_sin", "idx"),
            ("sin_fold", "neg", "table_sin", "neg"),
            ("cos_fold", "idx", "table_cos", "idx"),
            ("cos_fold", "neg", "table_cos", "neg"),
            ("table_sin", "val", "rotate", "sinv"),
            ("table_cos", "val", "rotate", "cosv"),
            ("rotate", "yi", "fanout", "yi"),
            ("rotate", "yq", "fanout", "yq"),
            ("fanout", "yi_fwd", "ci0", "x"),
            ("fanout", "yq_fwd", "cq0", "x"),
        ]
        for pre in ("ci", "cq"):
            for m in range(n - 1):
                conns += [
                    (f"{pre}{m}", "pa_out", f"{pre}{m + 1}", "pa"),
                    (f"{pre}{m}", "pb_out", f"{pre}{m + 1}", "pb"),
                    (f"{pre}{m}", "x_out", f"{pre}{m + 1}", "x"),
                ]
        conns += [
            (f"ci{n - 1}", "pa_out", "berr", "Ar"),
            (f"ci{n - 1}", "pb_out", "berr", "Br"),
            (f"cq{n - 1}", "pa_out", "berr", "Aq"),
            (f"cq{n - 1}", "pb_out", "berr", "Bq"),
            ("berr", "err", "pi", "err"),
            # FEEDBACK: pi dphase -> phase (loop closure through the fold's
            # short transit corridor into phase's SOUTH face; traced by
            # _apply_internal_feedback, which also co-patches pi's lock-clear
            # WRITE.CFG hop).
            ("pi", "dphase", "phase", "dphase"),
        ]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        n = self._n_chain
        jumps: List[Tuple[Any, str, Any, str]] = [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "rotate", "default"),
            ("rotate", "trig", "fanout", "default"),
            ("fanout", "trig", "ci0", "default"),
            # fanout's SECOND JUMP fires the downstream consumer of the tapped
            # pair; unconsumed (standalone) it self-terminates (the qpd idiom).
            ("fanout", "tap_trig", "__terminate__", "default"),
        ]
        for pre, nxt in (("ci", "cq0"), ("cq", "berr")):
            for m in range(n - 1):
                jumps.append((f"{pre}{m}", "fwd", f"{pre}{m + 1}", "default"))
            jumps.append((f"{pre}{n - 1}", "fwd", nxt, "default"))
        jumps += [
            ("berr", "trig", "pi", "default"),
            ("pi", "trig", "__terminate__", "default"),
        ]
        return jumps

    # ------------------------------------------------------------------ layout
    _HEAD_W = 7                # head-row width = the 7 head cells (<= 7, INV-9)

    def _pair_heights(self) -> List[int]:
        """Balanced depths (a, b, c) of the three chain column PAIRS.

        The 2*n_chain + 2 chain cells (ci*, cq*, berr, pi) fold into up to
        three boustrophedon column pairs — (6,5), (4,3), (2,1) — each pair
        holding ``2*h`` cells (down the east column, across, up the west
        column). ``a + b + c = n_chain + 1`` exactly (the chain count is
        always even), balanced so the fold stays as SHALLOW as possible:
        max depth 4 at the fs=27 ceiling -> total height 5 <= 7 (INV-9)."""
        half = self._n_chain + 1
        parts = [half // 3] * 3
        for i in range(half % 3):
            parts[i] += 1
        if parts[0] + 1 > 7:   # unreachable at MAX_CHAIN_CELLS=9; guard anyway
            raise ValueError(
                f"FLLBandEdgeBlock: chain of {2 * half} cells cannot serpentine "
                f"within a 7-tall fold (INV-9)")
        return parts

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Compact SERPENTINE fold (replaces the perimeter RING, whose enclosed
        interior was unusable dead area). Both dimensions <= 7 (the <=7-wide
        big-block routability rule); NO enclosed interior — every non-block
        cell inside the bounding box sits on an open edge.

        Row 0 holds the 7 head cells west -> east; the chain cells then snake
        through three column PAIRS right-to-left below it (down the east
        column of a pair, across, up its west column), so the fully-serial
        trigger chain is ONE connected face-path with every consecutive
        handoff @1-abutted — exactly the ring's corridor property, minus the
        hollow middle. Default fs=17 (n_chain=6, pair depths 3/2/2)::

            col:    0     1     2     3     4     5     6
            row 0: phase sin_f cos_f tbl_s tbl_c rot   fanout   (all E; fanout S)
            row 1: fb0   pi    cq4   cq3   cq0   ci5   ci0
            row 2:       berr  cq5   cq2   cq1   ci4   ci1
            row 3:                               ci3   ci2

        Long internal handoffs (phase->rotate xi/xq, fanout->cq0 crossing the
        whole I chain, ci-tail->berr crossing the Q chain) ride this single
        fwd-face corridor as HOP<31 transit words, unchanged from the ring.

        The LAST chain cell ``pi`` lands at (1,1) whenever the third pair is
        occupied (n_chain >= 2), i.e. directly WEST of the one transit cell
        (0,1), whose NORTH face closes the loop into ``phase``'s SOUTH face —
        the lock_face reset default (INV-19), same closure geometry as the
        ring and the Costas 4x2. For n_chain=1 (fs<=3) the chain ends earlier
        on row 1 and the leftover row-1 slots down to (0,1) are declared
        ``transit_fb_*`` cells (face-only, first-class, last in the layout per
        INV-33); ``_apply_internal_feedback`` traces the corridor either way.

        I/O: input (phase, (0,0)) and output (fanout, (6,0)) both on the TOP
        edge — the same bus-facing edge the 8-wide ring presented (INV-8), so
        an existing placement's port geometry is preserved while the footprint
        shrinks from 8x5 to 7x4 at fs=17."""
        heads = ["phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
                 "rotate", "fanout"]
        chain = [f"ci{m}" for m in range(self._n_chain)]
        chain += [f"cq{m}" for m in range(self._n_chain)]
        chain += ["berr", "pi"]
        layout: Dict[Any, Tuple[int, int, str]] = {}
        # Head row, west -> east; fanout turns SOUTH into the chain fold.
        for x, cid in enumerate(heads):
            layout[cid] = (x, 0, "east" if x < self._HEAD_W - 1 else "south")
        # Chain slots: boustrophedon column pairs, right to left.
        slots: List[Tuple[int, int, str]] = []
        for (ce, cw, h) in zip((6, 4, 2), (5, 3, 1), self._pair_heights()):
            if h <= 0:
                break
            for y in range(1, h + 1):           # down the pair's east column
                slots.append((ce, y, "south" if y < h else "west"))
            for y in range(h, 0, -1):           # up the pair's west column
                slots.append((cw, y, "north" if y > 1 else "west"))
        assert len(slots) == len(chain), (len(slots), len(chain))
        for cid, s in zip(chain, slots):
            layout[cid] = s
        # Feedback return: pi's WEST neighbours along row 1 down to (0,1),
        # whose NORTH face lands in phase's SOUTH face (face-only transit
        # cells — first-class block cells, last in the layout per INV-33).
        pi_col = layout["pi"][0]
        for t, x in enumerate(range(pi_col - 1, 0, -1)):
            layout[f"transit_fb_{t}"] = (x, 1, "west")
        layout[f"transit_fb_{pi_col - 1}"] = (0, 1, "north")
        return layout

    def output_cell_id(self) -> Any:
        """The corrected complex pair leaves the (mid-block) fanout cell."""
        return "fanout"

    # -------------------------------------------------------------- references
    def process_reference_q15(self, iq_q15) -> List[Tuple[int, int]]:
        """Bit-exact predictor of the on-chip datapath: returns the corrected
        (yi, yq) u16 pair per input (xi, xq) u16 pair."""
        qt = self._quarter_wave_table()

        def qw(ph16):
            fi = (ph16 >> 10) & 0x3F
            neg = (fi & 32) != 0
            mir = (fi & 16) != 0
            lo = fi & 15
            if mir:
                lo = 16 - lo
            v = qt[lo]
            return _u16(-_s16(v)) if neg else v

        fs = self._filter_size
        S2 = 2 * self._head_shift
        aq, bq = self._aq, self._bq
        ah, bh = self._ah_q15, self._bh_q15
        phase = 0
        freq = 0
        facc = 0
        dphase = 0
        dli = [0] * fs   # newest at index 0
        dlq = [0] * fs
        out: List[Tuple[int, int]] = []
        for (xi, xq) in iq_q15:
            xi = int(xi) & 0xFFFF
            xq = int(xq) & 0xFFFF
            phase = _u16(phase + dphase)
            sinv = qw(phase)
            cosv = qw(_u16(phase + 16384))
            yi = _u16(_s16(_mq(xi, cosv)) - _s16(_mq(xq, sinv)))
            yq = _u16(_s16(_mq(xi, sinv)) + _s16(_mq(xq, cosv)))
            out.append((yi, yq))
            dli = [yi] + dli[:-1]
            dlq = [yq] + dlq[:-1]
            Ar = Br = Aq = Bq = 0
            for k in range(fs):
                Ar = _u16(Ar + _mq(aq[k], dli[k]))
                Br = _u16(Br + _mq(bq[k], dli[k]))
                Aq = _u16(Aq + _mq(aq[k], dlq[k]))
                Bq = _u16(Bq + _mq(bq[k], dlq[k]))
            err = _u16(_s16(_mq(Ar, Bq)) - _s16(_mq(Aq, Br)))
            if S2 > 0:
                # berr's saturating << 2S restore (INV-13).
                e = _s16(err)
                lo_b, hi_b = -(1 << (15 - S2)), (1 << (15 - S2)) - 1
                if e < lo_b:
                    err = 0x8000
                elif e > hi_b:
                    err = 0x7FFF
                else:
                    err = _u16(e << S2)
            # pi: freq integrator with the exact error-feedback split.
            prod = _s16(err) * _s16(bh)
            hi = _u16(prod >> 15)
            lo15 = prod & 0x7FFF
            t = facc + lo15
            facc = t & 0x7FFF
            carry = t >> 15
            freq = _u16(freq + hi + carry)
            dphase = _u16(freq + _s16(_mq(err, ah)))
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference = the GR-structure-exact float model (float64) on the
        SAME taps/gains structure, WITHOUT GR's scheduler-history input delay
        (GR's stream lags its input by ``filter_size`` samples; the chip does
        not). Returns the corrected complex output."""
        arr = np.asarray(input_samples)
        if not np.iscomplexobj(arr):
            arr = arr.astype(np.float64) + 0j
        a, b = self._taps_a_float, self._taps_b_float
        taps_lower = a + 1j * b
        taps_upper = np.conj(taps_lower)
        alpha, beta = self._alpha, self._beta
        fs = self._filter_size
        fmax = 2 * np.pi * (2.0 / self._sps)
        phase = 0.0
        freq = 0.0
        buf = np.zeros(fs, dtype=np.complex128)
        out = np.zeros(len(arr), dtype=np.complex128)
        for n in range(len(arr)):
            y = arr[n] * np.exp(1j * phase)
            out[n] = y
            buf[1:] = buf[:-1]
            buf[0] = y
            out_upper = np.dot(taps_lower, buf)   # GR's crossed naming
            out_lower = np.dot(taps_upper, buf)
            e = (out_lower.real ** 2 + out_lower.imag ** 2) \
                - (out_upper.real ** 2 + out_upper.imag ** 2)
            freq += beta * e
            phase += freq + alpha * e
            while phase > 2 * np.pi:
                phase -= 2 * np.pi
            while phase < -2 * np.pi:
                phase += 2 * np.pi
            freq = min(fmax, max(-fmax, freq))
        return out.astype(np.complex64)
