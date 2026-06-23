"""
Shape Generation for Filament Placement

Generates valid filament shapes as self-avoiding walks on a 2D grid.
Each consecutive pair of cells must share a face (up/down/left/right).

For N <= 10: Full enumeration of all self-avoiding walks
For N > 10: Heuristic shapes (straight, L, S, U, rectangles)
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Set, Optional, Iterator
from enum import Enum


class Direction(Enum):
    """Cardinal directions for grid movement."""
    NORTH = (0, -1)   # Up (decreasing row)
    SOUTH = (0, 1)    # Down (increasing row)
    EAST = (1, 0)     # Right (increasing column)
    WEST = (-1, 0)    # Left (decreasing column)


# Direction deltas for neighbor enumeration
DELTAS = [(0, -1), (0, 1), (1, 0), (-1, 0)]  # N, S, E, W


@dataclass
class Shape:
    """
    A filament shape as a sequence of relative cell positions.

    The first cell is always at (0, 0). Subsequent cells are relative offsets.
    All consecutive cells must share a face.
    """
    # List of (col, row) offsets from anchor position
    cells: List[Tuple[int, int]] = field(default_factory=list)
    # Optional explicit OUTPUT cell offset. Most blocks output from their last
    # cell (the default), but some (e.g. a Costas loop whose recovered signal
    # leaves the rotate cell in the MIDDLE of the block) output from elsewhere.
    # When None, exit_cell() falls back to cells[-1].
    exit_offset: Optional[Tuple[int, int]] = None

    @property
    def cell_count(self) -> int:
        """Number of cells in this shape."""
        return len(self.cells)

    @property
    def bounding_box(self) -> Tuple[int, int, int, int]:
        """
        Bounding box as (min_col, min_row, max_col, max_row).
        """
        if not self.cells:
            return (0, 0, 0, 0)
        cols = [c[0] for c in self.cells]
        rows = [c[1] for c in self.cells]
        return (min(cols), min(rows), max(cols), max(rows))

    @property
    def width(self) -> int:
        """Width of bounding box."""
        min_col, _, max_col, _ = self.bounding_box
        return max_col - min_col + 1

    @property
    def height(self) -> int:
        """Height of bounding box."""
        _, min_row, _, max_row = self.bounding_box
        return max_row - min_row + 1

    @property
    def bounding_box_area(self) -> int:
        """Area of bounding box."""
        return self.width * self.height

    def normalize(self) -> 'Shape':
        """
        Return a copy with cells shifted so minimum coords are (0, 0).
        """
        if not self.cells:
            return Shape([])
        min_col, min_row, _, _ = self.bounding_box
        normalized = [(c - min_col, r - min_row) for c, r in self.cells]
        return Shape(normalized)

    def rotate_90(self) -> 'Shape':
        """
        Rotate shape 90 degrees clockwise.
        Transform: (x, y) -> (y, -x)
        """
        rotated = [(y, -x) for x, y in self.cells]
        return Shape(rotated).normalize()

    def rotate_180(self) -> 'Shape':
        """Rotate shape 180 degrees."""
        rotated = [(-x, -y) for x, y in self.cells]
        return Shape(rotated).normalize()

    def rotate_270(self) -> 'Shape':
        """Rotate shape 270 degrees clockwise (90 counter-clockwise)."""
        rotated = [(-y, x) for x, y in self.cells]
        return Shape(rotated).normalize()

    def flip_horizontal(self) -> 'Shape':
        """Flip shape horizontally (mirror across vertical axis)."""
        flipped = [(-x, y) for x, y in self.cells]
        return Shape(flipped).normalize()

    def flip_vertical(self) -> 'Shape':
        """Flip shape vertically (mirror across horizontal axis)."""
        flipped = [(x, -y) for x, y in self.cells]
        return Shape(flipped).normalize()

    def all_rotations(self) -> List['Shape']:
        """Return all 4 rotations of this shape."""
        return [
            self.normalize(),
            self.rotate_90(),
            self.rotate_180(),
            self.rotate_270(),
        ]

    def all_orientations(self) -> List['Shape']:
        """Return all 8 orientations (4 rotations + 4 flipped rotations)."""
        flipped = self.flip_horizontal()
        return self.all_rotations() + flipped.all_rotations()

    def absolute_cells(self, anchor: Tuple[int, int]) -> List[Tuple[int, int]]:
        """
        Return cell positions given an anchor position.

        Args:
            anchor: (col, row) position for the first cell

        Returns:
            List of absolute (col, row) positions
        """
        anchor_col, anchor_row = anchor
        return [(anchor_col + dc, anchor_row + dr) for dc, dr in self.cells]

    def entry_cell(self) -> Tuple[int, int]:
        """First cell position (input entry point)."""
        return self.cells[0] if self.cells else (0, 0)

    def exit_cell(self) -> Tuple[int, int]:
        """Output exit cell offset. Uses the explicit ``exit_offset`` when set
        (a block whose output leaves a NON-last cell), else the last cell."""
        if self.exit_offset is not None:
            return self.exit_offset
        return self.cells[-1] if self.cells else (0, 0)

    def io_position(self, port_index: int) -> Tuple[int, int]:
        """
        Get position for a port.

        Args:
            port_index: 0 for input (first cell), -1 for output (last cell)
        """
        if port_index == 0:
            return self.entry_cell()
        elif port_index == -1:
            return self.exit_cell()
        else:
            return self.cells[port_index]

    def __hash__(self) -> int:
        return hash(tuple(self.cells))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Shape):
            return False
        return self.cells == other.cells

    def __repr__(self) -> str:
        return f"Shape({self.cells})"


def _enumerate_walks_recursive(
    n: int,
    current: List[Tuple[int, int]],
    visited: Set[Tuple[int, int]],
    results: List[Shape]
) -> None:
    """
    Recursively enumerate self-avoiding walks.

    Args:
        n: Target length
        current: Current path
        visited: Set of visited positions
        results: Accumulator for found walks
    """
    if len(current) == n:
        results.append(Shape(list(current)))
        return

    x, y = current[-1]

    for dx, dy in DELTAS:
        next_pos = (x + dx, y + dy)
        if next_pos not in visited:
            visited.add(next_pos)
            current.append(next_pos)
            _enumerate_walks_recursive(n, current, visited, results)
            current.pop()
            visited.remove(next_pos)


def enumerate_self_avoiding_walks(n: int) -> List[Shape]:
    """
    Enumerate all self-avoiding walks of length n on a 2D grid.

    A self-avoiding walk is a path where each step goes to an adjacent
    cell (up/down/left/right) and no cell is visited twice.

    Args:
        n: Number of cells in the walk

    Returns:
        List of all unique self-avoiding walks starting at (0, 0)

    Note:
        This grows rapidly: n=6 has 36 walks, n=10 has 4,655 walks.
        For n > 10, consider using heuristic_shapes() instead.
    """
    if n <= 0:
        return []
    if n == 1:
        return [Shape([(0, 0)])]

    results: List[Shape] = []
    start = (0, 0)
    _enumerate_walks_recursive(n, [start], {start}, results)
    return results


def _deduplicate_shapes(shapes: List[Shape]) -> List[Shape]:
    """
    Remove duplicate shapes, considering rotations and reflections.

    Two shapes are considered duplicates if one can be transformed
    into the other by rotation or reflection.
    """
    seen: Set[Tuple[Tuple[int, int], ...]] = set()
    unique: List[Shape] = []

    for shape in shapes:
        # Get canonical form (sorted tuple of normalized cells)
        normalized = shape.normalize()
        canonical = tuple(sorted(normalized.cells))

        # Check all orientations
        is_dup = False
        for oriented in normalized.all_orientations():
            key = tuple(sorted(oriented.cells))
            if key in seen:
                is_dup = True
                break

        if not is_dup:
            seen.add(canonical)
            unique.append(normalized)

    return unique


def heuristic_shapes(n: int) -> List[Shape]:
    """
    Generate heuristic shapes for larger filaments (n > 10).

    Includes:
    - Straight line (horizontal and vertical)
    - L-shapes (all rotations)
    - S-shapes (horizontal and vertical)
    - U-shapes (all rotations)
    - Rectangle-filling snakes

    Args:
        n: Number of cells

    Returns:
        List of heuristic shapes suitable for placement
    """
    shapes: List[Shape] = []

    # Straight horizontal
    shapes.append(Shape([(i, 0) for i in range(n)]))

    # Straight vertical
    shapes.append(Shape([(0, i) for i in range(n)]))

    if n >= 3:
        # L-shapes: go right then down
        for corner in range(1, n):
            cells = [(i, 0) for i in range(corner)]
            cells += [(corner - 1, j) for j in range(1, n - corner + 1)]
            if len(cells) == n:
                shapes.append(Shape(cells))

    if n >= 4:
        # S-shapes: zigzag
        cells = []
        direction = 1
        x, y = 0, 0
        for i in range(n):
            cells.append((x, y))
            if i < n - 1:
                if len(cells) % 2 == 1:
                    x += direction
                else:
                    y += 1
                    direction = -direction
        shapes.append(Shape(cells))

    if n >= 5:
        # U-shape: down, right, up
        arm_len = (n - 1) // 2
        bottom_len = n - 2 * arm_len
        cells = [(0, i) for i in range(arm_len)]  # Down
        cells += [(i, arm_len) for i in range(1, bottom_len)]  # Right
        cells += [(bottom_len - 1, arm_len - 1 - i) for i in range(arm_len)]  # Up
        if len(cells) == n:
            shapes.append(Shape(cells))

    # Snake (fill a rectangle efficiently)
    # Find best rectangle dimensions
    for width in range(2, n):
        height = (n + width - 1) // width
        cells = []
        for row in range(height):
            if row % 2 == 0:
                cols = range(width)
            else:
                cols = range(width - 1, -1, -1)
            for col in cols:
                if len(cells) < n:
                    cells.append((col, row))
        if len(cells) == n:
            shapes.append(Shape(cells))

    # Get all rotations of each shape
    all_shapes: List[Shape] = []
    for shape in shapes:
        all_shapes.extend(shape.all_rotations())

    # Deduplicate
    return _deduplicate_shapes(all_shapes)


def enumerate_shapes(n: int, max_enumerate: int = 10) -> List[Shape]:
    """
    Get all valid shapes for a filament of n cells.

    For small n (<= max_enumerate): Full enumeration of self-avoiding walks
    For large n (> max_enumerate): Heuristic shapes only

    Args:
        n: Number of cells
        max_enumerate: Threshold for full enumeration (default 10)

    Returns:
        List of valid shapes
    """
    if n <= 0:
        return []
    if n == 1:
        return [Shape([(0, 0)])]

    if n <= max_enumerate:
        return enumerate_self_avoiding_walks(n)
    else:
        return heuristic_shapes(n)


# Pre-computed walk counts for reference
SELF_AVOIDING_WALK_COUNTS = {
    1: 1,
    2: 4,
    3: 12,
    4: 36,
    5: 100,
    6: 284,
    7: 780,
    8: 2172,
    9: 5916,
    10: 16268,
}


if __name__ == '__main__':
    # Test shape enumeration
    print("Self-avoiding walk counts:")
    for n in range(1, 8):
        walks = enumerate_self_avoiding_walks(n)
        print(f"  n={n}: {len(walks)} walks")

    print("\nHeuristic shapes for n=12:")
    shapes = heuristic_shapes(12)
    for i, shape in enumerate(shapes[:5]):
        print(f"  {i}: {shape.cells}")
    print(f"  ... ({len(shapes)} total)")

    print("\nShape properties:")
    shape = Shape([(0, 0), (1, 0), (1, 1), (2, 1)])
    print(f"  Original: {shape}")
    print(f"  Bounding box: {shape.bounding_box}")
    print(f"  Size: {shape.width}x{shape.height}")
    print(f"  Rotated 90: {shape.rotate_90()}")
