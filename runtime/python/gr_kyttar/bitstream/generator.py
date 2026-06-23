"""
Bitstream Generator for Kyttar arrays.

Generates programming sequences that configure cells through the input port,
exactly as real hardware would be programmed.

The programming pattern is:
1. Program row 0 cells with FACE=East (horizontal routing)
2. Program columns right-to-left, bottom-up

This ensures each cell can be reached before its routing is configured.
"""

from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .intel_hex import IntelHexWriter
from .myc_format import MycWriter


# CONFIG register bit positions (from config_reg.rs)
# CONFIG format:
#   [5:0]   FLAGS (read-only status flags)
#   [9:8]   FWD_FACE (forward direction)
#   [11:10] REV_FACE (reverse direction)
#   [13:12] LOCK_FACE
#   [15:14] reserved
#
# Face encoding: 00=South, 01=East, 10=West, 11=North
FACE_SOUTH = 0
FACE_EAST = 1
FACE_WEST = 2
FACE_NORTH = 3


def encode_config(fwd_face: int) -> int:
    """Encode data value for WRITE.CFG to FACE register.

    When writing to CONFIG[1] (FACE), the data value is simply the face
    direction (0-3) in bits [1:0]. No shifting needed.
    """
    return fwd_face & 0x3


def encode_write(hop_cnt: int, dest: int, config: bool = False) -> int:
    """
    Encode a WRITE instruction.

    WRITE (v0.11): 0110 | RSV[11] | CFG[10] | HOP_CNT[9:5] | DEST[4:0]

    Args:
        hop_cnt: Hop count (0-31). Value 31 means execute locally.
        dest: Target memory address (0-31).
        config: If True, write to CONFIG space instead of memory.

    Returns:
        16-bit WRITE instruction word.
    """
    opcode = 0x6  # 0110 (v0.11)
    cfg_bit = 1 if config else 0
    word = (opcode << 12) | (cfg_bit << 10) | ((hop_cnt & 0x1F) << 5) | (dest & 0x1F)
    return word


def encode_jump(hop_cnt: int, dest: int) -> int:
    """
    Encode a JUMP instruction.

    JUMP (v0.11): 0111 | RSV[11] | RSV[10] | HOP_CNT[9:5] | DEST[4:0]

    Args:
        hop_cnt: Hop count (0-31). Value 31 means execute locally.
        dest: Target PC address (0-31).

    Returns:
        16-bit JUMP instruction word.
    """
    opcode = 0x7  # 0111 (v0.11)
    word = (opcode << 12) | ((hop_cnt & 0x1F) << 5) | (dest & 0x1F)
    return word


@dataclass
class CellProgram:
    """Program data for a single cell."""
    x: int
    y: int
    fwd_face: int = FACE_SOUTH  # Default: forward south
    memory: Dict[int, int] = field(default_factory=dict)  # addr -> value
    entry_addr: Optional[int] = None  # Entry point for JUMP triggers


@dataclass
class Bitstream:
    """Container for generated bitstream data."""
    words: List[int]
    myc_writer: MycWriter
    chip_name: str
    width: int
    height: int

    def write_hex(self, path: str):
        """Write Intel HEX format."""
        writer = IntelHexWriter()
        writer.write_file(self.words, path)

    def write_myc(self, path: str):
        """Write annotated MYC format."""
        self.myc_writer.write_file(path)

    def __len__(self):
        return len(self.words)


class BitstreamGenerator:
    """
    Generate programming bitstreams for Kyttar arrays.

    The generator creates a sequence of WRITE instructions that programs
    each cell through the input port, following the column-by-column,
    right-to-left, bottom-up pattern.
    """

    def __init__(self, chip_config: str):
        """
        Initialize generator from chip configuration.

        Args:
            chip_config: Path to YAML chip configuration file.
        """
        self.chip_config_path = chip_config
        self._load_config()
        self.cell_programs: Dict[Tuple[int, int], CellProgram] = {}

    def load_cell_map(self, cell_map: 'CellMap'):
        """
        Load cell configurations from a CellMap (output of placement engine).

        This is the primary interface for loading cell programs. The CellMap
        contains complete configurations for all cells that need programming.

        Args:
            cell_map: CellMap from the placement/routing engine
        """
        # Import here to avoid circular imports
        from ..placement.cell_map import CellMap as CM, Face

        # Convert CellMap entries to internal CellProgram format
        self.cell_programs.clear()

        for (col, row), config in cell_map.cells.items():
            # Convert Face enum to our FACE constants
            face_map = {
                Face.SOUTH: FACE_SOUTH,
                Face.EAST: FACE_EAST,
                Face.WEST: FACE_WEST,
                Face.NORTH: FACE_NORTH,
            }
            fwd_face = face_map.get(config.fwd_face, FACE_SOUTH)

            prog = CellProgram(
                x=col,
                y=row,
                fwd_face=fwd_face,
                memory=dict(config.memory),
                entry_addr=config.entry_addr,
            )
            self.cell_programs[(col, row)] = prog

    def _load_config(self):
        """Load chip configuration from YAML."""
        with open(self.chip_config_path, 'r') as f:
            config = yaml.safe_load(f)

        self.chip_name = config.get('chip_type', {}).get('name', 'unknown')
        fabric = config.get('fabric', {})
        self.width = fabric.get('width', 12)
        self.height = fabric.get('height', 12)

        # Find input and output ports
        ports = config.get('ports', {})
        self.input_port = None
        self.output_port = None

        # Handle both old format (list) and new format (dict)
        if isinstance(ports, dict):
            # New format: dict of port name -> config
            for name, port_data in ports.items():
                port_type = port_data.get('type', port_data.get('direction', ''))
                cell = port_data.get('cell', [0, 0])
                if isinstance(cell, list):
                    cell_pos = (cell[0], cell[1])
                else:
                    cell_pos = (cell.get('x', 0), cell.get('y', 0))

                if port_type == 'input' and self.input_port is None:
                    self.input_port = cell_pos
                elif port_type == 'output' and self.output_port is None:
                    self.output_port = cell_pos
        else:
            # Old format: list of port dicts — take first input/output port
            for port in ports:
                if port.get('direction') == 'input' and self.input_port is None:
                    cell = port.get('cell', {})
                    self.input_port = (cell.get('x', 0), cell.get('y', 0))
                elif port.get('direction') == 'output' and self.output_port is None:
                    cell = port.get('cell', {})
                    self.output_port = (cell.get('x', 0), cell.get('y', 0))

        # Default to (0,0) for input if not found
        if self.input_port is None:
            self.input_port = (0, 0)

    def _calculate_hop_count(self, target_x: int, target_y: int) -> int:
        """
        Calculate HOP_CNT to reach a cell from input port.

        Path: (0,0) → row 0 East → column x South → (x,y)
        Distance = x + y hops

        IMPORTANT: The receiving cell ALWAYS increments HOP_CNT BEFORE checking
        if it equals 31. So we use HOP_CNT = 30 - distance, which becomes 31
        after the final increment at the target cell.

        Examples:
        - Cell (0,0): distance=0, HOP_CNT=30, after increment: 31 → execute
        - Cell (1,0): distance=1, HOP_CNT=29, after 1 transit+1 arrival: 31 → execute
        - Cell (11,11): distance=22, HOP_CNT=8, after 22 increments: 30 → execute

        Args:
            target_x: Target cell X coordinate
            target_y: Target cell Y coordinate

        Returns:
            HOP_CNT value (0-30)
        """
        distance = target_x + target_y
        if distance > 30:
            raise ValueError(f"Cell ({target_x},{target_y}) is too far ({distance} hops, max 30)")
        return 30 - distance

    def set_cell_face(self, x: int, y: int, face: int):
        """
        Set the FWD_FACE for a cell.

        Args:
            x, y: Cell coordinates
            face: FACE_NORTH, FACE_EAST, FACE_SOUTH, or FACE_WEST
        """
        if (x, y) not in self.cell_programs:
            self.cell_programs[(x, y)] = CellProgram(x=x, y=y)
        self.cell_programs[(x, y)].fwd_face = face

    def set_cell_memory(self, x: int, y: int, addr: int, value: int):
        """
        Set a memory word in a cell.

        Args:
            x, y: Cell coordinates
            addr: Memory address (0-31)
            value: 16-bit value
        """
        if (x, y) not in self.cell_programs:
            self.cell_programs[(x, y)] = CellProgram(x=x, y=y)
        self.cell_programs[(x, y)].memory[addr] = value & 0xFFFF

    def set_cell_entry(self, x: int, y: int, entry_addr: int):
        """
        Set the entry address for a cell (where JUMP triggers start execution).

        Args:
            x, y: Cell coordinates
            entry_addr: Entry point address (0-31)
        """
        if (x, y) not in self.cell_programs:
            self.cell_programs[(x, y)] = CellProgram(x=x, y=y)
        self.cell_programs[(x, y)].entry_addr = entry_addr

    def add_routing_cell(self, x: int, y: int, face: int):
        """
        Add a simple routing cell that forwards data in the specified direction.

        Args:
            x, y: Cell coordinates
            face: Direction to forward (FACE_NORTH/EAST/SOUTH/WEST)
        """
        self.set_cell_face(x, y, face)

    def add_routing_path(self, from_xy: Tuple[int, int], to_xy: Tuple[int, int]):
        """
        Add routing cells to create a path between two points.

        Uses Manhattan routing: horizontal first (East/West), then vertical (North/South).

        Args:
            from_xy: Starting cell (x, y)
            to_xy: Ending cell (x, y)
        """
        fx, fy = from_xy
        tx, ty = to_xy

        x, y = fx, fy

        # Horizontal routing (towards target x)
        while x != tx:
            face = FACE_EAST if tx > x else FACE_WEST
            self.add_routing_cell(x, y, face)
            x += 1 if tx > x else -1

        # Vertical routing (towards target y)
        while y != ty:
            face = FACE_SOUTH if ty > y else FACE_NORTH
            self.add_routing_cell(x, y, face)
            y += 1 if ty > y else -1

    def generate(self, skip_duplicate_row0: bool = False, custom_row0: bool = False) -> Bitstream:
        """
        Generate the programming bitstream.

        Args:
            skip_duplicate_row0: If True, don't re-program row 0 cells in Phase 2
                                (used when row 0 is already set up in Phase 1)
            custom_row0: If True, use cell_programs for row 0 instead of automatic
                        FACE=East setup. This allows custom routing paths.

        Returns:
            Bitstream object containing the programming sequence.
        """
        words: List[int] = []
        myc = MycWriter(self.chip_name, self.width, self.height)

        # Phase 1: Setup row 0 for horizontal routing
        if custom_row0:
            # Use custom row 0 configuration from cell_programs
            myc.add_phase_comment("Phase 1: Row 0 custom routing setup")
            for x in range(self.width):
                cell_key = (x, 0)
                if cell_key in self.cell_programs:
                    prog = self.cell_programs[cell_key]
                    hop_cnt = self._calculate_hop_count(x, 0)

                    # Program CONFIG if non-default face
                    if prog.fwd_face != FACE_SOUTH:
                        write_instr = encode_write(hop_cnt, 1, config=True)  # CONFIG[1] = FACE
                        config_value = encode_config(prog.fwd_face)

                        words.append(write_instr)
                        words.append(config_value)

                        face_names = {FACE_NORTH: "North", FACE_EAST: "East",
                                      FACE_SOUTH: "South", FACE_WEST: "West"}
                        myc.add_write_instruction(
                            write_instr, x, 0, 0, hop_cnt,
                            f"Set FACE={face_names[prog.fwd_face]} for routing"
                        )
                        myc.add_data_word(
                            config_value, x, 0,
                            f"CONFIG: FWD_FACE={face_names[prog.fwd_face]}"
                        )
        else:
            # Default: Setup row 0 for horizontal routing (FACE=East)
            myc.add_phase_comment("Phase 1: Row 0 routing setup (FACE=East)")

            for x in range(self.width - 1):  # (0,0) to (width-2, 0)
                hop_cnt = self._calculate_hop_count(x, 0)

                # WRITE to CONFIG to set FWD_FACE=East
                write_instr = encode_write(hop_cnt, 1, config=True)  # CONFIG[1] = FACE
                config_value = encode_config(FACE_EAST)

                words.append(write_instr)
                words.append(config_value)

                myc.add_write_instruction(
                    write_instr, x, 0, 0, hop_cnt,
                    f"Set FACE=East for routing"
                )
                myc.add_data_word(
                    config_value, x, 0,
                    f"CONFIG: FWD_FACE=East"
                )

        # Phase 2: Program columns right-to-left, bottom-up
        myc.add_phase_comment("Phase 2: Column programming (right-to-left, bottom-up)")

        # Track which row 0 cells were programmed in Phase 1
        # (they got FACE=East and may need to be reprogrammed)
        row0_programmed_in_phase1 = set()
        if not custom_row0:
            row0_programmed_in_phase1 = set(range(self.width - 1))

        for col in range(self.width - 1, -1, -1):
            # CRITICAL: If this column has cells below row 0 that need programming,
            # we must first reprogram row 0 to FACE=South to enable the programming
            # path down the column. This must happen BEFORE processing the column.
            #
            # Check if row 0 of this column needs to turn South for column access:
            needs_turn_south = False
            for r in range(1, self.height):
                if (col, r) in self.cell_programs:
                    # There's a cell below row 0 that needs programming
                    needs_turn_south = True
                    break

            # Track if we turned this row 0 cell South for column access
            row0_turned_south = False

            if needs_turn_south and col in row0_programmed_in_phase1:
                # Reprogram (col, 0) to FACE=South FIRST
                hop_cnt = self._calculate_hop_count(col, 0)
                write_instr = encode_write(hop_cnt, 1, config=True)  # CONFIG[1] = FACE
                config_value = encode_config(FACE_SOUTH)

                words.append(write_instr)
                words.append(config_value)

                myc.add_write_instruction(
                    write_instr, col, 0, 0, hop_cnt,
                    f"Turn South for column {col} access"
                )
                myc.add_data_word(
                    config_value, col, 0,
                    f"CONFIG: FWD_FACE=South"
                )
                row0_turned_south = True

            for row in range(self.height - 1, -1, -1):
                cell_key = (col, row)

                # Skip row 0 cells if they were already programmed in Phase 1
                # UNLESS they need a different face (then reprogram them)
                if skip_duplicate_row0 and row == 0 and col < self.width - 1:
                    if cell_key not in self.cell_programs:
                        continue
                    # If the cell needs FACE=East (same as Phase 1), skip
                    if self.cell_programs[cell_key].fwd_face == FACE_EAST:
                        continue
                    # Otherwise, fall through to reprogram

                # Get cell program (or use defaults)
                if cell_key in self.cell_programs:
                    prog = self.cell_programs[cell_key]
                else:
                    # No explicit program - use defaults (FACE=South, no memory)
                    continue

                hop_cnt = self._calculate_hop_count(col, row)

                # Program CONFIG if:
                # 1. Non-default face (fwd_face != SOUTH), OR
                # 2. Row 0 cell that was programmed with FACE=East in Phase 1 but needs FACE=South
                #
                # BUT: Skip if we already turned this row 0 cell South for column access
                # (unless the cell needs a different FACE value)
                if row == 0 and row0_turned_south:
                    # Already turned South - only reprogram if cell needs different FACE
                    needs_face_program = (prog.fwd_face != FACE_SOUTH)
                else:
                    needs_face_program = (prog.fwd_face != FACE_SOUTH) or \
                                        (row == 0 and col in row0_programmed_in_phase1 and prog.fwd_face == FACE_SOUTH)

                if needs_face_program:
                    write_instr = encode_write(hop_cnt, 1, config=True)  # CONFIG[1] = FACE
                    config_value = encode_config(prog.fwd_face)

                    words.append(write_instr)
                    words.append(config_value)

                    face_names = {FACE_NORTH: "North", FACE_EAST: "East",
                                  FACE_SOUTH: "South", FACE_WEST: "West"}
                    myc.add_write_instruction(
                        write_instr, col, row, 0, hop_cnt,
                        f"Set FACE={face_names[prog.fwd_face]}"
                    )
                    myc.add_data_word(
                        config_value, col, row,
                        f"CONFIG: FWD_FACE={face_names[prog.fwd_face]}"
                    )

                # Program memory contents
                for addr, value in sorted(prog.memory.items()):
                    write_instr = encode_write(hop_cnt, addr, config=False)
                    words.append(write_instr)
                    words.append(value)

                    myc.add_write_instruction(
                        write_instr, col, row, addr, hop_cnt,
                        f"Write memory"
                    )
                    myc.add_data_word(value, col, row, f"MEM[{addr}] = 0x{value:04X}")

        return Bitstream(
            words=words,
            myc_writer=myc,
            chip_name=self.chip_name,
            width=self.width,
            height=self.height
        )

    def generate_routing_only(self) -> Bitstream:
        """
        Generate bitstream for routing-only configuration.

        This creates a simple path from input (0,0) to output (width-1, height-1)
        using row 0 (East) and column width-1 (South).

        Path:
            (0,0) → East → (1,0) → ... → (10,0) → East → (11,0)
                                                           ↓ South (default)
            (11,1) → ... → (11,10) → South → (11,11) → East → OUTPUT

        Returns:
            Bitstream for simple routing test.
        """
        # Clear any existing programs
        self.cell_programs.clear()

        # Row 0 cells (0,0) to (10,0): route East
        # Note: Cell (11,0) defaults to South which is correct for routing down
        for x in range(self.width - 1):  # 0 to 10
            self.add_routing_cell(x, 0, FACE_EAST)

        # Column 11 cells route South (this is the default, no programming needed)
        # But we need to explicitly set (11,11) to route East for output
        self.add_routing_cell(self.width - 1, self.height - 1, FACE_EAST)

        return self.generate(skip_duplicate_row0=True)
