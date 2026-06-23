"""FpgaRamBlock — see :class:`FpgaRamBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class FpgaRamBlock(KyttarBlock):
    """
    FPGA RAM Interface Block (2 cells) — Interleaver/Deinterleaver Controller.

    All address computation and sequencing is done ON-CHIP in the cell array.
    The RAM (FPGA SRAM in the demo, on-die SRAM in production) is pure dumb
    storage with no logic — just responds to read/write requests.

    RAM Protocol (via x1 port):
    ===========================

    DEST field encodes region + operation:
      bits [4:1] = region_id (0-15)
      bit  [0]   = operation (0 = STORE addr/data, 1 = FETCH addr)

    **STORE** (2 consecutive WRITEs):
      WRITE @x1, (region<<1)|0, DATA = address
      WRITE @x1, (region<<1)|0, DATA = symbol

    **FETCH** (1 WRITE, RAM responds):
      WRITE @x1, (region<<1)|1, DATA = address
      → RAM responds: WRITE @x1_in, data_reg, DATA = value
                      JUMP @x1_in, process_entry

    Architecture (2 cells):
    =======================

    Cell 0 (StoreCtrl): Receives symbols from upstream. Stores each to
    RAM at sequential write address. After a full block (rows × cols),
    triggers Cell 1 to begin reading.

    Cell 1 (FetchCtrl): Computes permuted read addresses (column-major
    for interleaver, row-major for deinterleaver). Sends fetch requests
    to RAM. On each response, forwards the symbol downstream and
    self-loops until all addresses read.

    The permutation is: read_addr = row * cols + col, where row cycles
    0..rows-1 and col increments after each full row sweep. This gives
    column-major readout from a row-major write.

    For deinterleaver: swap the write/read permutations.

    Placement: near row 11 (x1 ports) for short routing to SRAM.
    """
    CATEGORY = "memory_interface"
    TAGS = ["fpga", "ram", "memory_interface"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(
        self,
        name: str,
        rows: int = 40,
        cols: int = 36,
        region_id: int = 0,
        is_deinterleaver: bool = False,
    ):
        """
        Initialize interleaver/deinterleaver RAM controller.

        Args:
            name: Block name
            rows: Interleaver matrix rows (default 40 per MIL-STD-188-110B)
            cols: Interleaver matrix columns (36=short, 288=long)
            region_id: FPGA memory region (0-15)
            is_deinterleaver: If True, reverse the permutation
        """
        super().__init__(name, rows=rows, cols=cols, region_id=region_id,
                         is_deinterleaver=is_deinterleaver)
        self._rows = rows
        self._cols = cols
        self._depth = rows * cols
        self._region_id = region_id & 0xF
        self._is_deinterleaver = is_deinterleaver
        # Encode region + operation into DEST
        self._store_addr = (region_id << 1) | 0
        self._fetch_addr = (region_id << 1) | 1

    @property
    def cell_count(self) -> int:
        return 2  # StoreCtrl + FetchCtrl

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """2-cell interleaver controller with on-chip address computation.

        Cell 0 (StoreCtrl):
          - Receives symbol from upstream
          - Sends STORE(write_addr, symbol) to RAM via x1 (south face)
          - Increments write_addr
          - After depth symbols: resets counter, triggers Cell 1 (FetchCtrl)

        Cell 1 (FetchCtrl):
          Entry 'fetch':
            - Computes read_addr = row * cols + col (permuted)
            - Sends FETCH(read_addr) to RAM via x1 (south face)
            - HALTs (waits for RAM response)
          Entry 'process':
            - Receives fetched symbol from RAM
            - Forwards to downstream (east face)
            - Increments row/col counters
            - If more to read: GOTO fetch (self-loop)
            - If block complete: HALT
        """
        depth = self._depth

        # --- Cell 0: StoreCtrl ---
        cell_store = CellProgram(
            inputs=[Port("symbol", register=0)],
            outputs=[Port("to_ram_addr"), Port("to_ram_data"), Port("to_fetch")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("face_south", 0, address=1),
                DataWord("face_east", 1, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
                DataWord("depth", depth & 0xFFFF, address=5),
            ],
            state=[
                StateVar("save"),
                StateVar("w_ctr"),
            ],
            assembly_template="""\
start:
    MOVE R{state:save}, R0
    MOVE [FACE], R{data:face_south}
    MOVE R0, R{state:w_ctr}
    {write:to_ram_addr}
    MOVE R0, R{state:save}
    {write:to_ram_data}
    MOVE [FACE], R{data:face_east}
    ADD R{state:w_ctr}, R{data:one}
    MOVE R{state:w_ctr}, R0
    CMP R{state:w_ctr}, R{data:depth}
    BR.N done
    MOVE R{state:w_ctr}, R{data:zero}
    MOVE R0, R{data:zero}
    {write:to_fetch}
    {jump:to_fetch}
done:
    HALT
""",
        )

        # --- Cell 1: FetchCtrl ---
        cell_fetch = CellProgram(
            inputs=[Port("trigger", register=0)],
            outputs=[Port("to_ram_fetch"), Port("to_downstream")],
            entries=[EntryPoint("fetch"), EntryPoint("process")],
            data=[
                DataWord("face_south", 0, address=1),
                DataWord("face_east", 1, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
                DataWord("rows", self._rows, address=5),
                DataWord("cols", self._cols, address=6),
            ],
            state=[
                StateVar("r_row"),
                StateVar("r_col"),
            ],
            assembly_template="""\
fetch:
    MOVE [FACE], R{data:face_south}
    MUL R{state:r_row}, R{data:cols}
    ADD R0, R{state:r_col}
    {write:to_ram_fetch}
    MOVE [FACE], R{data:face_east}
    HALT

process:
    {write:to_downstream}
    ADD R{state:r_row}, R{data:one}
    MOVE R{state:r_row}, R0
    CMP R{state:r_row}, R{data:rows}
    BR.N fetch
    MOVE R{state:r_row}, R{data:zero}
    ADD R{state:r_col}, R{data:one}
    MOVE R{state:r_col}, R0
    CMP R{state:r_col}, R{data:cols}
    BR.N fetch
    MOVE R{state:r_col}, R{data:zero}
    {jump:to_downstream}
""",
        )

        return {0: cell_store, 1: cell_fetch}

    def process_reference(self, input_symbols: np.ndarray) -> np.ndarray:
        """
        Reference implementation of interleaver/deinterleaver.

        Interleaver: writes sequentially, reads column-by-column.
        Deinterleaver: writes column-by-column, reads sequentially.

        Output is delayed by one full block (depth symbols).
        """
        depth = self._depth
        rows = self._rows
        cols = self._cols
        n = len(input_symbols)
        output = []

        buf_write = [0] * depth
        buf_read = [0] * depth
        write_count = 0

        for i in range(n):
            if self._is_deinterleaver:
                # Write in column-major (permuted) order
                row = write_count % rows
                col = write_count // rows
                addr = row * cols + col
            else:
                # Write sequentially
                addr = write_count

            buf_write[addr] = int(input_symbols[i]) & 0xFFFF
            write_count += 1

            if write_count == depth:
                buf_read = buf_write[:]
                buf_write = [0] * depth
                write_count = 0

                for j in range(depth):
                    if self._is_deinterleaver:
                        read_addr = j
                    else:
                        row = j % rows
                        col = j // rows
                        read_addr = row * cols + col
                    output.append(buf_read[read_addr])

        return np.array(output, dtype=np.uint16) if output else np.array([], dtype=np.uint16)

    def reset(self):
        """Reset block state."""
        pass
