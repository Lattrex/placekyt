"""
Bitstream Generator for Kyttar Fabric

Generates programming files from placement and routing:
- .myc: Debug format (human-readable, like disassembly)
- .hex: Intel HEX format for device programming

The bitstream contains:
1. Cell memory contents (programs + coefficients)
2. CONFIG.FACE register values (routing)
3. Metadata (placement positions, block names)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, BinaryIO, TextIO
import struct
from pathlib import Path

from .placer import Placement, PlacedBlock
from .cell_map import CellMap, Face
from .region import ArrayConfig


@dataclass
class CellProgram:
    """Program and configuration for a single cell."""
    position: Tuple[int, int]  # (col, row)
    block_name: str
    cell_index: int  # Index within block

    # Memory contents (32 words)
    memory: List[int] = field(default_factory=lambda: [0] * 32)

    # CONFIG.FACE register value
    face_config: int = 0

    def set_memory(self, addr: int, value: int):
        """Set a memory word."""
        if 0 <= addr < 32:
            self.memory[addr] = value & 0xFFFF


@dataclass
class Bitstream:
    """
    Complete bitstream for a Kyttar array.

    Contains all data needed to program the fabric.
    """
    # Array dimensions
    rows: int
    cols: int

    # Per-cell programs
    cells: Dict[Tuple[int, int], CellProgram] = field(default_factory=dict)

    # Metadata
    block_positions: Dict[str, List[Tuple[int, int]]] = field(default_factory=dict)

    def get_cell(self, col: int, row: int) -> CellProgram:
        """Get or create cell program."""
        if (col, row) not in self.cells:
            self.cells[(col, row)] = CellProgram(position=(col, row), block_name="", cell_index=0)
        return self.cells[(col, row)]

    def total_words(self) -> int:
        """Total memory words across all cells."""
        return len(self.cells) * 32

    def non_zero_words(self) -> int:
        """Count of non-zero memory words."""
        count = 0
        for cell in self.cells.values():
            count += sum(1 for w in cell.memory if w != 0)
        return count


class BitstreamGenerator:
    """
    Generates bitstream from placement and routing.

    Process:
    1. Create cell programs from placement
    2. Apply FACE routing configuration
    3. Load block programs into cell memory
    4. Generate output files
    """

    def __init__(self, config: ArrayConfig):
        """
        Initialize generator.

        Args:
            config: Array configuration
        """
        self.config = config

    def generate(
        self,
        placement: Placement,
        cell_map: CellMap,
        programs: Optional[Dict[str, List[int]]] = None,
    ) -> Bitstream:
        """
        Generate bitstream from placement and cell map.

        Args:
            placement: Completed placement
            cell_map: Complete cell configurations from router
            programs: Optional block programs (block_name -> list of words)

        Returns:
            Bitstream ready for export
        """
        bs = Bitstream(rows=self.config.height, cols=self.config.width)

        # Step 1: Create cell entries from placement
        for block_name, pb in placement.placed_blocks.items():
            cells = pb.cells
            bs.block_positions[block_name] = cells

            for i, (col, row) in enumerate(cells):
                cell = bs.get_cell(col, row)
                cell.block_name = block_name
                cell.cell_index = i

        # Step 2: Apply cell configurations from cell_map
        for (col, row), config in cell_map.cells.items():
            cell = bs.get_cell(col, row)
            cell.face_config = config.fwd_face.value if isinstance(config.fwd_face, Face) else config.fwd_face
            for addr, value in config.memory.items():
                cell.set_memory(addr, value)

        # Step 3: Load programs if provided (legacy support)
        if programs:
            for block_name, program in programs.items():
                if block_name in placement.placed_blocks:
                    pb = placement.placed_blocks[block_name]
                    self._load_block_program(bs, pb, program)

        return bs

    def _load_block_program(
        self,
        bs: Bitstream,
        pb: PlacedBlock,
        program: List[int],
    ):
        """
        Load a program into a block's cells.

        For single-cell blocks: Load entire program into the cell.
        For multi-cell blocks: Distribute program across cells.
        """
        cells = pb.cells
        words_per_cell = 32  # R0-R31 all usable

        if len(cells) == 1:
            # Single cell: load all into one cell
            col, row = cells[0]
            cell = bs.get_cell(col, row)
            for addr, word in enumerate(program[:words_per_cell]):
                cell.set_memory(addr, word)
        else:
            # Multi-cell: distribute across cells
            # Each cell gets portion of program
            for i, (col, row) in enumerate(cells):
                cell = bs.get_cell(col, row)
                start = i * words_per_cell
                end = start + words_per_cell
                cell_program = program[start:end]
                for addr, word in enumerate(cell_program):
                    cell.set_memory(addr, word)


class MycWriter:
    """
    Writes .myc debug format.

    Human-readable format showing:
    - Cell positions and block assignments
    - Memory contents with disassembly
    - FACE configuration
    """

    def __init__(self, bitstream: Bitstream):
        self.bs = bitstream

    def write(self, f: TextIO):
        """Write bitstream to .myc file."""
        f.write("; Kyttar Bitstream Debug Format\n")
        f.write(f"; Array: {self.bs.cols}x{self.bs.rows}\n")
        f.write(f"; Cells: {len(self.bs.cells)}\n")
        f.write(f"; Words: {self.bs.non_zero_words()} non-zero / {self.bs.total_words()} total\n")
        f.write(";\n")

        # Block summary
        f.write("; Block Placement:\n")
        for block_name, cells in self.bs.block_positions.items():
            f.write(f";   {block_name}: {cells}\n")
        f.write(";\n")

        # Per-cell data
        for (col, row), cell in sorted(self.bs.cells.items()):
            f.write(f"\n; Cell ({col},{row})")
            if cell.block_name:
                f.write(f" - {cell.block_name}[{cell.cell_index}]")
            f.write("\n")

            # FACE config
            face_str = self._face_to_string(cell.face_config)
            f.write(f".cell {col} {row}\n")
            f.write(f".face {cell.face_config:04b}  ; {face_str}\n")

            # Memory contents
            for addr in range(32):
                word = cell.memory[addr]
                if word != 0 or addr < 10:  # Show first 10 and non-zero
                    disasm = self._disassemble(word, addr)
                    f.write(f"  R{addr:02d}: 0x{word:04X}  ; {disasm}\n")

    def _face_to_string(self, face_bits: int) -> str:
        """Convert face bits to string."""
        faces = []
        if face_bits & (1 << Face.NORTH):
            faces.append('N')
        if face_bits & (1 << Face.EAST):
            faces.append('E')
        if face_bits & (1 << Face.SOUTH):
            faces.append('S')
        if face_bits & (1 << Face.WEST):
            faces.append('W')
        return ''.join(faces) if faces else 'none'

    def _disassemble(self, word: int, addr: int) -> str:
        """Simple disassembly of instruction word."""
        if word == 0:
            return "NOP / 0"

        # Try to decode as instruction
        opcode = (word >> 12) & 0xF

        # Opcode mapping from the Kyttar ISA reference (see PROGRAMMING_GUIDE.md)
        opcodes = {
            0x0: "HALT",
            0x1: "HALT",      # reserved
            0x2: "HALT",      # reserved
            0x3: "HALT",      # reserved
            0x4: "MOVE",
            0x5: "BR",
            0x6: "WRITE",
            0x7: "JUMP",
            0x8: "LOGIC",
            0x9: "ARITH",
            0xA: "SHL",
            0xB: "SHR",
            0xC: "MUL",
            0xD: "MAC",
            0xE: "CMP",
            0xF: "LOAD",
        }

        if opcode in opcodes:
            # MUL (0xC) and MAC (0xD) carry a 2-bit MODE in bits [11:10] that
            # selects a sub-instruction (architecture_spec_v0.11.md §4.11/§4.12).
            # Decode it so MULQ/MULHI and MACQ/MSU/MSUQ disassemble to their real
            # mnemonic instead of the bare opcode group name.
            mode = (word >> 10) & 0x3
            if opcode == 0xC:  # MUL group (00=MUL, 01=MULQ, 10=MULHI, 11=reserved)
                sub = {0: "MUL", 1: "MULQ", 2: "MULHI", 3: "MUL?"}[mode]
                return f"{sub} (0x{word:04X})"
            if opcode == 0xD:  # MAC group (00=MAC, 01=MACQ, 10=MSU, 11=MSUQ)
                sub = {0: "MAC", 1: "MACQ", 2: "MSU", 3: "MSUQ"}[mode]
                return f"{sub} (0x{word:04X})"
            return f"{opcodes[opcode]} (0x{word:04X})"
        else:
            # Treat as data
            signed = word if word < 0x8000 else word - 0x10000
            return f"data: {signed} (0x{word:04X})"


class HexWriter:
    """
    Writes Intel HEX format for device programming.

    Format:
    :LLAAAATT[DD...]CC
    - LL: byte count
    - AAAA: address
    - TT: record type (00=data, 01=EOF, 02=ext addr)
    - DD: data bytes
    - CC: checksum
    """

    def __init__(self, bitstream: Bitstream):
        self.bs = bitstream

    def write(self, f: TextIO):
        """Write bitstream to Intel HEX file."""
        # Calculate base address for each cell
        # Layout: Each cell at (col, row) starts at address (row * cols + col) * 64
        # (64 bytes = 32 words * 2 bytes/word)

        for (col, row), cell in sorted(self.bs.cells.items()):
            cell_base = (row * self.bs.cols + col) * 64

            # Write extended address if needed
            if cell_base >= 0x10000:
                ext_addr = (cell_base >> 16) & 0xFFFF
                self._write_extended_addr(f, ext_addr)
                cell_base &= 0xFFFF

            # Write memory contents in 16-byte chunks
            for chunk_start in range(0, 32, 8):  # 8 words = 16 bytes per line
                addr = cell_base + chunk_start * 2
                data = []
                for i in range(8):
                    word = cell.memory[chunk_start + i]
                    # Little-endian word to bytes
                    data.append(word & 0xFF)
                    data.append((word >> 8) & 0xFF)

                self._write_data_record(f, addr, data)

            # Also write FACE config at appropriate offset
            face_addr = cell_base + 62
            face_data = [cell.face_config & 0xFF, (cell.face_config >> 8) & 0xFF]
            self._write_data_record(f, face_addr, face_data)

        # Write EOF record
        f.write(":00000001FF\n")

    def _write_data_record(self, f: TextIO, addr: int, data: List[int]):
        """Write a data record."""
        if not data:
            return

        # Skip all-zero records
        if all(b == 0 for b in data):
            return

        byte_count = len(data)
        record_type = 0x00

        checksum = byte_count
        checksum += (addr >> 8) & 0xFF
        checksum += addr & 0xFF
        checksum += record_type

        f.write(f":{byte_count:02X}{addr:04X}{record_type:02X}")
        for b in data:
            f.write(f"{b:02X}")
            checksum += b

        checksum = (~checksum + 1) & 0xFF
        f.write(f"{checksum:02X}\n")

    def _write_extended_addr(self, f: TextIO, ext_addr: int):
        """Write extended address record."""
        checksum = 0x02 + 0x00 + 0x00 + 0x02
        checksum += (ext_addr >> 8) & 0xFF
        checksum += ext_addr & 0xFF
        checksum = (~checksum + 1) & 0xFF
        f.write(f":02000002{ext_addr:04X}{checksum:02X}\n")


def generate_bitstream(
    placement: Placement,
    cell_map: CellMap,
    config: ArrayConfig,
    programs: Optional[Dict[str, List[int]]] = None,
) -> Bitstream:
    """
    Convenience function to generate bitstream.

    Args:
        placement: Completed placement
        cell_map: Complete cell configurations from router
        config: Array configuration
        programs: Optional block programs

    Returns:
        Bitstream
    """
    gen = BitstreamGenerator(config)
    return gen.generate(placement, cell_map, programs)


def write_myc(bitstream: Bitstream, path: Path):
    """Write bitstream to .myc debug file."""
    writer = MycWriter(bitstream)
    with open(path, 'w') as f:
        writer.write(f)


def write_hex(bitstream: Bitstream, path: Path):
    """Write bitstream to Intel HEX file."""
    writer = HexWriter(bitstream)
    with open(path, 'w') as f:
        writer.write(f)


if __name__ == '__main__':
    # Test bitstream generation
    from .block import BlockDefinition, Connection
    from .placer import Placer
    from .router import Router

    # Create test blocks
    blocks = [
        BlockDefinition(
            name="Filter",
            cell_count=3,
            connections=[Connection(target="Output", weight=1.0)],
        ),
        BlockDefinition(
            name="Output",
            cell_count=2,
        ),
    ]

    # Simple test program (opcodes per the Kyttar ISA reference (see PROGRAMMING_GUIDE.md))
    programs = {
        "Filter": [
            0xF001,  # LOAD R1 (opcode 0xF)
            0x4002,  # MOVE R2, R0 (opcode 0x4, SRC=R0 in [9:5], DEST=R2 in [4:0])
            0x0000,  # HALT (opcode 0x0)
        ],
        "Output": [
            0x6000,  # WRITE (opcode 0x6)
        ],
    }

    # Place and route
    config = ArrayConfig(width=6, height=6)
    placer = Placer(config)
    placement = placer.place(blocks)

    router = Router(config)
    cell_map = router.route(placement, blocks)

    # Generate bitstream
    gen = BitstreamGenerator(config)
    bs = gen.generate(placement, cell_map, programs)

    print("Bitstream generated:")
    print(f"  Cells: {len(bs.cells)}")
    print(f"  Non-zero words: {bs.non_zero_words()}")

    # Write debug format
    writer = MycWriter(bs)
    print("\n.myc output:")
    import sys
    writer.write(sys.stdout)
