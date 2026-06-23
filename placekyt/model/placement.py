"""Physical placement of a block's cells on a chip grid.

Mirrors the ``placement:`` section of a block in the ``.kyt`` schema
(the architecture notes §2.1)::

    placement:
      chip: 0
      cells:
        - {cell_id: ff0, x: 7, y: 1, face: west}
        - {cell_id: ff1, x: 6, y: 1, face: west}
      transit_cells:
        - {x: 8, y: 0, face: east}

A block with no ``placement`` (or an incomplete one) is "unplaced" — modeled by
``Block.placement is None`` rather than by an empty ``Placement`` here.

These are mutable dataclasses: the canvas edits cell coordinates and faces
through the command system, which writes back into these objects (the data
model is the single source of truth for positions per §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .enums import Face

# A cell identifier is whatever the block definition uses: an int like ``0`` or
# a string like ``"ff0"`` (§2.1 permits both). The original scalar type is
# preserved so integer ids round-trip unquoted through YAML.
CellId = Union[int, str]


@dataclass
class PlacedCell:
    """One cell of a block pinned to a grid position with an output face.

    ``cell_id`` matches the cell identifier in the block definition's ``cells:``
    list.
    """

    cell_id: CellId
    x: int
    y: int
    face: Face

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)


@dataclass
class TransitCell:
    """A routing-only cell: FACE config set to a direction, no program.

    Transit cells carry data between blocks via hop-count routing. They never
    hold instructions (DRC ``transit_programmed`` enforces all-zero memory).
    They have no ``cell_id`` — they are identified by position.
    """

    x: int
    y: int
    face: Face

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)


@dataclass
class InstrOverride:
    """A user override of one WRITE/JUMP instruction's handoff target (§3.3).

    The hop count and destination/entry address of a WRITE/JUMP are properties
    of the *instruction itself*, not of the route — the route is passive
    (it only connects cells; the cell decides where its result lands). The
    build auto-fills these from the route + the downstream block's interface,
    but the user may override any field here.

    Fields are all optional; ``None`` means "use the auto-computed value":

    * ``hop`` — handoff distance in **hops away** (``@N`` assembly semantics,
      NOT the raw ``HOP_CNT`` field; the build encodes ``HOP_CNT = 31 - hop``).
    * ``dest`` — destination address for a WRITE: a data register (R0–R31) or,
      when ``dest_config`` is set, a CONFIG address (C0–C31, e.g. C1=FACE). A
      JUMP cannot target CONFIG, so ``dest_config`` is meaningless for JUMP.
    * ``entry`` — entry address for a JUMP (the downstream block's entry point).
    * ``dest_config`` — True if a WRITE ``dest`` names a CONFIG address (sets the
      WRITE config bit). Defaults False (a normal data-register WRITE).

    Overrides are keyed by ``(cell_id, addr)`` on the owning :class:`Placement`,
    so they travel with the block when it is dragged and round-trip through the
    ``.kyt`` file.
    """

    hop: int | None = None
    dest: int | None = None
    entry: int | None = None
    dest_config: bool = False

    @property
    def is_empty(self) -> bool:
        return (self.hop is None and self.dest is None and self.entry is None
                and not self.dest_config)


@dataclass
class Placement:
    """A block's concrete placement on one chip.

    All cells of a block must lie on a single chip (DRC ``block_spans_chips``);
    that chip is recorded here as ``chip`` (a chip *instance* id within the
    project, not a chip type).

    ``instr_overrides`` holds per-instruction handoff overrides, keyed by
    ``cell_id`` then by instruction address. See :class:`InstrOverride`.
    """

    chip: int
    cells: list[PlacedCell] = field(default_factory=list)
    transit_cells: list[TransitCell] = field(default_factory=list)
    instr_overrides: dict[CellId, dict[int, "InstrOverride"]] = field(
        default_factory=dict)
    # Cumulative D4 transforms applied to this placement (in order), so the build
    # can transform a block's IN-PROGRAM face constants (a ``MOVE [FACE], k``
    # picks an ABSOLUTE direction; when the block is rotated/mirrored that
    # direction must rotate with it). Empty = as-authored. See ``transform``.
    orientation: list[str] = field(default_factory=list)

    def override(self, cell_id: CellId, addr: int) -> "InstrOverride | None":
        """Return the override for ``(cell_id, addr)``, or ``None`` if absent."""
        return self.instr_overrides.get(cell_id, {}).get(addr)

    def set_override(self, cell_id: CellId, addr: int,
                     ov: "InstrOverride | None") -> None:
        """Set (or clear, when ``ov`` is None/empty) one instruction override."""
        if ov is None or ov.is_empty:
            cell = self.instr_overrides.get(cell_id)
            if cell is not None:
                cell.pop(addr, None)
                if not cell:
                    self.instr_overrides.pop(cell_id, None)
            return
        self.instr_overrides.setdefault(cell_id, {})[addr] = ov

    def cell(self, cell_id: CellId) -> PlacedCell | None:
        """Return the placed cell with the given id, or ``None`` if absent."""
        for c in self.cells:
            if c.cell_id == cell_id:
                return c
        return None

    def occupied_positions(self) -> set[tuple[int, int]]:
        """Every grid position this placement occupies (block + transit cells).

        Used by project-level overlap detection before the engine's DRC runs.
        """
        positions = {c.pos for c in self.cells}
        positions.update(t.pos for t in self.transit_cells)
        return positions

    def bounding_box(self) -> tuple[int, int, int, int] | None:
        """``(min_x, min_y, max_x, max_y)`` over block cells, or ``None`` if empty.

        Transit cells are excluded — the bounding box describes the block's
        footprint, which the canvas uses for selection and zoom-to-fit.
        """
        if not self.cells:
            return None
        xs = [c.x for c in self.cells]
        ys = [c.y for c in self.cells]
        return (min(xs), min(ys), max(xs), max(ys))

    def full_bounding_box(self) -> tuple[int, int, int, int] | None:
        """Bounding box over block cells AND transit cells — the full footprint
        a transform must pivot around so transit cells stay attached."""
        positions = list(self.occupied_positions())
        if not positions:
            return None
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        return (min(xs), min(ys), max(xs), max(ys))

    def transform(self, kind: str) -> None:
        """Rotate/mirror this placement in place, pivoting on its full footprint
        (block + transit cells) and re-anchoring at the same top-left corner so
        the block stays put. Each cell's ``face`` is transformed to match so
        routing semantics are preserved. ``kind`` is one of ``"cw"`` / ``"ccw"``
        (90° rotations) / ``"mirror_h"`` / ``"mirror_v"``.

        Coordinates are screen-space (x right, y DOWN). After a 90° rotation the
        footprint's width/height swap; the cells are re-normalised so the
        minimum corner returns to the original ``(min_x, min_y)``.
        """
        box = self.full_bounding_box()
        if box is None:
            return
        minx, miny, maxx, maxy = box
        w, h = maxx - minx, maxy - miny

        def map_xy(x: int, y: int) -> tuple[int, int]:
            u, v = x - minx, y - miny           # local coords within the box
            if kind == "cw":
                return minx + (h - v), miny + u
            if kind == "ccw":
                return minx + v, miny + (w - u)
            if kind == "mirror_h":
                return minx + (w - u), y
            if kind == "mirror_v":
                return x, miny + (h - v)
            raise ValueError(f"unknown transform {kind!r}")

        def map_face(f: Face) -> Face:
            return {
                "cw": f.rotated_cw,
                "ccw": f.rotated_ccw,
                "mirror_h": f.mirrored_h,
                "mirror_v": f.mirrored_v,
            }[kind]

        for c in self.cells:
            c.x, c.y = map_xy(c.x, c.y)
            c.face = map_face(c.face)
        for t in self.transit_cells:
            t.x, t.y = map_xy(t.x, t.y)
            t.face = map_face(t.face)
        # Record the transform so the build can apply the SAME D4 map to the
        # block's in-program face constants (the cell `.face` above is the
        # resting/layout face; a `MOVE [FACE], const` inside the program names an
        # absolute direction that must rotate identically).
        self.orientation.append(kind)
