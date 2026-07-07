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

    The cell latches BOTH xi and xq (the pairing is the whole point) and emits a COMPLEX
    packet — the two rails ``yi`` (=xi) and ``yq`` (=xq) plus a ``trig`` — via declarative
    ``{write:yi}`` / ``{write:yq}`` / ``{jump:trig}`` (the ComplexMixer's exact output
    shape). The build BROKERS + hop-patches it like any complex source (no
    RAW_OUTPUT_HOPS). Because ``yi``/``yq`` are a same-cell I/Q pair, the importer's I/Q
    split (``_iq_sibling``) auto-wires yi→consumer.xi AND yq→consumer.xq to a genuine
    2-input complex downstream, so the imaginary rail is NOT lost. When the downstream is
    REAL (fed through a logical ``complex_to_real`` that drops Q), only the yi rail is
    wired and yq is simply not consumed — the identity converter chain's case.

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
    lands in R0; ``phase`` picks I vs Q; both latch to state xi/xq; on the Q trigger the
    matched pair is emitted as a 2-rail complex packet (yi=xi, yq=xq).
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
        # SINGLE-ENTRY PHASE-TOGGLE rendezvous (event-driven; no external "arm"), emitting
        # a COMPLEX PACKET via declarative {write:yi}/{write:yq}/{jump:trig} (the
        # ComplexMixer's output shape) so the build BROKERS + hop-patches it — no
        # RAW_OUTPUT_HOPS.
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
        # On the Q trigger it emits the paired complex sample as TWO rails yi=xi, yq=xq
        # (+ one trigger). `yi`/`yq` are a same-cell I/Q pair, so the importer's I/Q split
        # auto-wires yi->consumer.xi AND yq->consumer.xq to a 2-input complex downstream;
        # a REAL downstream (via a logical complex_to_real) consumes only yi.
        tmpl = (
            "recv:\n"
            "    CMP R{state:phase}, R{data:zero}\n"
            "    BR.NZ _q\n"
            # phase 0: this word is I. latch it, flip to phase 1, wait for Q.
            "    MOVE R{state:xi}, R{in:i}\n"
            "    MOVE R{state:phase}, R{data:one}\n"
            "    HALT\n"
            "_q:\n"
            # phase 1: this word is Q. latch it (pairing proven), emit the (yi,yq) packet.
            "    MOVE R{state:xq}, R{in:q}\n"
            "    MOVE R0, R{state:xi}\n"
            "    {write:yi}\n"                           # recovered I rail -> consumer.xi
            "    MOVE R0, R{state:xq}\n"
            "    {write:yq}\n"                           # recovered Q rail -> consumer.xq
            "    {jump:trig}\n"                          # trigger the downstream ONCE
            "    MOVE R{state:phase}, R{data:zero}\n"    # back to phase 0 (next I)
            "    HALT\n"
        )
        return {0: CellProgram(
            inputs=[Port("i", register=0), Port("q", register=0)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
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
