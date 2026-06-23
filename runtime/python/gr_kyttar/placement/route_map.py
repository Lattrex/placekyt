"""
Route Map for Kyttar Placement/Routing

Computes routes between blocks, determines hop counts, and tracks
routing cells needed for multi-hop connections.

This module handles the spatial routing for both linear chains and
branching topologies (demux/mux patterns).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set, Iterator
import heapq

from .graph import (
    BlockGraph, BlockEdge,
    get_face_direction, manhattan_distance,
    FACE_SOUTH, FACE_EAST, FACE_WEST, FACE_NORTH,
)


class RoutingError(Exception):
    """Raised when routing fails."""
    pass


@dataclass
class Route:
    """
    A route between two blocks (or block to port).

    Attributes:
        src_name: Source block name (or None for input port)
        channel: Channel number for this route
        dst_name: Destination block name (or None for output port)
        routing_cells: List of (position, face) for intermediate cells
        hop_count: Total number of hops from source exit to dest entry
    """
    src_name: Optional[str]
    channel: int
    dst_name: Optional[str]
    routing_cells: List[Tuple[Tuple[int, int], int]]  # [(pos, face), ...]
    hop_count: int

    def __repr__(self):
        return (
            f"Route({self.src_name}:{self.channel} -> {self.dst_name}, "
            f"hops={self.hop_count}, routing_cells={len(self.routing_cells)})"
        )


class RouteMap:
    """
    Contains all routes computed for a placement.

    Provides methods to:
    - Look up hop count for a specific connection
    - Iterate over all routing cells with their faces
    - Get routes by source or destination
    """

    def __init__(self):
        self.routes: List[Route] = []
        self._by_src: Dict[str, List[Route]] = {}
        self._by_dst: Dict[str, List[Route]] = {}
        self._by_src_channel: Dict[Tuple[str, int], Route] = {}

    def add_route(self, route: Route):
        """Add a route to the map."""
        self.routes.append(route)

        if route.src_name:
            if route.src_name not in self._by_src:
                self._by_src[route.src_name] = []
            self._by_src[route.src_name].append(route)
            self._by_src_channel[(route.src_name, route.channel)] = route

        if route.dst_name:
            if route.dst_name not in self._by_dst:
                self._by_dst[route.dst_name] = []
            self._by_dst[route.dst_name].append(route)

    def get_routes_from(self, src_name: str) -> List[Route]:
        """Get all routes originating from a block."""
        return self._by_src.get(src_name, [])

    def get_routes_to(self, dst_name: str) -> List[Route]:
        """Get all routes arriving at a block."""
        return self._by_dst.get(dst_name, [])

    def get_route(self, src_name: str, channel: int = 0) -> Optional[Route]:
        """Get the route from a source block on a specific channel."""
        return self._by_src_channel.get((src_name, channel))

    def get_hop_count(self, src_name: str, channel: int = 0) -> int:
        """Get the hop count for a route from source on channel."""
        route = self.get_route(src_name, channel)
        return route.hop_count if route else 1

    def get_output_hop_count(self, block_name: str) -> int:
        """Get the hop count to the output target."""
        routes = self.get_routes_from(block_name)
        if routes:
            return routes[0].hop_count
        return 1

    def get_target_name(self, src_name: str, channel: int = 0) -> Optional[str]:
        """Get the destination block name for a route."""
        route = self.get_route(src_name, channel)
        return route.dst_name if route else None

    def all_routing_cells(self) -> Iterator[Tuple[Tuple[int, int], int]]:
        """
        Iterate over all routing cells with their forward faces.

        Yields:
            (position, face) tuples for each routing cell
        """
        seen: Set[Tuple[int, int]] = set()
        for route in self.routes:
            for pos, face in route.routing_cells:
                if pos not in seen:
                    seen.add(pos)
                    yield (pos, face)


def compute_manhattan_path(
    from_pos: Tuple[int, int],
    to_pos: Tuple[int, int],
    blocked: Optional[Set[Tuple[int, int]]] = None,
    width: int = 100,
    height: int = 100,
) -> List[Tuple[int, int]]:
    """
    Compute a Manhattan path from one position to another.

    Uses A* pathfinding to avoid blocked cells.

    Args:
        from_pos: Starting position (col, row)
        to_pos: Ending position (col, row)
        blocked: Set of positions to avoid
        width: Array width for bounds checking
        height: Array height for bounds checking

    Returns:
        List of positions from from_pos to to_pos (inclusive)
    """
    if blocked is None:
        blocked = set()

    if from_pos == to_pos:
        return [from_pos]

    def heuristic(pos):
        return abs(pos[0] - to_pos[0]) + abs(pos[1] - to_pos[1])

    def get_neighbors(pos):
        x, y = pos
        neighbors = []
        for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height:
                if (nx, ny) not in blocked or (nx, ny) == to_pos:
                    neighbors.append((nx, ny))
        return neighbors

    # A* search
    counter = 0
    start_entry = (heuristic(from_pos), counter, from_pos, [from_pos])
    heap = [start_entry]
    visited: Set[Tuple[int, int]] = set()

    while heap:
        _, _, pos, path = heapq.heappop(heap)

        if pos in visited:
            continue
        visited.add(pos)

        if pos == to_pos:
            return path

        for npos in get_neighbors(pos):
            if npos not in visited:
                new_path = path + [npos]
                g_cost = len(new_path)
                f_cost = g_cost + heuristic(npos)
                counter += 1
                heapq.heappush(heap, (f_cost, counter, npos, new_path))

    # No path found - fall back to simple Manhattan
    # This shouldn't happen in a properly sized array
    path = [from_pos]
    x, y = from_pos
    while x != to_pos[0]:
        x += 1 if to_pos[0] > x else -1
        path.append((x, y))
    while y != to_pos[1]:
        y += 1 if to_pos[1] > y else -1
        path.append((x, y))

    return path


def path_to_routing_cells(
    path: List[Tuple[int, int]],
) -> List[Tuple[Tuple[int, int], int]]:
    """
    Convert a path to routing cells with faces.

    Each cell in the path (except the last) needs a face pointing to the next cell.

    Args:
        path: List of positions from source to destination

    Returns:
        List of (position, face) for routing cells (excludes endpoints)
    """
    if len(path) <= 2:
        # Direct connection - no routing cells needed
        return []

    routing_cells = []

    # Exclude first and last positions (they're the source and destination blocks)
    for i in range(1, len(path) - 1):
        pos = path[i]
        next_pos = path[i + 1]
        face = get_face_direction(pos, next_pos)
        if face is not None:
            routing_cells.append((pos, face))

    return routing_cells


def compute_routes(
    block_graph: BlockGraph,
    block_positions: Dict[str, Tuple[int, int]],
    entry_cells: Dict[str, Tuple[int, int]],
    exit_cells: Dict[str, Tuple[int, int]],
    width: int,
    height: int,
    input_port_pos: Optional[Tuple[int, int]] = None,
    output_port_pos: Optional[Tuple[int, int]] = None,
) -> RouteMap:
    """
    Compute all routes for a placement.

    This creates routes for:
    1. Connections between blocks (from block_graph edges)
    2. Input port to first block
    3. Last block to output port

    Args:
        block_graph: Block connectivity graph
        block_positions: Map of block name to position
        entry_cells: Map of block name to entry cell position
        exit_cells: Map of block name to exit cell position
        width: Array width
        height: Array height
        input_port_pos: Optional input port position
        output_port_pos: Optional output port position

    Returns:
        RouteMap with all routes and routing cells
    """
    route_map = RouteMap()

    # Track blocked cells (cells occupied by blocks)
    blocked: Set[Tuple[int, int]] = set()
    for name in block_graph.nodes:
        # For simplicity, assume single-cell blocks
        # For multi-cell blocks, we'd need to track all cells
        pos = block_positions.get(name)
        if pos:
            blocked.add(pos)

    # Route from input port to first block
    if input_port_pos:
        sources = block_graph.get_sources()
        if sources:
            first_block = sources[0]
            first_entry = entry_cells.get(first_block, block_positions.get(first_block))
            if first_entry:
                path = compute_manhattan_path(
                    input_port_pos, first_entry, blocked, width, height
                )
                routing_cells = path_to_routing_cells(path)
                hop_count = len(path) - 1
                route = Route(
                    src_name=None,  # From input port
                    channel=0,
                    dst_name=first_block,
                    routing_cells=routing_cells,
                    hop_count=hop_count,
                )
                route_map.add_route(route)

                # Add routing cells to blocked set for subsequent routes
                for pos, _ in routing_cells:
                    blocked.add(pos)

    # Route between connected blocks
    for edge in block_graph.iter_edges():
        src_exit = exit_cells.get(edge.src_block, block_positions.get(edge.src_block))
        dst_entry = entry_cells.get(edge.dst_block, block_positions.get(edge.dst_block))

        if src_exit is None or dst_entry is None:
            continue

        path = compute_manhattan_path(src_exit, dst_entry, blocked, width, height)
        routing_cells = path_to_routing_cells(path)
        hop_count = len(path) - 1

        route = Route(
            src_name=edge.src_block,
            channel=edge.channel,
            dst_name=edge.dst_block,
            routing_cells=routing_cells,
            hop_count=hop_count,
        )
        route_map.add_route(route)

        # Add routing cells to blocked set
        for pos, _ in routing_cells:
            blocked.add(pos)

    # Route from last block to output port
    if output_port_pos:
        sinks = block_graph.get_sinks()
        if sinks:
            last_block = sinks[0]
            last_exit = exit_cells.get(last_block, block_positions.get(last_block))
            if last_exit:
                path = compute_manhattan_path(
                    last_exit, output_port_pos, blocked, width, height
                )
                routing_cells = path_to_routing_cells(path)
                hop_count = len(path) - 1
                route = Route(
                    src_name=last_block,
                    channel=0,
                    dst_name=None,  # To output port
                    routing_cells=routing_cells,
                    hop_count=hop_count,
                )
                route_map.add_route(route)

    return route_map


def compute_route_for_edge(
    edge: BlockEdge,
    src_exit: Tuple[int, int],
    dst_entry: Tuple[int, int],
    blocked: Set[Tuple[int, int]],
    width: int,
    height: int,
) -> Route:
    """
    Compute a single route for an edge.

    Args:
        edge: The block graph edge
        src_exit: Exit cell of source block
        dst_entry: Entry cell of destination block
        blocked: Set of blocked positions
        width: Array width
        height: Array height

    Returns:
        Route object with routing cells and hop count
    """
    path = compute_manhattan_path(src_exit, dst_entry, blocked, width, height)
    routing_cells = path_to_routing_cells(path)
    hop_count = len(path) - 1

    return Route(
        src_name=edge.src_block,
        channel=edge.channel,
        dst_name=edge.dst_block,
        routing_cells=routing_cells,
        hop_count=hop_count,
    )
