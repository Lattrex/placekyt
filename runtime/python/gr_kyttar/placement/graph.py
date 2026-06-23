"""
Graph Data Structures for Kyttar Placement/Routing

This module provides the graph abstractions used by the placement and routing
engine to represent GRC topology and block connectivity.

ConnectionGraph: Raw GRC topology with port numbers
BlockGraph: Block-level connectivity with channel information
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional, Iterator
from collections import defaultdict


@dataclass
class ConnectionGraph:
    """
    Represents raw GRC connectivity with port numbers.

    Edges are stored as (src_block, src_port, dst_block, dst_port) tuples.
    Port numbers are crucial for demux/mux channel mapping.

    Example edge_list:
        kyttar_source_0:0 -> kyttar_demux_0:0
        kyttar_demux_0:0 -> kyttar_gain_i:0
        kyttar_demux_0:1 -> kyttar_gain_q:0
        kyttar_gain_i:0 -> kyttar_mux_0:0
        kyttar_gain_q:0 -> kyttar_mux_0:1
        kyttar_mux_0:0 -> kyttar_sink_0:0
    """
    edges: List[Tuple[str, int, str, int]] = field(default_factory=list)

    # Adjacency structures (lazily built)
    _outgoing: Dict[str, List[Tuple[int, str, int]]] = field(default_factory=lambda: defaultdict(list))
    _incoming: Dict[str, List[Tuple[str, int, int]]] = field(default_factory=lambda: defaultdict(list))
    _nodes: Set[str] = field(default_factory=set)

    def add_edge(self, src_block: str, src_port: int, dst_block: str, dst_port: int):
        """Add an edge to the graph."""
        self.edges.append((src_block, src_port, dst_block, dst_port))
        self._outgoing[src_block].append((src_port, dst_block, dst_port))
        self._incoming[dst_block].append((src_block, src_port, dst_port))
        self._nodes.add(src_block)
        self._nodes.add(dst_block)

    @property
    def nodes(self) -> Set[str]:
        """Get all node names."""
        return self._nodes

    def get_outgoing(self, block: str) -> List[Tuple[int, str, int]]:
        """
        Get outgoing edges from a block.

        Returns: List of (src_port, dst_block, dst_port) tuples
        """
        return self._outgoing.get(block, [])

    def get_incoming(self, block: str) -> List[Tuple[str, int, int]]:
        """
        Get incoming edges to a block.

        Returns: List of (src_block, src_port, dst_port) tuples
        """
        return self._incoming.get(block, [])

    def get_sources(self) -> List[str]:
        """Get blocks with no incoming edges (source blocks)."""
        return [n for n in self._nodes if not self._incoming.get(n)]

    def get_sinks(self) -> List[str]:
        """Get blocks with no outgoing edges (sink blocks)."""
        return [n for n in self._nodes if not self._outgoing.get(n)]


def parse_block_port(s: str) -> Tuple[str, int]:
    """
    Parse a block:port string.

    Args:
        s: String like "kyttar_demux_0:1"

    Returns:
        Tuple of (block_name, port_number)
    """
    s = s.strip()
    if ':' not in s:
        return (s, 0)  # Default to port 0 if not specified

    parts = s.rsplit(':', 1)  # rsplit to handle colons in names
    try:
        port = int(parts[1])
    except ValueError:
        port = 0
    return (parts[0], port)


def parse_edge_list(edge_list: str) -> ConnectionGraph:
    """
    Parse a GRC edge_list string into a ConnectionGraph.

    Args:
        edge_list: Multi-line string from top_block.edge_list()

    Returns:
        ConnectionGraph with all edges and port numbers

    Example input:
        kyttar_source_0:0->kyttar_demux_0:0
        kyttar_demux_0:0->kyttar_gain_i:0
        kyttar_demux_0:1->kyttar_gain_q:0
    """
    graph = ConnectionGraph()

    for line in edge_list.strip().split('\n'):
        line = line.strip()
        if '->' not in line:
            continue

        # Split on arrow
        parts = line.split('->')
        if len(parts) != 2:
            continue

        src_block, src_port = parse_block_port(parts[0])
        dst_block, dst_port = parse_block_port(parts[1])

        graph.add_edge(src_block, src_port, dst_block, dst_port)

    return graph


@dataclass
class BlockEdge:
    """
    An edge in the BlockGraph.

    Contains channel information for demux/mux routing.
    """
    src_block: str
    src_port: int  # For demux: which output port (= channel)
    dst_block: str
    dst_port: int  # For mux: which input port (= channel)
    channel: int   # Derived channel number

    def __hash__(self):
        return hash((self.src_block, self.src_port, self.dst_block, self.dst_port))


class BlockGraph:
    """
    Block-level connectivity graph with channel information.

    This is built from ConnectionGraph but contains:
    - References to actual KyttarBlock instances
    - Channel information for demux/mux routing
    - Methods for topological sorting and traversal
    """

    def __init__(self):
        self.nodes: Dict[str, 'KyttarBlock'] = {}
        self.edges: List[BlockEdge] = []
        self._outgoing: Dict[str, List[BlockEdge]] = defaultdict(list)
        self._incoming: Dict[str, List[BlockEdge]] = defaultdict(list)

    def add_node(self, name: str, block: 'KyttarBlock'):
        """Add a block to the graph."""
        self.nodes[name] = block

    def add_edge(
        self,
        src_name: str,
        src_port: int,
        dst_name: str,
        dst_port: int,
        channel: int,
    ):
        """
        Add an edge between blocks.

        Args:
            src_name: Source block name
            src_port: Source output port number
            dst_name: Destination block name
            dst_port: Destination input port number
            channel: Channel number for this connection
        """
        edge = BlockEdge(
            src_block=src_name,
            src_port=src_port,
            dst_block=dst_name,
            dst_port=dst_port,
            channel=channel,
        )
        self.edges.append(edge)
        self._outgoing[src_name].append(edge)
        self._incoming[dst_name].append(edge)

    def get_block(self, name: str) -> Optional['KyttarBlock']:
        """Get a block by name."""
        return self.nodes.get(name)

    def get_outputs(self, name: str) -> List[BlockEdge]:
        """Get outgoing edges from a block."""
        return self._outgoing.get(name, [])

    def get_inputs(self, name: str) -> List[BlockEdge]:
        """Get incoming edges to a block."""
        return self._incoming.get(name, [])

    def get_sources(self) -> List[str]:
        """Get blocks with no incoming edges (source blocks)."""
        return [n for n in self.nodes if not self._incoming.get(n)]

    def get_sinks(self) -> List[str]:
        """Get blocks with no outgoing edges (sink blocks)."""
        return [n for n in self.nodes if not self._outgoing.get(n)]

    def topological_sort(self) -> List[str]:
        """
        Return blocks in topological order (sources first, sinks last).

        Raises:
            ValueError: If graph contains a cycle
        """
        # Kahn's algorithm
        in_degree = {n: 0 for n in self.nodes}
        for edge in self.edges:
            if edge.dst_block in in_degree:
                in_degree[edge.dst_block] += 1

        # Queue of nodes with no incoming edges
        queue = [n for n, deg in in_degree.items() if deg == 0]
        result = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            for edge in self._outgoing.get(node, []):
                if edge.dst_block in in_degree:
                    in_degree[edge.dst_block] -= 1
                    if in_degree[edge.dst_block] == 0:
                        queue.append(edge.dst_block)

        if len(result) != len(self.nodes):
            raise ValueError("Graph contains a cycle")

        return result

    def iter_edges(self) -> Iterator[BlockEdge]:
        """Iterate over all edges."""
        return iter(self.edges)


def is_demux_block(block) -> bool:
    """Check if a block is a demux (by checking for DemuxInterface or class name)."""
    # Check by class name to avoid circular imports
    cls_name = type(block).__name__
    if 'Demux' in cls_name:
        return True
    # Also check for interface type
    if hasattr(block, 'interface'):
        iface_name = type(block.interface).__name__
        return 'Demux' in iface_name
    return False


def is_mux_block(block) -> bool:
    """Check if a block is a mux (by checking for MuxInterface or class name)."""
    cls_name = type(block).__name__
    if 'Mux' in cls_name:
        return True
    if hasattr(block, 'interface'):
        iface_name = type(block.interface).__name__
        return 'Mux' in iface_name
    return False


def build_block_graph(
    conn_graph: ConnectionGraph,
    blocks: Dict[str, 'KyttarBlock'],
) -> BlockGraph:
    """
    Build a BlockGraph from a ConnectionGraph and block instances.

    This determines channel numbers based on block types:
    - For demux: src_port is the channel number
    - For mux: dst_port is the channel number
    - For regular blocks: channel is 0

    Args:
        conn_graph: Parsed GRC topology
        blocks: Map of symbol name to KyttarBlock instance

    Returns:
        BlockGraph with channel-annotated edges
    """
    graph = BlockGraph()

    # Add all blocks as nodes
    for name, block in blocks.items():
        graph.add_node(name, block)

    # Add edges with channel information
    for src_name, src_port, dst_name, dst_port in conn_graph.edges:
        src_block = blocks.get(src_name)
        dst_block = blocks.get(dst_name)

        # Skip edges that aren't between Kyttar blocks
        if src_block is None or dst_block is None:
            continue

        # Determine channel number
        # For demux: the source port indicates which channel
        # For mux: the destination port indicates which channel
        # For regular blocks: use port 0 (single channel)
        if is_demux_block(src_block):
            channel = src_port
        elif is_mux_block(dst_block):
            channel = dst_port
        else:
            channel = 0

        graph.add_edge(src_name, src_port, dst_name, dst_port, channel)

    return graph


# Face constants (matching demux_block.py and mux_block.py)
FACE_SOUTH = 0
FACE_EAST = 1
FACE_WEST = 2
FACE_NORTH = 3

# Reverse direction lookup
OPPOSITE_FACE = {
    FACE_SOUTH: FACE_NORTH,
    FACE_NORTH: FACE_SOUTH,
    FACE_EAST: FACE_WEST,
    FACE_WEST: FACE_EAST,
}


def get_face_direction(from_pos: Tuple[int, int], to_pos: Tuple[int, int]) -> Optional[int]:
    """
    Get the face direction from one position toward another.

    For Manhattan routing, determines the primary direction.
    If positions are equal, returns None.
    Prefers horizontal (East/West) over vertical when both are needed.

    Args:
        from_pos: (col, row) of source
        to_pos: (col, row) of destination

    Returns:
        Face constant (FACE_EAST, etc.) or None if same position
    """
    dx = to_pos[0] - from_pos[0]
    dy = to_pos[1] - from_pos[1]

    if dx == 0 and dy == 0:
        return None

    # Prefer horizontal movement first
    if dx > 0:
        return FACE_EAST
    elif dx < 0:
        return FACE_WEST
    elif dy > 0:
        return FACE_SOUTH
    else:
        return FACE_NORTH


def manhattan_distance(a: Tuple[int, int], b: Tuple[int, int]) -> int:
    """Calculate Manhattan distance between two positions."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
