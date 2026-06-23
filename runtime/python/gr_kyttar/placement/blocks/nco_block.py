"""NCOBlock — see :class:`NCOBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class NCOBlock(KyttarBlock):
    """
    Numerically Controlled Oscillator (NCO) block.

    Generates sine wave output using a phase accumulator and 8-entry
    lookup table with LOAD indirect addressing.

    Each input sample triggers one NCO output (input value is ignored).

    output_freq = (freq_word / 65536) * sample_rate

    Interface (defaults):
    - Entry: R1
    - Input: R31 (ignored - just triggers execution, gets overwritten by port)
    """
    CATEGORY = "recovery"
    TAGS = ["nco", "oscillator", "recovery"]

    # Port injection always writes to R31, so we list R31 as input register.
    # The NCO ignores the input value - it just triggers execution.
    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # 64-entry equivalent via quarter-wave reconstruction (2 cells).
    # 17 quarter-wave entries stored in Cell 1. Cell 0 does phase logic +
    # quadrant folding. Reconstruction is exact — identical to full 64-entry table.
    # Resolution: 5.625° per entry, ±2.8° max quantization error.
    TABLE_SIZE = 64
    QUARTER_SIZE = 17  # entries 0-16 (sin 0° to sin 90°)

    def __init__(self, name: str, freq_word: int = 655, sample_rate: float = 32000.0):
        """
        Initialize NCO block.

        Args:
            name: Block name
            freq_word: Phase increment per sample (0-65535)
            sample_rate: Sample rate in Hz (for reference calculation only)
        """
        super().__init__(name, freq_word=freq_word, sample_rate=sample_rate)
        self._freq_word = freq_word & 0xFFFF
        self._sample_rate = sample_rate
        self._phase = 0  # For reference implementation

    @property
    def cell_count(self) -> int:
        return 2  # Phase logic + quarter-wave table/reconstruction

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def frequency(self) -> float:
        """Output frequency in Hz."""
        return (self._freq_word / 65536.0) * self._sample_rate

    def _generate_sine_table(self) -> List[int]:
        """Generate full 64-entry sine table in Q15 (for reference implementation)."""
        table = []
        for i in range(self.TABLE_SIZE):
            phase = 2 * np.pi * i / self.TABLE_SIZE
            value = np.sin(phase)
            table.append(float_to_q15(value))
        return table

    def _generate_quarter_wave_table(self) -> List[int]:
        """Generate 17-entry quarter-wave sine table in Q15.

        Entries 0-16 = sin(0°) to sin(90°).
        Full 64-entry wave reconstructed via symmetry:
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

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production NCO: 2-cell quarter-wave sine table.

        64-entry equivalent resolution (5.625°, ±2.8° max error) via
        quarter-wave reconstruction. Mathematically identical to full table.

        Cell 0 (Phase Logic):
          Updates phase accumulator. Computes 6-bit index (0-63).
          Folds into quarter-wave: extracts local index (0-16) and
          negate flag. Pre-stages both to Cell 1, triggers Cell 1.

        Cell 1 (Table + Reconstruct):
          17 quarter-wave entries (sin 0° to sin 90°).
          LOADs table[local], negates if flag set, outputs result.
        """
        quarter_table = self._generate_quarter_wave_table()

        # --- Cell 0: Phase Logic ---
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],  # ignored, triggers execution
            outputs=[Port("fwd_idx"), Port("fwd_neg"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("freq", self._freq_word, address=1),
                DataWord("thirty_two", 32, address=2),
                DataWord("fifteen", 15, address=3),
                DataWord("sixteen", 16, address=4),
                DataWord("zero", 0, address=5),
            ],
            state=[
                StateVar("phase"),
                StateVar("full_idx"),
                StateVar("local_save"),
            ],
            assembly_template="""\
start:
    ADD R{state:phase}, R{data:freq}
    MOVE R{state:phase}, R0
    SHR R{state:phase}, #10
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
    {jump:fwd_trigger}
""",
        )

        # --- Cell 1: Quarter-wave Table + Reconstruction ---
        table_data = [DataWord(f"qt{i}", val, address=i + 1)
                      for i, val in enumerate(quarter_table)]
        table_data.append(DataWord("tbase", 1, address=self.QUARTER_SIZE + 1))
        table_data.append(DataWord("zero", 0, address=self.QUARTER_SIZE + 2))

        cell1 = CellProgram(
            inputs=[Port("idx"), Port("neg_flag")],
            outputs=[Port("out")],
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
    {write:out}
    {jump:out}
""",
        )

        return {0: cell0, 1: cell1}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation - generates sine wave (ignores input).

        Uses quarter-wave reconstruction matching hardware exactly.
        """
        output = np.zeros(len(input_samples), dtype=np.float32)
        quarter = self._generate_quarter_wave_table()

        for i in range(len(input_samples)):
            self._phase = (self._phase + self._freq_word) & 0xFFFF
            full_idx = (self._phase >> 10) & 0x3F  # 6-bit index (0-63)

            # Quarter-wave reconstruction
            negate = (full_idx & 32) != 0
            mirror = (full_idx & 16) != 0
            local = full_idx & 15

            if mirror:
                local = 16 - local

            q15_val = quarter[local]
            if negate:
                q15_val = (-q15_val) & 0xFFFF

            output[i] = q15_to_float(q15_val)

        return output

    def reset(self):
        """Reset phase accumulator."""
        self._phase = 0
