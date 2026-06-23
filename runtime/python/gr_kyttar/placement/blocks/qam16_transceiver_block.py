"""QAM16TransceiverBlock — see :class:`QAM16TransceiverBlock`."""
import numpy as np
from ..block import CellProgram
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from .qam16_slicer_block import QAM16SlicerBlock
from .qam16_symbol_mapper_block import QAM16SymbolMapperBlock


class QAM16TransceiverBlock(KyttarBlock):
    """
    16-QAM symbol-level transceiver loopback — composed 4-cell block.

    A clean customer-discovery demo: on a clean channel it is the identity, 4 bits
    in -> the same 4-bit Gray symbol index out (proven 16/16 in
    the internal reference implementation)::

        bits -> [QAM16 mapper: bits -> I/Q] -> [QAM16 slicer: I/Q -> 4-bit sym] -> sym

    This composes the proven ``QAM16SymbolMapperBlock`` (2 cells) and
    ``QAM16SlicerBlock`` (2 cells) into ONE placeable block. The mapper emits TWO
    outputs (out_i, out_q) that must fan to the slicer's TWO inputs (in_i, in_q);
    expressing that as two separate placeKYT project connections does NOT route
    correctly (the per-connection handoff path resolves one dest per source block),
    so the dual-I/Q handoff is declared here as ``internal_connections`` — the same
    multi-signal internal-routing path the DFE / Costas blocks use — which routes
    each named signal to its own register.

    Cells (linear chain on row 0):

        map0 (bit-accumulate -> idx) -> map1 (idx -> I, Q)
            -> slice0 (I -> high 2 bits, fwd Q) -> slice1 (Q -> low 2 bits, combine)

    Interface: input = one bit per call at R0 (4 bits make a symbol); output = the
    4-bit symbol index.
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "transceiver", "modem", "demo", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    _CELL_IDS = ["map0", "map1", "slice0", "slice1"]

    def __init__(self, name: str):
        super().__init__(name)
        self._mapper = QAM16SymbolMapperBlock(name + "_map")
        self._slicer = QAM16SlicerBlock(name + "_slice")

    @property
    def cell_count(self) -> int:
        return 4

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        mp = self._mapper.build_cell_programs()
        sp = self._slicer.build_cell_programs()
        return {
            "map0": mp[0],
            "map1": mp[1],
            "slice0": sp[0],
            "slice1": sp[1],
        }

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Data handoffs (src_cell, src_out, dst_cell, dst_in). The DUAL-I/Q
        handoff (map1 out_i/out_q -> slice0 in_i/in_q) is the reason this is a
        composed block — two named signals to two registers of one consumer."""
        return [
            # map0 -> map1: the accumulated 4-bit symbol index.
            ("map0", "idx", "map1", "index"),
            # map1 -> slice0: the I and Q constellation components.
            ("map1", "out_i", "slice0", "in_i"),
            ("map1", "out_q", "slice0", "in_q"),
            # slice0 -> slice1: the partial symbol (high 2 bits) + the raw Q.
            ("slice0", "sym_partial", "slice1", "sym_partial"),
            ("slice0", "q_fwd", "slice1", "q_in"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """JUMP triggers forming the linear execution chain. map0 only emits its
        trigger on the 4th bit (one symbol), so the chain runs once per symbol."""
        return [
            ("map0", "idx", "map1", "default"),
            ("map1", "out_trigger", "slice0", "default"),
            ("slice0", "fwd_trigger", "slice1", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Linear forward chain on row 0, facing east."""
        return {cid: (i, 0, "east") for i, cid in enumerate(self._CELL_IDS)}

    def process_reference(self, input_bits) -> np.ndarray:
        """Reference: bits -> (I,Q) -> 4-bit Gray symbol index (the identity on a
        clean channel)."""
        iq = self._mapper.process_reference(input_bits)
        syms = self._slicer.process_reference(iq)
        return np.array(syms, dtype=np.int16)

    def reset(self):
        self._mapper.reset()
        self._slicer.reset()
