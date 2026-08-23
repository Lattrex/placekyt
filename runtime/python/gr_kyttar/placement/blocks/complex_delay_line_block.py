# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexDelayLineBlock — a multi-cell distributed COMPLEX delay line."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class ComplexDelayLineBlock(KyttarBlock):
    """
    Complex (I/Q) integer-sample delay line of parameterized ``depth`` — a pure
    delay of ``depth`` COMPLEX samples::

        out[n] = in[n - depth]          (out[n] = 0 for n < depth)

    Both rails are delayed by EXACTLY the same amount, sample-for-sample: the I
    and Q histories travel through the SAME cells in the SAME trigger wave, so an
    I/Q skew (one rail a sample early/late — a catastrophic bug for anything
    coherent downstream) is impossible by construction and is gated explicitly by
    the verification suite. The first ``depth`` output samples are (0, 0) — the
    zero prefill contract (all delay registers initialise to Q15 zero).

    Golden reference: an exact numpy complex delay (``[0]*depth + x``), bit-exact
    required (the datapath is pure MOVEs — no Q15 arithmetic, 0 LSB tolerance).

    DATAPATH — MULTI-CELL DISTRIBUTED depth (the DelayBlock idiom, extended
    ACROSS cells). The ancestor :class:`DelayBlock` holds its whole shift register
    in ONE 32-word cell, capping the (real) depth at 12. This block chains delay
    cells in series so ``depth`` reaches 64 complex samples ON-FABRIC: cell ``m``
    holds a SEGMENT of the line — ``L_m`` I-history registers ``di*`` plus ``L_m``
    Q-history registers ``dq*`` — and per trigger:

      * captures its oldest I sample (``osave = di0``) BEFORE the shift,
      * shifts its I segment down and ingests the incoming I sample at the tail,
      * forwards the captured oldest I onward (``xi_out`` → next cell's ``xi``),
      * repeats for the Q segment (one shared ``osave`` temp, reused after the I
        forward completes — the ComplexFIR forwarding idiom, minus the MACs),
      * JUMP-triggers the next cell.

    The forwarding happens INSIDE one sample's trigger wavefront (like the FIR's
    systolic sample handoff), so the chain contributes NO extra per-hop sample
    delay: the total delay is exactly ``Σ L_m = depth``. Both rails traverse the
    IDENTICAL cell chain with identical per-cell structure — same cell count,
    same hop structure — which is what pins the I/Q alignment. The LAST cell
    emits the delayed pair (``out_i``, ``out_q``) with ONE trigger (a same-source
    complex packet, INV-17: the output cell keeps a free word so the build can
    re-sequence it into the two-trigger fan-out form).

    CELL BUDGET (measured against real builds, see the verification suite): a
    MID cell (forwarding) fits a segment of ``SAMPLES_PER_CELL`` = 5 complex
    samples (2 inputs + 2·5+1 pinned state + the 2·5+7-instruction program); the
    LAST/output cell is capped at ``LAST_CELL_SAMPLES`` = 4 so the INV-17 fan-out
    JUMP always has room. Cost: ``cells(depth) = 1`` for ``depth ≤ 4``, else
    ``⌈(depth-4)/5⌉ + 1`` (≈ depth/5 + 1): depth 32 → 7 cells, depth 64 → 13
    cells — the streaming-FFT budget number.

    Params: ``depth`` (the integer complex-sample delay, default 32; ``depth=0``
    is the identity pass-through). There is no exact GNU Radio counterpart block
    (GR ``blocks.delay`` on gr_complex is the behavioural model; the golden here
    is the exact numpy delay per the manifest contract).

    Hardware deviations / limits:
      * ``depth`` is bounded at ``MAX_DEPTH`` = 64 — the deepest chain VERIFIED
        bit-exact on the fabric (13 cells, the N=128 streaming-FFT stage-1 need;
        the 10×12 array + the ≤8-across fold could geometrically host more, but
        depths beyond 64 are unverified and therefore refused). A larger
        ``depth`` RAISES ``ValueError`` (never silently clamps — a truncated
        delay computes a different function). A deeper line than the fabric can
        host would need the SRAM-panel memory tier (INV-31).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["delay", "delay_line", "complex", "iq", "shift_register",
            "fft", "signal_conditioning"]

    # Segment sizes (complex samples per cell), measured against real builds:
    # a mid cell holds 2 inputs (xi@R0, xq@R1) + 2L+1 pinned state words
    # (di0..di{L-1}, dq0..dq{L-1}, osave at addresses 2..2L+2) + a 2L+7 word
    # program (2 rails × [osave-capture + L shift/ingest MOVEs + reload + WRITE]
    # + 1 JUMP), packing at the top of the 31 usable words (R31 = auto-HALT).
    # L=5 → state top @12, program base @13: exactly full. The LAST cell has the
    # same shape but must ALSO leave a free word for the INV-17 fan-out JUMP the
    # build inserts when out_i/out_q feed two DIFFERENT blocks — L=4 leaves that
    # room with margin (verified by the fan-out budget test).
    SAMPLES_PER_CELL = 5
    LAST_CELL_SAMPLES = 4

    # HARDWARE/VERIFICATION LIMIT: the deepest chain verified bit-exact on the
    # fabric (13 cells). RAISE above it — never clamp (INV-0).
    MAX_DEPTH = 64

    # Fold geometry (INV-8/9/14): serpentine column-major fold, ≤4 tall, ≤8
    # across — the FIR fold conventions.
    FOLD_HEIGHT = 4
    MAX_CELLS_ACROSS = 8

    # Complex I/O: xi=R0, xq=R1 in; the block emits an (out_i, out_q) pair.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    def __init__(self, name: str, depth: int = 32):
        depth = int(depth)
        if depth < 0:
            raise ValueError(f"depth must be >= 0, got {depth}")
        if depth > self.MAX_DEPTH:
            # HARDWARE DEVIATION: the on-fabric chain is verified to MAX_DEPTH
            # complex samples. RAISE (never clamp) — a truncated delay is a
            # different block. Deeper lines need the SRAM-panel tier (INV-31).
            raise ValueError(
                f"depth={depth} exceeds the verified on-fabric delay-line "
                f"chain (MAX_DEPTH={self.MAX_DEPTH} complex samples, "
                f"{self._cells_for(self.MAX_DEPTH)} cells); a deeper line "
                f"needs the SRAM-panel memory tier and is not built yet.")
        super().__init__(name, depth=depth)
        self._depth = depth

    # ------------------------------------------------------------ geometry
    @classmethod
    def _cells_for(cls, depth: int) -> int:
        if depth <= cls.LAST_CELL_SAMPLES:
            return 1
        return math.ceil((depth - cls.LAST_CELL_SAMPLES) / cls.SAMPLES_PER_CELL) + 1

    def _segments(self) -> List[int]:
        """Per-cell segment lengths (complex samples). Cell 0 first; the LAST
        entry is the output cell's segment, in ``[1, LAST_CELL_SAMPLES]`` (or
        the whole depth when single-cell; ``[0]`` for the depth-0 identity)."""
        D = self._depth
        if D == 0:
            return [0]
        if D <= self.LAST_CELL_SAMPLES:
            return [D]
        M, T = self.SAMPLES_PER_CELL, self.LAST_CELL_SAMPLES
        m = math.ceil((D - T) / M)          # mid-cell count
        segs = [M] * m + [D - M * m]
        # Rebalance so the last segment lands in [1, T] (mirrors the FIR's
        # _segment_offsets tail rebalance; last ≤ T holds by construction).
        while segs[-1] < 1:
            j = max(range(len(segs) - 1), key=lambda i: segs[i])
            segs[j] -= 1
            segs[-1] += 1
        return segs

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def cell_count(self) -> int:
        return len(self._segments())

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------ programs
    def build_cell_programs(self) -> Dict[int, CellProgram]:
        segs = self._segments()
        n_cells = len(segs)

        # depth == 0: identity pass-through (no delay registers, no state).
        if self._depth == 0:
            return {0: CellProgram(
                inputs=[Port("xi", register=0), Port("xq", register=1)],
                outputs=[Port("out_i"), Port("out_q"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[], state=[],
                assembly_template="""\
start:
    {write:out_i}
    MOVE R0, R{in:xq}
    {write:out_q}
    {jump:trig}
""",
            )}

        programs: Dict[int, CellProgram] = {}
        for m, L in enumerate(segs):
            is_last = (m == n_cells - 1)

            # REGISTER PINNING (INV-33 no-data-words corollary, the DelayBlock
            # echo trap): this block has ZERO data words, so auto-allocated
            # state would start at R0 and land ON the xi/xq input registers —
            # the block would build cleanly and echo garbage. Pin the whole
            # line explicitly: di* at 2..L+1, dq* at L+2..2L+1, osave at 2L+2
            # (inputs own R0/R1).
            state = ([StateVar(f"di{i}", register=i + 2, initial_value=0)
                      for i in range(L)]
                     + [StateVar(f"dq{i}", register=L + i + 2, initial_value=0)
                        for i in range(L)])
            state.append(StateVar("osave", register=2 * L + 2, initial_value=0))

            inputs = [Port("xi", register=0), Port("xq", register=1)]
            if is_last:
                outputs = [Port("out_i"), Port("out_q"), Port("trig")]
                wr_i, wr_q, trig = "{write:out_i}", "{write:out_q}", "{jump:trig}"
            else:
                outputs = [Port("xi_out"), Port("xq_out"), Port("fwd")]
                wr_i, wr_q, trig = "{write:xi_out}", "{write:xq_out}", "{jump:fwd}"

            lines: List[str] = []
            # I rail: capture the oldest (di0 = x_i[n-Σ]) BEFORE the shift
            # overwrites it, shift the segment down, ingest the incoming I
            # sample (xi still intact in R0 — each input reg is read exactly
            # once), then emit/forward the captured oldest.
            lines.append("    MOVE R{state:osave}, R{state:di0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:di{i}}}, R{{state:di{i+1}}}")
            lines.append(f"    MOVE R{{state:di{L-1}}}, R{{in:xi}}")
            lines.append("    MOVE R0, R{state:osave}")
            lines.append(f"    {wr_i}")
            # Q rail: identical structure — osave is free again after the I
            # forward completed, so both rails share one temp.
            lines.append("    MOVE R{state:osave}, R{state:dq0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:dq{i}}}, R{{state:dq{i+1}}}")
            lines.append(f"    MOVE R{{state:dq{L-1}}}, R{{in:xq}}")
            lines.append("    MOVE R0, R{state:osave}")
            lines.append(f"    {wr_q}")
            lines.append(f"    {trig}")

            programs[m] = CellProgram(
                inputs=inputs, outputs=outputs,
                entries=[EntryPoint("default")],
                data=[], state=state,
                assembly_template="start:\n" + "\n".join(lines) + "\n",
            )
        return programs

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Cell m forwards its oldest (I, Q) pair to cell m+1's (xi, xq)."""
        if self.cell_count <= 1:
            return []
        conns: List[Tuple[int, str, int, str]] = []
        for m in range(self.cell_count - 1):
            conns += [(m, "xi_out", m + 1, "xi"),
                      (m, "xq_out", m + 1, "xq")]
        return conns

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        if self.cell_count <= 1:
            return []
        return [(m, "fwd", m + 1, "default")
                for m in range(self.cell_count - 1)]

    # ------------------------------------------------------------ layout
    def _fold_geometry(self):
        """(cols, rows) of the compact serpentine fold (the FIR chooser, INV-14):
        prefer the most compact fold whose cells fill an EVEN number of full
        columns ≤8 across (I/O co-locate on one edge); otherwise the compact
        fold, letting the router hook up the output from the last cell."""
        n = self.cell_count
        for H in range(self.FOLD_HEIGHT, 0, -1):
            if n % H == 0 and (n // H) % 2 == 0 and (n // H) <= self.MAX_CELLS_ACROSS:
                return n // H, H
        H = min(self.FOLD_HEIGHT, n)
        return math.ceil(n / H), H

    def default_layout(self):
        """Column-major serpentine fold (INV-8/9/14) — identical to the FIR's:
        snake DOWN column 0, OVER, UP column 1, …; each cell faces its successor;
        the last cell continues its column's travel direction for a clean egress."""
        n = self.cell_count
        if n <= 1:
            return {0: (0, 0, "east")}
        C, H = self._fold_geometry()

        def snake_pos(i):
            col, r = divmod(i, H)
            dy = r if (col % 2 == 0) else (H - 1 - r)
            return col, dy

        pos = {i: snake_pos(i) for i in range(n)}
        layout = {}
        for i in range(n):
            dx, dy = pos[i]
            nxt = pos.get(i + 1)
            if nxt is not None:
                nx, ny = nxt
                face = ("east" if nx > dx else "west" if nx < dx
                        else "south" if ny > dy else "north")
            else:
                face = "north" if (i // H) % 2 == 1 else "south"
            layout[i] = (dx, dy, face)
        return layout

    # ------------------------------------------------------------ references
    def process_reference_q15(self, iq_q15) -> list:
        """The exact per-trigger DUT stream: ``[(0,0)]*depth + pairs[:N-depth]``.

        One (i, q) output pair per input trigger; the first ``depth`` pairs are
        the zero prefill. Bit-exact (pure data movement, no arithmetic)."""
        pairs = [(int(a) & 0xFFFF, int(b) & 0xFFFF) for (a, b) in iq_q15]
        n = len(pairs)
        return ([(0, 0)] * self._depth + pairs)[:n]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float/complex reference: prepend ``depth`` complex zeros, keep the
        first N samples (one output pair per input trigger)."""
        x = np.asarray(input_samples, dtype=np.complex64)
        n = len(x)
        out = np.concatenate([np.zeros(self._depth, dtype=np.complex64), x])
        return out[:n].astype(np.complex64)
