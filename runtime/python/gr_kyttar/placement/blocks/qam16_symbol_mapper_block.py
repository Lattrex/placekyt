# SPDX-License-Identifier: GPL-3.0-or-later
"""QAM16SymbolMapperBlock — see :class:`QAM16SymbolMapperBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from ._qam16_common import qam16_points_q15


class QAM16SymbolMapperBlock(KyttarBlock):
    """
    16-QAM Symbol Mapper Block (3 cells).

    Mirrors GNU Radio ``digital.chunks_to_symbols_bc(constellation_16qam().points())``
    (the constellation-modulator symbol-mapping stage): maps a 4-bit symbol index
    0..15 to the EXACT ``digital.constellation_16qam()`` point and emits its I and Q
    components on separate output ports.

    THE MAP IS GR'S, VERIFIED AGAINST GR ITSELF. ``constellation_16qam()`` is a
    rectangular {±1,±3}/sqrt(10) grid, but its bit->point assignment is NOT the naive
    ``(I_bits<<2)|Q_bits`` separable Gray map — it is an idiosyncratic permutation
    (call ``constellation_16qam().points()`` to see it). This block stores that exact
    point table, so composed with :class:`QAM16SlicerBlock` (which mirrors
    ``decision_maker``) it is the identity on a clean channel: 4 bits -> symbol v ->
    points()[v] -> v.

    Bit packing is MSB-first (``symbol = (b3<<3)|(b2<<2)|(b1<<1)|b0``), matching the
    GR ``constellation_modulator`` / ``pack_k_bits`` upstream that feeds
    ``chunks_to_symbols``. Bit-in packing is a documented Kyttar convenience (the same
    one the PSK/FSK4 mappers use) so a bit-fed flowgraph maps directly; GR's
    ``chunks_to_symbols`` itself is index-in.

    Architecture (3 cells): a 16-entry I table (16 words) and a 16-entry Q table
    (16 words) do NOT co-fit one 32-word cell alongside the program, so the lookup is
    split — ``acc`` accumulates 4 bits into an index, ``itab`` LOADs I (emits out_i)
    and forwards ``idx`` to ``qtab``, which LOADs Q and emits out_q. The I and Q rails
    egress independently (paired by corridor at the port, the complex-egress co-rail
    contract)::

        acc (accumulate) --idx--> itab (I table; emit out_i) --idx--> qtab (Q; emit out_q)

    Interface:
        - Entry: R1 (cell 0)
        - Input: R0 (one bit per call; 4 bits make a symbol)
        - Outputs: out_i, out_q (Q15)
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "symbol_mapper", "modulation", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[0], output_registers=[0])

    BITS_PER_SYMBOL = 4
    _CELL_IDS = ["acc", "itab", "qtab"]

    def __init__(self, name: str):
        super().__init__(name)
        pts = qam16_points_q15()
        self._i_q15 = [p[0] for p in pts]
        self._q_q15 = [p[1] for p in pts]
        self._bit_buffer = 0
        self._bit_count = 0

    @property
    def cell_count(self) -> int:
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        # acc: accumulate 4 bits (MSB first) -> index 0..15, send to itab.
        acc = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("idx")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("mask", 15, address=1),
                DataWord("bps", 4, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[StateVar("in_save"), StateVar("bit_acc"), StateVar("bit_cnt")],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHL R{state:bit_acc}, #1
    OR R0, R{state:in_save}
    MOVE R{state:bit_acc}, R0
    ADD R{state:bit_cnt}, R{data:one}
    MOVE R{state:bit_cnt}, R0
    CMP R{state:bit_cnt}, R{data:bps}
    BR.N done
    MOVE R{state:bit_cnt}, R{data:zero}
    AND R{state:bit_acc}, R{data:mask}
    MOVE R{state:bit_acc}, R{data:zero}
    {write:idx}
    {jump:idx}
done:
    HALT
""",
        )

        # itab: LOAD the I value for symbol idx (16-entry table at addr 1..16);
        # forward (I, idx) to qtab as ONE atomic delivery (2 writes + trigger).
        i_tab = [DataWord(f"i{k}", v, address=1 + k)
                 for k, v in enumerate(self._i_q15)]
        itab = CellProgram(
            inputs=[Port("index", register=0)],
            outputs=[Port("i_fwd"), Port("idx_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=i_tab + [DataWord("one", 1, address=17)],
            state=[StateVar("idx_save"), StateVar("addr_tmp")],
            assembly_template="""\
start:
    MOVE R{state:idx_save}, R{in:index}
    ADD R{state:idx_save}, R{data:one}   ; R0 = idx + 1 = table address (1..16)
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}               ; R0 = I = mem[idx+1]
    {write:i_fwd}
    ; forward the table ADDRESS (idx+1) so qtab indexes directly (no re-add)
    MOVE R0, R{state:addr_tmp}
    {write:idx_fwd}
    {jump:trig}
""",
        )

        # qtab: receives (I, addr=idx+1) atomically; LOADs Q; emits (out_i, out_q)
        # from the SAME trigger so the complex pair is one delivery. out_i is emitted
        # straight from the snapshot so I and Q are the SAME symbol.
        #
        # CRITICAL: the 16-entry Q table lives in memory addresses 1..16, and memory IS
        # the register file (mem[n] == Rn). So the INPUT registers must NOT alias the
        # table — landing ``addr`` in R1 would CLOBBER q[0] (mem[1]), making symbol 0's
        # Q read back the delivered address instead of the table value. Pin the inputs
        # and state ABOVE the table (R17+).
        q_tab = [DataWord(f"q{k}", v, address=1 + k)
                 for k, v in enumerate(self._q_q15)]
        qtab = CellProgram(
            inputs=[Port("i_in", register=17), Port("addr", register=18)],
            outputs=[Port("out_i"), Port("out_q"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=q_tab,
            state=[StateVar("i_save"), StateVar("addr_tmp")],
            assembly_template="""\
start:
    MOVE R{state:i_save}, R{in:i_in}     ; snapshot I
    MOVE R{state:addr_tmp}, R{in:addr}   ; addr = idx + 1 (the table address, >=1)
    LOAD R{state:addr_tmp}               ; R0 = Q = mem[idx+1]
    MOVE R{state:addr_tmp}, R0           ; addr_tmp = Q
    MOVE R0, R{state:i_save}
    {write:out_i}
    MOVE R0, R{state:addr_tmp}
    {write:out_q}
    {jump:out_trigger}
""",
        )
        return {"acc": acc, "itab": itab, "qtab": qtab}

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        """acc.idx -> itab.index;  itab.(i_fwd,idx_fwd) -> qtab.(i_in,addr)."""
        return [
            ("acc", "idx", "itab", "index"),
            ("itab", "i_fwd", "qtab", "i_in"),
            ("itab", "idx_fwd", "qtab", "addr"),
        ]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        """Linear trigger chain acc -> itab -> qtab; qtab emits externally."""
        return [
            ("acc", "idx", "itab", "default"),
            ("itab", "trig", "qtab", "default"),
        ]

    def output_cell_id(self) -> Any:
        """The (I, Q) pair leaves the last cell, ``qtab``."""
        return "qtab"

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Linear 3-cell row: acc -> itab -> qtab, each facing EAST to the next."""
        return {
            "acc": (0, 0, "east"),
            "itab": (1, 0, "east"),
            "qtab": (2, 0, "east"),
        }

    def process_reference(self, input_bits):
        """Reference: 4 bits (MSB first) -> (I, Q) Q15 pair per symbol, using the
        EXACT GR ``constellation_16qam()`` point table."""
        out = []
        acc = cnt = 0
        for b in np.asarray(input_bits).ravel():
            acc = ((acc << 1) | (int(b) & 1)) & 0xF
            cnt += 1
            if cnt == 4:
                out.append((self._i_q15[acc], self._q_q15[acc]))
                acc = cnt = 0
        return out

    def reset(self):
        self._bit_buffer = 0
        self._bit_count = 0
