"""The project aggregate root — the in-memory model of a ``.kyt`` file.

Mirrors the top-level ``.kyt`` schema (the architecture notes §2.1). This class holds
the complete editable project state: metadata, the chip type, the board
reference, chip instances, inter-chip connections, blocks, connections, the
(Phase 2) mode-switching table, and simulation references.

This module is pure data + light bookkeeping. It deliberately:
  * imports no Qt, no simkyt, no ``gr_kyttar`` (§6 dependency rule);
  * performs NO YAML I/O — load/save lives in the engine/IO layer (Week 1-2
    plan: ".kyt YAML load/save" is a separate task layered on top of this);
  * exposes private ``_mutators`` (leading underscore) that the command layer
    drives. Public read access is fine; public mutation goes through commands
    so undo/redo and dirty tracking stay correct (§4.1).

Two independent dirty flags (§4.2 "Save State Management"):
  * ``project_dirty`` — unsaved model changes; cleared by save.
  * ``build_dirty``   — the current bitstream is stale; cleared only by a fresh
    build, never by save or undo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .block import Block
from .board import Board
from .chip import ChipInstance
from .connection import Connection, InterChipConnection, PanelConnection
from .enums import Face
from .panel import SramPanel
from .events import EventBus
from .placement import CellId, PlacedCell, Placement, TransitCell


@dataclass
class ProjectMetadata:
    """The ``project:`` header block (§2.1)."""

    name: str = "Untitled"
    version: str = "1.0"
    author: str = ""
    created: str = ""
    modified: str = ""
    format_version: int = 1


@dataclass
class BoardRef:
    """Reference to a board config file (§2.1 ``board:``)."""

    name: str
    config: str = ""  # path to the .kdb file


@dataclass
class FpgaModelRef:
    """A simulation-side FPGA model binding (§2.1 ``simulation.fpga_models``)."""

    connection: str
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class SimulationConfig:
    """The ``simulation:`` section (§2.1).

    Paths are stored as declared; they are NOT auto-loaded on project open
    (path-traversal protection, §2.1) — only when the user runs simulation.
    """

    default_stimulus: str | None = None
    golden_output: str | None = None
    golden_bits: str | None = None
    golden_symbols: str | None = None
    gnuradio_flowgraph: str | None = None
    fpga_latency_ns: float = 20.0
    fpga_models: list[FpgaModelRef] = field(default_factory=list)


@dataclass
class FaceOverride:
    """A single per-cell face override within a mode (§2.1 ``mode_switching``)."""

    chip: int
    x: int
    y: int
    face: Face


@dataclass
class Project:
    """In-memory model of a placeKYT project.

    Construct directly for tests; the engine/IO layer provides ``open()`` /
    ``save()`` and the command layer provides the mutation API. The chip-type
    *registry* (resolving ``chip_type`` to a :class:`ChipType`) is owned by the
    engine layer, so the model holds only the type *name* here.
    """

    metadata: ProjectMetadata = field(default_factory=ProjectMetadata)
    chip_type: str = ""
    board: BoardRef | None = None
    board_config: Board | None = None  # resolved board, populated by engine layer

    chips: list[ChipInstance] = field(default_factory=list)
    inter_chip_connections: list[InterChipConnection] = field(default_factory=list)
    panels: list[SramPanel] = field(default_factory=list)
    panel_connections: list[PanelConnection] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)

    # Phase 2 mode switching (schema present for forward-compat; §2.1).
    mode_switching: dict[str, list[FaceOverride]] = field(default_factory=dict)

    simulation: SimulationConfig = field(default_factory=SimulationConfig)

    # Unknown YAML fields preserved verbatim for round-trip safety (§2.1). The
    # IO layer stashes them here so save() can re-emit them.
    extra: dict = field(default_factory=dict)

    # -- runtime-only state (not serialized) ----------------------------------
    event_bus: EventBus = field(default_factory=EventBus, compare=False)
    project_dirty: bool = field(default=False, compare=False)
    build_dirty: bool = field(default=True, compare=False)
    current_generation_id: int = field(default=0, compare=False)
    # Monotonic DESIGN version: bumped on every mutation (mark_dirty), NEVER cleared
    # by a build. Unlike ``build_dirty`` (which the GUI's post-edit cached_build()
    # clears as soon as it refreshes the inspector/faces), this lets an independent
    # consumer — the GNURadio SERVER — detect "the design changed since I last hosted
    # it" and rebuild, even after the GUI already consumed build_dirty. A consumer
    # records the version it built and compares.
    design_version: int = field(default=0, compare=False)

    # -- lookups --------------------------------------------------------------

    def block(self, name: str) -> Block | None:
        for b in self.blocks:
            if b.name == name:
                return b
        return None

    def chip(self, chip_id: int) -> ChipInstance | None:
        for c in self.chips:
            if c.id == chip_id:
                return c
        return None

    def panel(self, panel_id: int) -> SramPanel | None:
        for p in self.panels:
            if p.id == panel_id:
                return p
        return None

    def panel_connections_for(self, panel_id: int) -> list[PanelConnection]:
        """All panel-to-chip links touching the given panel."""
        return [c for c in self.panel_connections if c.panel == panel_id]

    def connection(self, name: str) -> Connection | None:
        for c in self.connections:
            if c.name == name:
                return c
        return None

    def connections_for_block(self, block_name: str) -> list[Connection]:
        """All connections referencing ``block_name`` on either endpoint (§4.1)."""
        from .connection import BlockEndpoint  # local import: avoid cycle noise

        result = []
        for c in self.connections:
            for ep in (c.source, c.target):
                if isinstance(ep, BlockEndpoint) and ep.block == block_name:
                    result.append(c)
                    break
        return result

    # -- dirty tracking -------------------------------------------------------

    def mark_dirty(self) -> None:
        """Mark both project and build as dirty (called on every mutation).

        The command layer calls this; the distinction between the two flags is
        re-applied by save() (clears ``project_dirty``) and build() (clears
        ``build_dirty``) — see the module docstring.
        """
        self.project_dirty = True
        self.build_dirty = True
        self.design_version += 1

    def next_generation_id(self) -> int:
        """Bump and return the monotonic generation counter (§4.2 generator sync)."""
        self.current_generation_id += 1
        return self.current_generation_id

    # -- mutators (driven by the command layer) -------------------------------
    #
    # These mutate state and EMIT events on the bus but NEVER flush it (§6 —
    # only the CommandManager / Project.open / etc. flush). They are private
    # (leading underscore): public cell/face access is read-only (§4.1), all
    # mutation goes through commands so undo/redo + dirty tracking stay correct.

    def _add_chip(self, chip: ChipInstance) -> None:
        self.chips.append(chip)
        self.event_bus.emit("chip_added", chip_id=chip.id)

    def _remove_chip(self, chip_id: int) -> ChipInstance | None:
        chip = self.chip(chip_id)
        if chip is not None:
            self.chips.remove(chip)
            self.event_bus.emit("chip_removed", chip_id=chip_id)
        return chip

    def _add_block(self, block: Block) -> None:
        self.blocks.append(block)
        self.event_bus.emit("block_added", name=block.name)

    def _remove_block(self, name: str) -> Block | None:
        block = self.block(name)
        if block is not None:
            self.blocks.remove(block)
            self.event_bus.emit("block_removed", name=name)
        return block

    def _set_block_placement(self, name: str, placement: Placement | None) -> None:
        block = self.block(name)
        if block is None:
            raise KeyError(f"no block named {name!r}")
        block.placement = placement
        self.event_bus.emit("block_placement_changed", name=name)

    def _place_cell(self, name: str, chip: int, cell_id: CellId,
                    x: int, y: int, face: Face) -> None:
        """Add/replace one placed cell of a block (creates a Placement if absent)."""
        block = self.block(name)
        if block is None:
            raise KeyError(f"no block named {name!r}")
        if block.placement is None:
            block.placement = Placement(chip=chip)
        pl = block.placement
        existing = pl.cell(cell_id)
        if existing is not None:
            existing.x, existing.y, existing.face = x, y, face
        else:
            pl.cells.append(PlacedCell(cell_id, x, y, face))
        self.event_bus.emit("cell_placed", name=name, chip=chip, x=x, y=y)

    def _place_transit(self, name: str, chip: int, x: int, y: int,
                       face: Face) -> None:
        """Add/replace a block-internal transit (routing-only) cell. Used for a
        block's hand-authored internal relays (e.g. the DFE's relay into the
        decision cell) — a program-less FACE-only cell that moves with the block.
        """
        block = self.block(name)
        if block is None:
            raise KeyError(f"no block named {name!r}")
        if block.placement is None:
            block.placement = Placement(chip=chip)
        pl = block.placement
        for t in pl.transit_cells:
            if (t.x, t.y) == (x, y):
                t.face = face
                self.event_bus.emit("cell_placed", name=name, chip=chip, x=x, y=y)
                return
        pl.transit_cells.append(TransitCell(x, y, face))
        self.event_bus.emit("cell_placed", name=name, chip=chip, x=x, y=y)

    def _unplace_transit(self, name: str, x: int, y: int) -> None:
        block = self.block(name)
        if block is None or block.placement is None:
            return
        pl = block.placement
        pl.transit_cells = [t for t in pl.transit_cells if (t.x, t.y) != (x, y)]
        self.event_bus.emit("cell_unplaced", name=name, x=x, y=y)
        if not pl.cells and not pl.transit_cells:
            block.placement = None

    def _unplace_cell(self, name: str, cell_id: CellId) -> None:
        block = self.block(name)
        if block is None or block.placement is None:
            return
        pl = block.placement
        cell = pl.cell(cell_id)
        if cell is not None:
            pl.cells.remove(cell)
            self.event_bus.emit("cell_unplaced", name=name, x=cell.x, y=cell.y)
        if not pl.cells and not pl.transit_cells:
            block.placement = None

    def _set_cell_face(self, name: str, cell_id: CellId, face: Face) -> None:
        block = self.block(name)
        if block is None or block.placement is None:
            raise KeyError(f"block {name!r} not placed")
        cell = block.placement.cell(cell_id)
        if cell is None:
            raise KeyError(f"block {name!r} has no cell {cell_id!r}")
        cell.face = face
        self.event_bus.emit("cell_face_changed", name=name,
                            x=cell.x, y=cell.y, face=face.value)

    def _set_block_params(self, name: str, params: dict) -> None:
        block = self.block(name)
        if block is None:
            raise KeyError(f"no block named {name!r}")
        block.params = dict(params)
        self.event_bus.emit("block_params_changed", name=name)

    def _rename_block(self, old: str, new: str) -> None:
        """Rename a block instance and rewrite every reference to it.

        Connections are frozen dataclasses, so any whose endpoint names the
        block are replaced in place with renamed copies (preserving route +
        metadata). The block's own ``name`` is updated. Caller must ensure
        ``new`` is non-empty and not already taken (the command does)."""
        from dataclasses import replace

        from .connection import BlockEndpoint

        block = self.block(old)
        if block is None:
            raise KeyError(f"no block named {old!r}")
        block.name = new
        for i, c in enumerate(self.connections):
            src, tgt = c.source, c.target
            new_src = (replace(src, block=new)
                       if isinstance(src, BlockEndpoint) and src.block == old
                       else src)
            new_tgt = (replace(tgt, block=new)
                       if isinstance(tgt, BlockEndpoint) and tgt.block == old
                       else tgt)
            if new_src is not src or new_tgt is not tgt:
                self.connections[i] = replace(c, source=new_src, target=new_tgt)
        self.event_bus.emit("block_renamed", old=old, new=new)

    def _add_connection(self, conn: Connection) -> None:
        self.connections.append(conn)
        self.event_bus.emit("connection_added", name=conn.name)

    def _remove_connection(self, name: str) -> Connection | None:
        conn = self.connection(name)
        if conn is not None:
            self.connections.remove(conn)
            self.event_bus.emit("connection_removed", name=name)
        return conn

    def _add_inter_chip(self, ic: InterChipConnection) -> None:
        self.inter_chip_connections.append(ic)
        self.event_bus.emit("inter_chip_added",
                            from_chip=ic.from_chip, to_chip=ic.to_chip)

    def _remove_inter_chip(self, ic: InterChipConnection) -> bool:
        if ic in self.inter_chip_connections:
            self.inter_chip_connections.remove(ic)
            self.event_bus.emit("inter_chip_removed",
                                from_chip=ic.from_chip, to_chip=ic.to_chip)
            return True
        return False

    # -- SRAM / peripheral panels (the SRAM panel notes) -----------------------------

    def _add_panel(self, panel: SramPanel) -> None:
        self.panels.append(panel)
        self.event_bus.emit("panel_added", panel_id=panel.id)

    def _remove_panel(self, panel_id: int) -> SramPanel | None:
        panel = self.panel(panel_id)
        if panel is not None:
            # Drop the panel's links along with it.
            self.panel_connections = [
                c for c in self.panel_connections if c.panel != panel_id]
            self.panels.remove(panel)
            self.event_bus.emit("panel_removed", panel_id=panel_id)
        return panel

    def _add_panel_connection(self, pc: PanelConnection) -> None:
        self.panel_connections.append(pc)
        self.event_bus.emit("panel_connection_added",
                            panel=pc.panel, chip=pc.chip)

    def _remove_panel_connection(self, pc: PanelConnection) -> bool:
        if pc in self.panel_connections:
            self.panel_connections.remove(pc)
            self.event_bus.emit("panel_connection_removed",
                                panel=pc.panel, chip=pc.chip)
            return True
        return False

    def _set_instr_override(self, block_name: str, cell_id, addr: int,
                            override) -> None:
        """Set (or clear) a per-instruction handoff override on a block cell.

        ``override`` is an :class:`~model.placement.InstrOverride` (or ``None``
        to clear). It lands the WRITE/JUMP at ``addr`` of cell ``cell_id`` a
        specific number of hops away / at a specific dest/entry address (§3.3).
        """
        block = self.block(block_name)
        if block is None or block.placement is None:
            raise KeyError(f"block {block_name!r} not placed")
        block.placement.set_override(cell_id, addr, override)
        self.build_dirty = True
        self.event_bus.emit("instr_override_changed", name=block_name,
                            cell_id=cell_id, addr=addr)
