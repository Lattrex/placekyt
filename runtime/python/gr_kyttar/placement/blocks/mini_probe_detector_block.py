"""MiniProbeDetectorBlock — see :class:`MiniProbeDetectorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Tuple
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class MiniProbeDetectorBlock(KyttarBlock):
    """
    Mini-Probe Detector Block (2 cells).

    Detects the 31-symbol mini-probe sequences inserted every 256 data symbols
    in MIL-STD-188-110B. Uses correlation against known probe pattern.

    Architecture: Correlator + Detector (2 cells)
    ==============================================

    Cell Layout:
    ```
        In → [CORR] → [DET] → Out
    ```

    Components:
    - CORR (1 cell): 31-point correlation with known sequence
    - DET (1 cell): Peak detection and validation

    Total: 2 cells

    Mini-Probe Pattern:
        31-symbol known BPSK sequence (defined in MIL-STD-188-110B Appendix A)
        Used for:
        - Fine timing adjustment
        - Channel estimation update
        - Equalizer coefficient update

    Interface:
        - Entry: R1
        - Input: R31 (equalized symbols)
        - Output: Probe detected flag + channel estimate
    """
    CATEGORY = "frame_sync"
    TAGS = ["mini_probe", "detector", "frame_sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    PROBE_LENGTH = 31

    # Simplified mini-probe pattern (actual pattern from MIL-STD-188-110B)
    # This is a maximum length sequence
    PROBE_PATTERN = [1, 1, 1, -1, 1, 1, -1, -1, 1, -1, 1, -1, -1, -1, -1, 1,
                    1, -1, -1, -1, 1, -1, -1, 1, 1, 1, -1, -1, 1, -1, 1]

    def __init__(self, name: str, threshold: float = 0.7):
        """
        Initialize Mini-Probe Detector.

        Args:
            name: Block name
            threshold: Detection threshold
        """
        super().__init__(name, threshold=threshold)
        self._threshold = threshold
        self._buffer = []

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_correlator_program(self) -> CellProgram:
        """Build correlator cell with probe pattern."""
        prog = CellProgram()

        # Store first 16 probe values in Q15
        for i, val in enumerate(self.PROBE_PATTERN[:16]):
            prog.set_memory(10 + i, float_to_q15(val))

        prog.set_memory(26, 0)  # Correlation sum
        prog.set_memory(27, 0)  # Sample index

        assembly = """; Mini-Probe Correlator
; R10-R25: Probe pattern (16 values)
; R26: Running correlation, R27: Index
; R31: Input symbol

start:
    ; Get probe value at current index
    ADD R27, 10         ; R0 = index + base_addr
    MOVE R28, R0
    ; (Would need indirect load - simplified)

    ; Correlate: sum += input * probe[index]
    MULQ R31, R10       ; Simplified: use R10 as stand-in
    ADD R26, R0
    MOVE R26, R0

    ; Increment index
    ADD R27, 1
    MOVE R27, R0

    ; Check if probe complete
    CMP R27, 31
    BR.N continue

    ; Output correlation and reset
    MOVE R0, R26
    WRITE @1, 31
    XOR R0, R0
    MOVE R26, R0
    MOVE R27, R0
    JUMP @1, 1

continue:
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def _build_detector_program(self, output_hop: int, target_input: int, target_entry: int) -> CellProgram:
        """Build detector cell."""
        prog = CellProgram()

        threshold_q15 = float_to_q15(self._threshold * self.PROBE_LENGTH)
        prog.set_memory(16, threshold_q15)
        prog.set_memory(17, 0)  # Detection flag

        assembly = f"""; Mini-Probe Detector
; R16: Threshold, R17: Detection flag
; R31: Correlation from correlator

start:
    ; Compare correlation to threshold
    CMP R31, R16
    BR.N no_probe

    ; Probe detected
    MOVI R17, 1
    MOVE R0, R17
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT

no_probe:
    XOR R0, R0
    MOVE R17, R0
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 MiniProbeDetector: single-cell bitmask correlator.

        Computes 31-point correlation against known ±1 probe pattern using
        a bitmask approach. The probe signs are encoded as two 16-bit masks.
        Each invocation: SHR mask to extract sign bit via carry flag, then
        ADD (probe=+1) or SUB (probe=-1) the input to a running accumulator.
        After 31 samples, outputs the correlation sum and resets.

        Data layout:
          mask_lo:    probe signs for indices 0-15 (bit i = 1 if probe[i]=+1)
          mask_hi:    probe signs for indices 16-30
          thirty_one: constant 31
          sixteen:    constant 16
          one:        constant 1

        State:
          accum:    running correlation sum
          counter:  sample index within 31-point window (0-30)
          mask_run: current mask word being shifted

        Input: sample (auto-allocated register, no R0 save needed)
        Output: correlation sum (every 31st invocation)
        """
        # Encode probe pattern as bitmasks: +1 -> bit=1, -1 -> bit=0
        mask_lo = 0
        for i in range(16):
            if self.PROBE_PATTERN[i] == 1:
                mask_lo |= (1 << i)
        mask_hi = 0
        for i in range(16, self.PROBE_LENGTH):
            if self.PROBE_PATTERN[i] == 1:
                mask_hi |= (1 << (i - 16))

        cell0 = CellProgram(
            inputs=[Port("sample")],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("mask_lo", mask_lo, address=1),
                DataWord("mask_hi", mask_hi, address=2),
                DataWord("thirty_one", 31, address=3),
                DataWord("sixteen", 16, address=4),
                DataWord("one", 1, address=5),
            ],
            state=[
                StateVar("accum"),
                StateVar("counter"),
                StateVar("mask_run", initial_value=mask_lo),
            ],
            assembly_template="""\
start:
    SHR R{state:mask_run}, #1
    MOVE R{state:mask_run}, R0
    BR.NC sub_path
    ADD R{state:accum}, R{in:sample}
    GOTO done_acc
sub_path:
    SUB R{state:accum}, R{in:sample}
done_acc:
    MOVE R{state:accum}, R0
    ADD R{state:counter}, R{data:one}
    MOVE R{state:counter}, R0
    CMP R{state:counter}, R{data:sixteen}
    BR.NZ not_sixteen
    MOVE R{state:mask_run}, R{data:mask_hi}
not_sixteen:
    CMP R{state:counter}, R{data:thirty_one}
    BR.N done
    MOVE R0, R{state:accum}
    {write:out}
    SUB R{state:counter}, R{state:counter}
    MOVE R{state:counter}, R0
    MOVE R{state:accum}, R0
    MOVE R{state:mask_run}, R{data:mask_lo}
    {jump:out}
done:
""",
        )

        return {0: cell0}

    def process_reference(self, input_symbols: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Reference implementation of mini-probe detection.

        Args:
            input_symbols: Input symbols

        Returns:
            Tuple of (correlation_values, detection_flags)
        """
        n_samples = len(input_symbols)
        correlation = np.zeros(n_samples, dtype=np.float32)
        detected = np.zeros(n_samples, dtype=np.int32)

        probe = np.array(self.PROBE_PATTERN, dtype=np.float32)

        for i in range(n_samples):
            self._buffer.append(float(input_symbols[i]))

            if len(self._buffer) >= self.PROBE_LENGTH:
                # Compute correlation
                window = np.array(self._buffer[-self.PROBE_LENGTH:])
                corr = np.sum(window * probe) / self.PROBE_LENGTH
                correlation[i] = corr

                if abs(corr) > self._threshold:
                    detected[i] = 1

        return correlation, detected

    def reset(self):
        """Reset detector state."""
        self._buffer = []
