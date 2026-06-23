"""DFEEqualizerBlock — see :class:`DFEEqualizerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, Optional, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class DFEEqualizerBlock(KyttarBlock):
    """
    Decision Feedback Equalizer (DFE) Block (40 cells per channel).

    Implements a full DFE equalizer with 20 forward (feedforward) taps and
    20 feedback taps, as required for MIL-STD-188-110B HF modem compliance.
    Uses RLS (Recursive Least Squares) algorithm for fast channel tracking.

    Architecture: Dual Wavefront (40 cells)
    ========================================

    The DFE consists of two sections:
    1. Forward Filter: 20-tap FIR on received samples
    2. Feedback Filter: 20-tap FIR on past decisions (ISI cancellation)

    Cell Layout:
    ```
                        Forward Section (20 cells)
        In → [FF0] → [FF1] → ... → [FF19] → (+) → decision → Out
                                              ↑
                        Feedback Section (20 cells)
              [FB0] ← [FB1] ← ... ← [FB19] ←─┘
    ```

    Components:
    - FF0-FF19 (20 cells): Forward feedforward taps
    - FB0-FB19 (20 cells): Feedback (decision feedback) taps

    Total: 40 cells per channel (I or Q). Full complex DFE needs 80 cells
    but we process I and Q separately with shared decisions.

    Memory Layout Per Forward Tap Cell (32 words):
    - R0: Accumulator
    - R1-R12: Program code
    - R16: Tap coefficient (Q15)
    - R17: Delayed sample (state)
    - R18: P matrix diagonal element (RLS)
    - R19: Forgetting factor lambda (Q15, ~0.99)
    - R20: Partial sum input
    - R21: Current sample input
    - R22: Error signal
    - R23-R28: Working registers
    - R31: Input port

    RLS Algorithm (per tap):
        k = P * x / (lambda + x^T * P * x)
        w = w + k * e
        P = (P - k * x^T * P) / lambda

    Simplified for Kyttar (diagonal P approximation):
        k_diag = P_diag * x / (lambda + P_diag * x^2)
        w = w + k_diag * e
        P_diag = (P_diag * (1 - k_diag * x)) / lambda

    Interface:
        - Entry: R1
        - Input: R31 (I or Q sample)
        - Output: Equalized sample + hard decision

    MIL-STD-188-110B Compliance:
        - 20 forward taps at T/2 spacing (pre/post cursor ISI)
        - 20 feedback taps for decision-directed ISI cancellation
        - RLS for fast HF channel tracking (<100ms convergence)
        - Training on 287-symbol preamble, then decision-directed
    """
    CATEGORY = "equalization"
    TAGS = ["dfe", "equalizer", "equalization"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # Default parameters per MIL-STD-188-110B
    DEFAULT_FORWARD_TAPS = 20
    DEFAULT_FEEDBACK_TAPS = 20
    DEFAULT_LAMBDA = 0.99  # RLS forgetting factor

    def __init__(
        self,
        name: str,
        forward_taps: int = 21,
        feedback_taps: int = 21,
        step_size: float = 0.01,
        forgetting_factor: float = 0.99,
    ):
        """
        Initialize DFE Equalizer block.

        Args:
            name: Block name
            forward_taps: Number of forward (feedforward) taps (default 21)
            feedback_taps: Number of feedback taps (default 21)
            step_size: LMS adaptation step size mu (default 0.01)
            forgetting_factor: RLS forgetting factor lambda (default 0.99, for v1 only)
        """
        super().__init__(
            name,
            forward_taps=forward_taps,
            feedback_taps=feedback_taps,
            step_size=step_size,
            forgetting_factor=forgetting_factor,
        )
        self._forward_taps = forward_taps
        self._feedback_taps = feedback_taps
        self._step_size = step_size
        self._step_size_q15 = float_to_q15(step_size)
        self._lambda = forgetting_factor

        # Q15 parameters
        self._lambda_q15 = float_to_q15(forgetting_factor)

        # Initialize forward coefficients (center tap = 1, others = 0)
        self._forward_coeffs = [0.0] * forward_taps
        center = forward_taps // 2
        self._forward_coeffs[center] = 1.0

        # Initialize feedback coefficients (all zero)
        self._feedback_coeffs = [0.0] * feedback_taps

        # RLS P matrix diagonal (initialize to large value)
        self._P_diag_forward = [100.0] * forward_taps
        self._P_diag_feedback = [100.0] * feedback_taps

        # State for reference processing
        self._forward_delay = [0.0] * forward_taps
        self._feedback_delay = [0.0] * feedback_taps
        self._last_decision = 0.0

    @property
    def cell_count(self) -> int:
        # FF taps + FB taps + decision cell + SPLIT/output router + lock-driver
        # relay into the decision cell.
        return self._forward_taps + self._feedback_taps + 3

    @property
    def forward_taps(self) -> int:
        return self._forward_taps

    @property
    def feedback_taps(self) -> int:
        return self._feedback_taps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_forward_tap_program(
        self,
        tap_idx: int,
        is_last: bool,
        output_hop: int,
        target_interface: BlockInterface
    ) -> CellProgram:
        """Build a forward filter tap cell program."""
        prog = CellProgram()

        # Initialize coefficient
        center = self._forward_taps // 2
        if tap_idx == center:
            coeff_q15 = float_to_q15(1.0)
        else:
            coeff_q15 = float_to_q15(0.0)

        # Initialize P diagonal (large initial value for fast adaptation)
        p_init_q15 = float_to_q15(0.5)  # Scaled for Q15

        prog.set_memory(16, coeff_q15)          # Forward coefficient
        prog.set_memory(17, 0)                  # Delayed sample
        prog.set_memory(18, p_init_q15)         # P diagonal
        prog.set_memory(19, self._lambda_q15)   # Forgetting factor

        target_input = target_interface.input_registers[0]
        target_entry = target_interface.entry_address

        if is_last:
            # Last forward tap: sum with feedback, make decision, output
            assembly = f"""; DFE Forward Tap {tap_idx} (LAST - sums with feedback)
; R16: coeff, R17: delay, R18: P_diag, R19: lambda
; R20: partial_sum, R21: sample_in, R22: error, R23: fb_sum

start:
    ; y_partial = partial_sum + w[k] * x[n-k]
    MULQ R16, R17       ; R0 = w[k] * x[n-k]
    ADD R0, R20         ; R0 = forward sum
    MOVE R24, R0        ; R24 = forward output

    ; Add feedback sum (R23 received from feedback chain)
    ADD R24, R23        ; R0 = total output = forward + feedback
    MOVE R25, R0        ; R25 = equalizer output y

    ; Update delay
    MOVE R17, R21

    ; Hard decision (BPSK: sign)
    ; decision = +1 if y >= 0, else -1
    CMP R25, 0
    BR.N neg_decision
    MOVI R26, 0x7FFF    ; +1.0 in Q15
    GOTO done_decision
neg_decision:
    MOVI R26, 0x8001    ; -1.0 in Q15
done_decision:

    ; Error = decision - y
    SUB R26, R25
    MOVE R22, R0        ; R22 = error

    ; RLS coefficient update (simplified diagonal P)
    ; k = P * x / (lambda + P * x^2)
    ; w = w + k * e
    ; P = P * (1 - k * x) / lambda
    MULQ R18, R17       ; R0 = P * x
    MOVE R27, R0        ; R27 = Px
    MULQ R17, R17       ; R0 = x^2
    MULQ R18, R0        ; R0 = P * x^2
    ADD R19, R0         ; R0 = lambda + P*x^2
    MOVE R28, R0        ; R28 = denom

    ; k = Px / denom (approximate division by shift)
    SHR R27, 4          ; Scale Px
    ; w += k * e
    MULQ R27, R22
    ADD R16, R0
    MOVE R16, R0

    ; Output equalized sample
    MOVE R0, R25
    WRITE @{output_hop}, {target_input}

    ; Also send decision to feedback chain (start at FB0)
    ; Feedback cells are adjacent after forward cells
    MOVE R0, R26
    WRITE @1, 21        ; FB0.R21 = decision

    JUMP @{output_hop}, {target_entry}
    HALT
"""
        else:
            # Non-last forward tap: accumulate and forward
            assembly = f"""; DFE Forward Tap {tap_idx}
; R16: coeff, R17: delay, R18: P_diag, R19: lambda
; R20: partial_sum, R21: sample_in, R22: error

start:
    ; y_partial = partial_sum + w[k] * x[n-k]
    MULQ R16, R17       ; R0 = w[k] * x[n-k]
    ADD R0, R20         ; R0 = new partial sum
    MOVE R24, R0        ; Save partial sum

    ; Shift delay line
    MOVE R23, R17       ; R23 = old delay (to pass along)
    MOVE R17, R21       ; delay = sample_in

    ; RLS coefficient update
    MULQ R18, R23       ; R0 = P * x_old
    MOVE R27, R0
    SHR R27, 4
    MULQ R27, R22       ; R0 = k * e
    ADD R16, R0
    MOVE R16, R0

    ; Forward partial_sum and sample to next tap
    MOVE R0, R24
    WRITE @1, 20        ; next.R20 = partial_sum
    MOVE R0, R21
    WRITE @1, 21        ; next.R21 = sample
    MOVE R0, R22
    WRITE @1, 22        ; next.R22 = error
    JUMP @1, 1
    HALT
"""

        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def _build_feedback_tap_program(
        self,
        tap_idx: int,
        is_last: bool,
        output_hop: int,
        target_interface: BlockInterface
    ) -> CellProgram:
        """Build a feedback filter tap cell program."""
        prog = CellProgram()

        # Feedback coefficients start at zero
        prog.set_memory(16, 0)                  # Feedback coefficient
        prog.set_memory(17, 0)                  # Delayed decision
        prog.set_memory(18, float_to_q15(0.5))  # P diagonal
        prog.set_memory(19, self._lambda_q15)   # Forgetting factor

        if is_last:
            # Last feedback tap: return partial sum to forward section
            assembly = f"""; DFE Feedback Tap {tap_idx} (LAST)
; R16: coeff, R17: delay, R20: partial_sum, R21: decision_in

start:
    ; fb_partial = partial_sum + w[k] * d[n-k]
    MULQ R16, R17       ; R0 = w[k] * d[n-k]
    ADD R0, R20         ; R0 = feedback sum
    MOVE R24, R0

    ; Update delay
    MOVE R17, R21       ; delay = new decision

    ; Send feedback sum back to last forward tap
    ; (This would need routing back - simplified for now)
    ; The forward section reads R23 for feedback sum
    ; In practice, this routes back through the array

    HALT
"""
        else:
            # Non-last feedback tap
            assembly = f"""; DFE Feedback Tap {tap_idx}
; R16: coeff, R17: delay, R20: partial_sum, R21: decision_in

start:
    ; fb_partial = partial_sum + w[k] * d[n-k]
    MULQ R16, R17       ; R0 = w[k] * d[n-k]
    ADD R0, R20         ; R0 = new partial sum
    MOVE R24, R0

    ; Shift delay line
    MOVE R23, R17       ; old delay
    MOVE R17, R21       ; delay = decision_in

    ; Forward to next feedback tap
    MOVE R0, R24
    WRITE @1, 20        ; next.R20 = partial_sum
    MOVE R0, R21
    WRITE @1, 21        ; next.R21 = decision
    JUMP @1, 1
    HALT
"""

        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)

        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production DFE Equalizer: 55-cell serpentine layout.

        21 forward taps + 1 decision cell + 21 feedback taps + 1 split cell
        + 3 relays + 8 transit cells = 55 cells across 7 rows.

        Layout (from project_dfe_layout.md):
          Row 0: 8 transit cells (0,0)-(7,0) EAST, OUT at (9,0)
          FF path: serpentine from FF0(7,1) through FF20(9,3)
          DC at (8,2): sums FF+FB, decides, computes error
          FB path: spiral from FB0(7,2) through FB20(8,3)
          SPLIT at (8,1): routes output EAST, error WEST to FF0

        Each FF/FB tap: MACQ for partial sum, LMS coefficient update.
        Three values propagate through each chain: sample/decision, partial_sum, error.
        Pre-staging pattern: sample and error pre-staged (WRITE without JUMP),
        partial_sum arrives with WRITE+JUMP to trigger execution.
        """
        mu_q15 = self._step_size_q15
        n_ff = self._forward_taps
        n_fb = self._feedback_taps

        # --- Cell placement map ---
        # FF positions (ordered by data flow)
        ff_pos = [
            (7,1),(6,1),(5,1),(4,1),(3,1),(2,1),  # FF0-FF5: row 1 west
            (2,2),(2,3),(2,4),(2,5),(2,6),          # FF6-FF10: col 2 south
            (3,6),(4,6),(5,6),(6,6),(7,6),          # FF11-FF15: row 6 east
            (7,5),(8,5),(9,5),                      # FF16-FF18: east+north
            (9,4),(9,3),                            # FF19-FF20: col 9 north
        ]

        fb_pos = [
            (7,2),(7,3),(6,3),(6,2),                # FB0-FB3
            (5,2),(4,2),(3,2),                      # FB4-FB6
            (3,3),(3,4),(3,5),                      # FB7-FB9
            (4,5),(4,4),(4,3),                      # FB10-FB12
            (5,3),(5,4),(5,5),                      # FB13-FB15
            (6,5),(6,4),(7,4),                      # FB16-FB18
            (8,4),(8,3),                            # FB19-FB20
        ]

        # Initialize forward coefficients: center tap = 1.0, others = 0
        center = n_ff // 2
        ff_init = [0] * n_ff
        ff_init[center] = float_to_q15(1.0)

        programs = {}

        # --- Generic FF tap cell ---
        # Each receives: sample (pre-staged), error (pre-staged), partial_sum (WRITE+JUMP)
        # Computes: partial_sum += coeff * delay (MACQ)
        # LMS: coeff += mu * error * delay
        # Forwards: sample, error, new partial_sum to next tap
        for i in range(n_ff):
            programs[f'ff{i}'] = CellProgram(
                inputs=[Port("sample_in"), Port("error_in"), Port("partial_in")],
                outputs=[Port("fwd_sample"), Port("fwd_error"), Port("fwd_partial")],
                entries=[EntryPoint("default")],
                data=[DataWord("mu", mu_q15, address=1)],
                state=[
                    StateVar("coeff", initial_value=ff_init[i]),
                    StateVar("delay"),
                    StateVar("partial_save"),
                ],
                # Tapped delay line: each cell forwards its OLD delay value to the
                # next cell, so FF[k] sees sample[t-k]. Computation order:
                # 1. Compute partial_sum += coeff * delay (delay = sample[t-k-1])
                # 2. LMS update: coeff += mu * error * delay
                # 3. Forward OLD delay (sample[t-k-1]) to next cell as its sample_in
                # 4. Update delay := sample_in (sample[t-k])
                # This gives the next cell sample[t-k-1] -> stored as its new delay.
                assembly_template="""\
start:
    MOVE R{state:partial_save}, R{in:partial_in}
    MULQ R{state:coeff}, R{state:delay}
    ADD R{state:partial_save}, R0
    MOVE R{state:partial_save}, R0
    MULQ R{data:mu}, R{in:error_in}
    MULQ R0, R{state:delay}
    ADD R{state:coeff}, R0
    MOVE R{state:coeff}, R0
    MOVE R0, R{state:delay}
    {write:fwd_sample}
    MOVE R{state:delay}, R{in:sample_in}
    MOVE R0, R{in:error_in}
    {write:fwd_error}
    MOVE R0, R{state:partial_save}
    {write:fwd_partial}
    {jump:fwd_partial}
""",
            )

        # --- All FB taps (FB0-FB20) ---
        # FB0 receives decision_in and error_in from DC. Its partial_in register
        # is never written by DC, so it stays at initial value 0 — correct for
        # the first tap (partial sum starts at 0).
        for i in range(n_fb - 1):
            programs[f'fb{i}'] = CellProgram(
                inputs=[Port("decision_in"), Port("error_in"), Port("partial_in")],
                outputs=[Port("fwd_decision"), Port("fwd_error"), Port("fwd_partial")],
                entries=[EntryPoint("default")],
                data=[DataWord("mu", mu_q15, address=1)],
                state=[
                    StateVar("coeff"),
                    StateVar("delay"),
                    StateVar("partial_save"),
                ],
                # Tapped delay line: FB[k] sees decision[t-k].
                # Same pattern as FF: forward OLD delay before updating.
                assembly_template="""\
start:
    MOVE R{state:partial_save}, R{in:partial_in}
    MULQ R{state:coeff}, R{state:delay}
    ADD R{state:partial_save}, R0
    MOVE R{state:partial_save}, R0
    MULQ R{data:mu}, R{in:error_in}
    MULQ R0, R{state:delay}
    ADD R{state:coeff}, R0
    MOVE R{state:coeff}, R0
    MOVE R0, R{state:delay}
    {write:fwd_decision}
    MOVE R{state:delay}, R{in:decision_in}
    MOVE R0, R{in:error_in}
    {write:fwd_error}
    MOVE R0, R{state:partial_save}
    {write:fwd_partial}
    {jump:fwd_partial}
""",
            )

        # --- Last FB tap (FB20): pre-stages partial sum to DC, no JUMP ---
        last_fb = n_fb - 1
        programs[f'fb{last_fb}'] = CellProgram(
            inputs=[Port("decision_in"), Port("error_in"), Port("partial_in")],
            outputs=[Port("fwd_partial")],
            entries=[EntryPoint("default")],
            data=[DataWord("mu", mu_q15, address=1)],
            state=[
                StateVar("coeff"),
                StateVar("delay"),
                StateVar("partial_save"),
            ],
            assembly_template="""\
start:
    MOVE R{state:partial_save}, R{in:partial_in}
    MULQ R{state:coeff}, R{state:delay}
    ADD R{state:partial_save}, R0
    MOVE R{state:partial_save}, R0
    MULQ R{data:mu}, R{in:error_in}
    MULQ R0, R{state:delay}
    ADD R{state:coeff}, R0
    MOVE R{state:coeff}, R0
    MOVE R{state:delay}, R{in:decision_in}
    MOVE R0, R{state:partial_save}
    {write:fwd_partial}
""",
        )

        # --- Decision Cell (DC) ---
        # Receives: FF partial sum (east, via relay at 9,2), FB partial sum (south, pre-staged from 8,3)
        # Sums both, makes BPSK hard decision, computes error.
        # Dynamic FACE switching (3 switches):
        #   1. WEST: send decision immediately in branch (avoids saving to state var)
        #   2. NORTH: send output+error to routing cell (8,1) which handles output+FF0 error
        #   3. WEST: send error to FB0 + JUMP to trigger FB chain
        # FB0 initializes its own partial_sum to 0 (no partial sent from DC).
        # --- DC-A: Decision Cell at (8,2) ---
        # Receives: ff_sum at R0 (from relay WRITE+JUMP), fb_sum pre-staged from FB20
        # Computes output = ff+fb, error = decision - output, decision = sign(output)
        # Dynamic FACE: WEST first (send decision+error to FB0), then NORTH (send output+error to DC-B)
        # DC: Decision Cell at (8,2). Receives ff_sum at R0 (east, via relay),
        # fb_sum pre-staged from south (FB20). Computes output, decision, error.
        # Dynamic FACE: WEST to send decision+error to FB0, NORTH to send
        # output+error to DC-B router.
        programs['dc'] = CellProgram(
            inputs=[Port("fb_sum")],  # ff_sum arrives at R0
            outputs=[Port("fwd_fb_decision"), Port("fwd_fb_error"),
                     Port("fwd_output"), Port("fwd_error")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("pos_one", 0x7FFF, address=1),
                DataWord("neg_one", 0x8001, address=2),
                DataWord("face_north", 3, address=3),
                DataWord("face_west", 2, address=4),
            ],
            state=[
                StateVar("output_save"),
                StateVar("error_save"),
            ],
            assembly_template="""\
start:
    ADD R0, R{in:fb_sum}
    MOVE R{state:output_save}, R0
    BR.N neg
    SUB R{data:pos_one}, R{state:output_save}
    MOVE R{state:error_save}, R0
    MOVE R0, R{data:pos_one}
    GOTO send
neg:
    SUB R{data:neg_one}, R{state:output_save}
    MOVE R{state:error_save}, R0
    MOVE R0, R{data:neg_one}
send:
    MOVE [FACE], R{data:face_west}
    {write:fwd_fb_decision}
    MOVE R0, R{state:error_save}
    {write:fwd_fb_error}
    {jump:fwd_fb_error}
    MOVE [FACE], R{data:face_north}
    MOVE R0, R{state:output_save}
    {write:fwd_output}
    MOVE R0, R{state:error_save}
    {write:fwd_error}
    {jump:fwd_error}
    SUB R{state:error_save}, R{state:error_save}
    MOVE [LOCK], R0
""",
        )

        # --- DC-B: Output Router at (8,1) ---
        # Receives output + error from DC-A (south face).
        # Dynamic FACE: EAST sends output to relay(9,1)→OUT(9,0),
        #               WEST sends error to FF0(7,1) for next-cycle LMS update.
        # DC-B: Output Router at (8,1). Receives output + error from DC (south).
        # Dynamic FACE: EAST sends output to relay(9,1)→OUT(9,0),
        #               WEST sends error to FF0(7,1) for next-cycle LMS update.
        programs['dc_b'] = CellProgram(
            inputs=[Port("output_in"), Port("error_in")],
            outputs=[Port("to_relay"), Port("to_ff0_error")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("face_east", 1, address=1),
                DataWord("face_west", 2, address=2),
            ],
            state=[],
            assembly_template="""\
start:
    MOVE [FACE], R{data:face_east}
    MOVE R0, R{in:output_in}
    {write:to_relay}
    MOVE [FACE], R{data:face_west}
    MOVE R0, R{in:error_in}
    {write:to_ff0_error}
""",
        )

        # --- Lock driver / FF20 relay at (9,2) ---
        # FF20's partial sum arrives at R0. This cell relays it into the decision
        # cell (DC) AND sets DC's arbiter LOCK first, so DC's two inputs (this FF
        # sum on the east face, the FB sum pre-staged from the south) don't race.
        # DC's own program clears the lock at the end (MOVE [LOCK], R0), so DC is
        # DESIGNED to be locked by this cell before it runs. The LOCK is written
        # to DC's CONFIG via literal WRITE.CFG @1 (the relay always abuts DC);
        # the data forward + JUMP to DC use {write:to_dc}/{jump:to_dc}, resolved
        # via internal_connections (lock_drv -> dc), NOT the positional default.
        #   CONFIG[3] = LOCK_FACE (1 = east, the face FF data arrives on)
        #   CONFIG[4] = LOCK     (1 = enable)
        programs['lock_drv'] = CellProgram(
            inputs=[Port("ff_partial")],   # FF20's partial arrives at R0
            outputs=[Port("to_dc")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=1)],
            state=[StateVar("save")],
            assembly_template="""\
start:
    MOVE R{state:save}, R0
    MOVE R0, R{data:one}
    WRITE.CFG @1, 3
    WRITE.CFG @1, 4
    MOVE R0, R{state:save}
    {write:to_dc}
    {jump:to_dc}
""",
        )

        # Store positions for resolution
        self._ff_positions = ff_pos
        self._fb_positions = fb_pos
        self._dc_position = (8, 2)
        self._split_position = (8, 1)

        # Reorder so the lock-driver sits right after FF20 (its source), keeping
        # dc_b LAST as the block's exit cell. The placement system uses
        # cells[-1] as the exit (which the output port routes from), so dc_b —
        # the SPLIT/output router — must remain last; otherwise the output
        # routing would grab the lock-driver and mis-face it. Ordering must
        # match default_layout exactly (same positional sequence).
        last_ff = n_ff - 1
        ordered: Dict[Any, CellProgram] = {}
        for i in range(n_ff):
            ordered[f'ff{i}'] = programs[f'ff{i}']
        ordered['lock_drv'] = programs['lock_drv']
        for i in range(n_fb):
            ordered[f'fb{i}'] = programs[f'fb{i}']
        ordered['dc'] = programs['dc']
        ordered['dc_b'] = programs['dc_b']
        return ordered

    def internal_connections(self):
        """Non-default INTERNAL handoffs (router routes these explicitly instead
        of its positional 'next cell in dict order' default):

          * FF20's ``fwd_partial`` goes to the lock driver (not fb0, which is the
            dict-order neighbour), landing at the lock driver's ``ff_partial``.
          * the lock driver's ``to_dc`` goes to the decision cell ``dc``. DC
            reads the FF sum at R0 (its program's first line is
            ``ADD R0, R{in:fb_sum}``), so the relay writes to DC's R0 — the
            dst_input_port is the empty string, which the router resolves to
            address 0. DC's default entry is the JUMP target.

        Returns ``(src_cell_id, src_output_port, dst_cell_id, dst_input_port)``;
        an empty ``dst_input_port`` means "write to the target's R0".
        """
        last_ff = self._forward_taps - 1
        return [
            (f'ff{last_ff}', 'fwd_partial', 'lock_drv', 'ff_partial'),
            ('lock_drv', 'to_dc', 'dc', ''),  # '' -> DC's R0
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Hand-tuned serpentine layout for the DFE equalizer.

        Positions are the exact authored coordinates from
        ``build_cell_programs`` (see project_dfe_layout.md):
          - FF taps ff0..ffN snake through rows 1-6 (``ff_pos``)
          - FB taps fb0..fbN spiral through rows 2-5 (``fb_pos``)
          - ``dc`` decision cell at (8, 2)
          - ``dc_b`` output-router/SPLIT cell at (8, 1)

        Cell ids match exactly those returned by ``build_cell_programs``
        (``ff{i}``, ``fb{i}``, ``dc``, ``dc_b``). Routing-only transit/relay
        cells and the OUT pad mentioned in the layout docstring carry no
        program and are not included here.

        Faces point toward the NEXT cell in each cell's data chain and are
        derived from consecutive positions, so positions are exact while faces
        are an inferred best-effort (some cells, e.g. dc/dc_b, switch FACE
        dynamically at runtime; the static face here is the dominant/first
        output direction).
        """
        # FF and FB authored positions (same ordering as build_cell_programs).
        ff_pos = [
            (7, 1), (6, 1), (5, 1), (4, 1), (3, 1), (2, 1),
            (2, 2), (2, 3), (2, 4), (2, 5), (2, 6),
            (3, 6), (4, 6), (5, 6), (6, 6), (7, 6),
            (7, 5), (8, 5), (9, 5),
            (9, 4), (9, 3),
        ]
        fb_pos = [
            (7, 2), (7, 3), (6, 3), (6, 2),
            (5, 2), (4, 2), (3, 2),
            (3, 3), (3, 4), (3, 5),
            (4, 5), (4, 4), (4, 3),
            (5, 3), (5, 4), (5, 5),
            (6, 5), (6, 4), (7, 4),
            (8, 4), (8, 3),
        ]
        n_ff = self._forward_taps
        n_fb = self._feedback_taps
        ff_pos = ff_pos[:n_ff]
        fb_pos = fb_pos[:n_fb]
        dc_pos = (8, 2)
        dc_b_pos = (8, 1)

        def face_to(src: Tuple[int, int], dst: Tuple[int, int]) -> str:
            """Infer a cardinal output face pointing from src toward dst.

            +x = east, +y = south (screen coordinates, matching the layout
            comments "row 1 west" = decreasing x, "col 2 south" = increasing y).
            """
            ddx = dst[0] - src[0]
            ddy = dst[1] - src[1]
            if abs(ddx) >= abs(ddy):
                return 'east' if ddx > 0 else 'west'
            return 'south' if ddy > 0 else 'north'

        # EXPLICIT output face per cell, per the verified data flow — NOT the
        # naive src->next-in-chain inference, which produced wrong arrows at the
        # serpentine bends and at the FF-chain end (ff20 turns NORTH to the relay,
        # not WEST toward DC). Order matches build_cell_programs.
        ff_faces = [
            'west', 'west', 'west', 'west', 'west', 'south',
            'south', 'south', 'south', 'south', 'east',
            'east', 'east', 'east', 'east', 'north',
            'east', 'east', 'north', 'north', 'north',  # ff20 -> relay(9,2) NORTH
        ]
        fb_faces = [
            'south', 'west', 'north', 'west', 'west', 'west', 'south',
            'south', 'south', 'east', 'north', 'north', 'east',
            'south', 'south', 'east', 'north', 'east', 'east',
            'north', 'north',  # fb20 -> DC(8,2) NORTH
        ]

        # Order MUST match build_cell_programs exactly (positional):
        # ff0..ff20, lock_drv, fb0..fb20, dc, dc_b. dc_b stays LAST (the block's
        # exit cell, which the output port routes from).
        layout: Dict[Any, Tuple[int, int, str]] = {}
        for i, pos in enumerate(ff_pos):
            layout[f'ff{i}'] = (pos[0], pos[1], ff_faces[i])
        # Lock-driver relay at (9,2): FF20(9,3) outputs NORTH into it; it sets
        # DC's lock then forwards WEST into DC's east face. PROGRAMMED cell (it
        # sets the lock), not a routing-only transit — key matches its
        # build_cell_programs key 'lock_drv'.
        layout['lock_drv'] = (9, 2, 'west')
        for i, pos in enumerate(fb_pos):
            layout[f'fb{i}'] = (pos[0], pos[1], fb_faces[i])
        # DC: dominant NORTH to the SPLIT router (switches to WEST for fb0 at
        # runtime). SPLIT/dc_b: dominant EAST — drives the block output directly
        # (no output relay; that was sim scaffolding).
        layout['dc'] = (dc_pos[0], dc_pos[1], 'north')
        layout['dc_b'] = (dc_b_pos[0], dc_b_pos[1], 'east')

        return layout

    def process_reference(self, input_samples: np.ndarray, training_symbols: Optional[np.ndarray] = None) -> np.ndarray:
        """
        Reference implementation of DFE equalizer with RLS adaptation.

        Args:
            input_samples: Input samples (real-valued for one channel)
            training_symbols: Known training symbols for supervised adaptation.
                             If None, uses decision-directed mode.

        Returns:
            Equalized output samples
        """
        n_samples = len(input_samples)
        output = np.zeros(n_samples, dtype=np.float32)

        for i in range(n_samples):
            sample = float(input_samples[i])

            # Shift forward delay line
            self._forward_delay = [sample] + self._forward_delay[:-1]

            # Compute forward filter output
            y_forward = 0.0
            for k in range(self._forward_taps):
                y_forward += self._forward_coeffs[k] * self._forward_delay[k]

            # Compute feedback filter output (uses past decisions)
            y_feedback = 0.0
            for k in range(self._feedback_taps):
                y_feedback += self._feedback_coeffs[k] * self._feedback_delay[k]

            # Total equalizer output
            y = y_forward - y_feedback  # Subtract feedback (ISI cancellation)
            output[i] = y

            # Make decision
            if training_symbols is not None and i < len(training_symbols):
                decision = float(training_symbols[i])
            else:
                # BPSK hard decision
                decision = 1.0 if y >= 0 else -1.0

            # Compute error
            error = decision - y

            # Shift feedback delay line (with new decision)
            self._feedback_delay = [decision] + self._feedback_delay[:-1]

            # RLS adaptation (simplified diagonal P approximation)
            for k in range(self._forward_taps):
                x = self._forward_delay[k]
                p = self._P_diag_forward[k]

                # k_gain = p * x / (lambda + p * x^2)
                denom = self._lambda + p * x * x
                if abs(denom) > 1e-10:
                    k_gain = p * x / denom
                else:
                    k_gain = 0.0

                # Update coefficient
                self._forward_coeffs[k] += k_gain * error

                # Update P diagonal
                self._P_diag_forward[k] = (p - k_gain * x * p) / self._lambda
                # Bound P to prevent overflow
                self._P_diag_forward[k] = min(max(self._P_diag_forward[k], 0.01), 1000.0)

            # Update feedback coefficients
            for k in range(self._feedback_taps):
                x = self._feedback_delay[k]
                p = self._P_diag_feedback[k]

                denom = self._lambda + p * x * x
                if abs(denom) > 1e-10:
                    k_gain = p * x / denom
                else:
                    k_gain = 0.0

                # Feedback coefficients (note: negative because we subtract)
                self._feedback_coeffs[k] += k_gain * error

                self._P_diag_feedback[k] = (p - k_gain * x * p) / self._lambda
                self._P_diag_feedback[k] = min(max(self._P_diag_feedback[k], 0.01), 1000.0)

        return output

    def reset(self):
        """Reset DFE equalizer state and coefficients."""
        center = self._forward_taps // 2
        self._forward_coeffs = [0.0] * self._forward_taps
        self._forward_coeffs[center] = 1.0
        self._feedback_coeffs = [0.0] * self._feedback_taps

        self._P_diag_forward = [100.0] * self._forward_taps
        self._P_diag_feedback = [100.0] * self._feedback_taps

        self._forward_delay = [0.0] * self._forward_taps
        self._feedback_delay = [0.0] * self._feedback_taps
        self._last_decision = 0.0
