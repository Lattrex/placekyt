"""DualFloatToComplexBlock — see :class:`DualFloatToComplexBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, DataWord, StateVar
from ._base import KyttarBlock, BlockInterface


class DualFloatToComplexBlock(KyttarBlock):
    """Dual float -> complex rendezvous (1 cell) — pairs TWO independent real
    streams into ONE complex sample with a SINGLE-ENTRY PHASE TOGGLE.

    GNU Radio equivalent: ``blocks.float_to_complex`` fed by two DISTINCT real
    streams (I on input 0, Q on input 1) -> a single complex stream. On this
    clockless array the two producers fire at INDEPENDENT times, so the cell must
    consume them as strictly MATCHED PAIRS.

    Both producers JUMP the ONE ``recv`` entry; a persistent ``phase`` register
    alternates 0->1->0 to decide which word is I and which is Q:

        recv (phase 0): latch I ; phase := 1 ; HALT       (wait for the Q of this pair)
        recv (phase 1): latch Q ; emit the recovered rail ; phase := 0 ; HALT

    This COUNTS the two triggers of each pair rather than distinguishing them by
    arrival FACE — the earlier LOCK-by-face design FAILED under auto-P&R because the
    two backbone taps abut ONE neighbour, so both I and Q reach this cell from the SAME
    face and a face lock cannot tell them apart. The phase counter is face-agnostic and
    needs NO external "arm": ``phase`` boots 0 (cold-start memory), so the first word is
    I. It is a ``reset_per_batch`` StateVar so a persistently-hosted server cold-starts
    ``phase`` to 0 at each packet boundary (a mid-packet re-Run can't desync the pairing).

    The cell latches BOTH xi and xq (the pairing is the whole point) but emits ONE ``out``
    value — the recovered REAL rail (xi) — via the SAME declarative ``{write:out}`` /
    ``{jump:out}`` handoff a GainBlock uses, so the build BROKERS + hop-patches it like any
    block (no RAW_OUTPUT_HOPS). In every wired chain the DualFloatToComplex feeds a
    ``complex_to_real`` (logical, drops Q) before a real consumer, so xi is exactly what
    survives. (A dual feeding a genuine 2-input complex block would need a 2-rail packet —
    the mixer's ``{write:xi_fwd}``/``{write:xq_fwd}`` pattern; that is the FOLLOW-UP
    importer work, not this chain.)

    NOTE: this block is ONLY needed for TWO independent real producers. A single
    real stream feeding a complex block (real audio -> mixer, Q=0) is a LOGICAL-ONLY
    ``float_to_complex`` in GRC — the importer wires the float straight to the
    complex block's xi (xq=0), no cell. See
    dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md.

    Parameters:
      * ``face_i`` / ``face_q``: retained for API compatibility but no longer used by
        the phase-toggle rendezvous (the pairing is by trigger COUNT, not face).
      * ``hop`` / ``dest_i`` / ``dest_q`` / ``entry``: retained for API compatibility;
        the output handoff is now declarative + brokered, not authored.

    Interface: ONE entry ``recv`` (both I and Q producers target it). The input word
    lands in R0; ``phase`` picks I vs Q; both latch to state xi/xq; xi is emitted.
    """
    CATEGORY = "type_conversion"
    TAGS = ["float_to_complex", "rendezvous", "lock", "type_conversion", "complex"]

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str,
                 face_i: str = "west", face_q: str = "south",
                 hop: int = 1, dest_i: int = 0, dest_q: int = 1, entry: int = 1):
        super().__init__(name, face_i=face_i, face_q=face_q, hop=hop,
                         dest_i=dest_i, dest_q=dest_q, entry=entry)
        self._face_i, self._face_q = face_i, face_q
        self._hop, self._dest_i, self._dest_q = hop, dest_i, dest_q
        self._entry = entry

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # Single entry `recv` (address 1): BOTH the I and the Q producer JUMP here; the
        # phase-toggle inside decides which is which. Input lands in R0.
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        # SINGLE-ENTRY PHASE-TOGGLE rendezvous (event-driven; no external "arm"), with a
        # DECLARATIVE single `out` handoff (like GainBlock's {write:out}/{jump:out}) so
        # the build BROKERS + hop-patches it normally — no RAW_OUTPUT_HOPS.
        #
        # Both producers JUMP the one `recv` entry; a persistent `phase` register
        # alternates 0->1->0. This COUNTS the two triggers of each pair rather than
        # distinguishing them by arrival FACE — the earlier LOCK-by-face design FAILED
        # under auto-P&R because the two backbone taps abut ONE neighbour, so both I and
        # Q reach this cell from the SAME face and a face lock cannot tell them apart. The
        # phase counter is face-agnostic and needs NO arm: `phase` boots 0 (cold-start
        # memory) so the first word is I, the second Q+emit. `phase` is reset_per_batch
        # so a persistently-hosted server cold-starts it per packet.
        #
        # It latches BOTH xi and xq (the pairing is the whole point) but emits ONE `out`
        # value = the recovered REAL rail (xi). In every wired chain the DualFloatToComplex
        # feeds a `complex_to_real` (logical, drops Q) before any real consumer, so xi is
        # exactly what survives — delivering it as a normal single `out` lets the build
        # broker it like any block. (A dual feeding a genuine 2-input complex block would
        # need a 2-rail packet — the mixer's {write:xi_fwd}/{write:xq_fwd} pattern; that's
        # the FOLLOW-UP importer work, not this chain.)
        tmpl = (
            "recv:\n"
            "    CMP R{state:phase}, R{data:zero}\n"
            "    BR.NZ _q\n"
            # phase 0: this word is I. latch it, flip to phase 1, wait for Q.
            "    MOVE R{state:xi}, R{in:i}\n"
            "    MOVE R{state:phase}, R{data:one}\n"
            "    HALT\n"
            "_q:\n"
            # phase 1: this word is Q. latch it (pairing proven), emit xi, reset phase.
            "    MOVE R{state:xq}, R{in:q}\n"
            "    MOVE R0, R{state:xi}\n"
            "    {write:out}\n"                          # recovered I -> downstream (brokered)
            "    {jump:out}\n"                           # trigger the downstream ONCE
            "    MOVE R{state:phase}, R{data:zero}\n"    # back to phase 0 (next I)
            "    HALT\n"
        )
        return {0: CellProgram(
            inputs=[Port("i", register=0), Port("q", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("recv")],
            data=[DataWord("zero", 0, address=1),
                  DataWord("one", 1, address=2)],
            state=[StateVar("xi"), StateVar("xq"),
                   StateVar("phase", reset_per_batch=True)],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples):
        # Pairs (I, Q) -> complex. The reference just re-interleaves matched pairs;
        # the substrate proof is that only MATCHED pairs are emitted (the block
        # verification drives adversarial interleavings and checks the pairing).
        arr = np.asarray(input_samples)
        return arr

    def reset(self):
        pass
