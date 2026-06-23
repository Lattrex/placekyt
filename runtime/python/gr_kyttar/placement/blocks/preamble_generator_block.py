"""PreambleGeneratorBlock — see :class:`PreambleGeneratorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class PreambleGeneratorBlock(KyttarBlock):
    """
    Preamble Generator Block (1 cell) — Loopback Test Placeholder.

    In production, the MIL-STD-188-110B preamble (287 symbols with
    rate/interleaver encoding per Appendix A Table A-VIII) is generated
    by the FPGA and fed to the chip via x1_in. The preamble is static
    data that varies per-transmission only in the rate/interleaver
    fields — no computation is needed, making FPGA storage the natural
    choice. Storing 287 fixed symbols on-chip would waste ~9 cells.

    This placeholder generates a simple alternating ±1 pattern for
    loopback testing. It is NOT compliant with MIL-STD-188-110B.

    Interface:
        - Entry: R1 (trigger to output one symbol)
        - Input: R31 (trigger, value ignored)
        - Output: Alternating +1/-1 symbols
    """
    CATEGORY = "frame_sync"
    TAGS = ["preamble", "generator", "frame_sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    PREAMBLE_LENGTH = 287
    SEGMENT_LENGTH = 96

    def __init__(self, name: str, data_rate: int = 2400, interleaver: str = "short"):
        """
        Initialize Preamble Generator.

        Args:
            name: Block name
            data_rate: Data rate in bps
            interleaver: "short", "long", or "zero"
        """
        super().__init__(name, data_rate=data_rate, interleaver=interleaver)
        self._data_rate = data_rate
        self._interleaver = interleaver
        self._symbol_index = 0

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 preamble generator: counter-based alternating +1/-1 output.

        Triggered by WRITE+JUMP (trigger value in R0 ignored). Each execution
        produces one symbol. Even counter → +1 (0x7FFF), odd → -1 (0x8001).
        """
        return {0: CellProgram(
            inputs=[Port("trigger", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("plus", 0x7FFF, address=2),
                DataWord("minus", 0x8001, address=3),
            ],
            state=[
                StateVar("counter"),
            ],
            assembly_template="""\
start:
    AND R{state:counter}, R{data:one}
    BR.NZ odd
    MOVE R0, R{data:plus}
    GOTO output
odd:
    MOVE R0, R{data:minus}
output:
    {write:out}
    ADD R{state:counter}, R{data:one}
    MOVE R{state:counter}, R0
    {jump:out}
""",
        )}

    def _build_sequence_program(self) -> CellProgram:
        """Build sequence generator cell."""
        prog = CellProgram()

        # Encode rate/interleaver into preamble pattern
        # (Simplified - actual encoding per MIL-STD-188-110B Appendix)
        prog.set_memory(16, self.PREAMBLE_LENGTH)
        prog.set_memory(17, 0)  # Symbol counter
        prog.set_memory(18, 0)  # Segment counter

        assembly = """; Preamble Sequence Generator
; R16: preamble_length, R17: symbol_idx, R18: segment

start:
    ; Generate next preamble symbol
    ; Pattern: alternating +1/-1 with rate encoding

    ; Simple alternating pattern
    AND R17, 1
    MOVE R19, R0
    CMP R19, 0
    BR.Z send_plus
    MOVI R0, 0x8001     ; -1 in Q15
    GOTO send_sym

send_plus:
    MOVI R0, 0x7FFF     ; +1 in Q15

send_sym:
    WRITE @1, 31
    JUMP @1, 1

    ; Increment counter
    ADD R17, 1
    MOVE R17, R0

    ; Check if preamble complete
    CMP R17, R16
    BR.N continue
    ; Done - halt
    HALT

continue:
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def _build_modulator_program(self, output_hop: int, target_input: int, target_entry: int) -> CellProgram:
        """Build BPSK modulator cell."""
        prog = CellProgram()

        assembly = f"""; Preamble BPSK Modulator
; R31: Input symbol (+1/-1)

start:
    ; Pass through (already BPSK)
    MOVE R0, R31
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=1)
        prog.set_program(1, words)
        return prog

    def process_reference(self, trigger: bool = True) -> np.ndarray:
        """
        Reference implementation of preamble generation.

        Args:
            trigger: Whether to generate preamble

        Returns:
            Preamble symbols
        """
        if not trigger:
            return np.array([], dtype=np.float32)

        # Generate simple alternating preamble
        preamble = np.zeros(self.PREAMBLE_LENGTH, dtype=np.float32)
        for i in range(self.PREAMBLE_LENGTH):
            preamble[i] = 1.0 if (i % 2 == 0) else -1.0

        return preamble

    def reset(self):
        """Reset generator state."""
        self._symbol_index = 0
