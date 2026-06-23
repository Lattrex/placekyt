"""LMSEqualizerBlock — see :class:`LMSEqualizerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Optional
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class LMSEqualizerBlock(KyttarBlock):
    """
    LMS Adaptive Equalizer Block (8 cells).

    Implements an 8-tap LMS (Least Mean Squares) adaptive equalizer for
    HF channel equalization. This is required for MIL-STD-188-110B compliance.

    Architecture: Wavefront FIR with Tap Update (8 cells)
    =====================================================

    Each cell handles one tap of the equalizer:
    - Stores coefficient and one delay sample
    - Receives current sample and error from previous cell
    - Computes partial sum contribution
    - Updates coefficient using LMS algorithm
    - Forwards to next cell

    Cell Layout (linear wavefront):
    ```
        [TAP0] → [TAP1] → [TAP2] → ... → [TAP7] → Out
          ↑                                    │
          └────────── ERROR feedback ──────────┘
    ```

    Components:
    - TAP0-7 (8 cells): FIR tap with LMS coefficient update

    Total: 8 cells

    Memory Layout Per Tap Cell (32 words):
    - R0: Accumulator
    - R1-R10: Program code (~10 instructions)
    - R16: Tap coefficient (Q15)
    - R17: Delayed sample (state)
    - R18: Error signal (from feedback)
    - R19: Step size (mu, Q15)
    - R20: Partial sum input
    - R21: Current sample input
    - R22-R28: Working registers
    - R31: Input port

    LMS Algorithm (per tap):
        y_partial += w[k] * x[n-k]
        w[k] = w[k] + mu * e[n] * x[n-k]

    Interface:
        - Entry: R1
        - Input: R31 (complex sample or I/Q alternating)
        - Output: Equalized samples

    Training Mode:
        During preamble/training sequence, error is computed from known symbols.
        During data mode, decision-directed mode uses slicer output for error.
    """
    CATEGORY = "equalization"
    TAGS = ["lms", "equalizer", "equalization"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    DEFAULT_STEP_SIZE = 0.01  # LMS step size (mu)
    NUM_TAPS = 8

    def __init__(
        self,
        name: str,
        num_taps: int = 8,
        step_size: float = 0.01,
    ):
        """
        Initialize LMS Equalizer block.

        Args:
            name: Block name
            num_taps: Number of equalizer taps (default 8)
            step_size: LMS adaptation step size mu (default 0.01)
        """
        super().__init__(
            name,
            num_taps=num_taps,
            step_size=step_size,
        )
        self._num_taps = num_taps
        self._step_size = step_size

        # Q15 step size
        self._step_size_q15 = float_to_q15(step_size)

        # Initialize coefficients (center tap = 1, others = 0)
        self._coefficients = [0.0] * num_taps
        center = num_taps // 2
        self._coefficients[center] = 1.0

        # Delay line for reference processing
        self._delay_line = [0.0] * num_taps

    @property
    def cell_count(self) -> int:
        return self._num_taps

    @property
    def num_taps(self) -> int:
        return self._num_taps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_tap_program(
        self,
        tap_idx: int,
        is_last: bool,
        output_hop: int,
        target_interface: BlockInterface
    ) -> CellProgram:
        """
        Build a single tap cell program for LMS equalizer.

        Args:
            tap_idx: Index of this tap (0 = first)
            is_last: True if this is the last tap
            output_hop: Hop count to output target
            target_interface: Interface of output target
        """
        prog = CellProgram()

        # Initialize coefficient: center tap = 1.0, others = 0
        center = self._num_taps // 2
        if tap_idx == center:
            coeff_q15 = float_to_q15(1.0)
        else:
            coeff_q15 = float_to_q15(0.0)

        prog.set_memory(16, coeff_q15)    # Coefficient
        prog.set_memory(17, 0)             # Delayed sample
        prog.set_memory(18, 0)             # Error signal
        prog.set_memory(19, self._step_size_q15)  # Step size mu

        target_input = target_interface.input_registers[0]
        target_entry = target_interface.entry_address

        if is_last:
            # Last tap: output result and provide error feedback
            assembly = f"""; LMS Equalizer Tap {tap_idx} (LAST)
; R16: coeff, R17: delay, R18: error, R19: mu
; R20: partial_sum_in, R21: sample_in

start:
    ; y_partial = partial_sum_in + w[k] * x[n-k]
    MULQ R16, R17       ; R0 = w[k] * x[n-k]
    ADD R0, R20         ; R0 = partial_sum + product
    MOVE R22, R0        ; R22 = output y

    ; Update delay: delay = sample_in
    MOVE R17, R21

    ; Coefficient update: w[k] += mu * e * x[n-k]
    ; (Decision-directed: error computed externally)
    ; w_new = w + mu * error * delay_old
    MULQ R19, R18       ; R0 = mu * error
    MULQ R0, R17        ; R0 = mu * error * x[n-k]
    ADD R16, R0         ; R0 = w + update
    MOVE R16, R0        ; Update coefficient

    ; Output equalized sample
    MOVE R0, R22
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT
"""
        else:
            # Non-last tap: forward partial sum and sample to next tap
            assembly = f"""; LMS Equalizer Tap {tap_idx}
; R16: coeff, R17: delay, R18: error, R19: mu
; R20: partial_sum_in, R21: sample_in

start:
    ; y_partial = partial_sum_in + w[k] * x[n-k]
    MULQ R16, R17       ; R0 = w[k] * x[n-k]
    ADD R0, R20         ; R0 = partial_sum + product
    MOVE R22, R0        ; R22 = new partial sum

    ; Update delay: delay = sample_in (shift)
    MOVE R23, R17       ; R23 = old delay (to forward)
    MOVE R17, R21       ; delay = sample_in

    ; Coefficient update: w[k] += mu * e * x[n-k]
    MULQ R19, R18       ; R0 = mu * error
    MULQ R0, R23        ; R0 = mu * error * x_old (use old delay)
    ADD R16, R0
    MOVE R16, R0

    ; Forward: partial_sum to next.R20, sample to next.R21
    MOVE R0, R22
    WRITE @1, 20        ; next.R20 = partial_sum
    MOVE R0, R21
    WRITE @1, 21        ; next.R21 = sample
    ; Forward error to all taps
    MOVE R0, R18
    WRITE @1, 18        ; next.R18 = error
    JUMP @1, 1
    HALT
"""

        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 LMS Equalizer: 1-cell 2-tap decision-directed.

        Single cell implements the full LMS loop:
          y[n] = w0 * x[n] + w1 * x[n-1]        (FIR via MULQ + MACQ)
          d[n] = sign(y[n]) * 0x7FFF              (BPSK hard decision)
          e[n] = d[n] - y[n]                      (error)
          w0  += mu * e[n] * x[n]                 (tap 0 update)
          w1  += mu * e[n] * x[n-1]               (tap 1 update)
          x[n-1] = x[n]                           (shift delay)
          output y[n]

        Initialization: w0 = 0x7FFF (+1 Q15), w1 = 0 (center-tap).
        Step size mu is configurable (default 0.01 → 0x0148 Q15).

        Uses MACQ for FIR accumulation (R0 = R0 + src_a*src_b >> 15).
        """
        mu_q15 = self._step_size_q15

        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("pos_one", 0x7FFF, address=2),
                DataWord("neg_one", 0x8001, address=3),
                DataWord("mu", mu_q15, address=4),
            ],
            state=[
                StateVar("x_curr"),
                StateVar("x_prev"),
                StateVar("w0", initial_value=0x7FFF),  # +1.0 Q15
                StateVar("w1", initial_value=0),        # 0.0
                StateVar("mu_e"),                        # mu * error (temp)
            ],
            assembly_template="""\
start:
    MOVE R{state:x_curr}, R0
    MULQ R{state:w0}, R{state:x_curr}
    MACQ R{state:w1}, R{state:x_prev}
    {write:out}
    CMP R0, R{data:zero}
    BR.N neg
    SUB R{data:pos_one}, R0
    GOTO update
neg:
    SUB R{data:neg_one}, R0
update:
    MULQ R{data:mu}, R0
    MOVE R{state:mu_e}, R0
    MULQ R{state:mu_e}, R{state:x_curr}
    ADD R{state:w0}, R0
    MOVE R{state:w0}, R0
    MULQ R{state:mu_e}, R{state:x_prev}
    ADD R{state:w1}, R0
    MOVE R{state:w1}, R0
    MOVE R{state:x_prev}, R{state:x_curr}
    {jump:out}
""",
        )

        return {0: cell0}

    def process_reference(self, input_samples: np.ndarray, training_symbols: Optional[np.ndarray] = None) -> np.ndarray:
        """
        Reference implementation of LMS adaptive equalizer.

        Args:
            input_samples: Input samples (real or complex)
            training_symbols: Known training symbols for error computation.
                             If None, uses decision-directed mode.

        Returns:
            Equalized output samples
        """
        n_samples = len(input_samples)
        output = np.zeros(n_samples, dtype=np.float32)

        for i in range(n_samples):
            sample = float(input_samples[i])

            # Shift delay line
            self._delay_line = [sample] + self._delay_line[:-1]

            # Compute FIR output
            y = 0.0
            for k in range(self._num_taps):
                y += self._coefficients[k] * self._delay_line[k]

            output[i] = y

            # Compute error
            if training_symbols is not None and i < len(training_symbols):
                # Training mode: use known symbols
                error = training_symbols[i] - y
            else:
                # Decision-directed: slicer output
                # For BPSK: hard decision
                decision = 1.0 if y >= 0 else -1.0
                error = decision - y

            # LMS coefficient update
            for k in range(self._num_taps):
                self._coefficients[k] += self._step_size * error * self._delay_line[k]

        return output

    def reset(self):
        """Reset equalizer state and coefficients to initial values."""
        self._delay_line = [0.0] * self._num_taps
        self._coefficients = [0.0] * self._num_taps
        center = self._num_taps // 2
        self._coefficients[center] = 1.0
