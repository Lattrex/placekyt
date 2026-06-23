"""MiniProbeInserterBlock — see :class:`MiniProbeInserterBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class MiniProbeInserterBlock(KyttarBlock):
    """
    Mini-Probe Inserter Block (2 cells).

    Inserts 31-symbol mini-probe sequences every 256 data symbols as required
    by MIL-STD-188-110B. The probe provides known symbols for equalizer update.

    Architecture: Counter + Mux (2 cells)
    =====================================

    Cell Layout:
    ```
        Data_In → [COUNTER] → [MUX] → Out
                              ↑
                    Probe_Pattern
    ```

    Components:
    - COUNTER (1 cell): Counts to 256, triggers probe insertion
    - MUX (1 cell): Switches between data and probe symbols

    Total: 2 cells

    Interface:
        - Entry: R1
        - Input: R31 (data symbols)
        - Output: Data with mini-probes inserted
    """
    CATEGORY = "frame_sync"
    TAGS = ["mini_probe", "inserter", "frame_sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    DATA_BLOCK_LENGTH = 256
    PROBE_LENGTH = 31

    # Mini-probe pattern (same as detector)
    PROBE_PATTERN = [1, 1, 1, -1, 1, 1, -1, -1, 1, -1, 1, -1, -1, -1, -1, 1,
                    1, -1, -1, -1, 1, -1, -1, 1, 1, 1, -1, -1, 1, -1, 1]

    def __init__(self, name: str):
        """Initialize Mini-Probe Inserter."""
        super().__init__(name)
        self._data_count = 0
        self._probe_index = 0
        self._inserting_probe = False

    @property
    def cell_count(self) -> int:
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_counter_program(self) -> CellProgram:
        """Build counter cell."""
        prog = CellProgram()

        prog.set_memory(16, 0)  # Data counter
        prog.set_memory(17, self.DATA_BLOCK_LENGTH)
        prog.set_memory(18, 0)  # Probe mode flag

        assembly = """; Mini-Probe Inserter Counter
; R16: data_count, R17: block_length, R18: probe_mode
; R31: Input data symbol

start:
    ; Check if in probe mode
    CMP R18, 0
    BR.NZ probe_mode

    ; Data mode - forward data
    MOVE R0, R31
    WRITE @1, 31

    ; Increment counter
    ADD R16, 1
    MOVE R16, R0

    ; Check if block complete
    CMP R16, R17
    BR.N continue

    ; Block complete - switch to probe mode
    MOVI R18, 1
    XOR R0, R0
    MOVE R16, R0
    JUMP @1, 1
    HALT

probe_mode:
    ; Forward probe flag to mux
    MOVI R0, 1
    WRITE @1, 20    ; Indicate probe mode
    JUMP @1, 1
    HALT

continue:
    JUMP @1, 1
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def _build_mux_program(self, output_hop: int, target_input: int, target_entry: int) -> CellProgram:
        """Build mux cell with probe pattern."""
        prog = CellProgram()

        # Store probe pattern (first 16 values)
        for i, val in enumerate(self.PROBE_PATTERN[:16]):
            prog.set_memory(10 + i, float_to_q15(val))

        prog.set_memory(26, 0)  # Probe index
        prog.set_memory(27, self.PROBE_LENGTH)

        assembly = f"""; Mini-Probe Inserter Mux
; R10-R25: Probe pattern, R26: probe_idx, R27: probe_len
; R20: probe_mode, R31: data_in

start:
    ; Check mode
    CMP R20, 0
    BR.NZ output_probe

    ; Data mode - pass through
    MOVE R0, R31
    GOTO output

output_probe:
    ; Output probe symbol (simplified - would need indirect)
    MOVE R0, R10    ; First probe value
    ADD R26, 1
    MOVE R26, R0

    ; Check if probe done
    CMP R26, R27
    BR.N output
    ; Reset
    XOR R0, R0
    MOVE R26, R0
    MOVE R20, R0

output:
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 MiniProbeInserter: counter + 2 probe lookup tables.

        Cell 0 (Counter): Counts 0-286. Data mode (0-255): passes input to
          downstream (distance 2, skipping cell 1). Probe mode (256-286):
          sends probe index+1 (1-31) to Cell 1 for LOAD lookup.

        Cell 1 (Probe Lookup): probe[0]-probe[25] at addresses 1-26 (26 values).
          Receives address directly (1-26), LOAD, outputs to downstream.
          For indices > 25 (addresses 27-31 overlap with instructions/HALT),
          these are handled by Cell 0 directly.

        Actually: 31 probe values won't fit in one cell. Split across 2 cells:

        Cell 0 (Counter): Counts 0-286.
          - Data mode (counter < 256): passes input to downstream (dist 3).
          - Probe mode lo (counter 256-271, pidx 0-15): sends pidx+1 to Cell 1.
          - Probe mode hi (counter 272-286, pidx 16-30): sends pidx-15 to Cell 2.

        Cell 1 (Probe A): probe[0]-probe[15] at addresses 1-16.
          Receives address (1-16), LOAD, outputs to downstream (dist 2).

        Cell 2 (Probe B): probe[16]-probe[30] at addresses 1-15.
          Receives address (1-15), LOAD, outputs to downstream (dist 1).

        Layout: [Counter(0)] → [ProbeA(1)] → [ProbeB(2)] → [Relay(3)]
        """
        total_len = self.DATA_BLOCK_LENGTH + self.PROBE_LENGTH  # 287
        probe_q15 = [float_to_q15(v) for v in self.PROBE_PATTERN]

        # Cell 0: Counter + routing
        # Three exit paths: data_out, probe_lo, probe_hi.
        # External WRITE/JUMP do NOT halt the cell — execution continues.
        # Use GOTO to shared inc_counter after each exit path.
        # hi_probe path falls through to inc_counter naturally.
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("data_out"), Port("probe_lo"), Port("probe_hi")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("one", 1, address=2),
                DataWord("block_len", self.DATA_BLOCK_LENGTH, address=3),
                DataWord("total_len", total_len, address=4),
                DataWord("sixteen", 16, address=5),
            ],
            state=[
                StateVar("counter"),
                StateVar("in_save"),
            ],
            assembly_template="""\
start:
    ; R0 = input sample. Save it.
    MOVE R{state:in_save}, R0
    ; R0 = counter - block_len (sets flags). NO branch yet.
    SUB R{state:counter}, R{data:block_len}
    ; If negative, counter < block_len → data mode
    BR.N data_mode
    ; --- Probe mode: R0 = pidx (>= 0), valid (no branch crossed) ---
    MOVE R{state:in_save}, R0
    CMP R{state:in_save}, R{data:sixteen}
    BR.NN hi_probe
    ; pidx < 16: send pidx+1 to Cell 1
    ADD R{state:in_save}, R{data:one}
    {write:probe_lo}
    {jump:probe_lo}
    GOTO inc_counter
hi_probe:
    ; pidx >= 16: compute pidx-15 from in_save (avoid R0 pipeline hazard)
    ; SUB→ADD sequential (no branch), R0 survives
    SUB R{state:in_save}, R{data:sixteen}
    ADD R0, R{data:one}
    {write:probe_hi}
    {jump:probe_hi}
    GOTO inc_counter
data_mode:
    ; counter < block_len. in_save = original input (saved at start).
    MOVE R0, R{state:in_save}
    {write:data_out}
    {jump:data_out}
    ; falls through to inc_counter
inc_counter:
    ADD R{state:counter}, R{data:one}
    MOVE R{state:counter}, R0
    CMP R{state:counter}, R{data:total_len}
    BR.N done
    MOVE R{state:counter}, R{data:zero}
done:
""",
        )

        # Cell 1: Probe table A (probe[0]-probe[15] at addresses 1-16)
        # Minimal program: just LOAD and output.
        probe_a_data = [DataWord(f"p{i}", probe_q15[i], address=i + 1)
                        for i in range(16)]
        cell1 = CellProgram(
            inputs=[Port("addr")],  # auto-allocated in gap
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=probe_a_data,
            assembly_template="""\
start:
    LOAD R{in:addr}
    {write:out}
    {jump:out}
""",
        )

        # Cell 2: Probe table B (probe[16]-probe[30] at addresses 1-15)
        probe_b_data = [DataWord(f"p{16+i}", probe_q15[16 + i], address=i + 1)
                        for i in range(15)]
        cell2 = CellProgram(
            inputs=[Port("addr")],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=probe_b_data,
            assembly_template="""\
start:
    LOAD R{in:addr}
    {write:out}
    {jump:out}
""",
        )

        return {0: cell0, 1: cell1, 2: cell2}

    def process_reference(self, input_data: np.ndarray) -> np.ndarray:
        """
        Reference implementation of mini-probe insertion.

        Args:
            input_data: Input data symbols

        Returns:
            Data with mini-probes inserted
        """
        output = []
        data_idx = 0

        while data_idx < len(input_data):
            # Output data block
            block_end = min(data_idx + self.DATA_BLOCK_LENGTH, len(input_data))
            output.extend(input_data[data_idx:block_end])
            data_idx = block_end

            # Insert probe if we output a full block
            if block_end - (data_idx - self.DATA_BLOCK_LENGTH) == self.DATA_BLOCK_LENGTH:
                output.extend(self.PROBE_PATTERN)

        return np.array(output, dtype=np.float32)

    def reset(self):
        """Reset inserter state."""
        self._data_count = 0
        self._probe_index = 0
        self._inserting_probe = False
