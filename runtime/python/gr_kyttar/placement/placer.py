"""
Placement Engine for Kyttar Fabric

Two-phase placement algorithm:
1. Coarse placement: Assign blocks to regions based on I/O ratio and activity
2. Fine placement: Fit shapes within regions, optimizing wire length

Uses simulation-driven metrics (FilamentMetrics) to inform placement decisions.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Set, Tuple, Optional, Any
import math

from .shapes import Shape, enumerate_shapes
from .block import BlockDefinition, FilamentMetrics, DEFAULT_METRICS
from .region import Region, ArrayConfig


class PlacementError(Exception):
    """Raised when placement fails."""
    pass


@dataclass
class PlacedBlock:
    """A block that has been placed on the fabric."""
    block: BlockDefinition
    shape: Shape
    anchor: Tuple[int, int]  # (col, row) of anchor position

    @property
    def cells(self) -> List[Tuple[int, int]]:
        """Get absolute cell positions."""
        return self.shape.absolute_cells(self.anchor)

    @property
    def entry_cell(self) -> Tuple[int, int]:
        """Input entry cell position."""
        dc, dr = self.shape.entry_cell()
        return (self.anchor[0] + dc, self.anchor[1] + dr)

    @property
    def exit_cell(self) -> Tuple[int, int]:
        """Output exit cell position."""
        dc, dr = self.shape.exit_cell()
        return (self.anchor[0] + dc, self.anchor[1] + dr)


@dataclass
class Placement:
    """
    Result of the placement algorithm.

    Contains the position and shape of each placed block.
    """
    # Map from block name to placed block
    placed_blocks: Dict[str, PlacedBlock] = field(default_factory=dict)

    # Set of occupied cells
    occupied_cells: Set[Tuple[int, int]] = field(default_factory=set)

    def place(self, block: BlockDefinition, shape: Shape, anchor: Tuple[int, int]) -> None:
        """Place a block on the fabric."""
        placed = PlacedBlock(block, shape, anchor)

        # Check for overlaps
        new_cells = set(placed.cells)
        overlap = self.occupied_cells & new_cells
        if overlap:
            raise PlacementError(
                f"Block '{block.name}' overlaps with existing placement at {overlap}"
            )

        self.placed_blocks[block.name] = placed
        self.occupied_cells.update(new_cells)

    def get_block_position(self, name: str) -> Optional[Tuple[int, int]]:
        """Get anchor position of a placed block."""
        if name in self.placed_blocks:
            return self.placed_blocks[name].anchor
        return None

    def is_cell_occupied(self, col: int, row: int) -> bool:
        """Check if a cell is occupied."""
        return (col, row) in self.occupied_cells

    def available_cells(self, config: ArrayConfig) -> Set[Tuple[int, int]]:
        """Get set of unoccupied cells."""
        return config.all_cells() - self.occupied_cells

    def to_dict(self) -> Dict[str, Any]:
        """Export placement to dictionary."""
        return {
            'blocks': [
                {
                    'name': pb.block.name,
                    'anchor': list(pb.anchor),
                    'cells': pb.cells,
                    'entry': pb.entry_cell,
                    'exit': pb.exit_cell,
                }
                for pb in self.placed_blocks.values()
            ]
        }


class Placer:
    """
    Two-phase placement engine.

    Phase 1 (Coarse): Assign blocks to regions based on:
    - Activity: Most active blocks placed first (get best positions)
    - I/O ratio: Input-heavy blocks near input, output-heavy near output

    Phase 2 (Fine): Fit shapes within assigned regions:
    - Enumerate valid shapes
    - Score by wire length to connected blocks
    - Select best fitting shape
    """

    def __init__(
        self,
        config: ArrayConfig,
        input_port: Optional[str] = None,
        output_port: Optional[str] = None,
    ):
        """
        Initialize placer.

        Args:
            config: Array configuration (size, port positions)
            input_port: Name of input port to use (defaults to first input port)
            output_port: Name of output port to use (defaults to first output port)
        """
        self.config = config
        self._input_port = input_port
        self._output_port = output_port

    def get_input_position(self) -> Tuple[int, int]:
        """Get the input port cell position."""
        if self._input_port and self.config.ports:
            return self.config.get_port_position(self._input_port)
        return self.config.input_position()

    def get_output_position(self) -> Tuple[int, int]:
        """Get the output port cell position."""
        if self._output_port and self.config.ports:
            return self.config.get_port_position(self._output_port)
        return self.config.output_position()

    def place(
        self,
        blocks: List[BlockDefinition],
        metrics: Optional[Dict[str, FilamentMetrics]] = None,
    ) -> Placement:
        """
        Run the two-phase placement algorithm.

        Args:
            blocks: Block definitions to place
            metrics: Optional metrics from simulation (uses defaults if not provided)

        Returns:
            Placement result

        Raises:
            PlacementError: If placement fails
        """
        if metrics is None:
            metrics = {}

        placement = Placement()

        # Phase 0: Pre-place I/O port blocks at their fixed positions
        # These blocks have is_io_port=True and preferred_anchor set
        io_blocks = [b for b in blocks if b.is_io_port]
        regular_blocks = [b for b in blocks if not b.is_io_port]

        for block in io_blocks:
            if block.preferred_anchor is None:
                raise PlacementError(
                    f"I/O port block '{block.name}' has no preferred_anchor"
                )
            # I/O port blocks are single-cell shapes at a fixed position
            from .shapes import Shape
            shape = Shape([(0, 0)])  # Single cell
            anchor = block.preferred_anchor
            placement.place(block, shape, anchor)

        # Phase 1: Coarse placement for regular blocks
        coarse_regions = self._coarse_placement(regular_blocks, metrics)

        # Phase 2: Fine placement for regular blocks
        placement = self._fine_placement(regular_blocks, coarse_regions, metrics, placement)

        return placement

    def _coarse_placement(
        self,
        blocks: List[BlockDefinition],
        metrics: Dict[str, FilamentMetrics],
    ) -> Dict[str, Region]:
        """
        Phase 1: Assign blocks to coarse regions.

        Strategy: Compute topological depth in the connection graph to spread
        blocks along the input→output axis. Blocks closer to input get lower
        columns, blocks closer to output get higher columns.
        """
        if not blocks:
            return {}

        # Build connection graph and compute topological depth
        block_map = {b.name: b for b in blocks}
        # Find sources (blocks that no one connects TO)
        targets = set()
        for b in blocks:
            for c in b.connections:
                targets.add(c.target)
        sources = [b.name for b in blocks if b.name not in targets]
        if not sources:
            sources = [blocks[0].name]  # fallback

        # BFS from sources to compute depth
        depth = {}
        queue = list(sources)
        for s in sources:
            depth[s] = 0
        while queue:
            name = queue.pop(0)
            if name not in block_map:
                continue
            for c in block_map[name].connections:
                if c.target not in depth:
                    depth[c.target] = depth[name] + 1
                    queue.append(c.target)
        # Assign depth 0 to any unvisited blocks
        for b in blocks:
            if b.name not in depth:
                depth[b.name] = 0

        max_depth = max(depth.values()) if depth else 0

        # Group blocks by depth level
        depth_groups: Dict[int, List[BlockDefinition]] = {}
        for block in blocks:
            d = depth[block.name]
            if d not in depth_groups:
                depth_groups[d] = []
            depth_groups[d].append(block)

        # Calculate column stride: spread depth levels across fabric width
        # Use stride of 1 - blocks can abut directly (WRITE/JUMP to adjacent cells)
        col_stride = max(1, (self.config.width - 1) // (max_depth + 1)) if max_depth > 0 else 1

        # Calculate regions
        regions = {}
        for d, group in sorted(depth_groups.items()):
            col_center = min(d * col_stride, self.config.width - 2)
            # Spread blocks in same depth level vertically - allow adjacent placement
            row_stride = max(1, self.config.height // (len(group) + 1))
            for i, block in enumerate(group):
                ideal_y = (i + 1) * row_stride
                ideal_y = max(0, min(self.config.height - 2, ideal_y))

                region_size = max(3, int(math.sqrt(block.cell_count)) + 2)
                region_x = max(0, min(self.config.width - region_size, col_center - region_size // 2))
                region_y = max(0, min(self.config.height - region_size, ideal_y - region_size // 2))

                regions[block.name] = Region(
                    anchor=(region_x, region_y),
                    width=region_size,
                    height=region_size,
                )

        return regions

    def _fine_placement(
        self,
        blocks: List[BlockDefinition],
        coarse_regions: Dict[str, Region],
        metrics: Dict[str, FilamentMetrics],
        placement: Optional[Placement] = None,
    ) -> Placement:
        """
        Phase 2: Fit shapes within coarse regions.

        For each block (in activity order):
        1. Generate all valid shapes
        2. Filter to shapes that fit in available space
        3. Score shapes by wire length to connected blocks
        4. Place the best shape

        Args:
            blocks: Blocks to place (regular blocks, not I/O ports)
            coarse_regions: Coarse region assignments
            metrics: Placement metrics
            placement: Optional existing placement (with I/O port blocks pre-placed)
        """
        if placement is None:
            placement = Placement()

        # Process in topological order (from coarse placement depth)
        # so earlier blocks in the pipeline get placed first
        # Build depth map for sorting
        block_map = {b.name: b for b in blocks}
        targets = set()
        for b in blocks:
            for c in b.connections:
                targets.add(c.target)
        sources = [b.name for b in blocks if b.name not in targets]
        if not sources:
            sources = [blocks[0].name]
        depth = {}
        queue = list(sources)
        for s in sources:
            depth[s] = 0
        while queue:
            name = queue.pop(0)
            if name not in block_map:
                continue
            for c in block_map[name].connections:
                if c.target not in depth:
                    depth[c.target] = depth[name] + 1
                    queue.append(c.target)
        for b in blocks:
            if b.name not in depth:
                depth[b.name] = 0

        sorted_blocks = sorted(blocks, key=lambda b: depth.get(b.name, 0))

        for block in sorted_blocks:
            region = coarse_regions.get(block.name)
            if region is None:
                raise PlacementError(f"No coarse region for block '{block.name}'")

            # Generate shapes
            all_shapes = enumerate_shapes(block.cell_count)
            if not all_shapes:
                raise PlacementError(
                    f"No valid shapes for block '{block.name}' with {block.cell_count} cells"
                )

            # Find best fitting shape and position
            best_shape = None
            best_anchor = None
            best_cost = float('inf')

            for shape in all_shapes:
                for orientation in shape.all_rotations():
                    # Try all positions in the fabric (not just region)
                    # We need full-fabric search because the routing channel
                    # constraint may prevent placement near other blocks.
                    # The cost function still prefers positions near the ideal
                    # region, so this doesn't change quality - just ensures we
                    # find valid positions.
                    for col in range(self.config.width):
                        for row in range(self.config.height):
                            anchor = (col, row)
                            cells = orientation.absolute_cells(anchor)

                            # Check if shape fits
                            if not self._shape_fits(cells, placement):
                                continue

                            # Calculate cost
                            cost = self._shape_cost(
                                orientation, anchor, block, placement, metrics,
                                region=region,
                            )

                            if cost < best_cost:
                                best_cost = cost
                                best_shape = orientation
                                best_anchor = anchor

            if best_shape is None:
                raise PlacementError(
                    f"No valid placement for block '{block.name}' - "
                    f"no shape fits in available space"
                )

            placement.place(block, best_shape, best_anchor)

        return placement

    def _shape_fits(
        self,
        cells: List[Tuple[int, int]],
        placement: Placement,
    ) -> bool:
        """Check if a shape fits in the fabric without overlaps.

        Blocks CAN abut directly - Kyttar supports WRITE/JUMP to adjacent
        cells, so no routing channel is required between blocks.
        """
        for col, row in cells:
            # Check bounds
            if col < 0 or col >= self.config.width:
                return False
            if row < 0 or row >= self.config.height:
                return False
            # Check overlap
            if placement.is_cell_occupied(col, row):
                return False
        return True

    def _shape_cost(
        self,
        shape: Shape,
        anchor: Tuple[int, int],
        block: BlockDefinition,
        placement: Placement,
        metrics: Dict[str, FilamentMetrics],
        region: Optional[Region] = None,
    ) -> float:
        """
        Calculate placement cost for a shape.

        Lower is better. Considers:
        - Distance to coarse region center (DOMINANT term)
        - Wire length to connected blocks
        - Distance to I/O ports based on I/O ratio (weak)
        - Bounding box compactness
        """
        cost = 0.0

        abs_cells = shape.absolute_cells(anchor)
        entry = abs_cells[0]
        exit_cell = abs_cells[-1]
        m = metrics.get(block.name, DEFAULT_METRICS)

        # Cost 0: Distance to coarse region center (DOMINANT)
        # This ensures blocks actually land near their topologically-assigned
        # positions, leaving routing channels between depth levels.
        if region is not None:
            region_center = (
                region.anchor[0] + region.width // 2,
                region.anchor[1] + region.height // 2,
            )
            cost += self._manhattan_distance(entry, region_center) * 5.0

        # Cost 1: Wire length to connected blocks
        for conn in block.connections:
            if conn.target in placement.placed_blocks:
                other = placement.placed_blocks[conn.target]
                # Distance from exit to other's entry
                dist = self._manhattan_distance(exit_cell, other.entry_cell)
                cost += dist * conn.weight

        # Cost 2: Distance to I/O ports based on I/O ratio (weak)
        input_pos = self.get_input_position()
        output_pos = self.get_output_position()

        # Weak I/O pull - coarse region center dominates
        cost += self._manhattan_distance(entry, input_pos) * m.input_ratio * 0.1
        cost += self._manhattan_distance(exit_cell, output_pos) * m.output_ratio * 0.1

        # Cost 3: Compactness (prefer shapes that don't sprawl)
        cost += shape.bounding_box_area * 0.1

        # Cost 4: Fragmentation penalty (prefer shapes that leave contiguous space)
        cost += self._fragmentation_penalty(abs_cells, placement) * 0.2

        # Cost 5: Enclosure penalty - strongly penalize positions where a cell
        # has NO free neighbors. Every block cell (especially entry/exit) needs
        # at least one free neighbor for routing. Without this, the router's
        # A* pathfinding fails and falls back to broken Manhattan routing.
        cost += self._enclosure_penalty(abs_cells, placement) * 5.0

        return cost

    def _manhattan_distance(
        self,
        a: Tuple[int, int],
        b: Tuple[int, int],
    ) -> float:
        """Manhattan distance between two cells."""
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def _enclosure_penalty(
        self,
        cells: List[Tuple[int, int]],
        placement: Placement,
    ) -> float:
        """
        Penalty for cells with no free neighbors (completely enclosed).

        A cell with zero free neighbors cannot be routed to/from.
        This is a hard routing failure that must be avoided.
        Returns a high penalty for each enclosed cell.
        """
        penalty = 0.0
        cell_set = set(cells)
        occupied = placement.occupied_cells

        for col, row in cells:
            free_neighbors = 0
            for dc, dr in [(0, -1), (0, 1), (1, 0), (-1, 0)]:
                nc, nr = col + dc, row + dr
                if nc < 0 or nc >= self.config.width or nr < 0 or nr >= self.config.height:
                    continue  # Out of bounds doesn't count as free
                neighbor = (nc, nr)
                if neighbor not in occupied and neighbor not in cell_set:
                    free_neighbors += 1

            if free_neighbors == 0:
                # Completely enclosed - very bad
                penalty += 10.0
            elif free_neighbors == 1:
                # Only one exit - risky for routing
                penalty += 2.0

        return penalty

    def _fragmentation_penalty(
        self,
        cells: List[Tuple[int, int]],
        placement: Placement,
    ) -> float:
        """
        Penalty for shapes that create fragmented free space.

        Lower is better.
        """
        # Count how many edges of the shape border occupied cells
        # (These create awkward corners)
        penalty = 0.0
        occupied = placement.occupied_cells

        for col, row in cells:
            for dc, dr in [(0, -1), (0, 1), (1, 0), (-1, 0)]:
                neighbor = (col + dc, row + dr)
                if neighbor in occupied:
                    penalty += 1.0

        return penalty


def place_blocks(
    blocks: List[BlockDefinition],
    width: int,
    height: int,
    metrics: Optional[Dict[str, FilamentMetrics]] = None,
    input_port: Tuple[int, int] = (0, 0),
    output_port: Optional[Tuple[int, int]] = None,
) -> Placement:
    """
    Convenience function for placing blocks.

    Args:
        blocks: Block definitions
        width: Fabric width
        height: Fabric height
        metrics: Optional simulation metrics
        input_port: Input port position (col, row)
        output_port: Output port position (defaults to right edge)

    Returns:
        Placement result
    """
    if output_port is None:
        output_port = (width - 1, 0)

    config = ArrayConfig(
        width=width,
        height=height,
        input_port_edge=3 if input_port[0] == 0 else 1,
        input_port_offset=input_port[1],
        output_port_edge=1 if output_port[0] == width - 1 else 3,
        output_port_offset=output_port[1],
    )

    placer = Placer(config)
    return placer.place(blocks, metrics)


if __name__ == '__main__':
    # Test placement
    from .block import Connection, ConnectionType

    # Create some test blocks
    blocks = [
        BlockDefinition(
            name="FilterA",
            cell_count=3,
            connections=[Connection(target="FilterB", weight=1.0)],
        ),
        BlockDefinition(
            name="FilterB",
            cell_count=4,
            connections=[Connection(target="Output", weight=1.0)],
        ),
        BlockDefinition(
            name="Output",
            cell_count=2,
        ),
    ]

    # Create test metrics
    metrics = {
        "FilterA": FilamentMetrics(input_ratio=0.8, output_ratio=0.2, activity=0.4),
        "FilterB": FilamentMetrics(input_ratio=0.5, output_ratio=0.5, activity=0.35),
        "Output": FilamentMetrics(input_ratio=0.3, output_ratio=0.7, activity=0.25),
    }

    # Run placement
    config = ArrayConfig(width=10, height=10)
    placer = Placer(config)
    placement = placer.place(blocks, metrics)

    print("Placement result:")
    for name, pb in placement.placed_blocks.items():
        print(f"  {name}: anchor={pb.anchor}, cells={pb.cells}")

    print(f"\nOccupied cells: {len(placement.occupied_cells)}")
    print(f"Available cells: {config.total_cells - len(placement.occupied_cells)}")
