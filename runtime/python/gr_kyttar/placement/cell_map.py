"""
Cell Map: Complete programming specification for all cells.

This is the interface between the placement/routing engine and the bitstream generator.
The cell map contains everything needed to program the chip:
- Cell positions
- FACE configurations (for routing)
- Memory contents (for programs and data)
- Entry points (where JUMPs start execution)
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List
from enum import IntEnum


class Face(IntEnum):
    """Cell face directions for routing."""
    SOUTH = 0
    EAST = 1
    WEST = 2
    NORTH = 3


@dataclass
class CellConfig:
    """
    Complete configuration for a single cell.

    This is everything needed to program one cell:
    - fwd_face: Which direction to forward data/jumps
    - memory: Memory contents (addr -> value)
    - entry_addr: Where JUMP starts execution (if this cell runs a program)
    - block_name: Which block this cell belongs to (for debugging)
    - cell_index: Index within the block (0 = entry cell)
    """
    fwd_face: Face = Face.SOUTH  # Default: route South
    memory: Dict[int, int] = field(default_factory=dict)  # addr -> value
    entry_addr: Optional[int] = None  # Entry point for JUMP execution
    block_name: str = ""  # For debugging/visualization
    cell_index: int = 0  # Position within block (0 = first/entry)

    def set_memory(self, addr: int, value: int):
        """Set a memory word."""
        self.memory[addr] = value & 0xFFFF

    def set_program(self, start_addr: int, instructions: List[int]):
        """Load a program starting at the given address."""
        for i, instr in enumerate(instructions):
            self.memory[start_addr + i] = instr & 0xFFFF
        self.entry_addr = start_addr

    def is_routing_only(self) -> bool:
        """True if this cell is just for routing (no program)."""
        return self.entry_addr is None and len(self.memory) == 0


@dataclass
class CellMap:
    """
    Complete specification of all cells to be programmed.

    This is the output of the placement engine and input to the bitstream generator.
    It contains the complete configuration for every cell that needs programming.
    """
    # Cell configurations: (col, row) -> CellConfig
    cells: Dict[Tuple[int, int], CellConfig] = field(default_factory=dict)

    # Array dimensions (for bounds checking)
    width: int = 12
    height: int = 12

    def set_cell(self, col: int, row: int, config: CellConfig):
        """Set the configuration for a cell."""
        if not (0 <= col < self.width and 0 <= row < self.height):
            raise ValueError(f"Cell ({col},{row}) out of bounds for {self.width}x{self.height} array")
        self.cells[(col, row)] = config

    def get_cell(self, col: int, row: int) -> Optional[CellConfig]:
        """Get the configuration for a cell, or None if not configured."""
        return self.cells.get((col, row))

    def add_routing_cell(self, col: int, row: int, face: Face, block_name: str = "_routing"):
        """Add a simple routing cell that forwards in the given direction."""
        self.cells[(col, row)] = CellConfig(
            fwd_face=face,
            block_name=block_name,
        )

    def add_routing_path(self, from_pos: Tuple[int, int], to_pos: Tuple[int, int],
                         block_name: str = "_routing"):
        """
        Add routing cells to create a Manhattan path between two positions.

        Routes horizontally first (East/West), then vertically (North/South).
        Does NOT set the destination cell's face (caller should do that).
        """
        fx, fy = from_pos
        tx, ty = to_pos
        x, y = fx, fy

        # Horizontal routing
        while x != tx:
            face = Face.EAST if tx > x else Face.WEST
            self.add_routing_cell(x, y, face, block_name)
            x += 1 if tx > x else -1

        # Vertical routing
        while y != ty:
            face = Face.SOUTH if ty > y else Face.NORTH
            self.add_routing_cell(x, y, face, block_name)
            y += 1 if ty > y else -1

    def cell_count(self) -> int:
        """Number of cells that need programming."""
        return len(self.cells)

    def routing_cell_count(self) -> int:
        """Number of routing-only cells."""
        return sum(1 for c in self.cells.values() if c.is_routing_only())

    def program_cell_count(self) -> int:
        """Number of cells with programs."""
        return sum(1 for c in self.cells.values() if not c.is_routing_only())

    def to_dict(self) -> dict:
        """Export to dictionary (for debugging/serialization)."""
        return {
            'width': self.width,
            'height': self.height,
            'cells': {
                f"{col},{row}": {
                    'face': config.fwd_face.name,
                    'memory': {str(a): v for a, v in config.memory.items()},
                    'entry_addr': config.entry_addr,
                    'block': config.block_name,
                    'index': config.cell_index,
                }
                for (col, row), config in self.cells.items()
            }
        }

    def __repr__(self) -> str:
        return f"CellMap({self.width}x{self.height}, {len(self.cells)} cells)"
