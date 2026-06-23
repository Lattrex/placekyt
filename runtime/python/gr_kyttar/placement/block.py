"""
Block Definition for Filament Placement

Defines the structure for blocks (filaments) that need to be placed,
including their connections and metrics from simulation.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum
import yaml


@dataclass(frozen=True)
class Port:
    """Named input or output port for a new-style CellProgram."""
    name: str
    register: Optional[int] = None  # None = auto-allocate


@dataclass(frozen=True)
class EntryPoint:
    """Named entry point for a new-style CellProgram."""
    name: str
    address: Optional[int] = None  # None = first instruction address


@dataclass(frozen=True)
class StateVar:
    """Named state register for a new-style CellProgram."""
    name: str
    register: Optional[int] = None  # None = auto-allocate
    initial_value: int = 0


@dataclass(frozen=True)
class DataWord:
    """Named data word (coefficient, etc.) for a new-style CellProgram."""
    name: str
    value: int  # 16-bit value (Q15 coefficient, etc.)
    address: Optional[int] = None  # None = auto-pack from addr 0
    # True if `value` is a FACE register code (S=0,E=1,W=2,N=3) consumed by an
    # in-program `MOVE [FACE], R{data:...}`. The placer transforms it by the
    # block's orientation (a rotated block's absolute output direction rotates
    # with it). Normal coefficients are orientation-invariant (is_face=False).
    is_face: bool = False


class ConnectionType(Enum):
    """Type of connection between blocks."""
    DATA = "data"       # Normal data flow
    CONTROL = "control" # Control signals (JUMP)
    BIDIRECTIONAL = "bidirectional"  # Ring/feedback


@dataclass
class Connection:
    """
    A connection between two blocks.

    Connections inform the placement engine about data flow requirements.
    Blocks that communicate should be placed close together.
    """
    # Name of the connected block
    target: str

    # Connection type
    connection_type: ConnectionType = ConnectionType.DATA

    # Which port on this block (0 = input, -1 = output, or specific index)
    source_port: int = -1

    # Which port on the target block
    target_port: int = 0

    # Estimated traffic weight (higher = more important to minimize distance)
    weight: float = 1.0


@dataclass
class FilamentMetrics:
    """
    Metrics for a filament, derived from simulation.

    These metrics inform placement decisions:
    - High input_ratio → place near input port
    - High output_ratio → place near output port
    - High activity → place first (gets best positions)
    """
    # Fraction of transactions that are inputs (receives)
    # 1.0 = all input, 0.0 = all output
    input_ratio: float = 0.5

    # Fraction of transactions that are outputs (sends)
    # 1.0 = all output, 0.0 = all input
    output_ratio: float = 0.5

    # This filament's share of total transactions
    # Sum of all filaments' activity = 1.0
    # 0.0 = inactive, 1.0 = 100% of all activity
    activity: float = 0.5

    # Raw counts (for debugging)
    input_transactions: int = 0
    output_transactions: int = 0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'FilamentMetrics':
        """Create from dictionary (e.g., from YAML or JSON)."""
        return cls(
            input_ratio=d.get('input_ratio', 0.5),
            output_ratio=d.get('output_ratio', 0.5),
            activity=d.get('activity', 0.5),
            input_transactions=d.get('input_transactions', 0),
            output_transactions=d.get('output_transactions', 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'input_ratio': self.input_ratio,
            'output_ratio': self.output_ratio,
            'activity': self.activity,
            'input_transactions': self.input_transactions,
            'output_transactions': self.output_transactions,
        }


# Default metrics when no simulation data is available
DEFAULT_METRICS = FilamentMetrics(
    input_ratio=0.5,
    output_ratio=0.5,
    activity=0.5,
)


@dataclass
class CellProgram:
    """
    Program and data for a single cell within a block.

    This contains everything needed to program one cell of a block:
    - memory: Pre-loaded memory contents (coefficients, instructions)
    - entry_addr: Where JUMP execution starts (typically 16 for code)
    - fwd_face: Output direction (0=S, 1=E, 2=W, 3=N) - can be overridden by router
    """
    # --- Existing fields (unchanged) ---
    memory: Dict[int, int] = field(default_factory=dict)  # addr -> value
    entry_addr: Optional[int] = None  # Entry point for JUMP
    fwd_face: Optional[int] = None  # Output face (None = let router decide)
    # --- New fields (optional, used by new-style blocks) ---
    inputs: List[Port] = field(default_factory=list)
    outputs: List[Port] = field(default_factory=list)
    entries: List[EntryPoint] = field(default_factory=list)
    state: List[StateVar] = field(default_factory=list)
    data: List[DataWord] = field(default_factory=list)
    assembly_template: str = ""  # Non-empty = new-style block

    def set_memory(self, addr: int, value: int):
        """Set a memory word."""
        self.memory[addr] = value & 0xFFFF

    def set_program(self, start_addr: int, instructions: List[int]):
        """Load a program starting at the given address."""
        for i, instr in enumerate(instructions):
            self.memory[start_addr + i] = instr & 0xFFFF
        self.entry_addr = start_addr


@dataclass
class BlockDefinition:
    """
    Definition of a block (filament) to be placed.

    A block represents a logical unit of processing that occupies
    one or more cells in the fabric. It includes both placement metadata
    and the actual cell programs.
    """
    # Unique name for this block
    name: str

    # Number of cells this block needs
    cell_count: int

    # Connections to other blocks
    connections: List[Connection] = field(default_factory=list)

    # Cell programs: index -> CellProgram (0 = entry cell, -1 or last = exit cell)
    # If not provided, cells are routing-only
    cell_programs: Dict[int, CellProgram] = field(default_factory=dict)

    # Explicit INTERNAL (cell-to-cell, within this block) routing overrides.
    # Each is (src_cell_id, src_output_port, dst_cell_id, dst_input_port). Used
    # by the router to resolve a non-linear handoff instead of its default
    # "next cell in dict order" inference — e.g. the DFE's ff20 -> lock-driver
    # -> dc path. Cell ids are cell_programs keys (int or str). Empty = the
    # router uses its positional default for every output.
    internal_connections: List[Tuple] = field(default_factory=list)
    internal_jumps: List[Tuple] = field(default_factory=list)

    # Optional: preferred anchor position (for manual placement)
    preferred_anchor: Optional[tuple] = None

    # Optional: preferred shape name (for manual placement)
    preferred_shape: Optional[str] = None

    # Optional: rotation in degrees (0, 90, 180, 270)
    rotation: int = 0

    # Whether this block is an I/O port (fixed at edge)
    is_io_port: bool = False

    # If I/O port, which edge (0=top, 1=right, 2=bottom, 3=left)
    io_edge: Optional[int] = None

    # When True, this block's OUTPUT leaves a cell that ALSO carries internal
    # handoff WRITEs (a mid-block output cell — e.g. a Costas loop's rotate cell
    # writes yi -> the phase detector AND yi_tap -> the output). The block emits
    # the output WRITE LAST, so the Router's exit-cell fixup must patch ONLY the
    # last WRITE (the output one) and leave the earlier internal-handoff WRITEs
    # at their resolved @1 hops. Default False ⇒ the exit cell is output-only and
    # every WRITE is rewritten to the output hop (the long-standing behaviour).
    output_at_last_write: bool = False

    def set_cell_program(self, cell_index: int, program: CellProgram):
        """Set the program for a specific cell in this block."""
        if cell_index < 0:
            cell_index = self.cell_count + cell_index  # -1 = last cell
        self.cell_programs[cell_index] = program

    def get_cell_program(self, cell_index: int) -> Optional[CellProgram]:
        """Get the program for a specific cell, or None if not set."""
        if cell_index < 0:
            cell_index = self.cell_count + cell_index
        return self.cell_programs.get(cell_index)

    def add_connection(self, target: str, weight: float = 1.0,
                       connection_type: ConnectionType = ConnectionType.DATA) -> None:
        """Add a connection to another block."""
        self.connections.append(Connection(
            target=target,
            connection_type=connection_type,
            weight=weight,
        ))

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'BlockDefinition':
        """Create from dictionary (e.g., from YAML)."""
        connections = []
        for conn_dict in d.get('connections', []):
            conn_type = ConnectionType(conn_dict.get('type', 'data'))
            connections.append(Connection(
                target=conn_dict['target'],
                connection_type=conn_type,
                weight=conn_dict.get('weight', 1.0),
                source_port=conn_dict.get('source_port', -1),
                target_port=conn_dict.get('target_port', 0),
            ))

        return cls(
            name=d['name'],
            cell_count=d['cell_count'],
            connections=connections,
            preferred_anchor=tuple(d['anchor']) if 'anchor' in d else None,
            preferred_shape=d.get('shape'),
            rotation=d.get('rotation', 0),
            is_io_port=d.get('is_io_port', False),
            io_edge=d.get('io_edge'),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        d = {
            'name': self.name,
            'cell_count': self.cell_count,
        }
        if self.connections:
            d['connections'] = [
                {
                    'target': c.target,
                    'type': c.connection_type.value,
                    'weight': c.weight,
                    'source_port': c.source_port,
                    'target_port': c.target_port,
                }
                for c in self.connections
            ]
        if self.preferred_anchor:
            d['anchor'] = list(self.preferred_anchor)
        if self.preferred_shape:
            d['shape'] = self.preferred_shape
        if self.rotation != 0:
            d['rotation'] = self.rotation
        if self.is_io_port:
            d['is_io_port'] = True
            d['io_edge'] = self.io_edge
        return d


def load_blocks_from_yaml(path: str) -> List[BlockDefinition]:
    """
    Load block definitions from a YAML file.

    Expected format:
    ```yaml
    blocks:
      - name: "SSB Receiver 1"
        cell_count: 5
        connections:
          - target: "SSB Receiver 2"
            weight: 0.5
      - name: "SSB Receiver 2"
        cell_count: 5
    ```
    """
    with open(path, 'r') as f:
        data = yaml.safe_load(f)

    blocks = []
    for block_dict in data.get('blocks', []):
        blocks.append(BlockDefinition.from_dict(block_dict))

    return blocks


def load_metrics_from_yaml(path: str) -> Dict[str, FilamentMetrics]:
    """
    Load metrics from a YAML file (exported from simulation).

    Expected format:
    ```yaml
    simulation_info:
      total_input_transactions: 1000
      total_output_transactions: 800
    filament_metrics:
      SSB_Receiver_1:
        input_ratio: 0.6
        output_ratio: 0.4
        activity: 0.3
    ```
    """
    with open(path, 'r') as f:
        data = yaml.safe_load(f)

    metrics = {}
    for name, m_dict in data.get('filament_metrics', {}).items():
        metrics[name] = FilamentMetrics.from_dict(m_dict)

    return metrics


if __name__ == '__main__':
    # Test block creation
    block = BlockDefinition(
        name="SSB Receiver",
        cell_count=5,
    )
    block.add_connection("Audio Output", weight=2.0)
    print(f"Block: {block}")
    print(f"YAML: {yaml.dump(block.to_dict())}")

    # Test metrics
    metrics = FilamentMetrics(
        input_ratio=0.7,
        output_ratio=0.3,
        activity=0.4,
        input_transactions=700,
        output_transactions=300,
    )
    print(f"Metrics: {metrics}")
