"""FrameSyncBlock — see :class:`FrameSyncBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Tuple
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class FrameSyncBlock(KyttarBlock):
    """
    Frame Sync State Machine Block (2 cells).

    Tracks frame synchronization state for MIL-STD-188-110B:
    - HUNT: Looking for preamble
    - SYNC: Preamble detected, counting symbols
    - DATA: In data phase, tracking mini-probes

    Architecture: State Machine (2 cells)
    =====================================

    Cell Layout:
    ```
        In → [STATE] → [COUNTER] → Out
    ```

    Components:
    - STATE (1 cell): FSM state register and transitions
    - COUNTER (1 cell): Symbol counter for frame timing

    Total: 2 cells

    Frame Structure (MIL-STD-188-110B):
        287-symbol preamble
        → [256 data + 31 mini-probe] × N
        → 103-symbol mini-preamble every 72 blocks
        → EOM pattern

    Interface:
        - Entry: R1
        - Input: R31 (sync flags from correlator)
        - Output: Frame state + symbol index
    """
    CATEGORY = "frame_sync"
    TAGS = ["frame_sync", "state_machine", "sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # States
    STATE_HUNT = 0
    STATE_SYNC = 1
    STATE_DATA = 2

    # Frame parameters
    PREAMBLE_LENGTH = 287
    DATA_BLOCK_LENGTH = 256
    MINI_PROBE_LENGTH = 31
    MINI_PREAMBLE_INTERVAL = 72  # blocks

    def __init__(self, name: str):
        """Initialize Frame Sync block."""
        super().__init__(name)
        self._state = self.STATE_HUNT
        self._symbol_counter = 0
        self._block_counter = 0

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Two-cell frame sync.

        Cell 0 (State + Preamble Counter):
        - HUNT: outputs 0 every sample until sync_flag is nonzero
        - On sync detection: transitions to SYNC, counts preamble symbols
        - On preamble complete: transitions to DATA, sends state=2 to cell 1

        Cell 1 (Data Block Counter + Output):
        - Receives state from cell 0
        - In DATA state: counts symbols per block (256 data + 31 probe)
        - Outputs packed (state << 8) | block_position
        """
        block_size = self.DATA_BLOCK_LENGTH + self.MINI_PROBE_LENGTH  # 287

        # Cell 0: state machine + preamble counting
        # Instruction count: ~18
        cell0 = CellProgram(
            inputs=[Port("flag", register=0)],
            outputs=[Port("state_out"), Port("fwd")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("one", 1, address=2),
                DataWord("two", 2, address=3),
                DataWord("preamble_len", self.PREAMBLE_LENGTH, address=4),
            ],
            state=[
                StateVar("st"),       # 0=HUNT, 1=SYNC, 2=DATA
                StateVar("cnt"),      # preamble symbol counter
            ],
            assembly_template="""\
start:
    CMP R{state:st}, R{data:zero}
    BR.NZ check_sync
    CMP R{in:flag}, R{data:zero}
    BR.Z send_state
    MOVE R{state:st}, R{data:one}
    MOVE R{state:cnt}, R{data:zero}
    GOTO send_state
check_sync:
    CMP R{state:st}, R{data:one}
    BR.NZ send_state
    ADD R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    CMP R{state:cnt}, R{data:preamble_len}
    BR.N send_state
    MOVE R{state:st}, R{data:two}
    MOVE R{state:cnt}, R{data:zero}
send_state:
    MOVE R0, R{state:st}
    {write:state_out}
    {jump:fwd}
""",
        )

        # Cell 1: block counter + output
        # Tracks: symbol position within block (0-286), block count within
        # mini-preamble interval (0-71). Output packed as:
        #   (state << 8) | sym_position
        # Block count available in blk_cnt state for external monitoring.
        # state_in must NOT be R0 since R0 is clobbered by ADD/CMP
        cell1 = CellProgram(
            inputs=[Port("state_in")],  # auto-allocated, not R0
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("one", 1, address=2),
                DataWord("two", 2, address=3),
                DataWord("block_size", block_size, address=4),
                DataWord("mini_preamble_interval", self.MINI_PREAMBLE_INTERVAL, address=5),
            ],
            state=[
                StateVar("sym_cnt"),   # symbol position in block (0 to block_size-1)
                StateVar("blk_cnt"),   # block count within mini-preamble interval (0-71)
            ],
            assembly_template="""\
start:
    CMP R{in:state_in}, R{data:two}
    BR.NZ not_data
    ADD R{state:sym_cnt}, R{data:one}
    MOVE R{state:sym_cnt}, R0
    CMP R{state:sym_cnt}, R{data:block_size}
    BR.N output
    ; Block complete — reset sym_cnt, increment blk_cnt
    MOVE R{state:sym_cnt}, R{data:zero}
    ADD R{state:blk_cnt}, R{data:one}
    MOVE R{state:blk_cnt}, R0
    CMP R{state:blk_cnt}, R{data:mini_preamble_interval}
    BR.N output
    ; Mini-preamble interval complete — reset blk_cnt
    MOVE R{state:blk_cnt}, R{data:zero}
    GOTO output
not_data:
    MOVE R{state:sym_cnt}, R{data:zero}
    MOVE R{state:blk_cnt}, R{data:zero}
output:
    SHL R{in:state_in}, #8
    OR R0, R{state:sym_cnt}
    {write:out}
    {jump:out}
""",
        )

        return {0: cell0, 1: cell1}

    def process_reference(self, sync_flags: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Reference implementation of frame sync state machine.

        Args:
            sync_flags: Sync detection flags from preamble correlator

        Returns:
            Tuple of (states, symbol_indices)
        """
        n_samples = len(sync_flags)
        states = np.zeros(n_samples, dtype=np.int32)
        indices = np.zeros(n_samples, dtype=np.int32)

        block_size = self.DATA_BLOCK_LENGTH + self.MINI_PROBE_LENGTH

        for i in range(n_samples):
            sync = int(sync_flags[i])

            if self._state == self.STATE_HUNT:
                if sync:
                    self._state = self.STATE_SYNC
                    self._symbol_counter = 0
            elif self._state == self.STATE_SYNC:
                self._symbol_counter += 1
                if self._symbol_counter >= self.PREAMBLE_LENGTH:
                    self._state = self.STATE_DATA
                    self._symbol_counter = 0
                    self._block_counter = 0
            elif self._state == self.STATE_DATA:
                self._symbol_counter += 1
                if self._symbol_counter >= block_size:
                    self._symbol_counter = 0
                    self._block_counter += 1
                    if self._block_counter >= self.MINI_PREAMBLE_INTERVAL:
                        self._block_counter = 0

            states[i] = self._state
            indices[i] = self._symbol_counter

        return states, indices

    def reset(self):
        """Reset frame sync state."""
        self._state = self.STATE_HUNT
        self._symbol_counter = 0
        self._block_counter = 0
