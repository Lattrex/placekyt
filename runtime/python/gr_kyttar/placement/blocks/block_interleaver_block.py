"""BlockInterleaverBlock — see :class:`BlockInterleaverBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class BlockInterleaverBlock(KyttarBlock):
    """
    Block Interleaver/Deinterleaver — Loopback Test Placeholder.

    In production, MIL-STD-188-110B interleaver storage (1440 symbols
    short, 11520 symbols long) is offloaded to FPGA SRAM. The on-chip
    block becomes a 1-2 cell memory controller that:
    1. Receives symbols from upstream, WRITEs to FPGA SRAM via x1_out
    2. After a full block, requests readback in permuted order
    3. Forwards deinterleaved symbols to downstream

    This placeholder uses a reduced 8×16 = 128 symbol depth for
    loopback testing. It is NOT sufficient for production use.

    Cell Layout:
    ```
        [MEM0] → [MEM1] → [MEM2] → [MEM3] → Out
          ↑                           │
          └─────── write ptr ─────────┘
    ```

    Total: 4 cells

    Interface:
        - Entry: R1
        - Input: R31
        - Output: Interleaved/deinterleaved symbols
    """
    CATEGORY = "fec"
    TAGS = ["interleaver", "deinterleaver", "fec"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(
        self,
        name: str,
        rows: int = 8,
        cols: int = 16,
        is_deinterleaver: bool = False,
    ):
        """
        Initialize Block Interleaver.

        Args:
            name: Block name
            rows: Number of rows in interleave matrix
            cols: Number of columns in interleave matrix
            is_deinterleaver: If True, deinterleave (reverse operation)
        """
        super().__init__(
            name,
            rows=rows,
            cols=cols,
            is_deinterleaver=is_deinterleaver,
        )
        self._rows = rows
        self._cols = cols
        self._is_deinterleaver = is_deinterleaver
        self._depth = rows * cols

        # Buffer for reference implementation
        self._buffer = np.zeros((rows, cols), dtype=np.float32)
        self._write_idx = 0
        self._read_idx = 0

    @property
    def cell_count(self) -> int:
        return 4

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_cell_program(
        self,
        cell_idx: int,
        is_last: bool,
        output_hop: int,
        target_interface: BlockInterface
    ) -> CellProgram:
        """Build a single memory cell program."""
        prog = CellProgram()

        # Memory layout:
        # R1-R8: Program
        # R9-R24: Symbol storage (16 symbols)
        # R25: Write pointer
        # R26: Read pointer
        # R27: Full flag
        # R28: One constant

        prog.set_memory(25, 9)   # Write pointer starts at R9
        prog.set_memory(26, 9)   # Read pointer starts at R9
        prog.set_memory(27, 0)   # Not full
        prog.set_memory(28, 1)   # One

        target_input = target_interface.input_registers[0]
        target_entry = target_interface.entry_address

        if is_last:
            assembly = f"""; Interleaver Cell {cell_idx} (LAST)
; R9-R24: symbol storage, R25: write_ptr, R26: read_ptr

start:
    ; Store incoming symbol
    ; (Simplified - actual impl needs indirect write)
    MOVE R9, R31

    ; Increment write pointer
    ADD R25, R28
    MOVE R25, R0

    ; Check if buffer full (write_ptr >= 24)
    ; If full, output from read position

    ; Output symbol (simplified)
    MOVE R0, R9
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT
"""
        else:
            assembly = f"""; Interleaver Cell {cell_idx}
; R9-R24: symbol storage, R25: write_ptr, R26: read_ptr

start:
    ; Store incoming symbol
    MOVE R9, R31

    ; Increment write pointer
    ADD R25, R28
    MOVE R25, R0

    ; Forward to next cell
    MOVE R0, R31
    WRITE @1, 31
    JUMP @1, 1
    HALT
"""

        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 BlockInterleaver: single-cell LOAD-based matrix interleaver.

        Fill-then-drain protocol with depth = rows × cols.

        Memory layout:
          R1 .. R{depth}:         Data buffer (filled externally via WRITE)
          R{depth+1}..R{2*depth}: Permutation table (col-major read order)
          Constants: one, perm_start, perm_end

        Two program sections:
          fill_entry (default): No-op — data already written by external WRITE.
                               Just HALTs (no output during fill).
          drain_entry:          LOAD perm[drain_ptr] → output, advance ptr.
                               On last drain, resets drain_ptr for next cycle.

        Test protocol:
          Fill phase:  WRITE(hop, addr=k+1) + data + JUMP(hop, fill_entry)  × depth
          Drain phase: WRITE(hop, addr=0)   + dummy + JUMP(hop, drain_entry) × depth
        """
        depth = self._depth
        rows = self._rows
        cols = self._cols

        # Compute column-major readout permutation
        # Fill is row-major: index k → row k//cols, col k%cols → buffer addr k+1
        # Drain is col-major: for c in cols, for r in rows → buffer addr r*cols+c+1
        if not self._is_deinterleaver:
            perm = []
            for c in range(cols):
                for r in range(rows):
                    perm.append(r * cols + c + 1)
        else:
            # Deinterleaver: fill col-major, drain row-major
            perm = []
            for r in range(rows):
                for c in range(cols):
                    perm.append(c * rows + r + 1)

        buf_start = 1
        perm_start = depth + 1
        perm_end = perm_start + depth  # one past last

        # Data: buffer (init 0) + perm table + constants
        data_words = []
        # Buffer slots at addresses 1..depth (init to 0)
        for k in range(depth):
            data_words.append(DataWord(f"buf{k}", 0, address=buf_start + k))
        # Perm table at addresses perm_start..perm_start+depth-1
        for k in range(depth):
            data_words.append(DataWord(f"perm{k}", perm[k], address=perm_start + k))
        # Constants after perm table
        const_base = perm_start + depth
        data_words.append(DataWord("one", 1, address=const_base))
        data_words.append(DataWord("perm_start_val", perm_start, address=const_base + 1))
        data_words.append(DataWord("perm_end_val", perm_end, address=const_base + 2))

        # Count data+state to determine instruction region.
        # Data: 2*depth + 3 constants. State: 1 (drain_ptr). No inputs.
        total_data = 2 * depth + 3
        total_state = 1
        # Instructions: 10 (1 HALT for fill + 9 for drain). HALT at R31.
        n_instr = 10
        base_addr = 31 - n_instr
        fill_entry_addr = base_addr       # HALT instruction
        drain_entry_addr = base_addr + 1  # LOAD instruction

        cell0 = CellProgram(
            inputs=[],  # No auto-allocated input — fill uses varying external WRITE addrs
            outputs=[Port("out")],
            entries=[
                EntryPoint("fill_entry", address=fill_entry_addr),
                EntryPoint("drain_entry", address=drain_entry_addr),
            ],
            state=[
                StateVar("drain_ptr", initial_value=perm_start),
            ],
            data=data_words,
            assembly_template="""\
fill_entry:
    HALT
drain_entry:
    LOAD R{state:drain_ptr}
    LOAD R0
    {write:out}
    ADD R{state:drain_ptr}, R{data:one}
    MOVE R{state:drain_ptr}, R0
    CMP R{state:drain_ptr}, R{data:perm_end_val}
    BR.N drain_done
    MOVE R{state:drain_ptr}, R{data:perm_start_val}
drain_done:
    {jump:out}
""",
        )

        self._fill_entry_addr = fill_entry_addr
        self._drain_entry_addr = drain_entry_addr

        return {0: cell0}

    def process_reference(self, input_symbols: np.ndarray) -> np.ndarray:
        """
        Reference implementation of block interleaver.

        Args:
            input_symbols: Input symbols

        Returns:
            Interleaved/deinterleaved symbols
        """
        n_symbols = len(input_symbols)
        output = np.zeros(n_symbols, dtype=np.float32)

        if self._is_deinterleaver:
            # Deinterleave: read column-by-column, output row-by-row
            for i, sym in enumerate(input_symbols):
                col = self._write_idx % self._cols
                row = self._write_idx // self._cols
                self._buffer[row, col] = sym
                self._write_idx += 1

                if self._write_idx == self._depth:
                    # Buffer full, output row-by-row
                    for r in range(self._rows):
                        for c in range(self._cols):
                            out_idx = i - self._depth + 1 + r * self._cols + c
                            if 0 <= out_idx < n_symbols:
                                output[out_idx] = self._buffer[r, c]
                    self._write_idx = 0
        else:
            # Interleave: write row-by-row, read column-by-column
            for i, sym in enumerate(input_symbols):
                row = self._write_idx // self._cols
                col = self._write_idx % self._cols
                self._buffer[row, col] = sym
                self._write_idx += 1

                if self._write_idx == self._depth:
                    # Buffer full, output column-by-column
                    for c in range(self._cols):
                        for r in range(self._rows):
                            out_idx = i - self._depth + 1 + c * self._rows + r
                            if 0 <= out_idx < n_symbols:
                                output[out_idx] = self._buffer[r, c]
                    self._write_idx = 0

        return output

    def reset(self):
        """Reset interleaver state."""
        self._buffer = np.zeros((self._rows, self._cols), dtype=np.float32)
        self._write_idx = 0
        self._read_idx = 0
