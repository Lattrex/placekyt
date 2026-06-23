"""
Kyttar annotated bitstream format (*.myc).

This is a human-readable disassembly format that shows exactly what
the bitstream does, cell by cell. It's useful for debugging and
understanding the programming sequence.

Format:
    # Comments start with #
    # Header contains chip info and generation timestamp

    # Each entry shows:
    # WORD  CELL(x,y)  ADDR  INSTRUCTION  ; COMMENT

Example:
    # Kyttar Bitstream Disassembly
    # Chip: dev_12x12 (12x12)
    # Generated: 2026-01-20 12:00:00

    # === Phase 1: Row 0 routing ===
    # Opcodes per the Kyttar ISA reference (see PROGRAMMING_GUIDE.md): WRITE=0x6, JUMP=0x7
    0x681F  (0,0)   R31   WRITE.CFG @0, R31   ; Set CONFIG (FACE=East), CFG bit=1
    0x0001  (0,0)   -     DATA: 0x0001        ; CONFIG value

    0x63DF  (1,0)   R31   WRITE @1, R31       ; HOP=30, LOCAL=31
    0x0001  (1,0)   -     DATA: 0x0001
"""

from typing import List, Tuple, Optional, TextIO
from datetime import datetime
from dataclasses import dataclass


@dataclass
class MycEntry:
    """A single entry in the MYC file."""
    word: int
    cell_x: int
    cell_y: int
    address: Optional[int]  # None for data words
    is_instruction: bool
    mnemonic: str
    comment: str


class MycWriter:
    """Write Kyttar annotated bitstream format."""

    def __init__(self, chip_name: str, width: int, height: int):
        """
        Initialize writer.

        Args:
            chip_name: Name of the chip configuration
            width: Array width in cells
            height: Array height in cells
        """
        self.chip_name = chip_name
        self.width = width
        self.height = height
        self.entries: List[MycEntry] = []
        self.phase_comments: List[Tuple[int, str]] = []  # (entry_index, comment)

    def add_phase_comment(self, comment: str):
        """Add a phase separator comment before the next entry."""
        self.phase_comments.append((len(self.entries), comment))

    def add_write_instruction(self, word: int, cell_x: int, cell_y: int,
                               dest: int, hop_cnt: int, comment: str = ""):
        """Add a WRITE instruction entry."""
        mnemonic = f"WRITE @{31 - hop_cnt}, R{dest}"
        entry = MycEntry(
            word=word,
            cell_x=cell_x,
            cell_y=cell_y,
            address=dest,
            is_instruction=True,
            mnemonic=mnemonic,
            comment=comment
        )
        self.entries.append(entry)

    def add_data_word(self, word: int, cell_x: int, cell_y: int, comment: str = ""):
        """Add a data word entry (follows a WRITE instruction)."""
        mnemonic = f"DATA: 0x{word:04X}"
        entry = MycEntry(
            word=word,
            cell_x=cell_x,
            cell_y=cell_y,
            address=None,
            is_instruction=False,
            mnemonic=mnemonic,
            comment=comment
        )
        self.entries.append(entry)

    def add_jump_instruction(self, word: int, cell_x: int, cell_y: int,
                              dest: int, hop_cnt: int, comment: str = ""):
        """Add a JUMP instruction entry."""
        mnemonic = f"JUMP @{31 - hop_cnt}, {dest}"
        entry = MycEntry(
            word=word,
            cell_x=cell_x,
            cell_y=cell_y,
            address=dest,
            is_instruction=True,
            mnemonic=mnemonic,
            comment=comment
        )
        self.entries.append(entry)

    def add_raw_entry(self, word: int, cell_x: int, cell_y: int,
                      address: Optional[int], is_instruction: bool,
                      mnemonic: str, comment: str = ""):
        """Add a raw entry with custom formatting."""
        entry = MycEntry(
            word=word,
            cell_x=cell_x,
            cell_y=cell_y,
            address=address,
            is_instruction=is_instruction,
            mnemonic=mnemonic,
            comment=comment
        )
        self.entries.append(entry)

    def write(self, output: TextIO):
        """Write the MYC file to output."""
        # Header
        output.write("# Kyttar Bitstream Disassembly\n")
        output.write(f"# Chip: {self.chip_name} ({self.width}x{self.height})\n")
        output.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        output.write(f"# Total words: {len(self.entries)}\n")
        output.write("#\n")
        output.write("# Format: WORD  CELL(x,y)  ADDR  INSTRUCTION  ; COMMENT\n")
        output.write("# " + "=" * 70 + "\n")
        output.write("\n")

        # Convert phase comments to a dict for easy lookup
        phase_dict = dict(self.phase_comments)

        # Entries
        for i, entry in enumerate(self.entries):
            # Check for phase comment
            if i in phase_dict:
                output.write(f"\n# === {phase_dict[i]} ===\n")

            # Format address field
            if entry.address is not None:
                addr_str = f"R{entry.address:2d}"
            else:
                addr_str = "   -"

            # Format cell coordinates
            cell_str = f"({entry.cell_x:2d},{entry.cell_y:2d})"

            # Format the line
            line = f"0x{entry.word:04X}  {cell_str}  {addr_str}  {entry.mnemonic:<20s}"
            if entry.comment:
                line += f"  ; {entry.comment}"

            output.write(line + "\n")

    def write_file(self, path: str):
        """Write to a file."""
        with open(path, 'w') as f:
            self.write(f)

    def get_words(self) -> List[int]:
        """Get the raw word list (same as would be in .hex file)."""
        return [entry.word for entry in self.entries]
