"""AGCBlock — see :class:`AGCBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class AGCBlock(KyttarBlock):
    """
    Automatic Gain Control block.

    Uses feedback control to maintain a target output level:
        output = input * gain
        if |output| > target: gain -= rate
        else: gain += rate

    The gain is updated each sample to maintain the target level.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["agc", "gain", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(
        self,
        name: str,
        target: float = 0.7,
        attack_rate: float = 0.05,
        decay_rate: float = 0.001,
        initial_gain: float = 0.5,
        min_gain: float = 0.01,
        max_gain: float = 0.99,
    ):
        """
        Initialize AGC block.

        Args:
            name: Block name
            target: Target output magnitude (0.0 to 1.0)
            attack_rate: Fast gain reduction rate (for signal peaks)
            decay_rate: Slow gain increase rate (for fades)
            initial_gain: Initial gain value
            min_gain: Minimum gain clamp
            max_gain: Maximum gain clamp
        """
        super().__init__(name, target=target, attack_rate=attack_rate,
                         decay_rate=decay_rate, initial_gain=initial_gain,
                         min_gain=min_gain, max_gain=max_gain)
        self._target = target
        self._attack_rate = attack_rate
        self._decay_rate = decay_rate
        self._initial_gain = initial_gain
        self._min_gain = min_gain
        self._max_gain = max_gain
        self._current_gain = initial_gain

        # Convert to Q15
        self._target_q15 = float_to_q15(target)
        self._attack_rate_q15 = float_to_q15(attack_rate)
        self._decay_rate_q15 = float_to_q15(decay_rate)
        self._gain_q15 = float_to_q15(initial_gain)
        self._min_gain_q15 = float_to_q15(min_gain)
        self._max_gain_q15 = float_to_q15(max_gain)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def target(self) -> float:
        return self._target

    @property
    def attack_rate(self) -> float:
        return self._attack_rate

    @property
    def decay_rate(self) -> float:
        return self._decay_rate

    @property
    def gain(self) -> float:
        return self._current_gain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production AGC: proper |output| comparison, asymmetric attack/decay, gain clamping.

        Algorithm:
          output = input × gain
          |output| = abs(output) via conditional negate
          if |output| >= target: gain -= attack_rate (fast, prevent clipping)
          else: gain += decay_rate (slow, recover during fades)
          gain = clamp(gain, min_gain, max_gain)
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("target", self._target_q15, address=2),
                DataWord("attack_rate", self._attack_rate_q15, address=3),
                DataWord("decay_rate", self._decay_rate_q15, address=4),
                DataWord("min_gain", self._min_gain_q15, address=5),
                DataWord("max_gain", self._max_gain_q15, address=6),
            ],
            state=[
                StateVar("gain", initial_value=self._gain_q15),
                StateVar("out_save"),
            ],
            assembly_template="""\
start:
    MULQ R{in:sample}, R{state:gain}
    MOVE R{state:out_save}, R0
    CMP R0, R{data:zero}
    BR.NN have_abs
    SUB R{data:zero}, R{state:out_save}
have_abs:
    CMP R0, R{data:target}
    BR.NC do_attack
    ADD R{state:gain}, R{data:decay_rate}
    MOVE R{state:gain}, R0
    GOTO clamp
do_attack:
    SUB R{state:gain}, R{data:attack_rate}
    MOVE R{state:gain}, R0
clamp:
    CMP R{state:gain}, R{data:min_gain}
    BR.NN clamp_hi
    MOVE R{state:gain}, R{data:min_gain}
clamp_hi:
    CMP R{state:gain}, R{data:max_gain}
    BR.N output
    MOVE R{state:gain}, R{data:max_gain}
output:
    MOVE R0, R{state:out_save}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation with proper |output|, asymmetric rates, clamping."""
        output = np.zeros(len(input_samples), dtype=np.float32)

        for i, sample in enumerate(input_samples):
            out = float(sample) * self._current_gain
            output[i] = out

            if abs(out) >= self._target:
                self._current_gain -= self._attack_rate  # fast attack
            else:
                self._current_gain += self._decay_rate   # slow decay

            self._current_gain = max(self._min_gain, min(self._max_gain, self._current_gain))

        return output

    def reset(self):
        """Reset gain to initial value."""
        self._current_gain = self._initial_gain
