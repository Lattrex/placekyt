"""CoherentBPSKRxBlock — see :class:`CoherentBPSKRxBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from .bpsk_slicer_block import BPSKSlicerBlock
from .complex_costas_loop_block import ComplexCostasLoopBlock


class CoherentBPSKRxBlock(KyttarBlock):
    """
    Coherent BPSK receiver — carrier recovery + hard-decision, recovered BITS out.

    A complete, placeable BPSK receiver: it composes the proven complex Costas
    carrier-recovery loop with a 1-cell BPSK slicer so the chip OUTPUTS recovered
    BITS (0/1), not soft I samples. Input is complex baseband with a carrier
    offset (xi at R0, xq at R1 of the phase landing cell); output is the sliced
    bit on x16_out.

        x16_in(xi,xq) -> [Costas NCO derotate + PI loop] -> recovered I
                      -> [BPSK slicer: sign -> bit] -> x16_out(bit)

    COMPACT 4x2 LAYOUT — slicer is the END-OF-CHAIN cell that BOTH closes the
    carrier loop (relays the dphase feedback to phase) AND packs recovered bits
    into 16-bit words for efficient egress::

        col:     0           1          2           3
        row 0:  phase(E)    sin_fold(E) cos_fold(E) table_sin(S)
        row 1:  SLICER(N)   pd_pi(W)    rotate(W)   table_cos(W)
                  ↓ (south, only on a full 16-bit word) = packed word out

    Forward chain (boustrophedon): phase→sin_fold→cos_fold→table_sin→(S)→
    table_cos→(W)→rotate→(W)→pd_pi→(W)→slicer. Every internal handoff is @1
    (adjacent) or @2 (transit one cell) — no long corridors, no mid-loop tap.

    The SLICER at (0,1) is the block's single output cell and the loop's return
    leg. Its DEFAULT face is NORTH = the feedback path: it RELAYS the dphase word
    it receives from pd_pi up into phase (0,0), one hop, closing the loop. It also
    receives the recovered I (yi_tap, @2 from rotate, transiting pd_pi west) and
    hard-decides it to a bit (I>=0 -> 0, I<0 -> 1), shifting it MSB-first into a
    16-bit accumulator. On every 16th sample (a full word) it briefly flips
    FACE = SOUTH, WRITEs the packed word to the port, JUMPs to hand it off, then
    RESTORES FACE = NORTH before relaying dphase back to the loop. 15 of every 16
    samples it never moves its face — the emit is a rare, self-restoring flip, so
    there is no per-sample face conflict. A trailing partial word (<16 bits) is
    dropped. The packed word is the block's output (output_cell_id = "slicer").
    """
    CATEGORY = "recovery"
    TAGS = ["costas", "bpsk", "receiver", "carrier_recovery", "demodulation",
            "recovery"]

    QUARTER_SIZE = 17
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0]
    )
    _CELL_IDS = [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
        "rotate", "pd_pi", "slicer",
    ]

    def __init__(self, name: str, loop_bw: float = 0.05, damping: float = 1.0,
                 live_monitor: bool = False):
        super().__init__(name, loop_bw=loop_bw, damping=damping,
                         live_monitor=live_monitor)
        # live_monitor: the slicer emits the recovered I (yi) SOUTH every sample
        # (1:1, watchable on a live scope as the loop locks) INSTEAD of packing
        # bits into 16-bit words. The carrier loop + feedback are identical; only
        # the slicer's output behaviour differs. Use for the live GNURadio demo;
        # leave False for the production packed-word receiver.
        self._live_monitor = bool(live_monitor)
        self._costas = ComplexCostasLoopBlock(name + "_costas",
                                              loop_bw=loop_bw, damping=damping)
        self._slicer = BPSKSlicerBlock(name + "_slicer")

    @property
    def cell_count(self) -> int:
        return 8

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _packing_slicer(self) -> CellProgram:
        """End-of-chain slicer + 16-bit packer + dphase feedback relay (slicer at
        (0,1), default FACE = NORTH = feedback to phase).

        Every sample:
          1. slice the recovered I (yi_tap in R{in:llr}):  I>=0 -> 0, I<0 -> 1
          2. pack MSB-first:  word = (word << 1) | bit
          3. count++ ; when count == 16, a full word is ready:
               flip FACE=SOUTH, WRITE the packed word to the port, JUMP it onward,
               restore FACE=NORTH, reset word+count.
          4. relay the dphase feedback (R{in:dphase}) NORTH to phase, then JUMP to
             hand control back to the loop.

        The default face is NORTH the whole time except for the brief, self-
        restoring SOUTH flip on a full word — so the per-sample feedback relay and
        the rare word emit never contend for the single fwd_face. A trailing
        partial word (<16 bits) is dropped (emit only on the 16-bit boundary)."""
        return CellProgram(
            # llr = recovered I to slice (from rotate.yi_tap, @2 transiting pd_pi);
            # dphase = the loop correction from pd_pi (@1) we relay back to phase.
            inputs=[Port("llr", register=0), Port("dphase", register=1)],
            outputs=[Port("out"), Port("out_trig"), Port("fb")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0x0000, address=2),
                  DataWord("one", 0x0001, address=3),
                  DataWord("sixteen", 0x0010, address=4),
                  DataWord("face_north", 3, address=5),   # feedback (default)
                  DataWord("face_south", 0, address=6)],  # word egress
            state=[StateVar("bit"), StateVar("word"), StateVar("count")],
            assembly_template="""\
start:
    MOVE R{state:bit}, R{data:zero}
    CMP R{in:llr}, R{data:zero}
    BR.NN packed
    MOVE R{state:bit}, R{data:one}
packed:
    SHL R{state:word}, #1
    OR R0, R{state:bit}
    MOVE R{state:word}, R0
    MOVE R0, R{state:count}
    ADD R0, R{data:one}
    MOVE R{state:count}, R0
    MOVE R0, R{in:dphase}
    {write:fb}
    CMP R{state:count}, R{data:sixteen}
    BR.NZ done
    MOVE [FACE], R{data:face_south}
    MOVE R0, R{state:word}
    {write:out}
    {jump:out_trig}
    MOVE [FACE], R{data:face_north}
    MOVE R{state:word}, R{data:zero}
    MOVE R{state:count}, R{data:zero}
done:
""",
        )

    def _monitor_slicer(self) -> CellProgram:
        """Live-demo slicer: relay the dphase feedback (NORTH, default) AND emit
        the recovered I (yi) SOUTH every sample (1:1) so a live scope shows the
        loop locking. No packing. Same single-face discipline as the packer:
        default face NORTH (feedback), flip SOUTH to emit the recovered I, restore.

        Because it emits every sample, the SOUTH flip happens each pass; the
        restore-to-NORTH before the feedback relay keeps the feedback correct."""
        return CellProgram(
            inputs=[Port("llr", register=0), Port("dphase", register=1)],
            outputs=[Port("out"), Port("out_trig"), Port("fb")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_north", 3, address=2),
                  DataWord("face_south", 0, address=3)],
            state=[StateVar("yi")],
            assembly_template="""\
start:
    MOVE R{state:yi}, R{in:llr}
    MOVE R0, R{in:dphase}
    {write:fb}
    MOVE [FACE], R{data:face_south}
    MOVE R0, R{state:yi}
    {write:out}
    {jump:out_trig}
    MOVE [FACE], R{data:face_north}
""",
        )

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        cp = self._costas.build_cell_programs()
        slicer = (self._monitor_slicer() if self._live_monitor
                  else self._packing_slicer())
        return {
            "phase": cp["phase"],
            "sin_fold": cp["sin_fold"],
            "cos_fold": cp["cos_fold"],
            "table_sin": cp["table_sin"],
            "table_cos": cp["table_cos"],
            # Use the PROVEN single-fwd_face rotate (yi-FIRST WRITE order): in this
            # abutted layout yi/yq AND yi_tap all go WEST (no dual-face flip needed),
            # and yi_tap is an INTERNAL @2 handoff transiting pd_pi — which requires
            # the yi-first order (the standalone Costas's dual-face rotate emits yi
            # LAST, racing the yi_tap @2 transit against pd_pi's R0). See
            # ComplexCostasLoopBlock._rotate_legacy_single_face.
            "rotate": ComplexCostasLoopBlock._rotate_legacy_single_face(),
            "pd_pi": cp["pd_pi"],
            "slicer": slicer,
        }

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
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
            # rotate -> pd_pi (yi, yq) @1 adjacent; the recovered I (yi_tap) goes to
            # the slicer @2 (transits pd_pi west).
            ("rotate", "yi", "pd_pi", "yi"),
            ("rotate", "yq", "pd_pi", "yq"),
            ("rotate", "yi_tap", "slicer", "llr"),
            # pd_pi -> slicer (dphase) @1; the slicer RELAYS it on to phase.
            ("pd_pi", "dphase", "slicer", "dphase"),
            # FEEDBACK: slicer relays dphase -> phase (slicer faces NORTH into
            # phase). Data only — phase is re-triggered externally each sample, so
            # the dphase sits in phase's register ready for the next sample.
            ("slicer", "fb", "phase", "dphase"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        # Forward trigger chain ending at the slicer. phase is NOT re-triggered by
        # the loop — it is externally clocked each sample (the dphase from the
        # previous sample is already in its register), exactly as the bare Costas
        # loop closes (data feedback, external trigger).
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "rotate", "default"),
            ("rotate", "trig", "pd_pi", "default"),
            ("pd_pi", "trig", "slicer", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # Compact 4x2 snake. slicer (0,1) faces NORTH into phase (0,0) = the 1-hop
        # dphase feedback relay (default); it flips SOUTH only to egress a full
        # packed word. Every internal handoff is @1 or @2.
        return {
            "phase": (0, 0, "east"),
            "sin_fold": (1, 0, "east"),
            "cos_fold": (2, 0, "east"),
            "table_sin": (3, 0, "south"),
            "table_cos": (3, 1, "west"),
            "rotate": (2, 1, "west"),
            "pd_pi": (1, 1, "west"),      # rotate->pd_pi @1; dphase->slicer @1
            "slicer": (0, 1, "north"),    # NORTH=feedback (default) / SOUTH=word out
        }

    def output_cell_id(self) -> Any:
        """The packed 16-bit word leaves the SLICER cell (south)."""
        return "slicer"

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference. In live_monitor mode: the recovered I (yi) per sample (1:1),
        matching the on-chip monitor slicer. Otherwise: recovered I -> sliced bits
        -> packed 16-bit words, MSB-first, one word per 16 samples (trailing
        partial dropped). I>=0 -> 0, I<0 -> 1."""
        yi = self._costas.process_reference(input_samples)
        if self._live_monitor:
            return np.asarray(yi, dtype=np.int16)
        words = []
        word = 0
        count = 0
        for v in yi:
            bit = 0 if int(v) >= 0 else 1
            word = ((word << 1) | bit) & 0xFFFF
            count += 1
            if count == 16:
                words.append(word)
                word = 0
                count = 0
        return np.array(words, dtype=np.uint16)

    def reset(self):
        self._costas.reset()
