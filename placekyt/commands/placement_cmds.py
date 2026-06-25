"""Placement commands: place/move/remove blocks and cells (§4.2)."""

from __future__ import annotations

import copy

from model.block import Block
from model.enums import Face
from model.placement import CellId, Placement, PlacedCell, TransitCell
from model.project import Project

from .base import Command, CompositeCommand


class PlaceCellCommand(Command):
    """Place (or reposition) one cell of a block. Undo restores the prior
    cell state (or removes the cell if it was newly added)."""

    def __init__(self, project: Project, block_name: str, chip: int,
                 cell_id: CellId, x: int, y: int, face: Face):
        self.project = project
        self.block_name = block_name
        self.chip = chip
        self.cell_id = cell_id
        self.x, self.y, self.face = x, y, face
        self._prev: PlacedCell | None = None
        self._had_placement = False

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        self._had_placement = block is not None and block.placement is not None
        if block and block.placement is not None:
            existing = block.placement.cell(self.cell_id)
            self._prev = copy.copy(existing) if existing else None
        self.project._place_cell(self.block_name, self.chip, self.cell_id,
                                 self.x, self.y, self.face)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._place_cell(self.block_name, self.chip, self.cell_id,
                                     self._prev.x, self._prev.y, self._prev.face)
        else:
            self.project._unplace_cell(self.block_name, self.cell_id)

    def description(self) -> str:
        return f"Place {self.block_name}[{self.cell_id}]"


class PlaceTransitCommand(Command):
    """Place (or reposition) a block-internal transit (routing-only) cell. Undo
    removes it. Used for a block's hand-authored internal relays."""

    def __init__(self, project: Project, block_name: str, chip: int,
                 x: int, y: int, face: Face):
        self.project = project
        self.block_name = block_name
        self.chip = chip
        self.x, self.y, self.face = x, y, face

    def execute(self) -> None:
        self.project._place_transit(self.block_name, self.chip,
                                    self.x, self.y, self.face)

    def undo(self) -> None:
        self.project._unplace_transit(self.block_name, self.x, self.y)

    def description(self) -> str:
        return f"Place {self.block_name} transit ({self.x},{self.y})"


class PlaceBlockCommand(CompositeCommand):
    """Add a block and place all its cells atomically (§4.2 — a 45-cell DFE is
    one composite of its block-cell + transit-cell placements)."""

    def __init__(self, project: Project, block: Block, chip: int,
                 cells: list[PlacedCell],
                 transit_cells: list[TransitCell] | None = None):
        self.project = project
        self.block = block
        self._add = _AddBlockCommand(project, block)
        cell_cmds = [
            PlaceCellCommand(project, block.name, chip, c.cell_id, c.x, c.y, c.face)
            for c in cells
        ]
        transit_cmds = [
            PlaceTransitCommand(project, block.name, chip, t.x, t.y, t.face)
            for t in (transit_cells or [])
        ]
        # The block's anchor (x, y) for replay = its FIRST placed cell.
        self._anchor = (chip, cells[0].x, cells[0].y) if cells else (chip, 0, 0)
        super().__init__(f"Place block {block.name}",
                         [self._add, *cell_cmds, *transit_cmds])

    def to_trace(self) -> dict:
        chip, x, y = self._anchor
        return {"op": "place_block",
                "args": {"type_name": self.block.type, "chip": chip,
                         "x": x, "y": y, "library": self.block.library,
                         "params": dict(self.block.params),
                         "name": self.block.name}}


class _AddBlockCommand(Command):
    """Add a block instance (no placement). Internal — used by PlaceBlockCommand
    and exposed indirectly; undo removes the block."""

    def __init__(self, project: Project, block: Block):
        self.project = project
        self.block = block

    def execute(self) -> None:
        self.project._add_block(self.block)

    def undo(self) -> None:
        self.project._remove_block(self.block.name)

    def description(self) -> str:
        return f"Add block {self.block.name}"


class MoveBlockCommand(Command):
    """Translate every cell of a placed block by (dx, dy). Undo translates back.

    A move shifts the block's cells to a new location, so any existing physical
    route to/from this block is left pinned at the block's OLD coordinates — its
    waypoints no longer touch the block. Like :class:`TransformBlockCommand`, this
    therefore UNROUTES every connection touching the block (clears its waypoints)
    while PRESERVING the logical net: the connection re-derives from the new
    position as a clean fly line (or abutment), so the user re-routes or
    re-abuts, instead of leaving stale, unselectable route-marker cells stranded
    at the old location and phantom fly lines on nets that never got reconciled.
    Undo restores both the placement and the original routes.
    """

    def __init__(self, project: Project, block_name: str, dx: int, dy: int):
        self.project = project
        self.block_name = block_name
        self.dx, self.dy = dx, dy
        # (connection_name, prior_route) snapshots so undo restores the routes.
        self._prev_routes: list = []

    def _shift(self, dx: int, dy: int) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None:
            raise KeyError(f"block {self.block_name!r} not placed")
        pl = block.placement
        for c in pl.cells:
            self.project._place_cell(self.block_name, pl.chip, c.cell_id,
                                     c.x + dx, c.y + dy, c.face)
        for t in pl.transit_cells:
            t.x += dx
            t.y += dy

    def execute(self) -> None:
        # Clear the route of every connection touching this block (keep the net),
        # snapshotting the prior routes for undo. The block's cells then move
        # without leaving stale waypoints behind.
        self._prev_routes = []
        for conn in self.project.connections_for_block(self.block_name):
            self._prev_routes.append((conn.name, conn.route))
            conn.route = None
        self._shift(self.dx, self.dy)

    def undo(self) -> None:
        self._shift(-self.dx, -self.dy)
        for name, route in self._prev_routes:
            conn = self.project.connection(name)
            if conn is not None:
                conn.route = route

    def description(self) -> str:
        return f"Move {self.block_name}"

    def to_trace(self) -> dict:
        return {"op": "move_block",
                "args": {"block_name": self.block_name,
                         "dx": self.dx, "dy": self.dy}}


class MoveBlockToChipCommand(Command):
    """Move a whole block to a DIFFERENT chip, anchoring it at ``(ax, ay)`` on
    the target chip (drop-point placement, like a fresh library drag).

    Cell (x, y) are chip-relative, so moving chips is mostly setting
    ``placement.chip``; the cells are additionally shifted so the block's first
    cell lands at the drop anchor, preserving the block's shape. Undo restores
    the exact prior placement (chip + cells + transit cells).
    """

    def __init__(self, project: Project, block_name: str, chip: int,
                 ax: int, ay: int):
        self.project = project
        self.block_name = block_name
        self.chip = chip
        self.ax, self.ay = ax, ay
        self._prev: Placement | None = None
        self._connections: list = []  # routes removed by the move (for undo)

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None or not block.placement.cells:
            raise KeyError(f"block {self.block_name!r} not placed")
        pl = block.placement
        self._prev = copy.deepcopy(pl)
        # The block's routes reference cells on its OLD chip — moving chips
        # breaks them. Remove them (restored on undo); the user re-routes on the
        # new chip. Routes NOT referencing this block are untouched.
        self._connections = [
            copy.deepcopy(c)
            for c in self.project.connections_for_block(self.block_name)
        ]
        for conn in self._connections:
            self.project._remove_connection(conn.name)
        # Shift so the FIRST cell (the anchor) lands at (ax, ay) on the new chip.
        anchor = pl.cells[0]
        dx, dy = self.ax - anchor.x, self.ay - anchor.y
        new_cells = [PlacedCell(c.cell_id, c.x + dx, c.y + dy, c.face)
                     for c in pl.cells]
        new_transit = [copy.deepcopy(t) for t in pl.transit_cells]
        for t in new_transit:
            t.x += dx
            t.y += dy
        new_pl = Placement(chip=self.chip, cells=new_cells,
                           transit_cells=new_transit,
                           instr_overrides=copy.deepcopy(pl.instr_overrides))
        self.project._set_block_placement(self.block_name, new_pl)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._set_block_placement(
                self.block_name, copy.deepcopy(self._prev))
        for conn in self._connections:
            self.project._add_connection(copy.deepcopy(conn))

    def description(self) -> str:
        return f"Move {self.block_name} to chip {self.chip}"


class TransformBlockCommand(Command):
    """Rotate or mirror a placed block in place (§3.2). The block pivots on its
    own footprint and re-anchors at the same top-left corner, so it stays put;
    each cell's face is transformed so routing semantics survive.

    A transform reshapes the cells under any existing physical routes, so this
    block's connections are UNROUTED (their waypoints cleared) — but the LOGICAL
    NETS are PRESERVED: the fly line reappears so the user can re-route, or simply
    re-attach by abutment, without losing the connection. (Previously this removed
    the connections entirely, so the fly lines vanished and the design lost its
    wiring after a transform — the packed-layout breakage.) Undo restores both the
    placement and the original routes.

    ``kind`` ∈ ``{"cw", "ccw", "mirror_h", "mirror_v"}``.
    """

    def __init__(self, project: Project, block_name: str, kind: str):
        self.project = project
        self.block_name = block_name
        self.kind = kind
        self._prev: Placement | None = None
        # (connection_name, prior_route) snapshots so undo restores the routes.
        self._prev_routes: list = []

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None or not block.placement.cells:
            raise KeyError(f"block {self.block_name!r} not placed")
        pl = block.placement
        self._prev = copy.deepcopy(pl)
        # Clear the route of every connection touching this block (keep the net).
        self._prev_routes = []
        for conn in self.project.connections_for_block(self.block_name):
            self._prev_routes.append((conn.name, conn.route))
            conn.route = None
        new_pl = copy.deepcopy(pl)
        new_pl.transform(self.kind)
        self.project._set_block_placement(self.block_name, new_pl)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._set_block_placement(
                self.block_name, copy.deepcopy(self._prev))
        for name, route in self._prev_routes:
            conn = self.project.connection(name)
            if conn is not None:
                conn.route = route

    def description(self) -> str:
        names = {"cw": "Rotate CW", "ccw": "Rotate CCW",
                 "mirror_h": "Mirror H", "mirror_v": "Mirror V"}
        return f"{names.get(self.kind, self.kind)} {self.block_name}"

    def to_trace(self) -> dict:
        return {"op": "transform_block",
                "args": {"block_name": self.block_name, "kind": self.kind}}


class OrientBlockCommand(Command):
    """Rotate/mirror a placed block WITHOUT touching its connections — for
    auto-orient (P3.2), where the connections are still UNROUTED logical nets
    (no waypoints to invalidate). Unlike :class:`TransformBlockCommand` (the
    manual flow, which drops routes), this preserves the logical nets so the
    auto-router can route them in the same pass. Undo restores the placement.
    """

    def __init__(self, project: Project, block_name: str, kind: str):
        self.project = project
        self.block_name = block_name
        self.kind = kind
        self._prev: Placement | None = None

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None or block.placement is None or not block.placement.cells:
            raise KeyError(f"block {self.block_name!r} not placed")
        self._prev = copy.deepcopy(block.placement)
        new_pl = copy.deepcopy(block.placement)
        new_pl.transform(self.kind)
        self.project._set_block_placement(self.block_name, new_pl)

    def undo(self) -> None:
        if self._prev is not None:
            self.project._set_block_placement(
                self.block_name, copy.deepcopy(self._prev))

    def description(self) -> str:
        return f"Orient {self.block_name} ({self.kind})"

    def to_trace(self) -> dict:
        return {"op": "transform_block",
                "args": {"block_name": self.block_name, "kind": self.kind}}


class RemoveBlockCommand(Command):
    """Remove a block, its placement, and all connections referencing it,
    atomically. Undo restores everything (§4.1)."""

    def __init__(self, project: Project, block_name: str):
        self.project = project
        self.block_name = block_name
        self._block: Block | None = None
        self._connections: list = []

    def execute(self) -> None:
        block = self.project.block(self.block_name)
        if block is None:
            raise KeyError(f"no block named {self.block_name!r}")
        # Snapshot for undo (deep copy so later edits don't alias).
        self._block = copy.deepcopy(block)
        self._connections = [
            copy.deepcopy(c)
            for c in self.project.connections_for_block(self.block_name)
        ]
        for conn in self._connections:
            self.project._remove_connection(conn.name)
        self.project._remove_block(self.block_name)

    def undo(self) -> None:
        if self._block is not None:
            self.project._add_block(copy.deepcopy(self._block))
        for conn in self._connections:
            self.project._add_connection(copy.deepcopy(conn))

    def description(self) -> str:
        return f"Remove block {self.block_name}"
