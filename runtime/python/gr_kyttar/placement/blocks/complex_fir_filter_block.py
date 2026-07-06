# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexFIRFilter — a complex (I/Q) FIR sharing ONE set of real taps.

Drop-in for GNU Radio's ``filter.fir_filter_ccf`` (complex-in, complex-out, FLOAT
taps): the SAME real coefficients are applied independently to the I and the Q
rail. This is the fabric-native shape for a complex filter stage (SSB Weaver's
baseband LowPass): the up-stream ComplexMixer emits its I/Q pair as ONE complex
sample to this ONE block (a same-source complex packet — no fan-out), the filter
runs both rails, and emits an I/Q pair to the next complex block (again a packet).
That collapses the GNU-Radio ``complex_to_float → 2× fir_filter_fff →
float_to_complex`` idiom into a single block, so no cell ever fans a complex pair
out to two different blocks and no two-source reconvergent fan-in ever forms.

DATAPATH (two-chain). Each cell reuses the proven :class:`FIRFilterBlock` systolic
wavefront but carries TWO delay-line segments — ``di{i}`` (I history) and ``dq{i}``
(Q history) — SHARING the coefficient words ``c{i}``. Per trigger the cell runs the
FIR MAC chain TWICE: once over the I delay line (→ the I partial sum) and once over
the Q delay line (→ the Q partial sum), forwarding BOTH partial sums + BOTH
shifted-out samples to the next cell. The two rails are independent signals with
INDEPENDENT z⁻¹ histories (a shared history would cross-contaminate I and Q); only
the taps are shared. Bit-exact to ``fir_filter_ccf`` fed the same taps.

The coefficient-headroom (INV-13) scaling + the single saturating-shift gain
restore are inherited unchanged from :class:`FIRFilterBlock` and applied to EACH
rail's final sum on the last cell. Per-cell tap density is ~half the real FIR's
(two delay lines + two MAC chains per cell), so a given tap count folds to ~2× the
cells of the real filter — but it is ONE block and every handoff is a complex
packet.
"""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface
from .fir_filter_block import FIRFilterBlock


def _q15(f: float) -> int:
    v = int(round(max(-1.0, min(1.0, float(f))) * 32767))
    return v & 0xFFFF


class ComplexFIRFilterBlock(FIRFilterBlock):
    """Complex FIR (I/Q in, I/Q out) with SHARED real taps — GR ``fir_filter_ccf``.

    Parameters mirror :class:`FIRFilterBlock` (``coefficients`` = GR ``taps``).
    ``decimation``/``interpolation`` are NOT supported yet (the plain complex FIR
    is what the Weaver needs); a rate-changing complex FIR raises a clear error.
    """

    CATEGORY = "filtering"
    TAGS = ["complex_fir", "fir", "complex", "iq", "filter", "filtering"]

    # Dual-rail cells: each cell carries TWO delay-line segments + runs TWO MAC
    # chains, so per-cell tap density is ~half the real FIR's. A MID cell holds
    # L coeffs + 2L delay regs + 2 old_saves + 2 inputs + 2 MAC chains; L=3 fits
    # the 32-word budget. The LAST cell carries the (shared) headroom restore for
    # BOTH rails but has no old_save regs.
    TAPS_PER_CELL = 2
    MAX_SINGLE_CELL_TAPS = 3
    MAX_SINGLE_CELL_TAPS_WITH_SHIFT = 2
    LAST_CELL_TAPS_WITH_SHIFT = 2

    def __init__(self, name: str, coefficients: List[float],
                 decimation: int = 1, interpolation: int = 1):
        if int(decimation) > 1 or int(interpolation) > 1:
            raise ValueError(
                "ComplexFIRFilter does not support decimation/interpolation yet "
                "(GR fir_filter_ccf is a plain complex FIR); got "
                f"decim={decimation}, interp={interpolation}. Compose with a "
                "separate rate-change block.")
        super().__init__(name, coefficients=coefficients,
                         decimation=1, interpolation=1)
        # Complex I/O: xi=R0, xq=R1 in; the block emits an I/Q pair.
        self._interface = BlockInterface(
            entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ---- cell programs (dual-rail) -----------------------------------------
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        if self._num_taps > self._single_cell_max():
            return self._build_multicell_complex()
        return self._build_single_cell_complex()

    def _macq_chain(self, prefix: str, L: int) -> List[str]:
        """The shift + MULQ/MACQ chain for ONE rail's delay segment ``d{prefix}{i}``
        against the SHARED coeffs ``c{i}``. Leaves the (scaled, in-range) partial
        sum in R0. Does NOT shift in the new sample or forward — the caller frames
        the shift/emit around it (so both rails share one coeff set)."""
        lines = [f"    MULQ R{{state:d{prefix}0}}, R{{data:c0}}"]
        for i in range(1, L):
            lines.append(f"    MACQ R{{state:d{prefix}{i}}}, R{{data:c{i}}}")
        return lines

    def _build_single_cell_complex(self) -> Dict[int, CellProgram]:
        """Single-cell complex FIR: two delay lines (di*, dq*), shared coeffs,
        two MAC chains, emit I then Q with ONE trigger."""
        S = self._head_shift
        N = self._num_taps
        # Data words start at R2 — R0/R1 are the complex INPUT registers (xi/xq), so
        # coeffs MUST NOT collide with them (the resolver honors explicit data
        # addresses but does not reserve input regs; a coeff at R1 would overwrite
        # xq). This is the one place the complex FIR differs from the real FIR's
        # single (R0) input.
        CBASE = 2
        rev = list(reversed(self._coeff_q15))     # d{i} multiplies h[N-1-i]
        data = [DataWord(f"c{i}", c, address=i + CBASE) for i, c in enumerate(rev)]
        addr = N + CBASE - 1
        if S > 0:
            data.append(DataWord("bias", 1 << (15 - S), address=addr + 1))
            data.append(DataWord("satpos", self.SAT_POS_Q15, address=addr + 2))
            addr += 2
        state = ([StateVar(f"di{i}") for i in range(N)]
                 + [StateVar(f"dq{i}") for i in range(N)])
        if S > 0:
            state.append(StateVar("acc_save"))

        lines: List[str] = []
        # Shift both delay lines, ingest xi into di, xq into dq.
        for i in range(N - 1):
            lines.append(f"    MOVE R{{state:di{i}}}, R{{state:di{i+1}}}")
        lines.append(f"    MOVE R{{state:di{N-1}}}, R{{in:xi}}")
        for i in range(N - 1):
            lines.append(f"    MOVE R{{state:dq{i}}}, R{{state:dq{i+1}}}")
        lines.append(f"    MOVE R{{state:dq{N-1}}}, R{{in:xq}}")
        # I rail: MAC chain → restore → emit out_i (NO jump yet).
        lines += self._macq_chain("i", N)
        lines += self._satshift_and_emit(S, ["    {write:out_i}"])
        # Q rail: MAC chain → restore → emit out_q → ONE trigger.
        lines += self._macq_chain("q", N)
        lines += self._satshift_and_emit(S, ["    {write:out_q}", "    {jump:trig}"])

        template = "start:\n" + "\n".join(lines) + "\n"
        return {0: CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("out_i"), Port("out_q"), Port("trig")],
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template=template,
        )}

    def _build_multicell_complex(self) -> Dict[int, CellProgram]:
        """Multi-cell complex systolic FIR: each cell carries an I and a Q delay
        segment (shared coeffs), forwards BOTH partial sums + BOTH shifted-out
        samples to the next cell. The last cell emits the I/Q output pair."""
        N = self._num_taps
        S = self._head_shift
        offsets = self._segment_offsets()
        n_cells = len(offsets) - 1
        programs: Dict[int, CellProgram] = {}

        # A multi-cell complex FIR runs TWO saturating-restore emit sequences on the
        # last cell (one per rail). When S>0 (Σ|h|>1) those two restores blow the
        # 32-word budget on the final cell. A low-pass / band filter is Σ|h|≤1 by
        # construction once gain≤1, giving S=0 and a clean fit — so require it rather
        # than silently rescale (RULE #0: never fake magnitude vs fir_filter_ccf).
        if S > 0:
            raise ValueError(
                f"ComplexFIRFilterBlock '{self.name}': {N} taps fold to {n_cells} "
                f"cells and Σ|h|>1 (head_shift={S}); the last cell's dual saturating "
                f"restore overflows 32 words. Scale coefficients so Σ|h|≤1 "
                f"(e.g. reduce filter gain) or reduce the tap count to a single cell.")

        for m in range(n_cells):
            start, end = offsets[m], offsets[m + 1]
            L = end - start
            is_first = (m == 0)
            is_last = (m == n_cells - 1)
            # Coeffs start at R2 — R0/R1 are the xi/xq inputs on EVERY cell (the
            # wavefront passes the sample pair down the chain), so coeffs must not
            # collide with them (see the single-cell note).
            CBASE = 2
            cell_coeffs = list(reversed(self._coeff_q15[start:end]))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + CBASE)
                    for i in range(L)]
            if is_last and S > 0:
                data.append(DataWord("bias", 1 << (15 - S), address=L + CBASE))
                data.append(DataWord("satpos", self.SAT_POS_Q15, address=L + CBASE + 1))

            state = ([StateVar(f"di{i}") for i in range(L)]
                     + [StateVar(f"dq{i}") for i in range(L)])
            if not is_last:
                # ONE shared temp holds the oldest sample of whichever rail is being
                # processed (I then Q) — captured before that rail's shift overwrites
                # di0/dq0, then written to xi_out/xq_out. One reg, reused across rails.
                state.append(StateVar("osave"))
            if is_last and S > 0:
                state.append(StateVar("acc_save"))

            if is_first:
                inputs = [Port("xi", register=0), Port("xq", register=1)]
            else:
                # xi/xq are the fixed complex-sample landing regs (R0/R1); the two
                # partial-sum inputs (pi/pq) are AUTO-allocated by the resolver into
                # free gaps (a manual base collided with the instruction region on
                # dense mid cells). The source cell's WRITE dest is resolved from the
                # port map, so auto-allocation stays consistent end-to-end.
                inputs = [Port("xi", register=0), Port("xq", register=1),
                          Port("pi"), Port("pq")]

            if is_last:
                outputs = [Port("out_i"), Port("out_q"), Port("trig")]
            else:
                outputs = [Port("pi_out"), Port("pq_out"),
                           Port("xi_out"), Port("xq_out"), Port("fwd")]

            lines: List[str] = []
            # I rail. Capture the oldest sample (di0) into osave BEFORE the shift
            # overwrites it (xi lives in R0, so we can't stage it there yet). Then
            # shift+ingest xi (still in R0), run the MAC chain, add the incoming I
            # partial, and forward pi_out + the shifted-out sample (from osave).
            if not is_last:
                lines.append("    MOVE R{state:osave}, R{state:di0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:di{i}}}, R{{state:di{i+1}}}")
            lines.append(f"    MOVE R{{state:di{L-1}}}, R{{in:xi}}")
            lines += self._macq_chain("i", L)
            if not is_first:
                lines.append("    ADD R0, R{in:pi}")
            if is_last:
                lines += self._satshift_and_emit(S, ["    {write:out_i}"])
            else:
                lines.append("    {write:pi_out}")
                lines.append("    MOVE R0, R{state:osave}")
                lines.append("    {write:xi_out}")

            # Q rail. Same structure — osave is free again now the I forward is done.
            if not is_last:
                lines.append("    MOVE R{state:osave}, R{state:dq0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:dq{i}}}, R{{state:dq{i+1}}}")
            lines.append(f"    MOVE R{{state:dq{L-1}}}, R{{in:xq}}")
            lines += self._macq_chain("q", L)
            if not is_first:
                lines.append("    ADD R0, R{in:pq}")
            if is_last:
                lines += self._satshift_and_emit(
                    S, ["    {write:out_q}", "    {jump:trig}"])
            else:
                lines.append("    {write:pq_out}")
                lines.append("    MOVE R0, R{state:osave}")
                lines.append("    {write:xq_out}")
                lines.append("    {jump:fwd}")

            template = "start:\n" + "\n".join(lines) + "\n"
            programs[m] = CellProgram(
                inputs=inputs, outputs=outputs,
                entries=[EntryPoint("default")],
                data=data, state=state,
                assembly_template=template,
            )
        return programs

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Cell m forwards its two partial sums (pi/pq) and two shifted-out samples
        (xi/xq) to cell m+1. Single-cell blocks have none."""
        if self.cell_count <= 1:
            return []
        conns: List[Tuple[int, str, int, str]] = []
        for m in range(self.cell_count - 1):
            conns += [
                (m, "pi_out", m + 1, "pi"),
                (m, "pq_out", m + 1, "pq"),
                (m, "xi_out", m + 1, "xi"),
                (m, "xq_out", m + 1, "xq"),
            ]
        return conns

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        if self.cell_count <= 1:
            return []
        return [(m, "fwd", m + 1, "default") for m in range(self.cell_count - 1)]

    def output_cell_ids(self) -> List[Any]:
        return [self.cell_count - 1]

    # ---- Q15 reference (bit-exact, dual-rail wrapping) ----------------------
    def process_reference_q15(self, iq_q15) -> list:
        """Bit-exact predictor: run the parent's real Q15 FIR on the I stream and
        the Q stream INDEPENDENTLY with the same taps, return (i,q) pairs. Mirrors
        the on-chip two-chain datapath (shared coeffs, separate delay lines)."""
        arr = list(iq_q15)
        i_in = [int(a) & 0xFFFF for (a, _b) in arr]
        q_in = [int(b) & 0xFFFF for (_a, b) in arr]
        i_out = super().process_reference_q15(i_in)
        q_out = super().process_reference_q15(q_in)
        return [(int(i) & 0xFFFF, int(q) & 0xFFFF) for i, q in zip(i_out, q_out)]

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference: apply the real FIR to Re and Im independently."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            re = super().process_reference(np.real(arr).astype(np.float32))
            im = super().process_reference(np.imag(arr).astype(np.float32))
            return (np.asarray(re) + 1j * np.asarray(im)).astype(np.complex64)
        # Real input: treat as I with Q=0.
        return super().process_reference(arr)
