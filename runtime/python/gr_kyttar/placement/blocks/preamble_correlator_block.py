"""PreambleCorrelatorBlock — see :class:`PreambleCorrelatorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Tuple
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class PreambleCorrelatorBlock(KyttarBlock):
    """
    Preamble Correlator Block (4 cells).

    Correlates incoming samples against the MIL-STD-188-110B 287-symbol preamble
    to detect frame start. The preamble consists of 3 repetitions of a 0.2s
    segment, allowing detection via autocorrelation peaks.

    Architecture: Sliding Correlator (4 cells)
    ==========================================

    Instead of storing the full 287-symbol preamble, we use autocorrelation:
    - Correlate x[n] with x[n-96] (96 = one 0.2s segment at 2400 baud)
    - Three peaks indicate preamble detected

    Cell Layout:
    ```
        In → [DELAY0-1] → [CORR0-1] → [PEAK_DET] → Out
    ```

    Components:
    - DELAY (2 cells): 96-symbol sliding delay line (packed storage)
    - CORR (1 cell): Multiply-accumulate correlator
    - PEAK_DET (1 cell): Peak detector and threshold

    Total: 4 cells

    Memory Layout for DELAY cell (32 words):
    - R0-R15: 16 delay samples (6 cells would give 96 samples)
    - Actually, we use a simplified 48-symbol check with 2 cells

    Interface:
        - Entry: R1
        - Input: R31 (complex samples)
        - Output: Correlation magnitude + sync flag

    MIL-STD-188-110B Preamble Structure:
        - Total: 287 symbols
        - 3 × 0.2s segments (96 symbols each @ 2400 baud) = 288
        - Minus 1 for framing = 287
        - Detectable by autocorrelation peaks at lag 96
    """
    CATEGORY = "frame_sync"
    TAGS = ["preamble", "correlator", "frame_sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    SEGMENT_LENGTH = 96  # Symbols per 0.2s segment at 2400 baud
    THRESHOLD = 0.8      # Correlation threshold for detection

    def __init__(
        self,
        name: str,
        threshold: float = 0.8,
    ):
        """
        Initialize Preamble Correlator.

        Args:
            name: Block name
            threshold: Detection threshold (0.0-1.0)
        """
        super().__init__(name, threshold=threshold)
        self._threshold = threshold

        # State for reference processing
        self._delay_buffer = [0.0] * self.SEGMENT_LENGTH
        self._correlation_history = []
        self._peak_count = 0
        self._sync_detected = False

    @property
    def cell_count(self) -> int:
        return self.SEGMENT_LENGTH // 8 + 1  # 12 delay cells + 1 correlator = 13

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_delay_program(self, cell_idx: int) -> CellProgram:
        """Build delay buffer cell."""
        prog = CellProgram()

        # Each cell stores 16 samples in R10-R25
        # Implements a shift register
        assembly = f"""; Preamble Correlator Delay Cell {cell_idx}
; R10-R25: Delay line samples (16 per cell)
; R31: Input sample

start:
    ; Shift delay line
    MOVE R25, R24
    MOVE R24, R23
    MOVE R23, R22
    MOVE R22, R21
    MOVE R21, R20
    MOVE R20, R19
    MOVE R19, R18
    MOVE R18, R17
    MOVE R17, R16
    MOVE R16, R15
    MOVE R15, R14
    MOVE R14, R13
    MOVE R13, R12
    MOVE R12, R11
    MOVE R11, R10
    MOVE R10, R31   ; New sample at head

    ; Forward oldest sample to next delay cell or correlator
    MOVE R0, R25
    WRITE @1, 31    ; Pass to next cell
    JUMP @1, 1
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def _build_correlator_program(self) -> CellProgram:
        """Build correlator cell."""
        prog = CellProgram()

        assembly = """; Preamble Correlator - Correlation Cell
; R16: Current sample (from delay), R17: Delayed sample
; R18: Running correlation sum, R19: Sample count
; R31: Input sample

start:
    ; Correlate: sum += x[n] * x[n-delay]
    MULQ R31, R17       ; R0 = current * delayed
    ADD R18, R0         ; R18 = running sum
    MOVE R18, R0

    ; Update delayed sample
    MOVE R17, R31

    ; Increment sample count
    ADD R19, 1
    MOVE R19, R0

    ; Check if we have enough samples (e.g., 32)
    CMP R19, 32
    BR.N continue

    ; Output correlation magnitude
    MOVE R0, R18
    WRITE @1, 31        ; Send to peak detector

    ; Reset for next window
    XOR R0, R0
    MOVE R18, R0
    MOVE R19, R0
    JUMP @1, 1

continue:
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def _build_peak_detector_program(self, output_hop: int, target_input: int, target_entry: int) -> CellProgram:
        """Build peak detector cell."""
        prog = CellProgram()

        # Threshold in Q15
        threshold_q15 = float_to_q15(self._threshold)
        prog.set_memory(16, threshold_q15)
        prog.set_memory(17, 0)  # Peak count
        prog.set_memory(18, 0)  # Sync flag

        assembly = f"""; Preamble Correlator - Peak Detector
; R16: Threshold, R17: Peak count, R18: Sync flag
; R31: Correlation value from correlator

start:
    ; Compare correlation to threshold
    CMP R31, R16
    BR.N no_peak

    ; Peak detected - increment count
    ADD R17, 1
    MOVE R17, R0

    ; Check if we have 3 peaks (preamble confirmed)
    CMP R17, 3
    BR.N output_no_sync

    ; Sync detected!
    MOVI R18, 1
    MOVE R0, R18
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}

    ; Reset peak count
    XOR R0, R0
    MOVE R17, R0
    HALT

no_peak:
    ; No peak - could reset count if gap too large
    ; (simplified: just continue)
    HALT

output_no_sync:
    ; Output no-sync indicator
    XOR R0, R0
    MOVE R18, R0
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production PreambleCorrelator: D=96 autocorrelator.

        12 delay cells (8 samples each) + 1 correlator = 13 cells.
        Total delay = 12 × 8 = 96 samples = one 0.2s preamble segment at 2400 baud.

        Cell 0 (Head delay): Receives input, pre-stages x[n] directly to
          correlator cell (WRITE-without-JUMP, distance=12), shifts 8-sample
          delay line, forwards oldest to Cell 1.

        Cells 1-11 (Intermediate delay): Each shifts 8-sample delay line,
          forwards oldest to next cell. No pre-staging needed.

        Cell 12 (Correlator): Receives x[n] pre-staged from Cell 0 into
          'current' register. Receives x[n-96] from Cell 11 via WRITE+JUMP.
          Computes MULQ(current, delayed), accumulates, outputs every 96 samples.
        """
        delay_per_cell = 8
        num_delay_cells = self.SEGMENT_LENGTH // delay_per_cell  # 96/8 = 12
        total_delay = num_delay_cells * delay_per_cell  # 96
        correlator_idx = num_delay_cells  # cell 12
        window = total_delay  # accumulation window = full delay

        programs = {}

        # --- Cell 0 (Head delay + pre-stage current to correlator) ---
        delay_data = [DataWord(f"d{i}", 0, address=i + 1)
                      for i in range(delay_per_cell)]
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("fwd_current"), Port("fwd_delayed")],
            entries=[EntryPoint("default")],
            data=delay_data,
            state=[
                StateVar("in_save"),
                StateVar("oldest"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R0
    {write:fwd_current}
    MOVE R0, R{data:d7}
    MOVE R{state:oldest}, R0
    MOVE R{data:d7}, R{data:d6}
    MOVE R{data:d6}, R{data:d5}
    MOVE R{data:d5}, R{data:d4}
    MOVE R{data:d4}, R{data:d3}
    MOVE R{data:d3}, R{data:d2}
    MOVE R{data:d2}, R{data:d1}
    MOVE R{data:d1}, R{data:d0}
    MOVE R{data:d0}, R{state:in_save}
    MOVE R0, R{state:oldest}
    {write:fwd_delayed}
    {jump:fwd_delayed}
""",
        )
        programs[0] = cell0

        # --- Cells 1 to num_delay_cells-1 (Intermediate delay cells) ---
        for cell_idx in range(1, num_delay_cells):
            d_data = [DataWord(f"d{i}", 0, address=i + 1)
                      for i in range(delay_per_cell)]
            cell = CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("fwd")],
                entries=[EntryPoint("default")],
                data=d_data,
                state=[
                    StateVar("in_save"),
                    StateVar("oldest"),
                ],
                assembly_template="""\
start:
    MOVE R{state:in_save}, R0
    MOVE R0, R{data:d7}
    MOVE R{state:oldest}, R0
    MOVE R{data:d7}, R{data:d6}
    MOVE R{data:d6}, R{data:d5}
    MOVE R{data:d5}, R{data:d4}
    MOVE R{data:d4}, R{data:d3}
    MOVE R{data:d3}, R{data:d2}
    MOVE R{data:d2}, R{data:d1}
    MOVE R{data:d1}, R{data:d0}
    MOVE R{data:d0}, R{state:in_save}
    MOVE R0, R{state:oldest}
    {write:fwd}
    {jump:fwd}
""",
            )
            programs[cell_idx] = cell

        # --- Correlator cell ---
        corr_window_lo = window & 0xFFFF
        cell_corr = CellProgram(
            inputs=[Port("delayed", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("window_size", corr_window_lo, address=2),
            ],
            state=[
                StateVar("current"),  # pre-staged by Cell 0
                StateVar("accum"),
                StateVar("counter"),
            ],
            assembly_template="""\
start:
    MULQ R{state:current}, R0
    ADD R{state:accum}, R0
    MOVE R{state:accum}, R0
    ADD R{state:counter}, R{data:one}
    MOVE R{state:counter}, R0
    CMP R{state:counter}, R{data:window_size}
    BR.N done
    MOVE R0, R{state:accum}
    {write:out}
    SUB R{state:counter}, R{state:counter}
    MOVE R{state:counter}, R0
    MOVE R{state:accum}, R0
    {jump:out}
done:
""",
        )
        programs[correlator_idx] = cell_corr

        return programs

    def process_reference(self, input_samples: np.ndarray) -> Tuple[np.ndarray, bool]:
        """
        Reference implementation of preamble correlation.

        Args:
            input_samples: Input samples

        Returns:
            Tuple of (correlation_values, sync_detected)
        """
        n_samples = len(input_samples)
        correlation = np.zeros(n_samples, dtype=np.float32)

        for i in range(n_samples):
            sample = float(input_samples[i])

            # Get delayed sample
            if len(self._delay_buffer) >= self.SEGMENT_LENGTH:
                delayed = self._delay_buffer[-self.SEGMENT_LENGTH]
            else:
                delayed = 0.0

            # Update delay buffer
            self._delay_buffer.append(sample)
            if len(self._delay_buffer) > self.SEGMENT_LENGTH * 2:
                self._delay_buffer.pop(0)

            # Compute correlation
            corr = sample * delayed
            correlation[i] = corr

            # Simple peak detection
            if abs(corr) > self._threshold:
                self._peak_count += 1
                if self._peak_count >= 3:
                    self._sync_detected = True

        return correlation, self._sync_detected

    def reset(self):
        """Reset correlator state."""
        self._delay_buffer = [0.0] * self.SEGMENT_LENGTH
        self._correlation_history = []
        self._peak_count = 0
        self._sync_detected = False
