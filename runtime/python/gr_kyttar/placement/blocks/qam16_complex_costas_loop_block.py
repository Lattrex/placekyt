"""QAM16ComplexCostasLoopBlock — see :class:`QAM16ComplexCostasLoopBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Optional, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class QAM16ComplexCostasLoopBlock(KyttarBlock):
    """
    16-QAM decision-directed Costas carrier recovery — production 10-cell block.

    16-QAM is non-constant-modulus, so the order-4 (QPSK) and sign (BPSK) phase
    detectors fail. This block runs a DECISION-DIRECTED loop: derotate, SLICE each
    axis to the nearest Gray 4-PAM level, then form the phase error from the
    decision::

        err = yq*di - yi*dq          (= Im{ y * conj(decision) })

    which nulls when y aligns with its decision (the loop pulls the constellation
    onto the grid). Validated cell-exact and on-chip (locks + tracks carrier
    offsets, SER<=0.03) in the internal reference implementation.

    The carrier datapath REUSES the proven complex-Costas cells (phase | sin_fold
    | cos_fold | table_sin | table_cos | rotate). The DD phase detector is a
    3-cell INCREMENTAL-ERROR pipeline (the key on-chip insight)::

        islice_pi : di = slice(yi) ; pi = yq*di           ; fwd pi, yi, yq
        qslice_err: dq = slice(yq) ; err = pi - yi*dq      ; fwd err
        pi        : freq += beta*err ; dphase = freq + alpha*err   (feedback)

    The error is ACCUMULATED ALONG the pipeline so the PI cell receives only the
    finished ``err`` (one input). The naive design that fanned all four operands
    (yi, yq, di, dq) into a single dd-error cell mixed operands from two different
    samples (a cell that needs several operands must receive them as one atomic
    delivery — consecutive WRITEs followed by a single trigger — or it can fire on a
    partially-updated input set) and never tracked an offset; incremental
    accumulation keeps every consumer at <=3 operands per fire.

    Separable Gray 4-PAM per axis: levels {-3,-1,+1,+3}/sqrt(10), threshold
    t = 2/sqrt(10). The branchless slice computes |y| first (|y| and t are both
    <= 32767, so |y| - t never overflows 16 bits — comparing signed y vs +-t near
    the rails would wrap and flip the sign flag)::

        mag = (|y| >= t) ? P3 : P1 ;  d = (y>=0) ? mag : -mag

    Interface: COMPLEX input (xi at R0, xq at R1 of the phase landing cell); the
    recovered (yi, yq) are read from the rotate cell. 16-QAM has the same 90-deg
    4-fold phase ambiguity as QPSK (resolved downstream).

    Cells (forward chain on row 0, dphase feedback returns via row 1):

        phase | sin_fold | cos_fold | table_sin | table_cos | rotate
              | islice_pi | qslice_err | pi
          ^                                                   |
          └──────────── dphase feedback (row-1 west return) ──┘
    """
    CATEGORY = "recovery"
    TAGS = ["costas", "pll", "carrier_recovery", "qam16", "complex",
            "decision_directed", "recovery"]

    QUARTER_SIZE = 17

    # Landing cell is the phase cell; complex input lands at R0 (xi) and R1 (xq).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0]
    )

    _CELL_IDS = [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
        "rotate", "islice_pi", "qslice_err", "pi",
    ]

    # Validated DD loop gains (Q15), hand-tuned for a stable decision-directed
    # 16-QAM lock in proto_qam16_onchip.py / proto_qam16_rx.py. DD is noisier than
    # constant-modulus BPSK/QPSK, so the bandwidth is deliberately low. These are
    # used DIRECTLY (not derived from a loop_bw/damping formula, which over-shoots
    # for the DD detector); pass alpha_q15/beta_q15 to override.
    DEFAULT_ALPHA_Q15 = 0x0800
    DEFAULT_BETA_Q15 = 0x0040

    def __init__(self, name: str, alpha_q15: Optional[int] = None,
                 beta_q15: Optional[int] = None):
        """
        Args:
            name: Block name.
            alpha_q15: Proportional loop gain in Q15 (default 0x0800, validated).
            beta_q15: Integral loop gain in Q15 (default 0x0040, validated).
        """
        a = self.DEFAULT_ALPHA_Q15 if alpha_q15 is None else (alpha_q15 & 0xFFFF)
        b = self.DEFAULT_BETA_Q15 if beta_q15 is None else (beta_q15 & 0xFFFF)
        super().__init__(name, alpha_q15=a, beta_q15=b)
        self._alpha_q15 = a
        self._beta_q15 = b
        # 4-PAM Q15 levels and threshold.
        norm = 1.0 / math.sqrt(10.0)
        self._p1 = float_to_q15(1 * norm)
        self._p3 = float_to_q15(3 * norm)
        self._thr = float_to_q15(2 * norm)
        self._phase = 0
        self._freq = 0

    @property
    def cell_count(self) -> int:
        return 9

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _quarter_wave_table(self) -> List[int]:
        return [
            int(round(math.sin(k / 16 * math.pi / 2) * 32767)) & 0xFFFF
            for k in range(self.QUARTER_SIZE)
        ]

    def _slice_compute(self, sliced_in: str) -> str:
        """Branchless 4-PAM slice (via |y|) leaving the decision in R0 (no WRITE).
        Shared by the I and Q slicer cells (callers use the R0 result locally)."""
        return f"""\
    MOVE R{{state:ys}}, R{{in:{sliced_in}}}
    MOVE R{{state:mag}}, R{{data:p1}}
    MOVE R0, R{{state:ys}}
    CMP R{{state:ys}}, R{{data:zero}}
    BR.NN abs_done
    SUB R{{data:zero}}, R{{state:ys}}
abs_done:
    CMP R0, R{{data:thr}}
    BR.N have_mag
    ADD R{{state:mag}}, R{{data:p3mp1}}
    MOVE R{{state:mag}}, R0
have_mag:
    MOVE R0, R{{state:mag}}
    CMP R{{state:ys}}, R{{data:zero}}
    BR.NN d_done
    SUB R{{data:zero}}, R{{state:mag}}
d_done:"""

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The 9 proven cells (ported from proto_qam16_onchip.py)."""
        qt = self._quarter_wave_table()
        alpha = self._alpha_q15
        beta = self._beta_q15
        p1 = self._p1
        p3mp1 = (self._p3 - self._p1) & 0xFFFF
        thr = self._thr

        # --- phase cell (identical to the BPSK Costas phase cell). ---
        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("dphase", register=2)],
            outputs=[Port("ph_sin"), Port("ph_cos"),
                     Port("xi_fwd"), Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("quarter", 16384, address=3),
                  DataWord("half", 32768, address=4)],
            state=[StateVar("phase"), StateVar("xis"), StateVar("xqs")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    ADD R{state:phase}, R{in:dphase}
    MOVE R{state:phase}, R0
    ADD R{state:phase}, R{data:half}
    {write:ph_sin}
    MOVE R0, R{state:phase}
    ADD R0, R{data:quarter}
    {write:ph_cos}
    MOVE R0, R{state:xis}
    {write:xi_fwd}
    MOVE R0, R{state:xqs}
    {write:xq_fwd}
    {jump:trig}
""",
        )

        def _fold_cell():
            return CellProgram(
                inputs=[Port("phase", register=0)],
                outputs=[Port("neg"), Port("idx"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("thirtytwo", 32, address=1),
                      DataWord("fifteen", 15, address=2),
                      DataWord("sixteen", 16, address=3),
                      DataWord("zero", 0, address=4)],
                state=[StateVar("ph"), StateVar("fidx"), StateVar("loc")],
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
                data=data, state=[StateVar("v")],
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

        # --- rotate cell: complex multiply -> yi, yq (both forwarded to the DD
        # slicer pipeline; the recovered yi/yq are also read from this cell). ---
        rotate_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)],
            state=[StateVar("xis"), StateVar("xqs"), StateVar("sv"),
                   StateVar("cv"), StateVar("acc")],
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
    MOVE R{state:acc}, R0
    {write:yi}
    MULQ R{state:xis}, R{state:sv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:cv}
    ADD R{state:acc}, R0
    {write:yq}
    {jump:trig}
""",
        )

        # --- islice_pi: di = slice(yi); pi = yq*di; forward pi, yi, yq. ---
        islice_pi_cell = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("pi"), Port("yi_fwd"), Port("yq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2),
                  DataWord("p1", p1, address=3),
                  DataWord("p3mp1", p3mp1, address=4),
                  DataWord("thr", thr, address=5)],
            state=[StateVar("ys"), StateVar("mag")],
            assembly_template="""\
start:
""" + self._slice_compute("yi") + """
    MULQ R{in:yq}, R0
    {write:pi}
    MOVE R0, R{state:ys}
    {write:yi_fwd}
    MOVE R0, R{in:yq}
    {write:yq_fwd}
    {jump:trig}
""",
        )

        # --- qslice_err: dq = slice(yq); err = pi - yi*dq; forward err. ---
        qslice_err_cell = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1),
                    Port("pi", register=2)],
            outputs=[Port("err"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=3),
                  DataWord("p1", p1, address=4),
                  DataWord("p3mp1", p3mp1, address=5),
                  DataWord("thr", thr, address=6)],
            state=[StateVar("ys"), StateVar("mag"), StateVar("yis")],
            assembly_template="""\
start:
    MOVE R{state:yis}, R{in:yi}
""" + self._slice_compute("yq") + """
    MULQ R{state:yis}, R0
    SUB R{in:pi}, R0
    {write:err}
    {jump:trig}
""",
        )

        # --- pi: freq += beta*err; dphase = freq + alpha*err (single input). ---
        pi_cell = CellProgram(
            inputs=[Port("err", register=0)],
            outputs=[Port("dphase"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("alpha", alpha, address=1),
                  DataWord("beta", beta, address=2)],
            state=[StateVar("freq"), StateVar("errs")],
            assembly_template="""\
start:
    MOVE R{state:errs}, R{in:err}
    MULQ R{state:errs}, R{data:beta}
    ADD R{state:freq}, R0
    MOVE R{state:freq}, R0
    MULQ R{state:errs}, R{data:alpha}
    ADD R{state:freq}, R0
    {write:dphase}
    {jump:trig}
""",
        )

        return {
            "phase": phase_cell,
            "sin_fold": _fold_cell(),
            "cos_fold": _fold_cell(),
            "table_sin": _table_cell(),
            "table_cos": _table_cell(),
            "rotate": rotate_cell,
            "islice_pi": islice_pi_cell,
            "qslice_err": qslice_err_cell,
            "pi": pi_cell,
        }

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Data handoffs (src_cell, src_out, dst_cell, dst_in), incl. the dphase
        FEEDBACK from the PI cell back to the phase cell (loop closure)."""
        return [
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
            # rotate -> islice_pi (yi, yq).
            ("rotate", "yi", "islice_pi", "yi"),
            ("rotate", "yq", "islice_pi", "yq"),
            # islice_pi -> qslice_err (pi, yi, yq).
            ("islice_pi", "pi", "qslice_err", "pi"),
            ("islice_pi", "yi_fwd", "qslice_err", "yi"),
            ("islice_pi", "yq_fwd", "qslice_err", "yq"),
            # qslice_err -> pi (the finished err — single input, no fan-in race).
            ("qslice_err", "err", "pi", "err"),
            # FEEDBACK: pi dphase -> phase cell (row-1 return).
            ("pi", "dphase", "phase", "dphase"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """JUMP triggers forming the linear execution chain (each cell triggers
        the next). The pi cell terminates the pass; the NEXT input sample
        re-triggers the phase cell (which applies the fed-back dphase)."""
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "rotate", "default"),
            ("rotate", "trig", "islice_pi", "default"),
            ("islice_pi", "trig", "qslice_err", "default"),
            ("qslice_err", "trig", "pi", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Row-0 forward chain (phase..pi) with a row-1 west return path carrying
        dphase back to the phase cell. The pi cell (8,0) faces SOUTH onto row 1;
        row-1 cells (8,1)..(1,1) face WEST and (0,1) faces NORTH up into the phase
        cell. Row-1 cells are FACE-only TRANSIT cells (ids start with
        ``"transit"`` so placeKYT materializes them as TransitCells)."""
        layout: Dict[Any, Tuple[int, int, str]] = {}
        for i, cid in enumerate(self._CELL_IDS):
            face = "south" if cid == "pi" else "east"
            layout[cid] = (i, 0, face)
        for x in range(1, 9):
            layout[f"transit_fb_{x}"] = (x, 1, "west")
        layout["transit_fb_0"] = (0, 1, "north")
        return layout

    def _slice_pam_ref(self, y_q15: int) -> int:
        """Reference branchless 4-PAM slice (signed Q15 decision level)."""
        def s16(v): return v - 0x10000 if v & 0x8000 else v
        ys = s16(y_q15)
        av = -ys if ys < 0 else ys
        mag = s16(self._p1) + (s16((self._p3 - self._p1) & 0xFFFF)
                               if av >= s16(self._thr) else 0)
        return -mag if ys < 0 else mag

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference Q15 DD QAM16 Costas (matches the on-chip cells). Returns the
        recovered yi as a real Q15 int16 array."""
        def s16(v): return v - 0x10000 if v & 0x8000 else v
        def u16(v): return v & 0xFFFF
        def mqr(a, b): return (s16(a) * s16(b) + (1 << 14)) >> 15
        qt = self._quarter_wave_table()

        def qw(ph16):
            fi = (ph16 >> 10) & 0x3F
            neg = (fi & 32) != 0
            mir = (fi & 16) != 0
            lo = fi & 15
            if mir:
                lo = 16 - lo
            v = qt[lo]
            return u16(-s16(v)) if neg else v

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(x) & 0xFFFF, int(y) & 0xFFFF) for x, y in arr]
        else:
            iq = [(float_to_q15(float(x)), 0) for x in arr]

        alpha = self._alpha_q15
        beta = self._beta_q15
        phase = 0
        freq = 0
        out = []
        for (xi, xq) in iq:
            cosv = qw(u16(phase + 16384))
            sinv = u16(-s16(qw(phase)))
            yi = u16(s16(mqr(xi, cosv)) - s16(mqr(xq, sinv)))
            yq = u16(s16(mqr(xi, sinv)) + s16(mqr(xq, cosv)))
            out.append(s16(yi))
            di = self._slice_pam_ref(yi)
            dq = self._slice_pam_ref(yq)
            err = u16((mqr(yq, u16(di)) - mqr(yi, u16(dq))) & 0xFFFF)
            freq = u16(s16(freq) + s16(mqr(beta, err)))
            phase = u16(s16(phase) + s16(freq) + s16(mqr(alpha, err)))
        return np.array(out, dtype=np.int16)

    def reset(self):
        self._phase = 0
        self._freq = 0
