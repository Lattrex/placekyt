"""CoherentRXBlock — see :class:`CoherentRXBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from .complex_costas_loop_block import ComplexCostasLoopBlock
from .gardner_timing_recovery import GardnerTimingRecovery


class CoherentRXBlock(KyttarBlock):
    """
    Full coherent BPSK receiver — carrier recovery + timing recovery + slice, in
    ONE bitstream. Recovered BITS out.

    This is the spatially-abutted dual loop (#226): the proven complex Costas
    carrier-recovery loop hands its recovered I (yi) DOWN to a Gardner timing-
    recovery loop, whose recovered symbol centers are sliced to bits — all on a
    single array. Validated BER=0 across carrier+timing offsets in
    the internal reference implementation.

        x16_in(xi,xq) -> [Costas: NCO derotate + PI] -> recovered yi
                      -> [yi relay] -> [Gardner: resampler/ted/loop_filter] -> centers
                      -> [BPSK slicer: sign -> bit] -> bits out

    LAYOUT (the proven proto geometry — Costas rows 0-1 cols 0-6, the yi handoff,
    Gardner row 1 cols 7-9, slicer at (9,0))::

        col:    0       1        2        3        4        5      6        7       8     9
        row 0:  phase   sin_fold cos_fold table_sin table_cos rotate pd_pi  yi_relay rs_b? slicer
        row 1:  fb0(N)  fb1(W)   fb2(W)   fb3(W)   fb4(W)   fb5(W) fb6(W) resampler ted  loop_filter

    The yi handoff is the key: ``pd_pi`` keeps its default face SOUTH (the Costas
    dphase feedback, unchanged) and FLIPS EAST to drop the recovered yi to a FREE
    relay at (7,0), which drops it SOUTH to the Gardner resampler at (7,1). The flip
    targets are programless / fire-and-forget so the flip never stalls (the
    feedback-path-coupling deadlock fix). The Gardner runs at the nominal period
    (period feedback routed to a dead reg — the proven Gardner runs open-loop at
    nominal and still hits BER 0). The loop_filter center -> slicer at (9,0) -> bit
    out x16_out.
    """
    CATEGORY = "recovery"
    TAGS = ["costas", "gardner", "bpsk", "receiver", "carrier_recovery",
            "timing_recovery", "coherent", "recovery"]

    QUARTER_SIZE = 17
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0]
    )
    # 12 programmed cells (Costas 7 + yi_relay + Gardner 3 + slicer) — the dphase
    # feedback corridor cells are FACE-only transit (ids start with "transit").
    _CELL_IDS = [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos", "rotate",
        "pd_pi", "yi_relay", "resampler", "ted", "loop_filter", "slicer",
    ]

    DEAD_REG = 30  # period_fb dead-ends here (resampler stays at nominal period)

    def __init__(self, name: str, loop_bw: float = 0.05, damping: float = 1.0,
                 kp: int = 3, ki: int = 1):
        super().__init__(name, loop_bw=loop_bw, damping=damping, kp=kp, ki=ki)
        self._costas = ComplexCostasLoopBlock(name + "_costas",
                                              loop_bw=loop_bw, damping=damping)
        self._gardner = GardnerTimingRecovery(name + "_gardner", kp=kp, ki=ki)
        self._kp, self._ki = int(kp), int(ki)

    @property
    def cell_count(self) -> int:
        return 12

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _pdpi_with_yitap(self) -> CellProgram:
        """pd_pi that computes dphase (Costas feedback) AND taps yi to Gardner on a
        decoupled path: flip EAST to a free relay, restore SOUTH, emit dphase from a
        scratch reg (NOT by storing into freq — that would corrupt the PI
        integrator and cap the carrier lock). Proven in proto_dual_loop_stage2."""
        # Reuse the Costas block's resolved alpha/beta (Q15) for an identical loop.
        cp = self._costas.build_cell_programs()["pd_pi"]
        # alpha/beta live as data words in the Costas pd_pi; read them back.
        amap = {d.name: d.value for d in cp.data}
        alpha_q = amap.get("alpha", 0x1738)
        beta_q = amap.get("beta", 0x0129)
        return CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("dphase"), Port("yi_tap"), Port("ytrig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2),
                  DataWord("alpha", alpha_q, address=3),
                  DataWord("beta", beta_q, address=4),
                  DataWord("face_east", 1, address=5),
                  DataWord("face_south", 0, address=6)],
            state=[StateVar("freq"), StateVar("err"), StateVar("yqs"),
                   StateVar("yis")],
            assembly_template="""\
start:
    MOVE R{state:yis}, R{in:yi}
    MOVE R{state:yqs}, R{in:yq}
    MOVE R{state:err}, R{in:yq}
    CMP R{in:yi}, R{data:zero}
    BR.NN pos
    SUB R{data:zero}, R{state:yqs}
    MOVE R{state:err}, R0
pos:
    MULQ R{state:err}, R{data:beta}
    ADD R{state:freq}, R0
    MOVE R{state:freq}, R0
    MULQ R{state:err}, R{data:alpha}
    ADD R{state:freq}, R0
    MOVE R{state:yqs}, R0
    MOVE [FACE], R{data:face_east}
    MOVE R0, R{state:yis}
    {write:yi_tap}
    {jump:ytrig}
    MOVE [FACE], R{data:face_south}
    MOVE R0, R{state:yqs}
    {write:dphase}
""",
        )

    def _yi_relay(self) -> CellProgram:
        """(7,0) relay: receives yi from pd_pi (east), faces SOUTH, relays it to the
        Gardner resampler + triggers it. Single entry, no flip — never strands."""
        return CellProgram(
            inputs=[Port("yi", register=0)],
            outputs=[Port("yout"), Port("ytrig")],
            entries=[EntryPoint("relay")],
            data=[], state=[StateVar("yis")],
            assembly_template="""\
relay:
    MOVE R{state:yis}, R{in:yi}
    MOVE R0, R{state:yis}
    {write:yout}
    {jump:ytrig}
""",
        )

    def _loopfilter_split_out(self) -> CellProgram:
        """Gardner loop_filter for this layout: period_fb dead-ends (nominal
        period); the recovered center `out` goes NORTH to the slicer."""
        kp, ki = self._kp, self._ki
        return CellProgram(
            inputs=[Port("e_in", register=0), Port("cval", register=1)],
            outputs=[Port("out"), Port("period_fb"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kp", kp, address=2),
                  DataWord("ki", ki, address=3),
                  DataWord("one_q14", 1 << 14, address=4),
                  DataWord("face_south", 0, address=5),
                  DataWord("face_north", 3, address=6)],
            state=[StateVar("integ"), StateVar("es"), StateVar("cs")],
            assembly_template="""\
start:
    MOVE R{state:es}, R{in:e_in}
    MOVE R{state:cs}, R{in:cval}
    MULQ R{state:es}, R{data:ki}
    ADD R{state:integ}, R0
    MOVE R{state:integ}, R0
    MULQ R{state:es}, R{data:kp}
    ADD R0, R{state:integ}
    SHR R0, #1
    MOVE R{state:es}, R0
    MOVE R0, R{data:one_q14}
    SUB R0, R{state:es}
    MOVE [FACE], R{data:face_south}
    {write:period_fb}
    MOVE [FACE], R{data:face_north}
    MOVE R0, R{state:cs}
    {write:out}
    {jump:trig}
""",
        )

    def _bit_slicer(self) -> CellProgram:
        """Slice the recovered center (R0) to a BPSK bit: center<0 -> 1 else 0."""
        return CellProgram(
            inputs=[Port("center", register=0)],
            outputs=[Port("bit")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1),
                  DataWord("bit0", 0, address=2),
                  DataWord("bit1", 1, address=3)],
            state=[],
            assembly_template="""\
start:
    CMP R{in:center}, R{data:zero}
    MOVE R0, R{data:bit0}
    BR.NN emit
    MOVE R0, R{data:bit1}
emit:
    {write:bit}
""",
        )

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        c = self._costas.build_cell_programs()
        g = self._gardner.build_cell_programs()
        return {
            "phase": c["phase"],
            "sin_fold": c["sin_fold"],
            "cos_fold": c["cos_fold"],
            "table_sin": c["table_sin"],
            "table_cos": c["table_cos"],
            # Single-fwd_face rotate (yi-FIRST): this fused layout taps yi off pd_pi
            # (not rotate), so rotate only emits yi/yq → pd_pi on its one resting face;
            # the standalone Costas's dual-face rotate (face_internal hardcoded WEST,
            # yi computed last) would mis-face the EAST rotate→pd_pi handoff here and
            # race its unused yi_tap @2 transit against pd_pi. See
            # ComplexCostasLoopBlock._rotate_legacy_single_face.
            "rotate": ComplexCostasLoopBlock._rotate_legacy_single_face(),
            "pd_pi": self._pdpi_with_yitap(),
            "yi_relay": self._yi_relay(),
            "resampler": g["resampler"],
            "ted": g["ted"],
            "loop_filter": self._loopfilter_split_out(),
            "slicer": self._bit_slicer(),
        }

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            # Costas forward datapath (unchanged from ComplexCostasLoopBlock).
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
            ("rotate", "yi", "pd_pi", "yi"),
            ("rotate", "yq", "pd_pi", "yq"),
            # Costas dphase feedback: pd_pi -> phase (the proven row-1 return).
            ("pd_pi", "dphase", "phase", "dphase"),
            # yi handoff: pd_pi -> yi_relay -> Gardner resampler.
            ("pd_pi", "yi_tap", "yi_relay", "yi"),
            ("yi_relay", "yout", "resampler", "xi"),
            # Gardner forward datapath.
            ("resampler", "val", "ted", "val"),
            ("resampler", "par", "ted", "par"),
            ("ted", "e_out", "loop_filter", "e_in"),
            ("ted", "c_out", "loop_filter", "cval"),
            # loop_filter period_fb dead-ends (nominal period) — to a dead reg.
            ("loop_filter", "period_fb", "loop_filter", "period_fb_sink"),
            # recovered center -> slicer -> bit out.
            ("loop_filter", "out", "slicer", "center"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "rotate", "default"),
            ("rotate", "trig", "pd_pi", "default"),
            ("pd_pi", "ytrig", "yi_relay", "relay"),
            ("yi_relay", "ytrig", "resampler", "default"),
            ("resampler", "val", "ted", "default"),
            ("ted", "c_out", "loop_filter", "default"),
            ("loop_filter", "trig", "slicer", "default"),
            # Gardner dead-end branches: the resampler's no-strobe path and the
            # TED's MID-strobe path JUMP NOWHERE (they must NOT advance the chain —
            # only CENTER strobes produce a recovered symbol). Without these, the
            # router would fall the unmapped `trig` ports through to the positional-
            # next cell (loop_filter), firing it on every sample => 2x bit output.
            ("resampler", "trig", "__terminate__", "default"),
            ("ted", "trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # The proven proto geometry (proto_dual_loop_stage2.build_dual).
        return {
            "phase": (0, 0, "east"),
            "sin_fold": (1, 0, "east"),
            "cos_fold": (2, 0, "east"),
            "table_sin": (3, 0, "east"),
            "table_cos": (4, 0, "east"),
            "rotate": (5, 0, "east"),
            "pd_pi": (6, 0, "south"),       # default south (dphase); flips east for yi
            "yi_relay": (7, 0, "south"),    # drops yi to resampler(7,1)
            "resampler": (7, 1, "east"),
            "ted": (8, 1, "east"),
            "loop_filter": (9, 1, "east"),  # sets own face in-program (S/N)
            "slicer": (9, 0, "east"),       # bit -> x16_out east port
            # Costas dphase feedback return corridor (FACE-only transit, cols 0-6).
            "transit_fb_6": (6, 1, "west"),
            "transit_fb_5": (5, 1, "west"),
            "transit_fb_4": (4, 1, "west"),
            "transit_fb_3": (3, 1, "west"),
            "transit_fb_2": (2, 1, "west"),
            "transit_fb_1": (1, 1, "west"),
            "transit_fb_0": (0, 1, "north"),
        }

    def output_cell_id(self) -> Any:
        """Recovered bits leave the slicer (east, to x16_out)."""
        return "slicer"

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference: complex baseband -> Costas yi -> Gardner centers -> bits."""
        yi = self._costas.process_reference(input_samples)
        # Gardner reference over the recovered I, then slice.
        centers = self._gardner.process_reference(np.asarray(yi, dtype=np.int16))
        return np.array([1 if int(c) < 0 else 0 for c in centers], dtype=np.int16)

    def reset(self):
        self._costas.reset()
        self._gardner.reset()
