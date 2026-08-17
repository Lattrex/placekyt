# SPDX-License-Identifier: GPL-3.0-or-later
"""DelayBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class DelayBlock(KyttarBlock):
    """
    Integer-sample delay line — drop-in for GNU Radio ``blocks.delay``: a pure
    integer delay ``y[n] = x[n-delay]`` (prepend ``delay`` zeros, then the input
    stream). No filtering, no interpolation — the samples are passed through
    unchanged, only shifted later in time by exactly ``delay`` samples.

    Datapath (single cell): a ``delay``-deep shift register held in state
    registers ``d0..d(delay-1)``, oldest at ``d0`` (= ``x[n-delay]``), newest at
    ``d(delay-1)`` (= ``x[n-1]``), ALL initialised to 0 (Q15 zero) — which is
    exactly the ``delay`` prepended zeros GR emits before the first real sample
    reaches the output. On each trigger the oldest register is emitted, then the
    line shifts one place and the new input is pushed in at the tail::

        osave = d0                 ; save the oldest (= x[n-delay])
        d0 <- d1, d1 <- d2, ...    ; shift the line down
        d(delay-1) <- x[n]         ; push the new sample at the tail
        emit osave                 ; y[n] = x[n-delay]

    Pure data movement (MOVE only, NO Q15 arithmetic), so the output is BIT-EXACT
    to the input samples — the block is EXACT (0 LSB vs GR). Group delay is the
    ``delay`` param itself: the DUT emits one word per trigger, so its stream is
    ``[0]*delay + x[:N-delay]`` — identical to GR ``blocks.delay(delay)``'s first
    ``N`` outputs (GR additionally flushes its line for ``delay`` more samples,
    which have no per-sample-trigger counterpart and are trimmed in compare).

    Params mirror GR VERBATIM: ``delay`` (GRC ``delay``; the integer sample delay,
    default 1). ``delay=0`` is the identity (pass-through, no delay line).

    Hardware deviations from ``blocks.delay``:
      * The delay depth is bounded by the cell's register/RAM budget: the whole
        delay line + the shift program must fit ONE 32-word cell (~31 usable
        registers, R0 is the accumulator). ``delay`` samples need ``delay`` state
        words + 1 save word + the program (~``delay``+5 instrs), so the depth is
        capped at ``MAX_DELAY`` (:data:`MAX_DELAY`, derived from the 32-word cell
        budget). A larger ``delay`` RAISES ``ValueError`` (never silently
        clamps/truncates the delay — that would compute a different function).
        GR's ``blocks.delay`` is unbounded (host RAM); a fabric FIFO / multi-cell
        delay line would lift this, and is deferred (Tier-2).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["delay", "delay_line", "shift_register", "signal_conditioning"]

    # Depth ceiling: the shift register + its program must fit one 32-word cell.
    # A ``delay``-deep line costs ``delay`` state words (d0..d(delay-1)) pinned to
    # registers 1..delay + 1 save word (osave, register delay+1), while the shift
    # program (the delay+1 MOVEs + emit MOVE + WRITE + JUMP + HALT) packs at the
    # TOP of the 32-word cell (R31 reserved for HALT). Empirically the two meet at
    # delay=13: osave would need register 14 but the instructions have grown down
    # to base_addr=13, so osave overlaps an instruction and the block builds but
    # produces NO egress. delay=12 is the deepest that both fits AND emits, so the
    # register/RAM-depth ceiling is 12. Verified by build+sim across delay 1..13
    # (1..12 emit the correct delayed stream; 13 builds but emits None; 14 raises).
    MAX_DELAY = 12

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, delay: int = 1):
        if int(delay) < 0:
            raise ValueError(f"delay must be >= 0, got {delay}")
        if int(delay) > self.MAX_DELAY:
            # HARDWARE DEVIATION: the delay line must fit one cell's register/RAM
            # budget. RAISE (never clamp) — a truncated delay is a different block.
            raise ValueError(
                f"delay={delay} exceeds the single-cell delay-line budget "
                f"(MAX_DELAY={self.MAX_DELAY}); a deeper delay needs a fabric "
                f"FIFO / multi-cell line (not yet supported). GR blocks.delay is "
                f"unbounded; this is a HARDWARE limit of the 32-word cell.")
        super().__init__(name, delay=delay)
        self._delay = int(delay)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def delay(self) -> int:
        return self._delay

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        D = self._delay

        # delay == 0: identity pass-through (no delay line, no state).
        if D == 0:
            return {0: CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=[],
                state=[],
                assembly_template="""\
start:
    {write:out}
    {jump:out}
    HALT
""",
            )}

        # delay >= 1: a D-deep shift register, oldest at d0, all init 0.
        #
        # Register-allocation note (the echo trap): the input port `sample` is
        # pinned to register 0 (R0, the accumulator/landing reg). State registers
        # auto-allocate from the low end of the free gap, which (with no data
        # words) STARTS at register 0 — so an auto-allocated d0 would land ON the
        # input register and every trigger would emit the raw input un-delayed.
        # Pin the delay line + save reg to registers 1..D+1 so they never overlap
        # the input reg 0 (mirrors how KeepOneInN's data words push state off 0).
        state = [StateVar(f"d{i}", register=i + 1, initial_value=0)
                 for i in range(D)]
        state.append(StateVar("osave", register=D + 1, initial_value=0))

        lines = ["start:"]
        # Save the oldest sample (= x[n-delay]) before it is overwritten.
        lines.append("    MOVE R{state:osave}, R{state:d0}")
        # Shift the line down: d0<-d1, d1<-d2, ..., d(D-2)<-d(D-1).
        for i in range(D - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        # Push the new input at the tail.
        lines.append(f"    MOVE R{{state:d{D-1}}}, R{{in:sample}}")
        # Emit the saved oldest sample as this trigger's output.
        lines.append("    MOVE R0, R{state:osave}")
        lines.append("    {write:out}")
        lines.append("    {jump:out}")
        lines.append("    HALT")

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            state=state,
            assembly_template="\n".join(lines) + "\n",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """The exact per-trigger DUT stream: ``[0]*delay + x[:N-delay]``.

        One output word per input trigger (as ``run_block_dut`` drives it): the
        first ``delay`` outputs are the prepended zeros, then the input shifted
        by ``delay``. Bit-exact (pure data movement)."""
        x = [int(w) & 0xFFFF for w in x_q15]
        n = len(x)
        return ([0] * self._delay + x)[:n]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: prepend ``delay`` zeros, keep the first N samples
        (one output per input trigger). Equals GR ``blocks.delay``'s first N."""
        x = np.asarray(input_samples, dtype=np.float32)
        n = len(x)
        out = np.concatenate([np.zeros(self._delay, dtype=np.float32), x])
        return out[:n]
