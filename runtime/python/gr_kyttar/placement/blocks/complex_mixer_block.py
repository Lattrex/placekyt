"""ComplexMixerBlock — see :class:`ComplexMixerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ComplexMixerBlock(KyttarBlock):
    """
    Complex Mixer / Frequency Shifter block.

    Multiplies input by a complex exponential (sine/cosine) for
    frequency translation. In single-channel mode, outputs the
    real part (cosine multiplication):

        output = input * cos(2*pi*freq*n/sample_rate)

    Uses the same 64-entry quarter-wave NCO as the standalone NCO block.
    Computes cos(phase) = sin(phase + 90°) by adding 16384 (90° in 16-bit
    phase space) before the quarter-wave lookup.

    3-cell design: Phase logic + quarter-wave table + mixer (MULQ).

    Interface (defaults):
    - Entry: R1
    - Input: R31 (signal to mix)
    """
    CATEGORY = "recovery"
    TAGS = ["mixer", "frequency_shift", "recovery"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    TABLE_SIZE = 64
    QUARTER_SIZE = 17

    def __init__(self, name: str, freq_word: int = 655, sample_rate: float = 32000.0):
        """
        Initialize Complex Mixer block.

        Args:
            name: Block name
            freq_word: Phase increment per sample (0-65535)
            sample_rate: Sample rate in Hz
        """
        super().__init__(name, freq_word=freq_word, sample_rate=sample_rate)
        self._freq_word = freq_word & 0xFFFF
        self._sample_rate = sample_rate
        self._phase = 0

    @property
    def cell_count(self) -> int:
        return 3  # Phase logic + quarter-wave table + mixer

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def frequency(self) -> float:
        """Mixing frequency in Hz."""
        return (self._freq_word / 65536.0) * self._sample_rate

    def _generate_sine_table(self) -> List[int]:
        """Generate full 64-entry sine table in Q15 (for reference)."""
        table = []
        for i in range(self.TABLE_SIZE):
            phase = 2 * np.pi * i / self.TABLE_SIZE
            value = np.sin(phase)
            table.append(float_to_q15(value))
        return table

    def _generate_quarter_wave_table(self) -> List[int]:
        """Generate 17-entry quarter-wave sine table in Q15."""
        table = []
        for i in range(self.QUARTER_SIZE):
            phase = (np.pi / 2) * i / 16
            value = np.sin(phase)
            table.append(float_to_q15(value))
        return table

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production complex mixer: output = input * cos(phase).

        3-cell design with 64-entry quarter-wave NCO:
        Cell 0: Phase logic — saves input, computes cos(phase) = sin(phase+90°),
                folds into quarter-wave, pre-stages to Cells 1 and 2.
        Cell 1: Quarter-wave table + reconstruction — LOADs, negates, pre-stages
                cosine value to Cell 2, triggers Cell 2.
        Cell 2: Mixer — receives input (from Cell 0) and cosine (from Cell 1),
                computes MULQ, outputs.
        """
        quarter_table = self._generate_quarter_wave_table()
        phase_90 = 16384  # 90° in 16-bit phase space

        # --- Cell 0: Phase Logic ---
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("fwd_idx"), Port("fwd_neg"), Port("fwd_input"),
                     Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("freq", self._freq_word, address=1),
                DataWord("phase_90", phase_90, address=2),
                DataWord("thirty_two", 32, address=3),
                DataWord("fifteen", 15, address=4),
                DataWord("sixteen", 16, address=5),
                DataWord("zero", 0, address=6),
            ],
            state=[
                StateVar("phase"),
                StateVar("full_idx"),
                StateVar("local_save"),
                StateVar("in_save"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R0
    ADD R{state:phase}, R{data:freq}
    MOVE R{state:phase}, R0
    ADD R0, R{data:phase_90}
    SHR R0, #10
    MOVE R{state:full_idx}, R0
    AND R{state:full_idx}, R{data:thirty_two}
    {write:fwd_neg}
    AND R{state:full_idx}, R{data:fifteen}
    MOVE R{state:local_save}, R0
    AND R{state:full_idx}, R{data:sixteen}
    CMP R0, R{data:zero}
    BR.Z no_mirror
    SUB R{data:sixteen}, R{state:local_save}
    MOVE R{state:local_save}, R0
no_mirror:
    MOVE R0, R{state:local_save}
    {write:fwd_idx}
    MOVE R0, R{state:in_save}
    {write:fwd_input}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 1: Quarter-wave Table + Reconstruct ---
        table_data = [DataWord(f"qt{i}", val, address=i + 1)
                      for i, val in enumerate(quarter_table)]
        table_data.append(DataWord("tbase", 1, address=self.QUARTER_SIZE + 1))
        table_data.append(DataWord("zero", 0, address=self.QUARTER_SIZE + 2))

        cell1 = CellProgram(
            inputs=[Port("idx"), Port("neg_flag")],
            outputs=[Port("fwd_cos"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=table_data,
            state=[],
            assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    CMP R{in:neg_flag}, R{data:zero}
    BR.Z output
    SUB R{data:zero}, R0
output:
    {write:fwd_cos}
    {jump:fwd_trigger}
""",
        )

        # --- Cell 2: Mixer ---
        cell2 = CellProgram(
            inputs=[Port("input_val"), Port("cos_val")],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            state=[],
            assembly_template="""\
start:
    MULQ R{in:input_val}, R{in:cos_val}
    {write:out}
    {jump:out}
""",
        )

        return {0: cell0, 1: cell1, 2: cell2}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation using quarter-wave reconstruction."""
        output = np.zeros(len(input_samples), dtype=np.float32)
        quarter = self._generate_quarter_wave_table()

        for i, sample in enumerate(input_samples):
            self._phase = (self._phase + self._freq_word) & 0xFFFF
            # cos(phase) = sin(phase + 90°)
            cos_phase = (self._phase + 16384) & 0xFFFF
            full_idx = (cos_phase >> 10) & 0x3F

            negate = (full_idx & 32) != 0
            mirror = (full_idx & 16) != 0
            local = full_idx & 15
            if mirror:
                local = 16 - local

            cos_q15 = quarter[local]
            if negate:
                cos_q15 = (-cos_q15) & 0xFFFF

            cos_f = q15_to_float(cos_q15)
            output[i] = float(sample) * cos_f

        return output

    def reset(self):
        """Reset phase accumulator."""
        self._phase = 0
