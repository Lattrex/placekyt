"""CostasLoopBlock — see :class:`CostasLoopBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class CostasLoopBlock(KyttarBlock):
    """
    DEPRECATED — real-input Costas loop that CANNOT bootstrap a lock.

    A single REAL input carries no quadrature to drive the loop, so this block
    does not actually achieve carrier lock. It is kept only for reference / the
    legacy 3-way RTL test. For real carrier recovery use
    ``ComplexCostasLoopBlock`` (complex baseband, decision-directed, validated to
    lock 50/50 over multiple offsets) or ``QAM16ComplexCostasLoopBlock`` for
    16-QAM. Do NOT use this block in new chains.

    --- original description (for the legacy reference) ---

    Implements carrier frequency and phase recovery using a Costas loop with
    feedback (Ring pattern). This is essential for coherent demodulation of
    BPSK and QPSK signals.

    Architecture (7 cells in Ring pattern):
    ```
         ┌──────────────────────────────────────────────────────────┐
         │                                                          │
         ▼                                                          │
    [NCO] ──► [Mixer_I] ──► [LPF_I] ──► [Phase Det] ──► [Loop Filt]─┘
      │            │                         ▲
      │            │                         │
      └──► [Mixer_Q] ──────► [LPF_Q] ────────┘
    ```

    Cell Layout (linear for ease of routing):
        Cell 0: NCO - generates sin/cos carriers, receives feedback
        Cell 1: Mixer_I - input × cos
        Cell 2: Mixer_Q - input × sin
        Cell 3: LPF_I - lowpass filter I branch
        Cell 4: LPF_Q - lowpass filter Q branch
        Cell 5: Phase Detector - computes I×Q error (BPSK)
        Cell 6: Loop Filter - PI controller, sends correction to NCO

    Signal Flow:
        - Input sample arrives at Cell 1 (Mixer_I) and Cell 2 (Mixer_Q) simultaneously
        - NCO provides cos to Mixer_I, sin to Mixer_Q
        - Mixed outputs go through LPFs
        - Phase detector computes error = I_lpf × Q_lpf
        - Loop filter integrates error and sends correction back to NCO
        - Derotated I output (from LPF_I) goes to downstream

    Interface:
        - Entry: R1 (on landing cell = Mixer_I)
        - Input: R31 (signal sample)
        - Output: Derotated I sample
    """
    CATEGORY = "recovery"
    TAGS = ["costas", "pll", "carrier_recovery", "recovery"]

    # Landing cell is Mixer_I (cell 1)
    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # 64-entry equivalent via quarter-wave reconstruction (same as NCOBlock).
    # 17 quarter-wave entries (sin 0° to sin 90°) give exact 64-entry resolution:
    # 5.625° per step, ±2.8° max quantization error.
    TABLE_SIZE = 64
    QUARTER_SIZE = 17

    def __init__(
        self,
        name: str,
        freq_word: int = 0,
        loop_bw: float = 0.01,
        damping: float = 0.707,
        mode: str = "bpsk",
        lpf_alpha: float = 0.25,
    ):
        """
        Initialize Costas Loop block.

        Args:
            name: Block name
            freq_word: Initial NCO frequency word (0-65535)
            loop_bw: Loop bandwidth (normalized, 0.001-0.1 typical)
            damping: Loop damping factor (0.707 = critically damped)
            mode: "bpsk" or "qpsk" (currently only BPSK implemented)
            lpf_alpha: LPF smoothing factor (0-1, higher = faster response)
        """
        super().__init__(
            name,
            freq_word=freq_word,
            loop_bw=loop_bw,
            damping=damping,
            mode=mode,
            lpf_alpha=lpf_alpha,
        )
        self._freq_word = freq_word & 0xFFFF
        self._loop_bw = loop_bw
        self._damping = damping
        self._mode = mode
        self._lpf_alpha = lpf_alpha

        # Compute loop filter gains from bandwidth and damping
        # Using standard 2nd-order PLL design equations
        # omega_n = loop_bw * 2 * pi
        # Kp = 2 * damping * omega_n
        # Ki = omega_n^2 (for type-2 loop)
        omega_n = loop_bw * 2 * np.pi
        self._Kp = 2.0 * damping * omega_n
        self._Ki = omega_n * omega_n

        # Clamp to Q15 range
        self._Kp_q15 = float_to_q15(min(0.99, max(-0.99, self._Kp)))
        self._Ki_q15 = float_to_q15(min(0.99, max(-0.99, self._Ki)))
        self._lpf_alpha_q15 = float_to_q15(min(0.99, lpf_alpha))

        # Reference implementation state
        self._phase = 0.0
        self._integrator = 0.0
        self._lpf_i = 0.0
        self._lpf_q = 0.0

    @property
    def cell_count(self) -> int:
        # NCO: SinPhase(1) + SinTable(1) + CosPhase(1) + CosTable(1)
        # Mixer(1) + PhaseDet+LPF(1) + LoopFilter(1) = 7
        return 7

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def frequency_word(self) -> int:
        return self._freq_word

    @property
    def loop_bandwidth(self) -> float:
        return self._loop_bw

    def _generate_sine_table(self) -> List[int]:
        """Generate full 64-entry sine table in Q15 (reference implementation)."""
        table = []
        for i in range(self.TABLE_SIZE):
            phase = 2 * np.pi * i / self.TABLE_SIZE
            value = np.sin(phase)
            table.append(float_to_q15(value))
        return table

    def _generate_quarter_wave_table(self) -> List[int]:
        """Generate 17-entry quarter-wave sine table in Q15.

        Entries 0-16 = sin(0°) to sin(90°).
        Full 64-entry wave reconstructed via symmetry (same as NCOBlock):
          Q1 (idx  0-15): +table[idx]
          Q2 (idx 16-31): +table[16-local]  (mirror)
          Q3 (idx 32-47): -table[idx-32]    (negate)
          Q4 (idx 48-63): -table[16-local]  (mirror+negate)
        """
        table = []
        for i in range(self.QUARTER_SIZE):
            phase = (np.pi / 2) * i / 16  # 0 to pi/2
            value = np.sin(phase)
            table.append(float_to_q15(value))
        return table

    def _build_nco_cell(self) -> CellProgram:
        """Build NCO cell program (Cell 0)."""
        prog = CellProgram()

        # Memory layout (CRITICAL: program at R1-R15, data at R16+):
        # R0: Accumulator
        # R1-R15: Program code (max 15 instructions)
        # R16: Phase accumulator (state)
        # R17: Base frequency word
        # R18: Table base address (22)
        # R19: Cosine offset (2 for 8-entry table = 90°)
        # R20: Table mask (7)
        # R21: Phase correction input (from Loop Filter feedback)
        # R22-R29: Sine table (8 entries)
        # R30: Temp for sin index
        # R31: (reserved for potential input)

        phase_reg = 16
        freq_reg = 17
        base_reg = 18
        cos_offset_reg = 19
        mask_reg = 20
        correction_reg = 21
        table_base = 22  # Sine table at R22-R29 (safe from code overlap)
        sin_idx_temp = 30

        prog.set_memory(phase_reg, 0)
        prog.set_memory(freq_reg, self._freq_word)
        prog.set_memory(base_reg, table_base)
        prog.set_memory(cos_offset_reg, 2)  # 90° offset
        prog.set_memory(mask_reg, 7)
        prog.set_memory(correction_reg, 0)  # Initial correction = 0

        # Sine table at R22-R29
        table = self._generate_sine_table()
        for i, val in enumerate(table):
            prog.set_memory(table_base + i, val)

        # NCO program (COMPACT - 14 instructions):
        # 1. Update phase with frequency and correction
        # 2. Look up sin, send to Mixer_Q
        # 3. Look up cos, send to Mixer_I
        assembly = f"""; NCO Cell - Costas Loop (Compact)
; R{phase_reg}=phase, R{freq_reg}=freq, R{correction_reg}=correction
; Table at R{table_base}-R{table_base + self.TABLE_SIZE - 1}
start:
    ; phase += freq + correction
    ADD R{phase_reg}, R{freq_reg}
    ADD R0, R{correction_reg}
    MOVE R{phase_reg}, R0

    ; Clear correction (consumed)
    XOR R0, R0
    MOVE R{correction_reg}, R0

    ; sin_idx = (phase >> 13) & 7
    SHR R{phase_reg}, #13
    AND R0, R{mask_reg}
    MOVE R{sin_idx_temp}, R0

    ; sin = table[sin_idx]
    ADD R0, R{base_reg}
    LOAD R0
    WRITE @2, 30                        ; sin to Mixer_Q

    ; cos_idx = (sin_idx + 2) & 7
    ADD R{sin_idx_temp}, R{cos_offset_reg}
    AND R0, R{mask_reg}
    ADD R0, R{base_reg}
    LOAD R0
    WRITE @1, 30                        ; cos to Mixer_I

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_mixer_i_cell(self) -> CellProgram:
        """Build Mixer_I cell program (Cell 1 - Landing Cell)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R10: Program code
        # R28: Input sample (saved)
        # R29: Temp
        # R30: Cosine value (from NCO)
        # R31: Input sample (from external)

        input_save = 28

        # Mixer_I program:
        # 1. Receive input from R31, cos from R30
        # 2. Multiply: I_mixed = input × cos
        # 3. Forward input to Mixer_Q (for Q branch)
        # 4. Send I_mixed to LPF_I
        # 5. Trigger NCO to generate next carrier sample
        assembly = f"""; Mixer_I Cell - Costas Loop (Landing Cell)
; R31=input, R30=cos from NCO
start:
    ; Save input for forwarding
    MOVE R{input_save}, R31

    ; Multiply input by cos
    MULQ R31, R30                       ; R0 = input × cos

    ; Send I_mixed to LPF_I (2 hops, skips Mixer_Q)
    WRITE @2, 31

    ; Forward input sample to Mixer_Q (1 hop)
    MOVE R0, R{input_save}
    WRITE @1, 31

    ; Trigger Mixer_Q execution
    JUMP @1, 1

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_mixer_q_cell(self) -> CellProgram:
        """Build Mixer_Q cell program (Cell 2)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R8: Program code
        # R30: Sine value (from NCO)
        # R31: Input sample (forwarded from Mixer_I)

        # Mixer_Q program:
        # 1. Receive input from R31, sin from R30
        # 2. Multiply: Q_mixed = input × sin
        # 3. Send Q_mixed to LPF_Q (2 hops, skips LPF_I)
        assembly = """; Mixer_Q Cell - Costas Loop
; R31=input, R30=sin from NCO
start:
    ; Multiply input by sin
    MULQ R31, R30                       ; R0 = input × sin

    ; Send Q_mixed to LPF_Q (2 hops, skips LPF_I)
    WRITE @2, 31

    ; Trigger LPF_I to process I branch
    JUMP @1, 1

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_lpf_i_cell(self, output_hop: int, target_input: int, target_entry: int) -> CellProgram:
        """Build LPF_I cell program (Cell 3)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R14: Program code
        # R20: LPF state (y[n-1])
        # R21: Alpha coefficient
        # R22: One minus alpha
        # R28: Filtered I (saved for phase detector)
        # R31: I_mixed input

        state_reg = 20
        alpha_reg = 21
        one_minus_alpha_reg = 22
        filtered_save = 28

        one_minus_alpha = 1.0 - self._lpf_alpha
        prog.set_memory(state_reg, 0)
        prog.set_memory(alpha_reg, self._lpf_alpha_q15)
        prog.set_memory(one_minus_alpha_reg, float_to_q15(one_minus_alpha))

        # LPF_I program:
        # 1. Compute y[n] = alpha × x[n] + (1-alpha) × y[n-1]
        # 2. Send filtered I to Phase Detector (2 hops, skips LPF_Q)
        # 3. Send filtered I to downstream (output)
        assembly = f"""; LPF_I Cell - Costas Loop
; R31=I_mixed, R{state_reg}=state, R{alpha_reg}=alpha
start:
    ; y[n] = alpha * x[n] + (1-alpha) * y[n-1]
    MULQ R31, R{alpha_reg}              ; R0 = alpha × input
    MACQ R{state_reg}, R{one_minus_alpha_reg}  ; R0 += (1-alpha) × state
    MOVE R{state_reg}, R0               ; Update state
    MOVE R{filtered_save}, R0           ; Save filtered I

    ; Send to Phase Detector (2 hops, skips LPF_Q)
    WRITE @2, 30                        ; Phase det expects I in R30

    ; Send derotated I to downstream block
    MOVE R0, R{filtered_save}
    WRITE @{output_hop + 3}, {target_input}   ; +3 to skip LPF_Q, PhaseDet, LoopFilt
    JUMP @{output_hop + 3}, {target_entry}

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_lpf_q_cell(self) -> CellProgram:
        """Build LPF_Q cell program (Cell 4)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R12: Program code
        # R20: LPF state (y[n-1])
        # R21: Alpha coefficient
        # R22: One minus alpha
        # R31: Q_mixed input

        state_reg = 20
        alpha_reg = 21
        one_minus_alpha_reg = 22

        one_minus_alpha = 1.0 - self._lpf_alpha
        prog.set_memory(state_reg, 0)
        prog.set_memory(alpha_reg, self._lpf_alpha_q15)
        prog.set_memory(one_minus_alpha_reg, float_to_q15(one_minus_alpha))

        # LPF_Q program:
        # 1. Compute y[n] = alpha × x[n] + (1-alpha) × y[n-1]
        # 2. Send filtered Q to Phase Detector (1 hop)
        assembly = f"""; LPF_Q Cell - Costas Loop
; R31=Q_mixed, R{state_reg}=state, R{alpha_reg}=alpha
start:
    ; y[n] = alpha * x[n] + (1-alpha) * y[n-1]
    MULQ R31, R{alpha_reg}              ; R0 = alpha × input
    MACQ R{state_reg}, R{one_minus_alpha_reg}  ; R0 += (1-alpha) × state
    MOVE R{state_reg}, R0               ; Update state

    ; Send filtered Q to Phase Detector (1 hop)
    WRITE @1, 31                        ; Phase det expects Q in R31

    ; Trigger Phase Detector
    JUMP @1, 1

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_phase_detector_cell(self) -> CellProgram:
        """Build Phase Detector cell program (Cell 5)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R8: Program code
        # R30: Filtered I (from LPF_I)
        # R31: Filtered Q (from LPF_Q)

        # Phase detector program (BPSK Costas):
        # error = I × Q
        # When locked, Q ≈ 0, so error ≈ 0
        # Phase error causes Q ≠ 0, generating correction signal
        assembly = """; Phase Detector Cell - Costas Loop (BPSK)
; R30=I_filtered, R31=Q_filtered
; error = I × Q
start:
    ; Compute error = I × Q
    MULQ R30, R31                       ; R0 = I × Q

    ; Send error to Loop Filter (1 hop)
    WRITE @1, 31

    ; Trigger Loop Filter
    JUMP @1, 1

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_loop_filter_cell(self) -> CellProgram:
        """Build Loop Filter cell program (Cell 6)."""
        prog = CellProgram()

        # Memory layout:
        # R1-R14: Program code
        # R20: Integrator state
        # R21: Kp (proportional gain)
        # R22: Ki (integral gain)
        # R28: Temp for proportional term
        # R31: Error input (from Phase Detector)

        integrator_reg = 20
        kp_reg = 21
        ki_reg = 22
        prop_temp = 28

        prog.set_memory(integrator_reg, 0)
        prog.set_memory(kp_reg, self._Kp_q15)
        prog.set_memory(ki_reg, self._Ki_q15)

        # Loop filter program (PI controller):
        # 1. Proportional: p = Kp × error
        # 2. Integral: i = i + Ki × error
        # 3. Output: correction = p + i
        # 4. Send correction back to NCO (6 hops feedback)
        assembly = f"""; Loop Filter Cell - Costas Loop (PI Controller)
; R31=error, R{integrator_reg}=integrator, R{kp_reg}=Kp, R{ki_reg}=Ki
start:
    ; Proportional term: p = Kp × error
    MULQ R31, R{kp_reg}                 ; R0 = Kp × error
    MOVE R{prop_temp}, R0               ; Save proportional

    ; Integral term: integrator += Ki × error
    MULQ R31, R{ki_reg}                 ; R0 = Ki × error
    ADD R{integrator_reg}, R0           ; R0 = integrator + Ki × error
    MOVE R{integrator_reg}, R0          ; Update integrator

    ; Total correction: correction = proportional + integrator
    ADD R{prop_temp}, R{integrator_reg} ; R0 = p + i

    ; Send correction back to NCO (6 hops, wraps around)
    ; NCO expects correction in R28
    WRITE @6, 28

    ; Trigger NCO for next sample
    JUMP @6, 1

    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 Costas Loop: 7-cell ring with 64-entry quarter-wave NCO.

        Architecture: Two independent NCOs (sin and cos, 90° offset) generate
        quadrature carriers. A mixer cell computes I/Q, PhaseDet+LPF tracks
        phase error, LoopFilter sends correction to BOTH NCOs.

        Cells:
          0: SinPhase — phase accumulator + quarter-wave fold for sin
          1: SinTable — 17-entry quarter-wave LOAD + negate → sin value
          2: CosPhase — same accumulator + fold for cos (phase + 90°)
          3: CosTable — 17-entry quarter-wave LOAD + negate → cos value
          4: Mixer    — I = input×cos, Q = input×sin
          5: PhaseDet+LPF — IIR LPF on I/Q, error = I_filt × Q_filt
          6: LoopFilter — PI controller, sends correction to both NCOs

        Data flow:
          Input → SinPhase(fwd_input to Mixer) → SinTable(sin to Mixer)
          SinPhase(fwd_phase to CosPhase) → CosPhase → CosTable(cos to Mixer, triggers Mixer)
          Mixer(I,Q to PhaseDet) → PhaseDet(error to LoopFilter, I to output)
          LoopFilter(correction to SinPhase AND CosPhase)
        """
        import math

        quarter_table = self._generate_quarter_wave_table()

        lpf_alpha_q15 = self._lpf_alpha_q15
        one_minus_alpha_q15 = float_to_q15(1.0 - self._lpf_alpha)

        omega_n = self._loop_bw * 2 * math.pi
        Kp = 2.0 * self._damping * omega_n
        Ki = omega_n * omega_n
        kp_q15 = float_to_q15(min(0.99, max(-0.99, Kp)))
        ki_q15 = float_to_q15(min(0.99, max(-0.99, Ki)))

        # Phase offset for cosine = 16384 (90° in 16-bit phase space)
        phase_90 = 16384

        # --- Cell 0: SinPhase — phase accumulator + sin quarter-wave fold ---
        # Sends: sin_idx, sin_neg → SinTable; phase → CosPhase; input → Mixer
        # Receives: correction from LoopFilter (feedback WRITE to state)
        cell_sin_phase = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("fwd_sin_neg"), Port("fwd_sin_idx"),
                     Port("fwd_phase"), Port("fwd_input"),
                     Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("freq", self._freq_word, address=1),
                DataWord("thirty_two", 32, address=2),
                DataWord("fifteen", 15, address=3),
                DataWord("sixteen", 16, address=4),
            ],
            state=[
                StateVar("phase"),
                StateVar("correction"),
                StateVar("fidx"),
                StateVar("local"),
            ],
            assembly_template="""\
start:
    {write:fwd_input}
    ADD R{state:phase}, R{data:freq}
    ADD R0, R{state:correction}
    MOVE R{state:phase}, R0
    SHR R{state:phase}, #10
    MOVE R{state:fidx}, R0
    AND R{state:fidx}, R{data:thirty_two}
    {write:fwd_sin_neg}
    AND R{state:fidx}, R{data:fifteen}
    MOVE R{state:local}, R0
    AND R{state:fidx}, R{data:sixteen}
    BR.Z sin_nm
    SUB R{data:sixteen}, R{state:local}
    MOVE R{state:local}, R0
sin_nm:
    MOVE R0, R{state:local}
    {write:fwd_sin_idx}
    SUB R{state:correction}, R{state:correction}
    MOVE R{state:correction}, R0
    MOVE R0, R{state:phase}
    {write:fwd_phase}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 1: SinTable — quarter-wave LOAD + negate ---
        # Same as NCOBlock Cell 1
        table_data = [DataWord(f"qt{i}", val, address=i + 1)
                      for i, val in enumerate(quarter_table)]
        table_data.append(DataWord("tbase", 1, address=self.QUARTER_SIZE + 1))
        table_data.append(DataWord("zero", 0, address=self.QUARTER_SIZE + 2))

        cell_sin_table = CellProgram(
            inputs=[Port("idx"), Port("neg_flag")],
            outputs=[Port("fwd_sin"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=table_data,
            state=[],
            assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    CMP R{in:neg_flag}, R{data:zero}
    BR.Z positive
    SUB R{data:zero}, R0
positive:
    {write:fwd_sin}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 2: CosPhase — cos = sin(phase + 90°), fold ---
        # Receives phase from SinPhase, adds 90° offset, does quarter-wave fold
        cell_cos_phase = CellProgram(
            inputs=[Port("phase_in", register=0)],
            outputs=[Port("fwd_cos_neg"), Port("fwd_cos_idx"),
                     Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("phase_90", phase_90, address=1),
                DataWord("thirty_two", 32, address=2),
                DataWord("fifteen", 15, address=3),
                DataWord("sixteen", 16, address=4),
            ],
            state=[
                StateVar("correction"),
                StateVar("cos_phase"),
                StateVar("fidx"),
                StateVar("local"),
            ],
            assembly_template="""\
start:
    ADD R0, R{data:phase_90}
    ADD R0, R{state:correction}
    MOVE R{state:cos_phase}, R0
    SHR R{state:cos_phase}, #10
    MOVE R{state:fidx}, R0
    AND R{state:fidx}, R{data:thirty_two}
    {write:fwd_cos_neg}
    AND R{state:fidx}, R{data:fifteen}
    MOVE R{state:local}, R0
    AND R{state:fidx}, R{data:sixteen}
    BR.Z cos_nm
    SUB R{data:sixteen}, R{state:local}
    MOVE R{state:local}, R0
cos_nm:
    MOVE R0, R{state:local}
    {write:fwd_cos_idx}
    SUB R{state:correction}, R{state:correction}
    MOVE R{state:correction}, R0
    {jump:fwd_trigger}
""",
        )

        # --- Cell 3: CosTable — quarter-wave LOAD + negate ---
        # Same structure as SinTable, different output name
        cell_cos_table = CellProgram(
            inputs=[Port("idx"), Port("neg_flag")],
            outputs=[Port("fwd_cos"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=list(table_data),  # same quarter-wave table
            state=[],
            assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    CMP R{in:neg_flag}, R{data:zero}
    BR.Z positive
    SUB R{data:zero}, R0
positive:
    {write:fwd_cos}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 4: Mixer — I = input×cos, Q = input×sin ---
        # Explicit register assignments for predictable cross-cell WRITE targets.
        # input_val saved to state before MULQ (which clobbers R0).
        # Dummy data word at addr 0 ensures state/inputs don't land at R0 (accumulator).
        cell_mixer = CellProgram(
            inputs=[Port("input_val", register=2),
                    Port("sin_val", register=3),
                    Port("cos_val", register=4)],
            outputs=[Port("fwd_i"), Port("fwd_q"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[DataWord("reserved", 0, address=0)],  # keeps R0 free for ALU
            state=[StateVar("in_save")],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:input_val}
    MULQ R{state:in_save}, R{in:cos_val}
    {write:fwd_i}
    MULQ R{state:in_save}, R{in:sin_val}
    {write:fwd_q}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 5: Phase Detector + LPF (unchanged from before) ---
        cell_phasedet = CellProgram(
            inputs=[],  # I and Q pre-staged by Mixer
            outputs=[Port("to_relay"), Port("to_loopfilter")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("alpha", lpf_alpha_q15, address=1),
                DataWord("one_m_alpha", one_minus_alpha_q15, address=2),
                DataWord("face_east", 1, address=3),
                DataWord("face_south", 0, address=4),
            ],
            state=[
                StateVar("i_in"),
                StateVar("q_in"),
                StateVar("i_filt"),
                StateVar("q_filt"),
            ],
            assembly_template="""\
start:
    MULQ R{data:alpha}, R{state:i_in}
    MACQ R{state:i_filt}, R{data:one_m_alpha}
    MOVE R{state:i_filt}, R0
    MOVE [FACE], R{data:face_east}
    {write:to_relay}
    {jump:to_relay}
    MULQ R{data:alpha}, R{state:q_in}
    MACQ R{state:q_filt}, R{data:one_m_alpha}
    MOVE R{state:q_filt}, R0
    MULQ R{state:i_filt}, R{state:q_filt}
    MOVE [FACE], R{data:face_south}
    {write:to_loopfilter}
    {jump:to_loopfilter}
""",
        )

        # --- Cell 6: Loop Filter (unchanged — PI controller) ---
        cell_loopfilter = CellProgram(
            inputs=[Port("error", register=0)],
            outputs=[Port("to_sin_nco"), Port("to_cos_nco")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("k1", kp_q15, address=1),
                DataWord("k2", ki_q15, address=2),
            ],
            state=[
                StateVar("err_save"),
                StateVar("prop"),
                StateVar("prod_lo"),
                StateVar("prod_hi"),
                StateVar("int_lo"),
                StateVar("int_hi"),
                StateVar("temp"),
            ],
            assembly_template="""\
start:
    MOVE R{state:err_save}, R0
    MULQ R{data:k1}, R{state:err_save}
    MOVE R{state:prop}, R0
    MUL R{data:k2}, R{state:err_save}
    MOVE R{state:prod_lo}, R0
    MULHI R{data:k2}, R{state:err_save}
    MOVE R{state:prod_hi}, R0
    ADD R{state:int_lo}, R{state:prod_lo}
    MOVE R{state:int_lo}, R0
    ADC R{state:int_hi}, R{state:prod_hi}
    MOVE R{state:int_hi}, R0
    SHR R{state:int_lo}, #15
    MOVE R{state:temp}, R0
    SHL R{state:int_hi}, #1
    ADD R0, R{state:temp}
    ADD R{state:prop}, R0
    {write:to_sin_nco}
    {write:to_cos_nco}
""",
        )

        return {
            0: cell_sin_phase,
            1: cell_sin_table,
            2: cell_cos_phase,
            3: cell_cos_table,
            4: cell_mixer,
            5: cell_phasedet,
            6: cell_loopfilter,
        }

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation of Costas loop carrier recovery.

        Args:
            input_samples: Complex input samples (I component only for real input)

        Returns:
            Derotated I samples
        """
        n = len(input_samples)
        output = np.zeros(n, dtype=np.float32)

        quarter = self._generate_quarter_wave_table()

        def qw_lookup(phase_16bit):
            """Quarter-wave sine lookup matching hardware exactly."""
            full_idx = (phase_16bit >> 10) & 0x3F
            negate = (full_idx & 32) != 0
            mirror = (full_idx & 16) != 0
            local = full_idx & 15
            if mirror:
                local = 16 - local
            val = quarter[local]
            if negate:
                val = (-val) & 0xFFFF
            return q15_to_float(val)

        for i in range(n):
            sample = float(input_samples[i])

            # NCO: generate sin/cos using quarter-wave reconstruction
            sin_val = qw_lookup(int(self._phase) & 0xFFFF)
            cos_val = qw_lookup((int(self._phase) + 16384) & 0xFFFF)

            # Mixers
            i_mixed = sample * cos_val
            q_mixed = sample * sin_val

            # LPFs
            self._lpf_i = self._lpf_alpha * i_mixed + (1.0 - self._lpf_alpha) * self._lpf_i
            self._lpf_q = self._lpf_alpha * q_mixed + (1.0 - self._lpf_alpha) * self._lpf_q

            # Phase detector (BPSK)
            error = self._lpf_i * self._lpf_q

            # Loop filter (PI)
            proportional = self._Kp * error
            self._integrator += self._Ki * error
            correction = proportional + self._integrator

            # Limit integrator (anti-windup)
            self._integrator = max(-0.5, min(0.5, self._integrator))

            # Update phase
            self._phase = (self._phase + self._freq_word + correction * 32768) % 65536

            # Output derotated I
            output[i] = self._lpf_i

        return output

    def reset(self):
        """Reset Costas loop state."""
        self._phase = 0.0
        self._integrator = 0.0
        self._lpf_i = 0.0
        self._lpf_q = 0.0
