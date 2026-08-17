# SPDX-License-Identifier: GPL-3.0-or-later
"""BlockInterleaverBlock — see the class docstring."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


def _read_permutation(rows: int, cols: int, deinterleave: bool) -> List[int]:
    """The per-block READ permutation sigma of the row-column interleaver.

    GOLDEN DEFINITION (the classic block/matrix interleaver of the coding
    literature — e.g. B. Sklar, *Digital Communications: Fundamentals and
    Applications*, 2nd ed., ch. 8 "Interleaving"; S. Lin & D. J. Costello,
    *Error Control Coding*, block-interleaving section):

      * The interleaver WRITES each block of ``rows*cols`` symbols into a
        ``rows x cols`` matrix ROW BY ROW (row-major, arrival order) and READS
        it out COLUMN BY COLUMN (column-major).  So the i-th symbol READ comes
        from matrix position (row = i mod rows, col = i div rows), i.e. from
        row-major (arrival) index

            sigma(i) = (i mod rows) * cols + (i div rows).

      * The deinterleaver is the exact transpose: it writes the received
        symbols row-major (arrival order) and reads

            sigma'(i) = (i mod cols) * rows + (i div cols),

        the inverse permutation (sigma(sigma'(i)) == i), so
        interleave -> deinterleave is the identity (with 2*rows*cols samples of
        pipeline delay — see the class docstring).

    ``deinterleave`` selects sigma' — ONE machinery, both directions (the two
    are the same walk with the stride swapped: stride = cols to interleave,
    stride = rows to deinterleave).
    """
    n = rows * cols
    if deinterleave:
        return [(i % cols) * rows + (i // cols) for i in range(n)]
    return [(i % rows) * cols + (i // rows) for i in range(n)]


class BlockInterleaverBlock(KyttarBlock):
    """
    Classic ``rows x cols`` BLOCK (matrix) interleaver / deinterleaver for the
    FEC chain.  NO GNU Radio streaming counterpart exists (gr-fec / gr-dtv
    interleavers are PDU/tagged); the golden reference is the standard
    row-column interleaver of the coding literature (see
    :func:`_read_permutation` for the citation and the EXACT write/read order).

    WRITE/READ ORDER (stated loudly): symbols are written into the matrix
    ROW BY ROW in arrival order and read out COLUMN BY COLUMN
    (``deinterleave=True`` swaps to the transpose read — the inverse
    permutation), per block of ``rows*cols`` symbols.

    RATE / LATENCY CONTRACT (INV-2): strict 1:1 — one output word per input
    trigger — with a group delay of EXACTLY ``rows*cols`` samples: the block is
    double-buffered (ping-pong), so while block ``b`` streams in, block ``b-1``
    streams out in permuted order.  The first ``rows*cols`` outputs are the
    initial buffer contents = Q15 zeros.  Formally, with ``N = rows*cols``:

        y[b*N + i] = x[(b-1)*N + sigma(i)]   for b >= 1,   y[g] = 0 for g < N.

    Pure data movement (MOVE/WRITE/LOAD, no arithmetic on the samples), so the
    output is BIT-EXACT to the input words — the verification gate is exact
    (0 LSB), like DelayBlock.

    DATAPATH (3 cells, vertical fold, I/O co-located on one edge):

      * ``rgen`` (input landing cell) — the READ-address generator.  Holds the
        column-walk state: relative read address ``ra`` steps by ``stride``
        (= cols to interleave, rows to deinterleave) and wraps by ``-(N-1)``
        when it leaves the block — the classic column-major walk of a row-major
        buffer.  The block boundary needs NO separate counter: a wrap landing
        exactly ON ``stride`` can only happen from ``ra == N-1``, the last read
        of a block (proven identity: wrap value ``ra+stride-(N-1) == stride``
        iff ``ra == N-1``), and that resets the walk + toggles the read bank
        (``rbase = sumbase - rbase``).  Forwards {sample, absolute read addr}
        to ``wctl``.
      * ``wctl`` — the WRITE controller.  Maintains the sequential write
        pointer ``wptr`` over the 2N-word ping-pong ring (writes land row-major
        in arrival order; the ring alternates banks automatically every N
        samples, opposite to the read bank).  Each sample it CONSTRUCTS the
        store instruction at runtime — ``0x63E0 | wptr`` is ``WRITE @0``
        (hop 31 = local, dest = wptr), the ISA's only computed-destination
        store — and installs it into ``store``'s patch slot, then forwards the
        read address and finally the sample (accumulator delivery into
        ``store``'s R0, INV-33) + trigger.
      * ``store`` — the 2N-word sample memory (register file 2..1+2N) plus a
        4-instruction engine: the patched slot stores the just-arrived sample
        at ``wptr`` (self-modifying store — proven on simKYT), then
        ``LOAD ra`` (the ISA's indirect read) fetches the previous block's
        sample at the permuted address and emits it.  All consumption (slot,
        LOAD) happens BEFORE the potentially-backpressured output WRITE, so a
        stalled egress cannot be overtaken by the next sample's deliveries.

    Hardware deviations (INV-0 — no GR counterpart, but the honest contract):
      * ``rows * cols <= MAX_DEPTH`` (= 12): the double buffer (2 words per
        matrix cell) + the store engine must fit ONE 32-word cell
        (2N <= 24 register-file words).  A larger matrix RAISES ``ValueError``
        (never silently clamps — a truncated interleaver is a different
        permutation).  The documented growth path for FEC-realistic depths
        (e.g. MIL-STD-188-110 1440/11520 symbols) is the SRAM-panel SCRATCH
        two-phase recipe (INV-29/31, the CWDecoder pass1/pass2 template):
        write the block to panel SCRATCH in arrival order, read back
        transposed via computed addresses.  That variant is NOT shipped here
        because it has not been built/verified — this block refuses, loudly,
        rather than pretend.
    """

    CATEGORY = "fec"
    TAGS = ["interleaver", "deinterleaver", "block_interleaver", "fec"]

    #: Deepest supported matrix (rows*cols).  The store cell holds 2N buffer
    #: words at registers 2..1+2N plus its 4-instruction engine at 27..30 and
    #: the ``ra`` input at R1 (R0 is the accumulator-delivered sample), so
    #: 1 + 2N <= 25 -> N <= 12.
    MAX_DEPTH = 12

    #: ``WRITE @0, 0`` — a LOCAL store (HOP_CNT=31) with dest field [4:0] = 0.
    #: OR-ing the destination register into the low bits yields the runtime
    #: computed-destination store instruction (verified against the assembler:
    #: ``WRITE @0, 5`` assembles to 0x63E5).
    WRITE_LOCAL_BASE = 0x63E0

    _interface = BlockInterface(entry_address=13, input_registers=[5],
                                output_registers=[0])

    def __init__(self, name: str, rows: int = 2, cols: int = 2,
                 deinterleave: bool = False):
        rows = int(rows)
        cols = int(cols)
        if rows < 1 or cols < 1:
            raise ValueError(
                f"rows and cols must be >= 1, got rows={rows} cols={cols}")
        if rows * cols > self.MAX_DEPTH:
            # HARDWARE DEVIATION: the ping-pong buffer (2*rows*cols words) must
            # fit the store cell's 32-word register file.  RAISE (never clamp).
            # Larger matrices are the SRAM-panel growth path (INV-29/31) — not
            # yet built, so refusing is the only honest behaviour.
            raise ValueError(
                f"rows*cols={rows * cols} exceeds the single-cell interleaver "
                f"buffer budget (MAX_DEPTH={self.MAX_DEPTH}); a deeper matrix "
                f"needs the SRAM-panel SCRATCH recipe (INV-29/31), which is "
                f"not yet implemented for this block. This is a HARDWARE "
                f"limit of the 32-word cell.")
        super().__init__(name, rows=rows, cols=cols, deinterleave=deinterleave)
        self._rows = rows
        self._cols = cols
        self._deinterleave = bool(deinterleave)
        self._depth = rows * cols
        self._sigma = _read_permutation(rows, cols, self._deinterleave)

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        return 3

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def deinterleave(self) -> bool:
        return self._deinterleave

    @property
    def depth(self) -> int:
        """The block length N = rows*cols (= the group delay in samples)."""
        return self._depth

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        n = self._depth
        stride = self._rows if self._deinterleave else self._cols
        bank_a = 2                 # store-cell buffer base (bank A)
        bank_b = bank_a + n        # bank B base
        sumbase = bank_a + bank_b  # rbase toggle: rbase' = sumbase - rbase

        # --- rgen: read-address generator (input landing cell) --------------
        # 18 instructions -> base_addr = 13.  Registers: data 1..4, input @5,
        # state ra@6 rbase@7 (pinned per INV-33; free 8..12).
        rgen = CellProgram(
            inputs=[Port("sample", register=5)],
            outputs=[Port("smp_f"), Port("ra_f"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("stride", stride, address=1),
                DataWord("ncnt", n, address=2),
                DataWord("nm1", n - 1, address=3),
                DataWord("sumbase", sumbase, address=4),
            ],
            state=[
                # Relative read address within the read bank (column walk).
                StateVar("ra", register=6, initial_value=0,
                         reset_per_batch=True),
                # Current read bank base.  Block 0 reads bank B (all zeros)
                # while the writes fill bank A.
                StateVar("rbase", register=7, initial_value=bank_b,
                         reset_per_batch=True, reset_value=bank_b),
            ],
            assembly_template="""\
rentry:
    MOVE R0, R{in:sample}
    {write:smp_f}
    ADD R{state:ra}, R{state:rbase}
    {write:ra_f}
    {jump:trig}
    ; column walk: ra += stride, wrap by -(N-1) when leaving the block
    ADD R{state:ra}, R{data:stride}
    MOVE R{state:ra}, R0
    CMP R{state:ra}, R{data:ncnt}
    BR.LT rdone
    SUB R{state:ra}, R{data:nm1}
    MOVE R{state:ra}, R0
    ; block boundary iff the wrap landed exactly on `stride` (<=> the walk
    ; just consumed relative address N-1, the last read of the block)
    CMP R{state:ra}, R{data:stride}
    BR.NZ rdone
    XOR R{state:ra}, R{state:ra}
    MOVE R{state:ra}, R0
    SUB R{data:sumbase}, R{state:rbase}
    MOVE R{state:rbase}, R0
rdone:
    HALT
""",
        )

        # --- wctl: write controller / instruction builder --------------------
        # 13 instructions -> base_addr = 18.  Registers: data 1..4, inputs 5/6,
        # state wptr@7 (free 8..17).
        wctl = CellProgram(
            inputs=[Port("ra_in", register=5), Port("smp_in", register=6)],
            outputs=[Port("ra_f"), Port("patch_f"), Port("smp_f"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("wlbase", self.WRITE_LOCAL_BASE, address=1),
                DataWord("one", 1, address=2),
                DataWord("wstart", bank_a, address=3),
                DataWord("wend", bank_a + 2 * n, address=4),
            ],
            state=[
                # Sequential write pointer over the 2N ping-pong ring
                # (absolute store-cell address; banks alternate automatically).
                StateVar("wptr", register=7, initial_value=bank_a,
                         reset_per_batch=True, reset_value=bank_a),
            ],
            assembly_template="""\
wentry:
    MOVE R0, R{in:ra_in}
    {write:ra_f}
    ; construct the computed-destination store: WRITE @0 (local) | wptr,
    ; and install it into the store cell's patch slot
    ADD R{state:wptr}, R{data:wlbase}
    {write:patch_f}
    ; the sample goes LAST (accumulator delivery into store's R0), then kick
    MOVE R0, R{in:smp_in}
    {write:smp_f}
    {jump:trig}
    ; advance the ring pointer
    ADD R{state:wptr}, R{data:one}
    MOVE R{state:wptr}, R0
    CMP R{state:wptr}, R{data:wend}
    BR.NZ wdone
    MOVE R{state:wptr}, R{data:wstart}
wdone:
    HALT
""",
        )

        # --- store: 2N-word sample memory + 4-instruction engine -------------
        # 4 instructions -> base_addr = 27:
        #   27  slot:  HALT at build; runtime-patched to `WRITE @0, wptr`
        #              (stores the accumulator-delivered sample in R0)
        #   28  LOAD R{in:ra}   (indirect read at the permuted address)
        #   29  {write:out}
        #   30  {jump:out}
        # The patch slot IS the entry (base_addr).  All input consumption
        # (R0 sample at 27, ra at 28) happens BEFORE the potentially-blocking
        # output WRITE at 29.
        buf = [DataWord(f"buf{k}", 0, address=bank_a + k, reset_per_batch=True)
               for k in range(2 * n)]
        store = CellProgram(
            inputs=[Port("smp", register=0),      # accumulator delivery
                    Port("ra", register=1),
                    Port("slot", register=27)],   # the patched instruction
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=buf,
            assembly_template="""\
slot:
    HALT
    LOAD R{in:ra}
    {write:out}
    {jump:out}
""",
        )

        return {"rgen": rgen, "wctl": wctl, "store": store}

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            ("rgen", "smp_f", "wctl", "smp_in"),
            ("rgen", "ra_f", "wctl", "ra_in"),
            ("wctl", "ra_f", "store", "ra"),
            ("wctl", "patch_f", "store", "slot"),
            ("wctl", "smp_f", "store", "smp"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            ("rgen", "trig", "wctl", "default"),
            ("wctl", "trig", "store", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # Vertical 3-cell column on one edge: input lands at rgen (0,0), the
        # output egresses store (0,2) — both on the same (west) edge within the
        # 2-cell co-location span (INV-8/14).  Every internal handoff is 1 hop
        # straight down.  Dict order MUST match build_cell_programs (INV-33).
        return {
            "rgen": (0, 0, "south"),
            "wctl": (0, 1, "south"),
            "store": (0, 2, "west"),
        }

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> List[int]:
        """The exact per-trigger DUT stream (bit-exact, pure data movement).

        One output word per input trigger: the first N (= rows*cols) outputs
        are the initial-buffer zeros, then ``y[b*N+i] = x[(b-1)*N+sigma(i)]``.
        """
        x = [int(w) & 0xFFFF for w in x_q15]
        n = self._depth
        out: List[int] = []
        for g in range(len(x)):
            b, i = divmod(g, n)
            if b == 0:
                out.append(0)
            else:
                out.append(x[(b - 1) * n + self._sigma[i]])
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: the same permutation/delay applied to floats."""
        x = np.asarray(input_samples, dtype=np.float32)
        n = self._depth
        out = np.zeros(len(x), dtype=np.float32)
        for g in range(len(x)):
            b, i = divmod(g, n)
            if b >= 1:
                out[g] = x[(b - 1) * n + self._sigma[i]]
        return out

    def reset(self):
        """Reference-state reset (the reference is stateless — kept for API
        parity with other stateful blocks)."""
