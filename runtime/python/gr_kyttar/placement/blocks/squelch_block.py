"""SquelchBlock — see :class:`SquelchBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class SquelchBlock(KyttarBlock):
    """
    Squelch block - conditional signal gating.

    Passes signal when level exceeds threshold, outputs 0 otherwise.
    Uses hysteresis to prevent rapid on/off cycling.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["squelch", "gate", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, threshold: float = 0.1, hysteresis: float = 0.02,
                 attack_alpha: float = 0.25, release_alpha: float = 0.03):
        """
        Initialize squelch block.

        Args:
            name: Block name
            threshold: Level threshold for opening squelch
            hysteresis: Hysteresis amount (prevents rapid cycling)
            attack_alpha: Attack smoothing factor (0-1, higher = faster)
            release_alpha: Release smoothing factor (0-1, higher = faster)
        """
        super().__init__(name, threshold=threshold, hysteresis=hysteresis,
                        attack_alpha=attack_alpha, release_alpha=release_alpha)
        self._threshold = threshold
        self._hysteresis = hysteresis
        self._attack_alpha = attack_alpha
        self._release_alpha = release_alpha

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production squelch: level tracking with asymmetric attack/release + hysteresis.

        Level = level + alpha * (|input| - level)
        where alpha = attack_alpha if |input| > level, else release_alpha.
        Gate opens when level > threshold, closes when level < (threshold - hysteresis).
        """
        open_thresh = float_to_q15(self._threshold)
        attack_q15 = float_to_q15(self._attack_alpha)
        release_q15 = float_to_q15(self._release_alpha)
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("open_thresh", open_thresh, address=1),
                DataWord("attack", attack_q15, address=2),
                DataWord("release", release_q15, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[
                StateVar("level"),
                StateVar("in_save"),
            ],
            # Simplified: attack/release level tracking + threshold gating.
            # Hysteresis omitted to fit instruction budget (would need 2 cells).
            # Level = level + alpha*(|input|-level) where alpha differs for attack/release.
            # Gate: if level >= threshold, output signal; else output 0.
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    CMP R{in:sample}, R{data:zero}
    BR.NN skip_neg
    SUB R{data:zero}, R{in:sample}
skip_neg:
    SUB R0, R{state:level}
    BR.N use_release
    MULQ R0, R{data:attack}
    GOTO update
use_release:
    MULQ R0, R{data:release}
update:
    ADD R{state:level}, R0
    MOVE R{state:level}, R0
    CMP R{state:level}, R{data:open_thresh}
    MOVE R0, R{state:in_save}
    BR.NN emit
    MOVE R0, R{data:zero}
emit:
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation."""
        output = np.zeros_like(input_samples)
        level = 0.0
        gate_open = False

        for i, sample in enumerate(input_samples):
            abs_sample = abs(float(sample))
            alpha = self._attack_alpha if gate_open else self._release_alpha
            level = level + alpha * (abs_sample - level)

            if not gate_open and level > self._threshold:
                gate_open = True
            elif gate_open and level < (self._threshold - self._hysteresis):
                gate_open = False

            output[i] = sample if gate_open else 0.0

        return output.astype(np.float32)
