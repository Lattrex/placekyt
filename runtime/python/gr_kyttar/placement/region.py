"""
Region Definition for Coarse Placement

Regions are rectangular areas of the fabric assigned during coarse placement.
Fine placement fits shapes within these regions.
"""

from dataclasses import dataclass, field
from typing import Tuple, Set, List, Optional, Dict
from enum import Enum


class PortDirection(Enum):
    """Direction of a port (input or output)."""
    INPUT = "input"
    OUTPUT = "output"


class Face(Enum):
    """Cell face direction."""
    NORTH = 0
    EAST = 1
    SOUTH = 2
    WEST = 3

    @classmethod
    def from_string(cls, s: str) -> 'Face':
        """Convert string to Face."""
        return cls[s.upper()]


@dataclass
class PortConfig:
    """
    Configuration for a physical I/O port on the chip.

    Each port connects to a specific cell face and can transfer data
    in or out of the chip.
    """
    # Port name (e.g., "x16_in", "x1_out")
    name: str

    # Direction: input or output
    direction: PortDirection

    # Cell position this port connects to (col, row)
    cell: Tuple[int, int]

    # Which face of the cell the port connects to
    face: Face

    # Data width in bits (1 for x1, 16 for x16)
    width: int = 16

    # Physical pin count (data + handshake)
    pins: int = 18

    # Optional description
    description: str = ""

    @property
    def is_input(self) -> bool:
        return self.direction == PortDirection.INPUT

    @property
    def is_output(self) -> bool:
        return self.direction == PortDirection.OUTPUT

    @classmethod
    def from_dict(cls, name: str, d: Dict) -> 'PortConfig':
        """Create from dictionary (e.g., from YAML)."""
        direction = PortDirection(d['type'])
        cell = tuple(d['cell'])
        face = Face.from_string(d['face'])
        return cls(
            name=name,
            direction=direction,
            cell=cell,
            face=face,
            width=d.get('width', 16),
            pins=d.get('pins', 18),
            description=d.get('description', ''),
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'type': self.direction.value,
            'cell': list(self.cell),
            'face': self.face.name.lower(),
            'width': self.width,
            'pins': self.pins,
            'description': self.description,
        }


@dataclass
class Region:
    """
    A rectangular region of the fabric.

    Used during coarse placement to assign blocks to areas of the chip.
    """
    # Top-left anchor position (col, row)
    anchor: Tuple[int, int]

    # Width and height in cells
    width: int
    height: int

    @property
    def area(self) -> int:
        """Total cells in this region."""
        return self.width * self.height

    @property
    def min_col(self) -> int:
        return self.anchor[0]

    @property
    def min_row(self) -> int:
        return self.anchor[1]

    @property
    def max_col(self) -> int:
        return self.anchor[0] + self.width - 1

    @property
    def max_row(self) -> int:
        return self.anchor[1] + self.height - 1

    def contains(self, col: int, row: int) -> bool:
        """Check if a cell position is within this region."""
        return (self.min_col <= col <= self.max_col and
                self.min_row <= row <= self.max_row)

    def contains_all(self, cells: List[Tuple[int, int]]) -> bool:
        """Check if all cell positions are within this region."""
        return all(self.contains(c, r) for c, r in cells)

    def overlaps(self, other: 'Region') -> bool:
        """Check if this region overlaps with another."""
        return not (
            self.max_col < other.min_col or
            other.max_col < self.min_col or
            self.max_row < other.min_row or
            other.max_row < self.min_row
        )

    def all_cells(self) -> List[Tuple[int, int]]:
        """Return all cell positions in this region."""
        cells = []
        for col in range(self.min_col, self.max_col + 1):
            for row in range(self.min_row, self.max_row + 1):
                cells.append((col, row))
        return cells

    def distance_to_edge(self, edge: int, fabric_width: int, fabric_height: int) -> float:
        """
        Distance from region center to a fabric edge.

        Args:
            edge: 0=top, 1=right, 2=bottom, 3=left
            fabric_width: Total fabric width
            fabric_height: Total fabric height

        Returns:
            Distance in cells
        """
        center_col = self.anchor[0] + self.width / 2
        center_row = self.anchor[1] + self.height / 2

        if edge == 0:  # Top
            return center_row
        elif edge == 1:  # Right
            return fabric_width - center_col
        elif edge == 2:  # Bottom
            return fabric_height - center_row
        elif edge == 3:  # Left
            return center_col
        else:
            raise ValueError(f"Invalid edge: {edge}")

    def __repr__(self) -> str:
        return f"Region(anchor={self.anchor}, size={self.width}x{self.height})"


@dataclass
class ArrayConfig:
    """
    Configuration for the cell array.

    Supports multiple named I/O ports for complex chip configurations.
    Maintains backward compatibility with single input/output port interface.
    """
    # Fabric dimensions
    width: int
    height: int

    # Legacy: Input port position (edge, offset)
    # edge: 0=top, 1=right, 2=bottom, 3=left
    input_port_edge: int = 3  # Left edge
    input_port_offset: int = 0  # First cell

    # Legacy: Output port position
    output_port_edge: int = 1  # Right edge
    output_port_offset: int = 0  # First cell

    # Multi-port support: Dictionary of named ports
    # If empty, falls back to legacy single input/output
    ports: Dict[str, PortConfig] = field(default_factory=dict)

    @property
    def total_cells(self) -> int:
        return self.width * self.height

    def input_position(self) -> Tuple[int, int]:
        """Get default input port cell position (legacy interface)."""
        # If we have named ports, return the first input port
        for port in self.ports.values():
            if port.is_input:
                return port.cell
        # Fall back to legacy
        return self._edge_position(self.input_port_edge, self.input_port_offset)

    def output_position(self) -> Tuple[int, int]:
        """Get default output port cell position (legacy interface)."""
        # If we have named ports, return the first output port
        for port in self.ports.values():
            if port.is_output:
                return port.cell
        # Fall back to legacy
        return self._edge_position(self.output_port_edge, self.output_port_offset)

    def get_port(self, name: str) -> Optional[PortConfig]:
        """Get a port by name."""
        return self.ports.get(name)

    def get_input_ports(self) -> List[PortConfig]:
        """Get all input ports."""
        return [p for p in self.ports.values() if p.is_input]

    def get_output_ports(self) -> List[PortConfig]:
        """Get all output ports."""
        return [p for p in self.ports.values() if p.is_output]

    def get_port_position(self, port_name: str) -> Tuple[int, int]:
        """Get cell position for a named port."""
        port = self.ports.get(port_name)
        if port is None:
            raise ValueError(f"Unknown port: {port_name}")
        return port.cell

    def get_port_face(self, port_name: str) -> Face:
        """Get face direction for a named port."""
        port = self.ports.get(port_name)
        if port is None:
            raise ValueError(f"Unknown port: {port_name}")
        return port.face

    def add_port(self, port: PortConfig) -> None:
        """Add a port configuration."""
        self.ports[port.name] = port

    def _edge_position(self, edge: int, offset: int) -> Tuple[int, int]:
        """Get cell position at an edge (legacy helper)."""
        if edge == 0:  # Top
            return (offset, 0)
        elif edge == 1:  # Right
            return (self.width - 1, offset)
        elif edge == 2:  # Bottom
            return (offset, self.height - 1)
        elif edge == 3:  # Left
            return (0, offset)
        else:
            raise ValueError(f"Invalid edge: {edge}")

    def edge_cells(self, edge: int) -> List[Tuple[int, int]]:
        """Get all cells along an edge."""
        if edge == 0:  # Top
            return [(col, 0) for col in range(self.width)]
        elif edge == 1:  # Right
            return [(self.width - 1, row) for row in range(self.height)]
        elif edge == 2:  # Bottom
            return [(col, self.height - 1) for col in range(self.width)]
        elif edge == 3:  # Left
            return [(0, row) for row in range(self.height)]
        else:
            raise ValueError(f"Invalid edge: {edge}")

    def all_cells(self) -> Set[Tuple[int, int]]:
        """Get set of all cell positions."""
        return {(col, row) for col in range(self.width) for row in range(self.height)}

    @classmethod
    def from_dict(cls, d: Dict) -> 'ArrayConfig':
        """Create from dictionary (e.g., from chip config YAML)."""
        # Handle different YAML structures
        # New format: fabric.width/height
        # Legacy format: width/height or array.width/height
        fabric = d.get('fabric', {})
        config = cls(
            width=fabric.get('width', d.get('width', d.get('array', {}).get('width', 12))),
            height=fabric.get('height', d.get('height', d.get('array', {}).get('height', 12))),
        )

        # Load ports if present
        ports_data = d.get('ports', {})

        if isinstance(ports_data, dict):
            # Dict format: {"port_name": {...}}
            for name, port_data in ports_data.items():
                config.add_port(PortConfig.from_dict(name, port_data))
        elif isinstance(ports_data, list):
            # List format: [{"name": "port_name", ...}, ...]
            for port_entry in ports_data:
                name = port_entry.get('name', 'unnamed')
                # Convert list format to dict format expected by PortConfig
                port_data = {
                    'type': port_entry.get('direction', 'input'),
                    'width': port_entry.get('width', 16),
                    'pins': port_entry.get('pins', port_entry.get('width', 16) + 2),
                    'description': port_entry.get('description', ''),
                }
                # Handle cell position
                cell = port_entry.get('cell', {})
                if isinstance(cell, dict):
                    port_data['cell'] = [cell.get('x', 0), cell.get('y', 0)]
                    port_data['face'] = cell.get('face', 'west')
                else:
                    port_data['cell'] = cell
                    port_data['face'] = 'west'
                config.add_port(PortConfig.from_dict(name, port_data))

        return config

    @classmethod
    def from_yaml(cls, yaml_path: str) -> 'ArrayConfig':
        """Load from YAML config file."""
        import yaml
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)


def divide_into_regions(
    config: ArrayConfig,
    num_regions: int,
    input_bias: float = 0.5,  # 0 = output side, 1 = input side
) -> List[Region]:
    """
    Divide the fabric into roughly equal regions.

    Args:
        config: Array configuration
        num_regions: Number of regions to create
        input_bias: Where to start dividing (0.5 = center)

    Returns:
        List of regions from input side to output side
    """
    if num_regions <= 0:
        return []

    if num_regions == 1:
        return [Region((0, 0), config.width, config.height)]

    # For simplicity, divide horizontally into strips
    # (Input on left, output on right, divide into columns)
    regions = []
    cells_per_region = config.width // num_regions
    remainder = config.width % num_regions

    col = 0
    for i in range(num_regions):
        width = cells_per_region + (1 if i < remainder else 0)
        regions.append(Region((col, 0), width, config.height))
        col += width

    return regions


if __name__ == '__main__':
    # Test region operations
    config = ArrayConfig(width=20, height=20)
    print(f"Array: {config.width}x{config.height} = {config.total_cells} cells")
    print(f"Input position: {config.input_position()}")
    print(f"Output position: {config.output_position()}")

    # Test region division
    regions = divide_into_regions(config, 4)
    print(f"\nDivided into {len(regions)} regions:")
    for r in regions:
        print(f"  {r}")
