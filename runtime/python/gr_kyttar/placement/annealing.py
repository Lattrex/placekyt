"""
Simulated Annealing Refinement for Kyttar Placement

After initial greedy placement, simulated annealing can improve quality by:
1. Swapping block positions
2. Trying different shapes for blocks
3. Shifting blocks to reduce wire length

The algorithm accepts worse moves with decreasing probability,
allowing escape from local minima.
"""

import math
import random
from dataclasses import dataclass, field
from typing import List, Dict, Set, Tuple, Optional, Callable
from copy import deepcopy

from .placer import Placement, PlacedBlock, Placer
from .block import BlockDefinition, FilamentMetrics, DEFAULT_METRICS
from .shapes import Shape, enumerate_shapes
from .region import ArrayConfig


@dataclass
class AnnealingConfig:
    """Configuration for simulated annealing."""
    # Temperature schedule
    initial_temp: float = 100.0
    final_temp: float = 0.1
    cooling_rate: float = 0.95

    # Iterations
    iterations_per_temp: int = 100
    max_iterations: int = 10000

    # Move probabilities
    swap_prob: float = 0.3      # Swap two blocks
    reshape_prob: float = 0.3   # Change shape of one block
    shift_prob: float = 0.4     # Shift block to adjacent position

    # Cost weights
    wire_weight: float = 1.0
    compact_weight: float = 0.2
    io_weight: float = 0.5

    # Random seed for reproducibility
    seed: Optional[int] = None


@dataclass
class AnnealingResult:
    """Result of simulated annealing."""
    placement: Placement
    initial_cost: float
    final_cost: float
    iterations: int
    accepted_moves: int
    temperature_history: List[float] = field(default_factory=list)
    cost_history: List[float] = field(default_factory=list)


class SimulatedAnnealing:
    """
    Simulated annealing optimizer for placement.

    Moves:
    1. Swap: Exchange positions of two blocks (with shape adjustment)
    2. Reshape: Keep position, try different shape
    3. Shift: Move block to adjacent available position
    """

    def __init__(
        self,
        config: ArrayConfig,
        annealing_config: Optional[AnnealingConfig] = None,
    ):
        """
        Initialize annealing optimizer.

        Args:
            config: Array configuration
            annealing_config: Annealing parameters
        """
        self.array_config = config
        self.config = annealing_config or AnnealingConfig()

        if self.config.seed is not None:
            random.seed(self.config.seed)

    def optimize(
        self,
        initial_placement: Placement,
        blocks: List[BlockDefinition],
        metrics: Optional[Dict[str, FilamentMetrics]] = None,
    ) -> AnnealingResult:
        """
        Optimize placement using simulated annealing.

        Args:
            initial_placement: Starting placement
            blocks: Block definitions
            metrics: Optional simulation metrics

        Returns:
            AnnealingResult with optimized placement
        """
        if metrics is None:
            metrics = {}

        # Deep copy to avoid modifying original
        current = self._copy_placement(initial_placement)
        current_cost = self._calculate_cost(current, blocks, metrics)

        best = self._copy_placement(current)
        best_cost = current_cost
        initial_cost = current_cost

        # Precompute shapes for each block
        block_shapes: Dict[str, List[Shape]] = {}
        for block in blocks:
            block_shapes[block.name] = enumerate_shapes(block.cell_count)

        # Annealing loop
        temp = self.config.initial_temp
        iterations = 0
        accepted = 0

        temp_history = [temp]
        cost_history = [current_cost]

        while temp > self.config.final_temp and iterations < self.config.max_iterations:
            for _ in range(self.config.iterations_per_temp):
                iterations += 1

                # Generate neighbor
                neighbor, move_type = self._generate_neighbor(
                    current, blocks, block_shapes, metrics
                )

                if neighbor is None:
                    continue

                # Calculate cost
                neighbor_cost = self._calculate_cost(neighbor, blocks, metrics)
                delta = neighbor_cost - current_cost

                # Accept or reject
                if delta < 0 or random.random() < math.exp(-delta / temp):
                    current = neighbor
                    current_cost = neighbor_cost
                    accepted += 1

                    # Track best
                    if current_cost < best_cost:
                        best = self._copy_placement(current)
                        best_cost = current_cost

            # Cool down
            temp *= self.config.cooling_rate
            temp_history.append(temp)
            cost_history.append(current_cost)

        return AnnealingResult(
            placement=best,
            initial_cost=initial_cost,
            final_cost=best_cost,
            iterations=iterations,
            accepted_moves=accepted,
            temperature_history=temp_history,
            cost_history=cost_history,
        )

    def _copy_placement(self, placement: Placement) -> Placement:
        """Deep copy a placement."""
        new_placement = Placement()
        new_placement.occupied_cells = set(placement.occupied_cells)
        new_placement.placed_blocks = {
            name: PlacedBlock(pb.block, pb.shape, pb.anchor)
            for name, pb in placement.placed_blocks.items()
        }
        return new_placement

    def _generate_neighbor(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        block_shapes: Dict[str, List[Shape]],
        metrics: Dict[str, FilamentMetrics],
    ) -> Tuple[Optional[Placement], str]:
        """
        Generate a neighbor placement.

        Returns (new_placement, move_type) or (None, "") if move failed.
        """
        r = random.random()
        total = self.config.swap_prob + self.config.reshape_prob + self.config.shift_prob

        if r < self.config.swap_prob / total:
            return self._try_swap(placement, blocks, block_shapes), "swap"
        elif r < (self.config.swap_prob + self.config.reshape_prob) / total:
            return self._try_reshape(placement, blocks, block_shapes), "reshape"
        else:
            return self._try_shift(placement, blocks, block_shapes), "shift"

    def _try_swap(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        block_shapes: Dict[str, List[Shape]],
    ) -> Optional[Placement]:
        """Try swapping two blocks."""
        block_names = list(placement.placed_blocks.keys())
        if len(block_names) < 2:
            return None

        # Pick two random blocks
        name1, name2 = random.sample(block_names, 2)
        pb1 = placement.placed_blocks[name1]
        pb2 = placement.placed_blocks[name2]

        # Try swapping positions
        new_placement = self._copy_placement(placement)

        # Remove both blocks
        for cell in pb1.cells:
            new_placement.occupied_cells.discard(cell)
        for cell in pb2.cells:
            new_placement.occupied_cells.discard(cell)

        # Try to place block1 at block2's position
        placed1 = self._try_place_at(
            pb1.block, pb2.anchor, block_shapes[name1],
            new_placement.occupied_cells
        )
        if placed1 is None:
            return None

        # Add block1's cells
        for cell in placed1.cells:
            new_placement.occupied_cells.add(cell)

        # Try to place block2 at block1's position
        placed2 = self._try_place_at(
            pb2.block, pb1.anchor, block_shapes[name2],
            new_placement.occupied_cells
        )
        if placed2 is None:
            return None

        # Update placements
        new_placement.placed_blocks[name1] = placed1
        new_placement.placed_blocks[name2] = placed2

        for cell in placed2.cells:
            new_placement.occupied_cells.add(cell)

        return new_placement

    def _try_reshape(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        block_shapes: Dict[str, List[Shape]],
    ) -> Optional[Placement]:
        """Try using a different shape for a block."""
        block_names = list(placement.placed_blocks.keys())
        if not block_names:
            return None

        name = random.choice(block_names)
        pb = placement.placed_blocks[name]
        shapes = block_shapes.get(name, [])

        if len(shapes) < 2:
            return None

        # Remove current block
        new_placement = self._copy_placement(placement)
        for cell in pb.cells:
            new_placement.occupied_cells.discard(cell)

        # Pick a different shape
        other_shapes = [s for s in shapes if s != pb.shape]
        if not other_shapes:
            return None

        new_shape = random.choice(other_shapes)

        # Try all rotations
        for rotation in new_shape.all_rotations():
            cells = rotation.absolute_cells(pb.anchor)
            if self._cells_valid(cells, new_placement.occupied_cells):
                new_pb = PlacedBlock(pb.block, rotation, pb.anchor)
                new_placement.placed_blocks[name] = new_pb
                for cell in cells:
                    new_placement.occupied_cells.add(cell)
                return new_placement

        return None

    def _try_shift(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        block_shapes: Dict[str, List[Shape]],
    ) -> Optional[Placement]:
        """Try shifting a block to adjacent position."""
        block_names = list(placement.placed_blocks.keys())
        if not block_names:
            return None

        name = random.choice(block_names)
        pb = placement.placed_blocks[name]

        # Remove current block
        new_placement = self._copy_placement(placement)
        for cell in pb.cells:
            new_placement.occupied_cells.discard(cell)

        # Try shifting in random direction
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        random.shuffle(directions)

        for dc, dr in directions:
            new_anchor = (pb.anchor[0] + dc, pb.anchor[1] + dr)
            cells = pb.shape.absolute_cells(new_anchor)

            if self._cells_valid(cells, new_placement.occupied_cells):
                new_pb = PlacedBlock(pb.block, pb.shape, new_anchor)
                new_placement.placed_blocks[name] = new_pb
                for cell in cells:
                    new_placement.occupied_cells.add(cell)
                return new_placement

        return None

    def _try_place_at(
        self,
        block: BlockDefinition,
        anchor: Tuple[int, int],
        shapes: List[Shape],
        occupied: Set[Tuple[int, int]],
    ) -> Optional[PlacedBlock]:
        """Try to place a block at a position with any shape."""
        for shape in shapes:
            for rotation in shape.all_rotations():
                cells = rotation.absolute_cells(anchor)
                if self._cells_valid(cells, occupied):
                    return PlacedBlock(block, rotation, anchor)
        return None

    def _cells_valid(
        self,
        cells: List[Tuple[int, int]],
        occupied: Set[Tuple[int, int]],
    ) -> bool:
        """Check if cells are valid (in bounds, not occupied)."""
        for col, row in cells:
            if col < 0 or col >= self.array_config.width:
                return False
            if row < 0 or row >= self.array_config.height:
                return False
            if (col, row) in occupied:
                return False
        return True

    def _calculate_cost(
        self,
        placement: Placement,
        blocks: List[BlockDefinition],
        metrics: Dict[str, FilamentMetrics],
    ) -> float:
        """Calculate total placement cost."""
        cost = 0.0

        # Wire length cost
        for name, pb in placement.placed_blocks.items():
            for conn in pb.block.connections:
                if conn.target in placement.placed_blocks:
                    target = placement.placed_blocks[conn.target]
                    # Distance from exit to entry
                    exit_cell = pb.exit_cell
                    entry_cell = target.entry_cell
                    dist = abs(exit_cell[0] - entry_cell[0]) + abs(exit_cell[1] - entry_cell[1])
                    cost += dist * conn.weight * self.config.wire_weight

        # I/O proximity cost
        input_pos = self.array_config.input_position()
        output_pos = self.array_config.output_position()

        for name, pb in placement.placed_blocks.items():
            m = metrics.get(name, DEFAULT_METRICS)
            entry = pb.entry_cell
            exit_cell = pb.exit_cell

            # High input ratio → close to input
            cost += (
                (abs(entry[0] - input_pos[0]) + abs(entry[1] - input_pos[1]))
                * m.input_ratio * self.config.io_weight
            )

            # High output ratio → close to output
            cost += (
                (abs(exit_cell[0] - output_pos[0]) + abs(exit_cell[1] - output_pos[1]))
                * m.output_ratio * self.config.io_weight
            )

        # Compactness cost
        for name, pb in placement.placed_blocks.items():
            cost += pb.shape.bounding_box_area * self.config.compact_weight

        return cost


def anneal_placement(
    placement: Placement,
    blocks: List[BlockDefinition],
    config: ArrayConfig,
    metrics: Optional[Dict[str, FilamentMetrics]] = None,
    annealing_config: Optional[AnnealingConfig] = None,
) -> AnnealingResult:
    """
    Convenience function for simulated annealing.

    Args:
        placement: Initial placement
        blocks: Block definitions
        config: Array configuration
        metrics: Optional simulation metrics
        annealing_config: Annealing parameters

    Returns:
        AnnealingResult with optimized placement
    """
    sa = SimulatedAnnealing(config, annealing_config)
    return sa.optimize(placement, blocks, metrics)


if __name__ == '__main__':
    # Test simulated annealing
    from .block import BlockDefinition, Connection
    from .placer import Placer

    # Create test blocks
    blocks = [
        BlockDefinition(
            name="A",
            cell_count=3,
            connections=[Connection(target="B", weight=1.0)],
        ),
        BlockDefinition(
            name="B",
            cell_count=4,
            connections=[Connection(target="C", weight=1.0)],
        ),
        BlockDefinition(
            name="C",
            cell_count=2,
            connections=[Connection(target="D", weight=1.0)],
        ),
        BlockDefinition(
            name="D",
            cell_count=3,
        ),
    ]

    # Initial placement
    config = ArrayConfig(width=10, height=10)
    placer = Placer(config)
    initial = placer.place(blocks)

    print("Initial placement:")
    for name, pb in initial.placed_blocks.items():
        print(f"  {name}: anchor={pb.anchor}, cells={pb.cells}")

    # Run annealing
    sa_config = AnnealingConfig(
        initial_temp=50.0,
        iterations_per_temp=50,
        max_iterations=2000,
        seed=42,
    )

    result = anneal_placement(initial, blocks, config, annealing_config=sa_config)

    print(f"\nAnnealing complete:")
    print(f"  Initial cost: {result.initial_cost:.2f}")
    print(f"  Final cost: {result.final_cost:.2f}")
    print(f"  Improvement: {(1 - result.final_cost/result.initial_cost)*100:.1f}%")
    print(f"  Iterations: {result.iterations}")
    print(f"  Accepted moves: {result.accepted_moves}")

    print("\nOptimized placement:")
    for name, pb in result.placement.placed_blocks.items():
        print(f"  {name}: anchor={pb.anchor}, cells={pb.cells}")
