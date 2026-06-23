"""Base class, interface, and shared helpers for Kyttar DSP blocks.

All concrete blocks subclass :class:`KyttarBlock` and live in their own
module under ``gr_kyttar.placement.blocks``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import math
import numpy as np

from ..block import BlockDefinition, CellProgram, Connection, ConnectionType, FilamentMetrics, Port, EntryPoint, StateVar, DataWord

# Import the Rust assembler
try:
    import simkyt
    HAS_KYTTAR = True
except ImportError:
    HAS_KYTTAR = False


def float_to_q15(value: float) -> int:
    """Convert a floating-point value to Q15 fixed-point."""
    scaled = int(round(value * 32768.0))
    if scaled > 32767:
        scaled = 32767
    elif scaled < -32768:
        scaled = -32768
    return scaled & 0xFFFF


def q15_to_float(value: int) -> float:
    """Convert a Q15 fixed-point value to floating-point."""
    if value > 32767:
        value = value - 65536
    return value / 32768.0


def assemble_to_words(assembly: str, base_addr: int = 0) -> List[int]:
    """
    Assemble text assembly into instruction words using the Rust assembler.

    Args:
        assembly: Assembly source code text
        base_addr: Base address for the program

    Returns:
        List of 16-bit instruction words
    """
    if not HAS_KYTTAR:
        raise RuntimeError("kyttar module not available - cannot assemble code")

    program = simkyt.Program.from_source("block", assembly, base_addr)
    return program.get_words()


@dataclass
class BlockInterface:
    """
    Describes a block's interface - where to write data and where to jump.

    Each block registers its interface so the placement system knows how
    to connect blocks together. This is FLEXIBLE - each block decides its
    own layout based on algorithm requirements.

    Conventions (defaults, not enforced):
    - entry_address: R1 (maximizes code space since R0 is accumulator)
    - input_registers: [31] for single input, [31, 30, 29...] for multiple
    """
    entry_address: int = 1  # Default: R1
    input_registers: List[int] = field(default_factory=lambda: [31])  # Default: R31
    output_registers: List[int] = field(default_factory=lambda: [31])  # What we write to next block


class KyttarBlock(ABC):
    """
    Abstract base class for Kyttar DSP blocks.

    Subclasses implement the DSP algorithm by providing:
    - Block definition (name, cell count, connections)
    - Cell programs (memory contents, entry addresses)
    - Block interface (entry address, input/output registers)

    The placement engine uses these to place and route the block
    within the fabric.
    """

    def __init__(self, name: str, **kwargs):
        """
        Initialize the block.

        Args:
            name: Unique name for this block instance
            **kwargs: Block-specific parameters
        """
        self._name = name
        self._kwargs = kwargs
        self._connections: List[Tuple[str, float]] = []  # (target_name, weight)
        self._metrics: Optional[FilamentMetrics] = None

    @property
    def name(self) -> str:
        """Block instance name."""
        return self._name

    @property
    def connections(self) -> List[Tuple[str, float]]:
        """List of connections as (target_name, weight) tuples."""
        return self._connections

    @property
    @abstractmethod
    def cell_count(self) -> int:
        """Number of cells this block requires."""
        pass

    @property
    def interface(self) -> BlockInterface:
        """
        Block interface - entry address and input/output registers.

        Override in subclass if different from defaults.
        Defaults: entry=R1, input=R31, output=R31
        """
        return BlockInterface()

    def connect_to(self, target: 'KyttarBlock', weight: float = 1.0):
        """
        Connect this block's output to another block's input.

        Args:
            target: Target block
            weight: Connection weight for placement optimization
        """
        self._connections.append((target.name, weight))

    def set_metrics(self, metrics: FilamentMetrics):
        """Set placement metrics from simulation."""
        self._metrics = metrics

    def get_metrics(self) -> FilamentMetrics:
        """Get placement metrics (default or from simulation)."""
        if self._metrics is not None:
            return self._metrics
        # Default: balanced I/O
        return FilamentMetrics(input_ratio=0.5, output_ratio=0.5, activity=0.5)

    # Reference implementation for testing
    @abstractmethod
    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation using floating-point.

        Used to validate the Q15 hardware implementation.

        Args:
            input_samples: Input samples as float32

        Returns:
            Output samples as float32
        """
        pass

    # --- New-style block API (v2) ---

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """
        Build new-style cell programs with declarative templates.

        Subclasses override this instead of build_cell_programs() to use
        the new template-based approach. Returns CellProgram objects with
        assembly_template and declarative inputs/outputs/state/data fields.

        The resolver will handle register allocation and WRITE/JUMP resolution.

        Returns:
            Dict mapping cell index to CellProgram (with assembly_template)
        """
        raise NotImplementedError

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """
        Internal data connections for multi-cell blocks.

        Returns list of (src_cell, src_output, dst_cell, dst_input) tuples
        describing how cells within this block are wired together.
        """
        return []

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """
        Internal jump connections for multi-cell blocks.

        Returns list of (src_cell, jump_name, dst_cell, dst_entry) tuples
        describing JUMP wiring between cells within this block.
        """
        return []

    @property
    def is_new_style(self) -> bool:
        """True if this block implements build_cell_programs."""
        try:
            self.build_cell_programs()
            return True
        except NotImplementedError:
            return False

    def get_block_definition(self) -> BlockDefinition:
        """
        Get block definition for new-style blocks (no hop/target params).

        Cell programs are returned with assembly_template set;
        the router resolves them after placement.
        """
        connections = [
            Connection(target=target, weight=weight)
            for target, weight in self._connections
        ]

        return BlockDefinition(
            name=self._name,
            cell_count=self.cell_count,
            connections=connections,
            cell_programs=self.build_cell_programs(),
            internal_connections=self.internal_connections(),
            internal_jumps=self.internal_jumps(),
        )

    # Default serpentine row width for the fallback layout.
    DEFAULT_LAYOUT_WIDTH = 8

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Hand-authored cell layout for this block.

        Returns a dict ``{cell_id: (dx, dy, face)}`` of relative cell offsets
        from the block's anchor at (0, 0). ``face`` is one of
        ``'south'``, ``'east'``, ``'west'``, ``'north'`` and indicates the
        nominal output direction of that cell (toward the next cell in the
        data chain).

        The base implementation is a SERPENTINE (boustrophedon) fallback that
        snakes ``cell_count`` cells within a maximum row width
        (``DEFAULT_LAYOUT_WIDTH``, default 8), so any block fits the array.
        Cells are laid left-to-right on row 0, right-to-left on row 1, and so
        on, wrapping at the width. Faces follow the snake travel direction:
        ``east`` on left-to-right rows, ``west`` on right-to-left rows, and
        ``south`` on the turn-down cell at the end of each row. Cell ids in the
        fallback are the integers ``0 .. cell_count-1``.

        Subclasses with a tuned spatial layout (e.g. DFEEqualizerBlock)
        override this to return their authored arrangement, reusing the SAME
        cell ids that their ``build_cell_programs``/``build_cell_programs``
        emit.

        This must remain cheap: the fallback only needs ``cell_count`` and does
        NOT build any cell programs.
        """
        return self._serpentine_layout(self.cell_count, self.DEFAULT_LAYOUT_WIDTH)

    def output_cell_id(self) -> Optional[Any]:
        """The cell_id (key in ``default_layout``) that this block's OUTPUT leaves
        from. Default ``None`` ⇒ the block outputs from its LAST placed cell (the
        long-standing assumption). Override when the recovered/result signal exits
        a NON-last cell — e.g. a Costas loop outputs the recovered I from its
        ``rotate`` cell, which sits in the MIDDLE of the block (pd_pi + the feedback
        transit cells come after it in the chain). placeKYT uses this to mark the
        output cell in the GUI and to route the block→output-port connection from
        the right cell (so the output WRITE's hop reaches the actual exit)."""
        return None

    @staticmethod
    def _serpentine_layout(cell_count: int, width: int) -> Dict[Any, Tuple[int, int, str]]:
        """Snake ``cell_count`` cells within ``width`` columns.

        Returns ``{int_id: (dx, dy, face)}``. See ``default_layout`` for the
        face convention.
        """
        width = max(1, int(width))
        layout: Dict[Any, Tuple[int, int, str]] = {}
        for i in range(max(0, int(cell_count))):
            row = i // width
            col_in_row = i % width
            left_to_right = (row % 2 == 0)
            dx = col_in_row if left_to_right else (width - 1 - col_in_row)
            dy = row
            is_row_end = (col_in_row == width - 1)
            is_last = (i == cell_count - 1)
            if is_row_end and not is_last:
                # Turn-down cell: hand off to the cell on the next row below.
                face = 'south'
            else:
                face = 'east' if left_to_right else 'west'
            layout[i] = (dx, dy, face)
        return layout


def build_block_chain(blocks: List[KyttarBlock]) -> List[BlockDefinition]:
    """
    Build a chain of connected blocks.

    Connects each block's output to the next block's input.

    Args:
        blocks: List of KyttarBlock instances

    Returns:
        List of BlockDefinitions ready for placement
    """
    # Connect blocks in sequence
    for i in range(len(blocks) - 1):
        blocks[i].connect_to(blocks[i + 1])

    # Get block definitions
    return [block.get_block_definition() for block in blocks]


def get_block_metrics(blocks: List[KyttarBlock]) -> Dict[str, FilamentMetrics]:
    """
    Get metrics for all blocks.

    Args:
        blocks: List of KyttarBlock instances

    Returns:
        Dict mapping block name to FilamentMetrics
    """
    return {block.name: block.get_metrics() for block in blocks}
