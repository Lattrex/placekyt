"""QAM16SlicerBlock — see :class:`QAM16SlicerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from ._qam16_common import qam16_sign_outer_lut


class QAM16SlicerBlock(KyttarBlock):
    """
    16-QAM Hard-Decision Slicer / Decoder Block (2 cells).

    Mirrors GNU Radio ``digital.constellation_decoder_cb(constellation_16qam())``:
    turns a received (I, Q) sample into the 4-bit symbol index 0..15 that GR's
    ``constellation_16qam().decision_maker()`` would return. Composed with
    :class:`QAM16SymbolMapperBlock` (which mirrors ``points()``) it is the identity on
    a clean channel.

    GR's ``constellation_16qam()`` map is NOT separable per axis, BUT the nearest-point
    decision FACTORS exactly into two per-axis binary decisions plus a fixed 16-entry
    permutation LUT (VERIFIED equal to ``decision_maker`` over the whole plane). Each
    axis contributes 2 key bits — a SIGN test and a MAGNITUDE test::

        sign  = (v >= 0)
        outer = (|v| >= 2/sqrt(10))
        key    = (Isign<<3) | (Iouter<<2) | (Qsign<<1) | Qouter
        symbol = LUT[key]

    with ``LUT = [1,6,10,13, 4,3,15,8, 0,7,11,12, 5,2,14,9]`` (the GR permutation, from
    :func:`qam16_sign_outer_lut`). This is two branchless per-axis tests + ONE
    LOAD-indirect lookup — GR-exact, and the compact idiom the QPSK/BPSK slicers use.
    (This replaces the legacy block's INVENTED separable-Gray map, which matched GR on
    0 of 16 symbols.)

    Architecture (3 cells): ``islice`` builds I's 2 key bits (Isign, Iouter) and carries
    Q; ``qslice`` appends Q's 2 bits to finish the 4-bit key; ``lut`` LOAD-indexes the
    16-entry permutation table at addresses 1..16 and emits the symbol. The two-axis
    slice (each axis needs a branchless |v| plus two tests) does not fit one 32-word
    cell, hence the I/Q split. ``lut``'s input register is pinned ABOVE the table (R17)
    — an input in R1..R16 would alias a table entry (the mapper's symbol-0 aliasing bug).

    Interface:
        - Entry: R1 (cell 0)
        - Inputs: I (R0), Q (R1) — a complex sample landing as a paired packet
        - Output: 4-bit symbol index (0..15)
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "slicer", "hard_decision", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0])
    _CELL_IDS = ["islice", "qslice", "lut"]

    def __init__(self, name: str):
        super().__init__(name)
        norm = 1.0 / (10.0 ** 0.5)
        self._t2 = float_to_q15(2.0 * norm) & 0xFFFF        # +2/sqrt(10)
        self._lut = qam16_sign_outer_lut()                   # 16-entry GR permutation

    @property
    def cell_count(self) -> int:
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @staticmethod
    def _axis_bits(vv: str) -> str:
        """Append 2 key bits for the value in state ``vv`` to the running key in
        ``key`` state: first the SIGN bit (v >= 0), then the OUTER bit (|v| >= t2).
        |v| is formed first (branchless negate) so a signed compare near the rails
        can't wrap. Keeps the key in ``key`` state (SHL/OR write R0, restored each
        step) — 2 bits in ~11 instrs."""
        return f"""\
    ; |v| -> mag (branchless)
    MOVE R{{state:mag}}, R{{state:{vv}}}
    CMP R{{state:{vv}}}, R{{data:zero}}
    BR.NN {vv}_sign
    SUB R{{data:zero}}, R{{state:{vv}}}
    MOVE R{{state:mag}}, R0
{vv}_sign:
    ; sign bit = (v >= 0)
    SHL R{{state:key}}, #1
    CMP R{{state:{vv}}}, R{{data:zero}}
    BR.N {vv}_outer
    OR R0, R{{data:one}}
{vv}_outer:
    ; outer bit = (|v| >= t2)
    SHL R0, #1
    CMP R{{state:mag}}, R{{data:t2}}
    BR.N {vv}_done
    OR R0, R{{data:one}}
{vv}_done:
    MOVE R{{state:key}}, R0"""

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        t2 = self._t2
        common = [
            DataWord("zero", 0, address=2),
            DataWord("one", 1, address=3),
            DataWord("t2", t2, address=4),
        ]

        # islice: I@R0, Q@R1 -> partial key (Isign<<1 | Iouter) + forward Q.
        islice = CellProgram(
            inputs=[Port("in_i", register=0), Port("in_q", register=1)],
            outputs=[Port("key_partial"), Port("q_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=list(common),
            state=[StateVar("iv"), StateVar("qv"), StateVar("key"), StateVar("mag")],
            assembly_template="""\
start:
    MOVE R{state:iv}, R{in:in_i}
    MOVE R{state:qv}, R{in:in_q}
    MOVE R{state:key}, R{data:zero}
""" + self._axis_bits("iv") + """
    MOVE R0, R{state:key}
    {write:key_partial}
    MOVE R0, R{state:qv}
    {write:q_fwd}
    {jump:trig}
""",
        )

        # qslice: partial key@R0, Q@R1 -> full key = (partial<<2)|(Qsign<<1|Qouter).
        qslice = CellProgram(
            inputs=[Port("key_in", register=0), Port("q_in", register=1)],
            outputs=[Port("key"), Port("trig")],
            entries=[EntryPoint("default")],
            data=list(common),
            state=[StateVar("qv"), StateVar("key"), StateVar("mag")],
            assembly_template="""\
start:
    MOVE R{state:key}, R{in:key_in}
    MOVE R{state:qv}, R{in:q_in}
""" + self._axis_bits("qv") + """
    {write:key}
    {jump:trig}
""",
        )

        # lut: key@R17 -> symbol = LUT[key] (16-entry table at addr 1..16). key input is
        # pinned to R17 (ABOVE the table) so it can't clobber a table entry.
        lut_tab = [DataWord(f"s{k}", v, address=1 + k)
                   for k, v in enumerate(self._lut)]
        lut_cell = CellProgram(
            inputs=[Port("key", register=17)],
            outputs=[Port("out"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=lut_tab + [DataWord("one", 1, address=18)],
            state=[StateVar("addr_tmp")],
            assembly_template="""\
start:
    MOVE R{state:addr_tmp}, R{in:key}
    ADD R{state:addr_tmp}, R{data:one}   ; table address = key + 1 (1..16)
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}               ; R0 = LUT[key]
    {write:out}
    {jump:out_trigger}
""",
        )
        return {"islice": islice, "qslice": qslice, "lut": lut_cell}

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        return [
            ("islice", "key_partial", "qslice", "key_in"),
            ("islice", "q_fwd", "qslice", "q_in"),
            ("qslice", "key", "lut", "key"),
        ]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        return [
            ("islice", "trig", "qslice", "default"),
            ("qslice", "trig", "lut", "default"),
        ]

    def output_cell_id(self) -> Any:
        return "lut"

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        return {"islice": (0, 0, "east"), "qslice": (1, 0, "east"),
                "lut": (2, 0, "east")}

    def process_reference(self, samples):
        """Reference: (I,Q) Q15 pairs -> GR 16-QAM symbol index (0..15) via the per-axis
        sign/outer key + the GR permutation LUT."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v
        t2 = s16(self._t2)
        out = []
        for (i, q) in samples:
            iv, qv = s16(i), s16(q)
            isign = 1 if iv >= 0 else 0
            iouter = 1 if abs(iv) >= t2 else 0
            qsign = 1 if qv >= 0 else 0
            qouter = 1 if abs(qv) >= t2 else 0
            key = (isign << 3) | (iouter << 2) | (qsign << 1) | qouter
            out.append(self._lut[key] & 0xF)
        return out

    def reset(self):
        pass
