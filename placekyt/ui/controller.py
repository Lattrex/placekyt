"""AppController — the hub between the UI and the model/command/engine layers.

Owns the live :class:`Project`, its :class:`CommandManager`, the
:class:`BlockCatalog`, and the chip-type registry. The UI calls high-level
methods here (``place_block``, ``move_block``, ``undo`` …); every mutation goes
through a Command so undo/redo and the event bus stay correct (§4.1, §4.2).

The controller subscribes to the project event bus and re-emits a single Qt
signal (``changed``) the views connect to, so the canvas/inspector refresh
without the model knowing about Qt (§6).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from commands import (
    CommandManager,
    EditParamsCommand,
    MoveBlockCommand,
    PlaceBlockCommand,
    RemoveBlockCommand,
    SetCellFaceCommand,
)
from pathlib import Path

from engine.catalog import BlockCatalog
from engine.registry import ChipTypeRegistry
from model.block import Block
from model.chip import ChipInstance
from model.enums import Face
from model.placement import PlacedCell
from model.project import Project
from resources import resource_path


class AppController(QObject):
    """Mediates UI ↔ model. One per open project."""

    # Emitted (post-flush) whenever the project changed — views refresh on this.
    changed = Signal()
    selection_changed = Signal(object)  # payload: a selection descriptor or None
    # Emitted when the GRC-out-of-sync status changes (after observing GRC params
    # or re-diffing). Payload: the current {block_name: BlockParamDiff} dict
    # (empty ⇒ in sync). The status-bar indicator subscribes to this.
    grc_sync_changed = Signal(object)

    def __init__(self, catalog: BlockCatalog | None = None,
                 registry: ChipTypeRegistry | None = None, parent=None):
        super().__init__(parent)
        self.catalog = catalog or BlockCatalog.from_gr_kyttar()
        self.registry = registry or _default_registry()
        self.project = Project()
        self.commands = CommandManager(self.project)
        self.project_path: Path | None = None
        # GRC↔placeKYT parameter-sync tracker (the GRC-side params last seen for
        # each placed block + the current out-of-sync diff). Reset per project.
        from engine.grc_sync import GrcSyncState
        self.grc_sync = GrcSyncState()
        # Path of the .grc this project was imported from (if any), so the GUI can
        # watch it for re-saves and flag GRC drift on SAVE. None until imported.
        self._grc_source_path = None
        self._wire_bus()

    # -- file I/O -------------------------------------------------------------

    def open_project(self, path: str | Path) -> Project:
        """Load a ``.kyt`` and make it the live project (§4.1)."""
        from engine.io.project_io import load_project

        project = load_project(path)
        self.set_project(project)
        self.project_path = Path(path)
        # Project.open flushes once so views reflect the loaded state (§6).
        project.event_bus.flush()
        return project

    def save_project(self, path: str | Path | None = None) -> Path:
        """Save the live project. ``path`` is required the first time (Save As);
        afterward it defaults to the last path."""
        from engine.io.project_io import save_project

        target = Path(path) if path is not None else self.project_path
        if target is None:
            raise ValueError("no path given and project has never been saved")
        save_project(self.project, target)  # clears project_dirty
        self.project_path = target
        return target

    def new_project(self, name: str, chip_type: str, *, n_chips: int = 1) -> Project:
        """Create a blank project with ``n_chips`` chips of ``chip_type``."""
        from model.project import ProjectMetadata

        project = Project(metadata=ProjectMetadata(name=name), chip_type=chip_type)
        for i in range(n_chips):
            project.chips.append(ChipInstance(i, f"Chip {i}", i * 720.0, 0.0))
        self.set_project(project)
        self.project_path = None
        return project

    # -- project lifecycle ----------------------------------------------------

    def set_project(self, project: Project) -> None:
        self.project = project
        # Preserve the command trace across project swaps so a whole session
        # (import → edits → re-import) is captured as one continuous, replayable
        # log. The first set_project (from __init__) seeds a fresh trace.
        prev_trace = getattr(getattr(self, "commands", None), "trace", None)
        self.commands = CommandManager(project, trace=prev_trace)
        # A new project starts in sync (no GRC params observed yet for it).
        self.grc_sync.clear()
        self._wire_bus()
        self.changed.emit()
        self.grc_sync_changed.emit(self.grc_sync.diffs)

    @property
    def trace(self):
        """The session command trace (shared across project swaps)."""
        return self.commands.trace

    def _wire_bus(self) -> None:
        # Any delivered event → one coalesced refresh signal. The bus is only
        # flushed by the CommandManager (and Project.open), so callbacks always
        # see a consistent post-operation state (§6).
        self.project.event_bus.subscribe_all(self._on_event)

    def _on_event(self, event_type: str, **payload) -> None:
        self.changed.emit()

    def chip_types(self) -> dict:
        return self.registry.chip_types()

    # -- block placement ------------------------------------------------------

    def _unique_block_name(self, base: str) -> str:
        if self.project.block(base) is None:
            return base
        i = 1
        while self.project.block(f"{base}_{i}") is not None:
            i += 1
        return f"{base}_{i}"

    def default_cells(self, type_name: str, library: str | None,
                      chip: int, x: int, y: int,
                      params: dict | None = None
                      ) -> tuple[list[PlacedCell], list]:
        """Cells for a newly-placed block at anchor (x, y).

        Uses the block's hand-authored ``default_layout`` (§2.2) — the tuned
        arrangement the block author specified (e.g. the DFE serpentine), or a
        serpentine fallback that wraps within the array. Each layout entry is
        ``cell_id -> (dx, dy, face)``; positions are offset from the anchor.

        A layout entry whose ``cell_id`` is a string starting with ``"transit"``
        is a block-INTERNAL routing-only cell (a relay, e.g. the DFE's relay into
        its decision cell): it carries a FACE but no program. These become
        :class:`TransitCell` s, not programmed cells.

        Returns ``(placed_cells, transit_cells)``. Falls back to a single cell
        when no layout is available.
        """
        from model.placement import TransitCell

        layout = self.catalog.default_layout(type_name, params, library=library)
        if not layout:
            return [PlacedCell(0, x, y, Face.EAST)], []
        # Normalize so the layout's bounding box starts at (0,0); otherwise a
        # block whose authored offsets begin at, say, (7,1) (the DFE) would land
        # shifted up/left of the drop point and couldn't reach column 0.
        min_dx = min(dx for dx, _dy, _f in layout.values())
        min_dy = min(dy for _dx, dy, _f in layout.values())
        placed: list[PlacedCell] = []
        transit: list[TransitCell] = []
        for cid, (dx, dy, face) in layout.items():
            ax, ay = x + dx - min_dx, y + dy - min_dy
            if isinstance(cid, str) and cid.startswith("transit"):
                transit.append(TransitCell(ax, ay, Face.from_str(face)))
            else:
                placed.append(PlacedCell(cid, ax, ay, Face.from_str(face)))
        return placed, transit

    def place_block(self, type_name: str, chip: int, x: int, y: int,
                    *, library: str | None = None,
                    params: dict | None = None, name: str | None = None) -> str:
        """Instantiate and place a block at (x, y). Returns the block name."""
        spec = self.catalog.get(type_name, library)
        if spec is None:
            raise KeyError(f"unknown block type {type_name!r}")
        block_name = name or self._unique_block_name(_default_name(type_name))
        resolved_params = dict(params or spec.default_params())
        block = Block(block_name, type_name, library=library or spec.library,
                      params=resolved_params)
        cells, transit = self.default_cells(
            type_name, library, chip, x, y, resolved_params)
        self.commands.execute(
            PlaceBlockCommand(self.project, block, chip, cells, transit))
        return block_name

    def move_block(self, block_name: str, dx: int, dy: int) -> None:
        self.commands.execute(MoveBlockCommand(self.project, block_name, dx, dy))

    def move_block_to_chip(self, block_name: str, chip: int,
                           ax: int, ay: int) -> None:
        """Move a whole block to another chip, anchored at ``(ax, ay)`` (drop-
        point placement, like a fresh drag). Undoable."""
        from commands import MoveBlockToChipCommand

        self.commands.execute(
            MoveBlockToChipCommand(self.project, block_name, chip, ax, ay))

    def transform_block(self, block_name: str, kind: str) -> None:
        """Rotate (``"cw"``/``"ccw"``) or mirror (``"mirror_h"``/``"mirror_v"``)
        a placed block in place. Removes the block's routes (restored on undo);
        the user re-routes after orienting. Undoable."""
        from commands import TransformBlockCommand

        self.commands.execute(
            TransformBlockCommand(self.project, block_name, kind))

    def move_cell(self, block_name: str, cell_id, x: int, y: int) -> None:
        """Reposition a single cell of a block (Alt+drag breakout). Undoable."""
        from commands import PlaceCellCommand

        blk = self.project.block(block_name)
        if blk is None or blk.placement is None:
            raise KeyError(f"block {block_name!r} not placed")
        cell = blk.placement.cell(cell_id)
        if cell is None:
            raise KeyError(f"block {block_name!r} has no cell {cell_id!r}")
        self.commands.execute(PlaceCellCommand(
            self.project, block_name, blk.placement.chip, cell_id, x, y, cell.face))

    def remove_block(self, block_name: str) -> None:
        self.commands.execute(RemoveBlockCommand(self.project, block_name))

    def set_cell_face(self, block_name: str, cell_id, face) -> None:
        # Accept a Face OR its string value (e.g. "north") so a command trace can
        # be replayed directly from its serialized args.
        if not isinstance(face, Face):
            face = Face(face)
        self.commands.execute(
            SetCellFaceCommand(self.project, block_name, cell_id, face))

    # -- routing --------------------------------------------------------------

    def remove_connection(self, name: str) -> None:
        """Remove a connection (route) by name. Undoable."""
        from commands import RemoveConnectionCommand

        self.commands.execute(RemoveConnectionCommand(self.project, name))

    def delete_route(self, name: str):
        """Smart-delete a block-to-block route (#267): break ONLY this
        connection's physical path, keeping the logical link as a fly line.

        Sole-occupant transit cells disappear with the route; cells shared with
        another routed connection (a multiplexed bus) STAY. Undoable. Returns the
        executed :class:`~commands.DeleteRouteCommand` so the caller can read
        ``cmd.shared`` / ``cmd.removed_cells`` (which branch ran, what vanished).
        """
        from commands import DeleteRouteCommand

        cmd = DeleteRouteCommand(self.project, name)
        self.commands.execute(cmd)
        return cmd

    def set_instr_override(self, block_name: str, cell_id, addr: int,
                           *, hop=..., dest=..., entry=..., dest_config=...) -> None:
        """Set/clear a per-instruction WRITE/JUMP handoff override (§3.3).

        Each field is tri-state: omit it to leave the current value untouched,
        pass an int to set it, or pass ``None`` to clear it back to the
        route-derived auto value. ``hop`` is in ``@N`` hops-away form.
        ``dest_config`` (bool) marks a WRITE dest as a CONFIG address (C0–C31).
        Setting every field back to its empty value removes the override.
        """
        from commands.edit_cmds import SetInstrOverrideCommand
        from model.placement import InstrOverride

        blk = self.project.block(block_name)
        cur = (blk.placement.override(cell_id, addr)
               if blk and blk.placement else None)
        new = InstrOverride(
            hop=(cur.hop if cur else None) if hop is ... else hop,
            dest=(cur.dest if cur else None) if dest is ... else dest,
            entry=(cur.entry if cur else None) if entry is ... else entry,
            dest_config=((cur.dest_config if cur else False)
                         if dest_config is ... else bool(dest_config)),
        )
        self.commands.execute(SetInstrOverrideCommand(
            self.project, block_name, cell_id, addr,
            None if new.is_empty else new))

    def add_route(self, source, target, points: list, *, name: str | None = None):
        """Route a connection from ``source`` to ``target`` endpoints.

        ``points`` is the full waypoint path (incl. source and target cells) as
        ``[(x, y), …]``. Validates the hop count (distance = len(points)-1, +1
        for a chip-output target — §2.6) and rejects > 31 before committing.

        If a LOGICAL connection between these exact endpoints already exists (e.g.
        an unrouted net imported from GRC, or one whose route the user just
        disconnected to a fly line), this RECONNECTS it — sets its route in place —
        instead of adding a duplicate. Drawing a route on an existing logical net
        must RE-ROUTE that net (and clear its fly line, #271), never create a second
        connection on the same endpoints (which left the original net unrouted, so
        the build still failed with "connection has no physical route"). Returns the
        connection name.
        """
        from commands import AddConnectionCommand, SetConnectionRouteCommand
        from model.connection import (
            ChipPortEndpoint,
            Connection,
            RoutePoint,
        )

        distance = max(0, len(points) - 1)
        if isinstance(target, ChipPortEndpoint) and target.port.endswith("_out"):
            distance += 1
        if distance > 31:
            raise ValueError(
                f"route is {distance} hops (max 31) — shorten the path")

        # Reconnect an existing logical net between the same endpoints (frozen
        # endpoints compare by value), rather than duplicating it.
        if name is None:
            existing = next(
                (c for c in self.project.connections
                 if c.source == source and c.target == target), None)
            if existing is not None:
                # An I/Q COMPLEX PAIR (two logical nets sharing the same physical
                # source-output cell + target-input cell — e.g. MF.yi→Costas.xi and
                # MF.yq→Costas.xq) routes as ONE path. Drawing the route on one must
                # route the sibling(s) too, else the sibling stays a fly line and
                # DRC errors "no physical route" on a link that looks connected.
                from commands import CompositeCommand
                cmds = [SetConnectionRouteCommand(
                    self.project, existing.name, list(points))]
                for sib in self._route_siblings(existing):
                    cmds.append(SetConnectionRouteCommand(
                        self.project, sib.name, list(points)))
                if len(cmds) == 1:
                    self.commands.execute(cmds[0])
                else:
                    names = [existing.name] + [
                        s.name for s in self._route_siblings(existing)]
                    self.commands.execute(
                        CompositeCommand(
                            "Route connection (I/Q pair)", cmds,
                            trace_op={"op": "set_route_group",
                                      "args": {"names": names,
                                               "points": [list(p) for p in points]}}))
                return existing.name

        conn_name = name or self._unique_connection_name(source, target)
        conn = Connection(
            conn_name, source=source, target=target,
            route=[RoutePoint(x, y) for x, y in points],
        )
        self.commands.execute(AddConnectionCommand(self.project, conn))
        return conn_name

    def set_route(self, name: str, points) -> None:
        """Set (or clear, ``points=None``) the route of an EXISTING connection by
        name. The replay target for ``SetConnectionRouteCommand`` in a command
        trace (a reroute the GUI performed in place). ``points`` is a waypoint
        list ``[[x, y], …]`` or None to drop back to a fly line."""
        from commands import SetConnectionRouteCommand

        pts = [tuple(p) for p in points] if points else None
        self.commands.execute(
            SetConnectionRouteCommand(self.project, name, pts))

    def set_route_group(self, names: list, points) -> None:
        """Route SEVERAL named connections along the SAME path as one undoable
        unit — the I/Q-pair reroute (two logical nets sharing one physical
        corridor). The replay target for that composite, so a trace reproduces
        the grouped route exactly."""
        from commands import CompositeCommand, SetConnectionRouteCommand

        pts = [tuple(p) for p in points] if points else None
        cmds = [SetConnectionRouteCommand(self.project, n, pts) for n in names]
        if len(cmds) == 1:
            self.commands.execute(cmds[0])
        else:
            self.commands.execute(
                CompositeCommand("Route connection (I/Q pair)", cmds,
                                 trace_op={"op": "set_route_group",
                                           "args": {"names": list(names),
                                                    "points": ([list(p) for p in points]
                                                               if points else None)}}))

    def _route_siblings(self, conn):
        """Other UNROUTED connections that share ``conn``'s physical source-output
        cell AND target-input cell — an I/Q complex pair the auto-router routes
        with one shared path. Used so a manually-drawn route on one rail also
        routes its sibling rail (else the sibling shows a fly line + DRC error on a
        link that visually appears connected). Empty unless both endpoints resolve
        to cells AND a distinct sibling matches."""
        from engine.bus_router import _source_output_cell, _target_input_cell
        from model.connection import BlockEndpoint

        def cells(c):
            s, t = c.source, c.target
            oc = ic = None
            if isinstance(s, BlockEndpoint):
                b = self.project.block(s.block)
                if b is not None and b.placement is not None and b.placement.cells:
                    oc = _source_output_cell(b, s.port, self.catalog)
            if isinstance(t, BlockEndpoint):
                b = self.project.block(t.block)
                if b is not None and b.placement is not None and b.placement.cells:
                    ic = _target_input_cell(b, t.port, self.catalog)
            return oc, ic

        try:
            key = cells(conn)
        except Exception:  # noqa: BLE001
            return []
        if key[0] is None or key[1] is None:
            return []
        out = []
        for c in self.project.connections:
            if c.name == conn.name or c.is_routed:
                continue
            try:
                if cells(c) == key:
                    out.append(c)
            except Exception:  # noqa: BLE001
                continue
        return out

    def import_grc(self, path, *, chip_type: str | None = None,
                   name: str | None = None):
        """Import a GNURadio .grc flowgraph as the live placeKYT project (P4.2 —
        the GRC-first flow). Maps the Kyttar DSP blocks to placeKYT blocks +
        logical nets (source/sink → chip I/O ports). The caller then runs
        ``auto_place`` + ``auto_route_all`` to fill the grid. Returns the
        :class:`~engine.grc_import.GrcImportResult` (names any unmapped blocks —
        sound, never silently dropped). Replaces the current project.

        ``chip_type`` defaults to the current project's type, or — when that is
        unset (a fresh controller) — the first available chip type, so the
        imported project is immediately routable."""
        from engine.grc_import import import_grc as _import_grc

        ct = chip_type or self.project.chip_type
        if not ct:
            types = list(self.chip_types().keys())
            ct = types[0] if types else "kyttar_10x12"
        # PORTABLE REPLAY: a trace records the .grc's absolute path, which won't
        # exist when the trace is replayed on another machine. If the given path is
        # missing, fall back to locating a same-named .grc shipped with the repo
        # (examples/ + the test fixtures) so a .kytrace replays anywhere. ``name``
        # (recorded alongside the path) is the basename hint for that fallback.
        path = self._resolve_grc_path(path, name)
        result = _import_grc(path, self.catalog, chip_type=ct)
        self.set_project(result.project)
        self.project_path = None
        # Remember the source .grc so the GUI can WATCH it and flag drift the
        # moment the user re-saves it in GNU Radio — detect-on-save, before any
        # run (no need to run first to discover a parameter changed).
        self._grc_source_path = str(path)
        # Seed the GRC-sync baseline: the params just imported ARE the GRC params,
        # so the freshly-imported design is in sync. Later GRC param changes (over
        # the wire) or comparing a re-imported .grc diff against this baseline.
        self.grc_sync.observe_many(
            {b.name: dict(b.params) for b in result.project.blocks})
        self.refresh_grc_sync()
        # Record as a replayable trace op (set_project swapped the CommandManager
        # but kept the trace). import_grc replaces the project, so it must be the
        # FIRST op in any trace that starts from an imported flowgraph.
        # Record BOTH the resolved path and the bare name: replay prefers the path
        # but falls back to the name (via _resolve_grc_path) so a trace authored on
        # one machine replays on another where the absolute path differs.
        self.trace.record_op(
            "import_grc",
            {"path": str(path), "name": Path(path).name, "chip_type": ct},
            f"Import GNURadio flowgraph {Path(path).name}")
        return result

    def _resolve_grc_path(self, path, name: str | None = None):
        """Return ``path`` if it exists; otherwise locate a same-named ``.grc`` in
        the repo (so a trace's absolute path from another machine still resolves).
        Searches ``examples/`` (recursively) and the test GRC fixtures dir. ``name``
        (the basename a trace records alongside the path) is the search key when the
        path is gone. Returns the original ``path`` unchanged if nothing matches
        (import then raises a clear not-found, not a silent wrong-file)."""
        import os
        if os.path.exists(path):
            return path
        name = name or os.path.basename(str(path))
        here = Path(__file__).resolve().parent          # .../placekyt/ui
        pkg = here.parent                               # .../placekyt
        repo = pkg.parent                               # repo root
        roots = [repo / "examples", pkg / "tests" / "data" / "grc"]
        for root in roots:
            if not root.exists():
                continue
            for cand in root.rglob(name):
                return str(cand)
        return path

    def check_grc_file_drift(self):
        """Re-read the imported .grc and flag drift vs the placed design — the
        detect-on-SAVE path. Parses the current block params straight from the
        .grc (no run / no GR process needed), feeds them to the same GRC-sync diff
        the run-time advertise path uses, and emits ``grc_sync_changed`` so the
        out-of-sync indicator appears the moment the user saves in GNU Radio,
        BEFORE any run. Returns the diff dict (empty ⇒ in sync), or None if no
        .grc source is tracked / the file is unreadable."""
        path = getattr(self, "_grc_source_path", None)
        if not path:
            return None
        try:
            from engine.grc_import import grc_block_params
            params_by_block = grc_block_params(path, self.catalog)
        except Exception:  # noqa: BLE001 — a mid-save / malformed file: ignore
            return None
        if not params_by_block:
            return None
        self.grc_sync.observe_many(params_by_block)
        return self.refresh_grc_sync()

    def auto_place(self, chip: int = 0, *, register: bool = True):
        """Flow-order the placed blocks on ``chip`` into a 1-D pipeline (auto-P&R
        §8): topological order by dataflow, packed left-to-right. Applies the
        repositioning as ONE undoable composite. Returns the
        :class:`~engine.autoplace.PlacePlan` (names any backward/ring-forcing
        edges — sound: nothing is hidden). Run BEFORE Route All for the
        drop-anywhere → auto-arrange → route flow.

        ``register=False`` executes the moves but does NOT push them onto the
        undo stack — used when a higher-level command (the GRC resync) owns the
        single undoable unit via its own snapshot, so the place/route steps it
        drives must not also self-register.
        """
        from engine.autoplace import AutoPlacer
        from commands import (CompositeCommand, MoveBlockCommand,
                              OrientBlockCommand)

        def footprint(block_type, library, params=None):
            pm = self.catalog.port_map(block_type, params=params, library=library)
            return pm.footprint

        def port_maps(block_type, library, params=None):
            return self.catalog.port_map(block_type, params=params, library=library)

        # The array bounds the serpentine wraps within.
        w, h = self._chip_dims(chip)
        # Anchor the pipeline at the chip INPUT port's cell so the lead (input-fed)
        # block's landing cell lands ON the port — the port injects AT its cell, so
        # a block one cell away never receives the data (builds, but won't compute).
        anchor = self._input_port_anchor(chip)
        placer = (AutoPlacer(self.project, footprint, anchor=anchor,
                             width=w, height=h)
                  .with_port_maps(port_maps)
                  .with_chip_ports(self._chip_port_cell)
                  .with_feedback(self._block_has_internal_feedback))
        plan = placer.plan(chip)
        # The INPUT-FED lead block (first in flow order, anchored at the port) must
        # land its INPUT/landing CELL on the port — not its min corner. The port
        # injects AT its own cell, and a multi-input block (e.g. Costas xi@R0/xq@R1
        # at the phase cell) only fires when BOTH land there. Orientation can move
        # the input cell off the min corner, so we translate by the input cell's
        # POST-orient offset for the lead block. (Other blocks anchor by min corner;
        # the bus router brokers their inputs.)
        lead = self._input_fed_block(plan, chip)
        cmds = []
        # Apply orientation + translate per block, EXECUTING each in order so the
        # post-orient cell positions are visible for the lead block's input-cell
        # delta. The whole set is registered as ONE undoable composite.
        for name in plan.order:
            blk = self.project.block(name)
            if blk is None or blk.placement is None or not blk.placement.cells:
                continue
            kind = plan.orientations.get(name)
            if kind:
                oc = OrientBlockCommand(self.project, name, kind)
                oc.execute()
                cmds.append(oc)
            tx, ty = plan.positions[name][1], plan.positions[name][2]
            if name == lead:
                # delta so the (post-orient) input cell lands on the port anchor.
                icx, icy = self._input_cell_pos(blk)
                dx, dy = tx - icx, ty - icy
            else:
                bb = blk.placement.bounding_box()
                cx, cy = (bb[0], bb[1]) if bb else (blk.placement.cells[0].x,
                                                    blk.placement.cells[0].y)
                dx, dy = tx - cx, ty - cy
            if (dx, dy) != (0, 0):
                mv = MoveBlockCommand(self.project, name, dx, dy)
                mv.execute()
                cmds.append(mv)
        # ABUT-FIRST pass (user-approved rule b): a SINGLE-CELL block whose sole
        # block-input comes from ONE upstream driver is the §5.3 in==out hazard. The
        # serpentine pack can wrap it to the array CORNER against the egress edge
        # (the flagship slicer at (9,3)), where it has only ONE free non-collinear
        # face — routing alone then cannot split input!=output (every free face walls
        # a net). Re-seat such a block ADJACENT to its driver's output cell at a
        # position with ≥2 free non-collinear faces (one toward the driver/input, a
        # different one toward the egress/bus), so the bus router's adaptive split
        # succeeds NATURALLY. This runs AFTER the main pack so it sees final
        # positions; it never moves the lead input-fed block.
        cmds.extend(self._abut_single_cell_terminals(chip, plan, lead))
        if cmds and register:
            self.commands.add_executed(
                CompositeCommand("Auto-place blocks", cmds,
                                 trace_op={"op": "auto_place",
                                           "args": {"chip": chip}}))
        return plan

    def _abut_single_cell_terminals(self, chip, plan, lead):
        """Re-seat each single-cell bus-fed terminal block next to its driver output
        (abut-first, §5.3). Returns the list of executed MoveBlockCommands. General:
        applies to ANY single-cell block whose only block-input is one driver block.
        """
        cmds = []
        # Occupied cells of EVERY placed block on this chip (block + transit cells),
        # rebuilt fresh each iteration so a moved block's new footprint is honoured.
        def occupied(exclude=None):
            occ = set()
            for b in self.project.blocks:
                pl = b.placement
                if pl is None or pl.chip != chip or b.name == exclude:
                    continue
                occ.update((c.x, c.y) for c in pl.cells)
                occ.update((t.x, t.y) for t in getattr(pl, "transit_cells", []))
            return occ

        w, h = self._chip_dims(chip)

        # The chip OUTPUT port cell a terminal block egresses toward (for face choice).
        egress = self._output_port_anchor(chip)

        for name in plan.order:
            if name == lead:
                continue
            blk = self.project.block(name)
            if blk is None or blk.placement is None \
                    or len(blk.placement.cells) != 1:
                continue
            driver = self._sole_block_driver(name, chip)
            if driver is None:
                continue
            out_cell = self._block_output_cell(driver)
            if out_cell is None:
                continue
            # Only re-seat the STRANDED case (the §5.3 corner hazard): the serpentine
            # pack WRAPPED this single-cell terminal to a band that does NOT overlap
            # its driver's band, landing it at the far egress edge. When it sits on
            # the SAME band as its driver (no wrap) the placer already gave it the
            # driver-adjacent, edge-free seat the router needs — leave it untouched
            # (re-seating it would only crowd the driver and BREAK a working layout,
            # e.g. the production RX slicer at (1,3) beside its same-band Gardner).
            srow = blk.placement.cells[0].y
            drows = {c.y for c in driver.placement.cells}
            if min(drows) - 1 <= srow <= max(drows) + 1:
                continue
            occ = occupied(exclude=name)
            # Pick a seat in a small window (Manhattan ≤ 2) around the driver's output
            # cell. The §5.3-safe seat is NOT directly abutting the driver: a directly-
            # abutting seat WEDGES the slicer between the driver and the broker that
            # must feed it (the broker can't sit on the slicer's output face, so it lands
            # on the far side — and the input net then has to DETOUR around the slicer,
            # crossing the egress traffic and starving the block). Leaving a one-cell
            # GAP toward the driver (distance 2) lets the broker sit IN the gap: the
            # driver writes straight to it and it delivers straight into the slicer, on a
            # face naturally distinct from the egress-bound output. We also steer the
            # seat INTO the array interior (away from the edge the stranded driver hugs)
            # so both nets have open routing room — the property the working same-band
            # layouts have and the stranded corner lacks.
            best = None
            for ddx in range(-2, 3):
                for ddy in range(-2, 3):
                    if abs(ddx) + abs(ddy) > 2 or (ddx, ddy) == (0, 0):
                        continue
                    sx, sy = out_cell[0] + ddx, out_cell[1] + ddy
                    if not (0 <= sx < w and 0 <= sy < h):
                        continue
                    if (sx, sy) in occ:
                        continue
                    free = [(nx, ny) for nx, ny in
                            ((sx, sy + 1), (sx + 1, sy), (sx - 1, sy), (sx, sy - 1))
                            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in occ]
                    # Need ≥2 free non-collinear faces (one in x, one in y) so input and
                    # output can leave on DIFFERENT, non-opposing links (§5.3).
                    has_x = any(ny == sy for _nx, ny in free)
                    has_y = any(nx == sx for nx, _ny in free)
                    if not (has_x and has_y):
                        continue
                    dist = abs(ddx) + abs(ddy)
                    # The §5.3-safe seat leaves a one-cell GAP toward the driver so the
                    # broker can sit IN it (the input feed is then a SHORT, straight shot
                    # driver→broker→slicer that does NOT detour around the slicer through
                    # the egress traffic — the failure mode of a directly-abutting or
                    # wrong-side seat). The IDEAL is COLLINEAR: the seat exactly 2 cells
                    # from the output cell along ONE axis with the midpoint cell FREE, so
                    # the broker shot is one straight hop and the slicer's output leaves
                    # on the OTHER axis (naturally distinct face). A diagonal seat (dist
                    # 2, ddx&ddy both nonzero) is the fallback — it routes via an elbow.
                    collinear = (dist == 2 and (ddx == 0 or ddy == 0))
                    mid = (out_cell[0] + ddx // 2, out_cell[1] + ddy // 2)
                    mid_free = mid not in occ and 0 <= mid[0] < w and 0 <= mid[1] < h
                    straight_gap = collinear and mid_free
                    gap_ok = dist >= 2
                    # Interior pull: distance from the nearest array edge (bigger = more
                    # open routing room around the seat). Caps at 2; the stranded edge
                    # seat has margin 0 on the hugged side.
                    edge_margin = min(sx, w - 1 - sx, sy, h - 1 - sy)
                    ed = (abs(sx - egress[0]) + abs(sy - egress[1])) if egress else 0
                    # Lower is better: prefer a STRAIGHT broker gap (the clean short
                    # input shot), then any broker gap, then more interior room (open
                    # routing space for both nets), then a short run to the egress port,
                    # then more free faces.
                    score = (0 if straight_gap else (1 if gap_ok else 2),
                             -min(edge_margin, 2), ed, -len(free))
                    if best is None or score < best[0]:
                        best = (score, (sx, sy))
            if best is None:
                continue
            seat = best[1]
            cur = (blk.placement.cells[0].x, blk.placement.cells[0].y)
            if seat == cur:
                continue
            dx, dy = seat[0] - cur[0], seat[1] - cur[1]
            mv = MoveBlockCommand(self.project, name, dx, dy)
            mv.execute()
            cmds.append(mv)
            plan.positions[name] = (chip, seat[0], seat[1])
        return cmds

    def _sole_block_driver(self, block_name, chip):
        """The single driver block whose output feeds ``block_name`` (a
        BlockEndpoint→BlockEndpoint connection targeting it). Returns the driver
        :class:`Block`, or None if there is not EXACTLY one block driver."""
        from model.connection import BlockEndpoint
        drivers = set()
        for conn in self.project.connections:
            s, t = conn.source, conn.target
            if isinstance(t, BlockEndpoint) and t.block == block_name \
                    and isinstance(s, BlockEndpoint):
                drivers.add(s.block)
        if len(drivers) != 1:
            return None
        drv = self.project.block(next(iter(drivers)))
        if drv is None or drv.placement is None \
                or drv.placement.chip != chip or not drv.placement.cells:
            return None
        return drv

    def _block_output_cell(self, blk):
        """(x, y) of ``blk``'s OUTPUT port cell (PortMap output → placed cell; falls
        back to the block's last placed cell)."""
        try:
            pm = self.catalog.port_map(blk.type, library=blk.library)
            outs = pm.outputs()
        except Exception:  # noqa: BLE001
            outs = []
        if outs:
            pc = blk.placement.cell(outs[0].cell_id)
            if pc is not None:
                return (pc.x, pc.y)
        c = blk.placement.cells[-1]
        return (c.x, c.y)

    def _output_port_anchor(self, chip):
        """(x, y) of the chip OUTPUT port a placed block egresses toward — the
        pipeline end. Prefers a port a connection targets from a block; falls back to
        the first output-direction port, then None."""
        from model.connection import BlockEndpoint, ChipPortEndpoint
        chip_inst = self.project.chip(chip)
        if chip_inst is None:
            return None
        name = getattr(chip_inst, "type_name", None) or self.project.chip_type
        ct = self.chip_types().get(name)
        if ct is None:
            return None
        for conn in self.project.connections:
            s, t = conn.source, conn.target
            if isinstance(t, ChipPortEndpoint) and t.chip == chip \
                    and isinstance(s, BlockEndpoint):
                port = ct.port(t.port)
                if port is not None:
                    return (port.cell_x, port.cell_y)
        for port in ct.ports:
            if getattr(port.direction, "value", "") == "output":
                return (port.cell_x, port.cell_y)
        return None

    def _input_fed_block(self, plan, chip: int):
        """Name of the block a chip INPUT port feeds directly (the pipeline lead,
        anchored at the port) — or None. Its INPUT cell must land ON the port."""
        from model.connection import BlockEndpoint, ChipPortEndpoint
        fed = set()
        for conn in self.project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, ChipPortEndpoint) and s.chip == chip \
                    and isinstance(t, BlockEndpoint):
                fed.add(t.block)
        for name in plan.order:           # the FIRST in flow order that's input-fed
            if name in fed:
                return name
        return None

    def _input_cell_pos(self, blk) -> tuple[int, int]:
        """(x, y) of ``blk``'s INPUT/landing cell in its CURRENT placement (post
        orient). Resolves the input port's cell via the PortMap; falls back to the
        first placed cell. Used to land the lead block's input cell on the port."""
        pm = self.catalog.port_map(blk.type, library=blk.library)
        ins = pm.inputs()
        if ins:
            cid = ins[0].cell_id
            pc = blk.placement.cell(cid)
            if pc is not None:
                return (pc.x, pc.y)
        c0 = blk.placement.cells[0]
        return (c0.x, c0.y)

    def _chip_dims(self, chip: int) -> tuple[int, int]:
        """(width, height) of ``chip``'s array, for the serpentine packer to wrap
        within. Defaults to (10, 12) if the chip type can't be resolved."""
        chip_inst = self.project.chip(chip)
        name = (getattr(chip_inst, "type_name", None) if chip_inst else None) \
            or self.project.chip_type
        ct = self.chip_types().get(name)
        if ct is None:
            return (10, 12)
        return (int(getattr(ct, "width", 10)), int(getattr(ct, "height", 12)))

    def _block_has_internal_feedback(self, block_type, library, params=None):
        """True if a block declares INTERNAL connections/jumps (a feedback loop or
        cross-cell forwarding whose assembly hardcodes per-cell faces). The
        flyline-minimising auto-orienter uses this to leave such blocks as-authored
        (a D4 transform would rotate the PortMap but not the direction-specific
        program → break the loop, e.g. rotating Gardner breaks the RX). Result is
        cached per (type, library) — the answer doesn't depend on params."""
        cache = getattr(self, "_feedback_cache", None)
        if cache is None:
            cache = self._feedback_cache = {}
        key = (block_type, library)
        if key in cache:
            return cache[key]
        result = False
        try:
            blk = self.catalog.instantiate(block_type, "__fb_probe__", params,
                                           library=library)
            ic = list(getattr(blk, "internal_connections", lambda: [])() or [])
            ij = list(getattr(blk, "internal_jumps", lambda: [])() or [])
            result = bool(ic or ij)
        except Exception:  # noqa: BLE001
            result = False
        cache[key] = result
        return result

    def _chip_port_cell(self, chip: int, port_name: str):
        """(cell_x, cell_y) of a chip port — for the flyline-minimising orienter to
        score block I/O against the actual chip I/O port cells. None if unknown."""
        chip_inst = self.project.chip(chip)
        name = (getattr(chip_inst, "type_name", None) if chip_inst else None) \
            or self.project.chip_type
        ct = self.chip_types().get(name)
        if ct is None:
            return None
        port = ct.port(port_name)
        if port is None:
            return None
        return (port.cell_x, port.cell_y)

    def _input_port_anchor(self, chip: int):
        """(x, y) of the chip INPUT port that feeds a placed block — the pipeline
        start. Prefers a port named like an input (``*_in``) that a connection
        targets a block from; falls back to the first input-direction port, then
        None (placer then defaults to (0, row 0))."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        chip_inst = self.project.chip(chip)
        if chip_inst is None:
            return None
        name = getattr(chip_inst, "type_name", None) or self.project.chip_type
        ct = self.chip_types().get(name)
        if ct is None:
            return None
        # An input port a connection actually drives a block from.
        for conn in self.project.connections:
            s, t = conn.source, conn.target
            if isinstance(s, ChipPortEndpoint) and s.chip == chip \
                    and isinstance(t, BlockEndpoint):
                port = ct.port(s.port)
                if port is not None:
                    return (port.cell_x, port.cell_y)
        # Else the first declared input-direction port.
        for port in ct.ports:
            if getattr(port.direction, "value", "") == "input":
                return (port.cell_x, port.cell_y)
        return None

    def auto_orient_for_flow(self, chip_types: dict | None = None):
        """Flow-orient each block (output faces the downstream consumer, P3.2)
        WITHOUT routing — the placement+orientation half of auto_route_all. Used
        by the GRC-import "rough placement" option so the imported design gets the
        SAME compact, flow-oriented layout as full place-and-route, just with the
        nets left unrouted (fly lines) for manual routing. Applied as one undoable
        composite. Returns the number of blocks reoriented."""
        from engine.autoroute import AutoRouter
        from commands import CompositeCommand, OrientBlockCommand

        if chip_types is None:
            chip_types = self.chip_types()

        def port_cells(block_type, library, params=None):
            pm = self.catalog.port_map(block_type, params=params, library=library)
            return {p.name: (p.cell_id, p.direction) for p in pm.ports}

        def port_maps(block_type, library, params=None):
            return self.catalog.port_map(block_type, params=params, library=library)

        router = AutoRouter(self.project, chip_types, port_cells,
                            port_map_provider=port_maps)
        router.with_feedback(self._block_has_internal_feedback)
        cmds = [OrientBlockCommand(self.project, name, kind)
                for name, kind in router.orient_for_flow().items()]
        if cmds:
            self.commands.execute(CompositeCommand("Auto-orient blocks", cmds))
        return len(cmds)

    def auto_route_all(self, chip_types: dict | None = None, *,
                       auto_orient: bool = True, use_cpsat: str = "auto",
                       use_bus: str = "auto", register: bool = True):
        """Auto-route every UNROUTED logical net (Phase 3 "Route All"). Optionally
        AUTO-ORIENTS each block first (flow-order: output faces the downstream
        consumer, P3.2), then runs a router, applying the orientations + routes as
        ONE undoable composite command. Returns the
        :class:`~engine.autoroute.AutoRouteReport` so the caller can surface which
        nets routed and which couldn't (sound failure — unroutable nets are named,
        never silently dropped).

        ``chip_types`` defaults to the controller's own ``chip_types()`` (as
        ``build()`` / ``run_drc()`` do); a caller may pass an explicit map. Set
        ``auto_orient=False`` to route without reorienting blocks.

        Router selection (in priority order):
          - ``use_bus`` selects the §1.2 BUS/BROKER router (the multi-block path):
            ``"auto"`` (default) runs the heuristic + CP-SAT first and only falls to
            the bus router when they leave a net unrouted (the dense coherent-chain
            case); ``"always"`` forces the bus router; ``"never"`` disables it.
          - ``use_cpsat`` selects the CP-SAT bus-sharing router (§7):
            ``"auto"`` runs the heuristic; if it leaves ANY net unrouted AND OR-Tools
            is available, retry with CP-SAT (common-sink sharing). ``"always"`` /
            ``"never"`` force/disable it. (Falls back silently if OR-Tools absent.)
        The bus router is the one that handles DIFFERENT-sink sharing (brokers), so
        it is what routes the multi-block coherent chain (net4/5/6).
        """
        from engine.autoroute import AutoRouter
        from commands import (CompositeCommand, OrientBlockCommand,
                              SetConnectionRouteCommand)

        if chip_types is None:
            chip_types = self.chip_types()

        def port_cells(block_type, library, params=None):
            pm = self.catalog.port_map(block_type, params=params, library=library)
            return {p.name: (p.cell_id, p.direction) for p in pm.ports}

        def port_maps(block_type, library, params=None):
            return self.catalog.port_map(block_type, params=params, library=library)

        router = AutoRouter(self.project, chip_types, port_cells,
                            port_map_provider=port_maps)
        router.with_feedback(self._block_has_internal_feedback)

        pre: list = []
        if auto_orient:
            # Auto-orient (P3.2): flow-order each block (output faces the
            # downstream consumer) BEFORE routing. Applied as part of the same
            # undoable composite. A block already oriented is left untouched.
            for block_name, kind in router.orient_for_flow().items():
                pre.append(OrientBlockCommand(self.project, block_name, kind))
            for cmd in pre:
                cmd.execute()              # apply so the router sees new geometry

        report = self._run_router(router, port_cells, chip_types, use_cpsat,
                                  use_bus, port_maps)
        # A chip INPUT-port → block net injects at the edge cell and needs NO
        # physical route (DRC + the build treat it as a direct port injection).
        # The router emits a vestigial 1-cell route (the input cell itself), which
        # renders as an undeletable "routing cell" sitting on the block. Leave such
        # input nets UNROUTED (logical / fly line) so there's no phantom cell —
        # they still build (the port entry is configured from the placement).
        from model.connection import ChipPortEndpoint as _CPE

        def _vestigial_input_route(r):
            conn = self.project.connection(r.name)
            return (conn is not None and isinstance(conn.source, _CPE)
                    and len(r.points or []) <= 1)

        cmds = list(pre) + [
            SetConnectionRouteCommand(self.project, r.name, r.points)
            for r in report.routed if not _vestigial_input_route(r)]
        # The pre-orient commands were executed above to inform routing; undo them
        # now so the single CompositeCommand re-applies everything atomically (one
        # clean undo step), keeping the command manager's stack consistent.
        for cmd in reversed(pre):
            cmd.undo()
        if cmds:
            composite = CompositeCommand("Auto-route all nets", cmds,
                                         trace_op={"op": "auto_route_all",
                                                   "args": {}})
            if register:
                self.commands.execute(composite)
            else:
                # A higher-level command (GRC resync) owns the undo unit via its
                # own snapshot — execute the routes but don't push our own.
                composite.execute()
                self.project.mark_dirty()
                self.project.event_bus.flush()
        return report

    def _run_router(self, heuristic_router, port_cells, chip_types, use_cpsat,
                    use_bus="auto", port_maps=None):
        """Pick the router and return the AutoRouteReport.

        Order: heuristic BFS → (CP-SAT if it left failures + ``use_cpsat`` allows) →
        (BUS/BROKER router if STILL failures + ``use_bus`` allows). The bus router
        is the §1.2 path that handles DIFFERENT-sink sharing via programmed brokers,
        so it is what finally routes the dense multi-block chain. ``use_bus="always"``
        forces it directly; ``"never"`` disables it."""
        from engine.cpsat_router import route_all_cpsat, CpSatUnavailable

        if use_bus == "always":
            return self._bus_route(port_cells, chip_types, port_maps)
        if use_cpsat == "always":
            report = route_all_cpsat(self.project, chip_types, port_cells)
        else:
            report = heuristic_router.route_all()
            if not (use_cpsat == "never" or report.ok):
                # Heuristic left failures — try CP-SAT (common-sink sharing).
                try:
                    cp = route_all_cpsat(self.project, chip_types, port_cells)
                    if len(cp.routed) > len(report.routed):
                        report = cp
                except CpSatUnavailable:
                    pass
        if use_bus == "never" or report.ok:
            return report
        # CP-SAT/heuristic can't demux DIFFERENT-sink streams — escalate to the
        # bus/broker router, which can. Keep its result only if it routes more.
        bus = self._bus_route(port_cells, chip_types, port_maps)
        if len(bus.routed) > len(report.routed):
            return bus
        return report

    def _bus_route(self, port_cells, chip_types, port_maps):
        """Run the §1.2 bus/broker router, supplying the placement spine derived
        from the current geometry (the serpentine the placer would lay)."""
        from engine.bus_router import route_all_bus

        def spine(chip):
            try:
                return self._derive_spine(chip)
            except Exception:  # noqa: BLE001 — spine is a HINT; the router copes
                return []

        return route_all_bus(self.project, chip_types, port_cells,
                             spine_provider=spine, port_map_provider=port_maps)

    def _derive_spine(self, chip: int):
        """Re-derive the placement spine (serpentine bus waypoints) for ``chip`` from
        the current block geometry, so the bus router prefers the snake the placer
        laid. Pure (no project mutation): runs the AutoPlacer's plan and returns its
        ``spine`` without moving anything."""
        from engine.autoplace import AutoPlacer

        def footprint(block_type, library, params=None):
            return self.catalog.port_map(block_type, params=params, library=library).footprint

        def port_maps(block_type, library, params=None):
            return self.catalog.port_map(block_type, params=params, library=library)

        w, h = self._chip_dims(chip)
        anchor = self._input_port_anchor(chip)
        placer = (AutoPlacer(self.project, footprint, anchor=anchor,
                             width=w, height=h)
                  .with_port_maps(port_maps)
                  .with_chip_ports(self._chip_port_cell)
                  .with_feedback(self._block_has_internal_feedback))
        return placer.plan(chip).spine

    def add_logical_connection(self, source, target, *, name: str | None = None,
                               kind: str | None = None):
        """Create an UNROUTED logical net (a fly line) from ``source`` to
        ``target`` — the auto-P&R capture path (P2.3). No waypoints: the Phase-3
        router materialises the physical route later. ``kind`` is the LogicalNet
        kind (data / trigger / data+trigger); defaults to the Connection default.
        Returns the connection name."""
        from commands import AddConnectionCommand
        from model.connection import Connection, NET_DATA_TRIGGER

        # Accept dict endpoints ({"block":..,"port":..} / {"chip":..,"port":..})
        # so a command trace replays directly from its serialized args.
        source = self._endpoint_from_any(source)
        target = self._endpoint_from_any(target)
        conn_name = name or self._unique_connection_name(source, target)
        conn = Connection(
            conn_name, source=source, target=target, route=None,
            kind=kind or NET_DATA_TRIGGER,
        )
        self.commands.execute(AddConnectionCommand(self.project, conn))
        return conn_name

    @staticmethod
    def _endpoint_from_any(ep):
        """An endpoint object from either an Endpoint OR a serialized dict
        (command-trace replay): ``{"block": n, "port": p}`` or
        ``{"chip": id, "port": p}``."""
        if isinstance(ep, dict):
            from model.connection import BlockEndpoint, ChipPortEndpoint
            if "block" in ep:
                return BlockEndpoint(ep["block"], ep["port"])
            return ChipPortEndpoint(ep["chip"], ep["port"])
        return ep

    # -- command trace (live console echo + replayable export) ----------------

    def export_trace(self, path) -> str | None:
        """Write the session command trace to ``path`` as a runnable Python replay
        SCRIPT — calls ``controller.<op>(**args)`` in order (plus undo()/redo()).
        The ``.py`` IS the trace: it both documents the session and replays it
        (``placekyt --replay``), so there is a SINGLE trace format (no JSON). The
        suffix is not interpreted — a Python script is always written.

        Returns a WARNING string if the trace has GAPS (operations that produced
        no replayable op — see :meth:`CommandTrace.gaps`), else None. A trace with
        gaps cannot fully reproduce the session, so the caller should surface the
        warning prominently (a trace that omits an op is worthless)."""
        from pathlib import Path

        events = self.trace.events()
        gaps = self.trace.gaps()
        lines = [
            "# placeKYT command trace (runnable replay) — replay with:",
            "#   placekyt --replay this_file.py",
            "# or inside the placeKYT console where `controller` exists:",
            "#   exec(open('this_file.py').read())",
        ]
        if gaps:
            lines.append("# WARNING: this trace has NON-REPLAYABLE gaps "
                         "(marked '!! GAP' below) — it cannot fully reproduce "
                         "the session. Fill them in by hand before replaying.")
        lines += ["ctrl = controller  # the live AppController", ""]
        for ev in events:
            op = ev.get("op")
            desc = ev.get("description", "")
            if ev.get("kind") == "undo":
                lines.append(f"ctrl.undo()  # {desc}")
                continue
            if ev.get("kind") == "redo":
                lines.append(f"ctrl.redo()  # {desc}")
                continue
            if not op:
                lines.append(f"# !! GAP (not replayable): {desc}")
                continue
            args = ev.get("args", {})
            kw = ", ".join(f"{k}={v!r}" for k, v in args.items())
            lines.append(f"ctrl.{op}({kw})  # {desc}")
        Path(path).write_text("\n".join(lines) + "\n")
        if gaps:
            return (f"Trace has {len(gaps)} non-replayable gap(s): "
                    + "; ".join(g.get("description", "?") for g in gaps)
                    + ". The trace cannot fully reproduce the session.")
        return None

    def replay_trace(self, path, *, strict: bool = True) -> list[dict]:
        """Replay a ``.kytrace`` JSON (or a list of events) onto THIS controller —
        reproduce a captured session. Each event with an ``op`` calls
        ``getattr(self, op)(**args)``; undo/redo events call undo()/redo().

        A 'do' event with NO op is a GAP (an operation that wasn't replayable). In
        ``strict`` mode (default) the replay RAISES at the first gap, because a
        replay that silently skips an op produces a DIFFERENT design than the
        captured session — a worthless, misleading reproduction. Pass
        ``strict=False`` to skip gaps and continue (returns the list of skipped
        gap events) — use only when you've confirmed the gaps don't matter."""
        import json
        from pathlib import Path

        events = path
        if not isinstance(path, list):
            events = json.loads(Path(path).read_text())
        skipped: list[dict] = []
        for ev in events:
            kind = ev.get("kind", "do")
            if kind == "undo":
                self.undo() if hasattr(self, "undo") else self.commands.undo()
                continue
            if kind == "redo":
                self.redo() if hasattr(self, "redo") else self.commands.redo()
                continue
            op = ev.get("op")
            if not op:
                desc = ev.get("description", "?")
                if strict:
                    raise ValueError(
                        f"replay GAP at seq {ev.get('seq')}: '{desc}' is not "
                        f"replayable — the trace is incomplete and cannot "
                        f"reproduce the session. (Re-export after the op gains a "
                        f"trace mapping, or replay with strict=False to skip.)")
                skipped.append(ev)
                continue
            getattr(self, op)(**ev.get("args", {}))
        return skipped

    def _unique_connection_name(self, source, target) -> str:
        from model.connection import BlockEndpoint

        def label(ep):
            return ep.block if isinstance(ep, BlockEndpoint) else ep.port
        base = f"{label(source)}_to_{label(target)}"
        if self.project.connection(base) is None:
            return base
        i = 1
        while self.project.connection(f"{base}_{i}") is not None:
            i += 1
        return f"{base}_{i}"

    def edit_params(self, block_name: str, params: dict) -> None:
        self.commands.execute(EditParamsCommand(self.project, block_name, params))

    # -- GRC↔placeKYT parameter sync (§GRC-sync) ------------------------------

    def observe_grc_params(self, params_by_block: dict) -> dict:
        """Record the GRC-side params for blocks (from the wire or a re-import),
        re-diff against the live design, and emit ``grc_sync_changed``.

        ``params_by_block`` is ``{placeKYT block name: raw GRC params}``. Returns
        the resulting diff (``{block_name: BlockParamDiff}``; empty ⇒ in sync)."""
        self.grc_sync.observe_many(params_by_block)
        return self.refresh_grc_sync()

    def refresh_grc_sync(self) -> dict:
        """Recompute the GRC out-of-sync diff against the current design and emit
        ``grc_sync_changed``. Returns the diff dict (empty ⇒ in sync)."""
        diffs = self.grc_sync.diff_against(self.project, self.catalog)
        self.grc_sync_changed.emit(diffs)
        return diffs

    def grc_out_of_sync(self) -> bool:
        """True when at least one block's GRC params differ from the design."""
        return not self.grc_sync.in_sync

    def resync_from_grc(self, *, mode: str | None = None,
                        chip_types: dict | None = None):
        """Re-apply the recorded GRC params to the out-of-sync blocks, then
        re-layout per ``mode`` (§GRC-sync). ONE undoable command.

        ``mode`` (defaults to the persisted preference):
          * ``notify`` / ``auto`` — re-apply params + re-place + re-route the
            whole chip (a param change can RESIZE a block, moving neighbours).
          * ``reanchor`` — re-apply params + rebuild each block's cells from its
            default layout at the SAME anchor (resize in place); do NOT reroute.

        Returns ``(diff_before, route_report_or_None)``. The route report (auto/
        notify modes) carries any unroutable nets so the caller can surface DRC;
        re-anchor returns None (the caller runs DRC to surface violations)."""
        from commands import ResyncFromGrcCommand
        from engine import preferences

        if mode is None:
            mode = preferences.grc_param_change_mode()
        diffs = self.refresh_grc_sync()
        if not diffs:
            return {}, None
        block_names = list(diffs.keys())
        # Snapshot the diffs for the apply closure (refresh runs again post-apply).
        affected = dict(diffs)
        # Does ANY affected block actually change footprint (cell_count)? A
        # param-only change that does NOT resize (e.g. a gain value, a tap-value
        # edit that keeps the tap COUNT) needs no re-layout at all — just the new
        # params. Only a resize can move neighbours / require re-routing. Without
        # this guard a gain-value change re-flowed the WHOLE chip and dumped
        # unrelated blocks (e.g. the FIR) into new, rotated, disconnected
        # positions — the reported garbage-placement bug.
        any_resize = any(getattr(d, "resizes", False) for d in affected.values())

        def _apply():
            # 1. Apply merged GRC params to each affected block (direct model
            #    mutation — the outer ResyncFromGrcCommand snapshot owns undo).
            for name, d in affected.items():
                blk = self.project.block(name)
                if blk is None:
                    continue
                from engine.grc_sync import merged_params
                self.project._set_block_params(
                    name, merged_params(blk, d.grc_params))
            # 2. Re-layout ONLY if a block resized. A non-resizing param change
            #    leaves every block's placement AND every connection untouched.
            if not any_resize:
                report = None
            elif mode == preferences.GRC_REANCHOR:
                self._reanchor_blocks(block_names)
                report = None
            else:
                # A resize CHANGES the block's cell COUNT — its placement cells
                # must be REBUILT from the new params, else the old footprint
                # lingers (a 40→8-tap FIR kept 8 cells instead of 2, leaving
                # stale cells + a stray fly line). Rebuild each RESIZED block's
                # cells from its default layout (same as a fresh import), THEN
                # re-place + re-route the whole chip (register=False: this
                # command owns undo).
                resized = [n for n, d in affected.items()
                           if getattr(d, "resizes", False)]
                self._rebuild_block_cells(resized)
                self.auto_place(0, register=False)
                # CLEAR every existing route before re-routing. A resize +
                # auto_place changes the footprint and reflows neighbours, so the
                # OLD routes' waypoints are stale (they still point at the old,
                # now-gone cell positions). auto_route_all only routes UNROUTED
                # nets — it would skip these still-"routed" nets and leave their
                # stale waypoints, so the canvas shows orphaned blue route cells
                # while the shrunken block sits elsewhere, disconnected. Unrouting
                # them forces a fresh route against the new layout.
                self._clear_routes(chip=0)
                report = self.auto_route_all(
                    chip_types=chip_types, register=False)
            self.project.mark_dirty()
            self.project.event_bus.flush()
            return report

        cmd = ResyncFromGrcCommand(
            self.project, block_names, _apply,
            description=f"Resync {len(block_names)} block(s) from GRC")
        self.commands.execute(cmd)
        # The design now matches the GRC params → in sync.
        self.refresh_grc_sync()
        return affected, cmd.result

    def _reanchor_blocks(self, block_names: list) -> None:
        """Rebuild each block's cells from its default layout at its CURRENT
        anchor (the min-corner of its present cells), so a param-driven resize is
        applied IN PLACE without moving the block. Routes are left as-is — the
        caller surfaces any resulting DRC violations (re-anchor mode)."""
        from model.placement import Placement

        for name in block_names:
            blk = self.project.block(name)
            if blk is None or blk.placement is None or not blk.placement.cells:
                continue
            pl = blk.placement
            bb = pl.bounding_box()
            ax, ay = (bb[0], bb[1]) if bb else (pl.cells[0].x, pl.cells[0].y)
            cells, transit = self.default_cells(
                blk.type, blk.library, pl.chip, ax, ay, params=blk.params)
            self.project._set_block_placement(
                name, Placement(chip=pl.chip, cells=cells, transit_cells=transit))

    def _clear_routes(self, chip: int = 0) -> None:
        """Unroute every connection whose route lives on ``chip`` (set route =
        None) so a subsequent auto_route_all recomputes them from scratch. The
        rendered routing cells are a projection of conn.route, so clearing it
        drops the stale route markers too. Used before a resize re-route, where
        the old waypoints point at cells that no longer exist."""
        from engine.route_analysis import route_chip_of

        for conn in self.project.connections:
            if not conn.is_routed:
                continue
            try:
                if route_chip_of(self.project, conn) == chip:
                    conn.route = None
            except Exception:  # noqa: BLE001 — be permissive; just unroute it
                conn.route = None

    def _rebuild_block_cells(self, block_names: list) -> None:
        """Regenerate each block's placement CELLS from its (new) params — used
        after a resync RESIZE so the block's cell COUNT matches its new size,
        instead of keeping the old footprint's cells. The block is rebuilt at its
        current anchor; auto_place repositions it afterward. Without this a
        40→8-tap FIR kept 8 cells (the old size) and stranded a fly line."""
        from model.placement import Placement

        for name in block_names:
            blk = self.project.block(name)
            if blk is None or blk.placement is None or not blk.placement.cells:
                continue
            pl = blk.placement
            bb = pl.bounding_box()
            ax, ay = (bb[0], bb[1]) if bb else (pl.cells[0].x, pl.cells[0].y)
            cells, transit = self.default_cells(
                blk.type, blk.library, pl.chip, ax, ay, params=blk.params)
            self.project._set_block_placement(
                name, Placement(chip=pl.chip, cells=cells, transit_cells=transit))

    def rename_block(self, old_name: str, new_name: str) -> None:
        """Rename a block instance (updates the block + all route references).
        Undoable. Raises ValueError on empty/duplicate names."""
        from commands import RenameBlockCommand

        self.commands.execute(
            RenameBlockCommand(self.project, old_name, new_name))

    def set_block_color(self, block_name: str, color: str | None) -> None:
        """Set a block's canvas display colour ("#rrggbb", or None to reset to
        the auto rotation). Cosmetic + non-structural → a direct model edit
        (not an undo command), then notify so the canvas re-renders."""
        blk = self.project.block(block_name)
        if blk is None or blk.color == color:
            return
        blk.color = color
        self.project.mark_dirty()
        self.changed.emit()

    # Canvas cell size in scene pixels (mirrors ui.canvas.cell_item.CELL_PX).
    # Chip positions are scene pixels at zoom=1 (§3.2); the controller computes
    # a non-overlapping placement for a new chip without importing Qt/canvas.
    _CELL_PX = 64
    _CHIP_GAP_CELLS = 2  # blank columns between abutted chips

    def add_chip(self, label: str = "") -> int:
        """Add a chip instance, auto-positioned to the right of existing chips."""
        from commands import AddChipCommand

        next_id = (max((c.id for c in self.project.chips), default=-1) + 1)
        # Place the new chip just past the rightmost existing chip.
        pos_x = 0.0
        for c in self.project.chips:
            ct = self._chip_type_for_instance(c)
            width_px = (ct.width if ct else 10) * self._CELL_PX
            right = c.position_x + width_px + self._CHIP_GAP_CELLS * self._CELL_PX
            pos_x = max(pos_x, right)
        chip = ChipInstance(next_id, label or f"Chip {next_id}",
                            position_x=pos_x, position_y=0.0)
        self.commands.execute(AddChipCommand(self.project, chip))
        return next_id

    def _chip_type_for_instance(self, chip_instance):
        """Resolve a chip instance's ChipType (its own type_name or project's)."""
        name = chip_instance.type_name or self.project.chip_type
        return self.chip_types().get(name)

    def add_inter_chip(self, from_chip: int, from_port: str,
                       to_chip: int, to_port: str):
        """Create a chip-to-chip connection (board-level wire). Undoable.

        Validates that ``from_port`` is an OUTPUT and ``to_port`` an INPUT on
        their respective chip types, and that the chips differ. Returns the new
        :class:`InterChipConnection`.
        """
        from commands import AddInterChipCommand
        from model.connection import InterChipConnection

        if from_chip == to_chip:
            raise ValueError("inter-chip connection must span two chips")
        src = self.project.chip(from_chip)
        dst = self.project.chip(to_chip)
        if src is None or dst is None:
            raise KeyError("inter-chip connection references an unknown chip")
        self._require_port_dir(src, from_port, "output")
        self._require_port_dir(dst, to_port, "input")
        ic = InterChipConnection(from_chip, from_port, to_chip, to_port)
        if ic in self.project.inter_chip_connections:
            raise ValueError("inter-chip connection already exists")
        self.commands.execute(AddInterChipCommand(self.project, ic))
        return ic

    def remove_inter_chip(self, ic) -> None:
        """Remove a chip-to-chip connection. Undoable."""
        from commands import RemoveInterChipCommand

        self.commands.execute(RemoveInterChipCommand(self.project, ic))

    # -- SRAM / peripheral panels (the SRAM panel notes) -----------------------------

    def add_panel(self, label: str = "", *, x: float | None = None,
                  y: float | None = None, size_words: int | None = None) -> int:
        """Add an SRAM panel, auto-positioned to the LEFT of the chips by default
        (panels typically feed the array). Returns the new panel id. Undoable."""
        from commands import AddPanelCommand
        from model.panel import DEFAULT_PANEL_WORDS, SramPanel

        next_id = max((p.id for p in self.project.panels), default=-1) + 1
        if x is None:
            # default: a bit to the left of the leftmost chip
            left = min((c.position_x for c in self.project.chips), default=0.0)
            x = left - 6 * self._CELL_PX
        if y is None:
            y = 0.0
        panel = SramPanel(
            id=next_id, label=label or f"Panel {next_id}",
            position_x=float(x), position_y=float(y),
            size_words=size_words or DEFAULT_PANEL_WORDS)
        # Default to the MIRRORED orientation (inputs EAST, outputs WEST) so a
        # panel placed to the LEFT of the chips naturally connects chip-output
        # (right edge) → panel-input and panel-output → chip-input.
        panel.mirror_h()
        self.commands.execute(AddPanelCommand(self.project, panel))
        return next_id

    def remove_panel(self, panel_id: int) -> None:
        """Remove a panel and its links. Undoable."""
        from commands import RemovePanelCommand

        self.commands.execute(RemovePanelCommand(self.project, panel_id))

    def move_panel(self, panel_id: int, x: float, y: float) -> None:
        """Move a panel to a new scene position. Undoable."""
        from commands import MovePanelCommand

        self.commands.execute(MovePanelCommand(self.project, panel_id, x, y))

    def mirror_panel(self, panel_id: int) -> None:
        """Horizontally mirror a panel (ports swap sides). Undoable."""
        from commands import MirrorPanelCommand

        self.commands.execute(MirrorPanelCommand(self.project, panel_id))

    def connect_panel(self, panel_id: int, panel_port: str,
                      chip_id: int, chip_port: str):
        """Wire a panel port to a chip port (the SRAM panel notes). Validates the panel
        + chip ports exist and their bus widths (x16/x1) match, and that the
        directions are opposite (a panel OUTPUT feeds a chip INPUT, and vice
        versa). Returns the new :class:`PanelConnection`. Undoable."""
        from commands import AddPanelConnectionCommand
        from model.connection import PanelConnection
        from model.enums import PortDirection

        panel = self.project.panel(panel_id)
        chip = self.project.chip(chip_id)
        if panel is None:
            raise KeyError(f"no panel {panel_id!r}")
        if chip is None:
            raise KeyError(f"no chip {chip_id!r}")
        pport = panel.port(panel_port)
        if pport is None:
            raise KeyError(f"panel {panel_id} has no port {panel_port!r}")
        ct = self._chip_type_for_instance(chip)
        cport = next((p for p in ct.ports if p.name == chip_port), None) \
            if ct else None
        if cport is None:
            raise KeyError(f"chip {chip_id} has no port {chip_port!r}")
        if pport.width != cport.width:
            raise ValueError(
                f"port width mismatch: panel {panel_port} is x{pport.width}, "
                f"chip {chip_port} is x{cport.width}")
        # A panel output must feed a chip input (and vice versa).
        same_dir = (pport.direction == PortDirection.INPUT) == \
            (cport.direction == PortDirection.INPUT)
        if same_dir:
            raise ValueError(
                "panel/chip ports must have opposite directions "
                f"(panel {panel_port}={pport.direction.value}, "
                f"chip {chip_port}={cport.direction.value})")
        pc = PanelConnection(panel_id, panel_port, chip_id, chip_port)
        if pc in self.project.panel_connections:
            raise ValueError("panel connection already exists")
        self.commands.execute(AddPanelConnectionCommand(self.project, pc))
        return pc

    def disconnect_panel(self, pc) -> None:
        """Remove a panel↔chip link. Undoable."""
        from commands import RemovePanelConnectionCommand

        self.commands.execute(RemovePanelConnectionCommand(self.project, pc))

    def _require_port_dir(self, chip_instance, port_name: str, direction: str):
        ct = self._chip_type_for_instance(chip_instance)
        if ct is None:
            raise KeyError(f"chip {chip_instance.id} has no resolvable type")
        port = next((p for p in ct.ports if p.name == port_name), None)
        if port is None:
            raise KeyError(f"chip {chip_instance.id} has no port {port_name!r}")
        if port.direction.value != direction:
            raise ValueError(
                f"port {port_name!r} is {port.direction.value}, expected {direction}")

    # -- undo / redo ----------------------------------------------------------

    def undo(self) -> None:
        self.commands.undo()

    def redo(self) -> None:
        self.commands.redo()

    def can_undo(self) -> bool:
        return self.commands.can_undo()

    def can_redo(self) -> bool:
        return self.commands.can_redo()

    # -- build / DRC ----------------------------------------------------------

    def run_drc(self):
        """Run project-level DRC and return the :class:`DRCResult`."""
        from engine.drc import check_project

        return check_project(self.project, self.chip_types())

    def build(self):
        """Build the project (DRC + bitstream). Returns the :class:`BuildResult`."""
        from engine.build import BuildEngine

        engine = BuildEngine(self.catalog, self.registry.paths())
        result = engine.build(self.project, self.chip_types())
        self._build_cache = result if result.ok else None
        return result

    def cached_build(self):
        """The last successful build, rebuilding if the project changed.

        Used by the Inspector memory/assembly view, which needs the resolved
        per-cell program. Returns the :class:`BuildResult` or None if the build
        currently fails (DRC errors).
        """
        if getattr(self, "_build_cache", None) is not None \
                and not self.project.build_dirty:
            return self._build_cache
        result = self.build()  # also clears build_dirty on success
        return self._build_cache

    def cell_program(self, chip: int, x: int, y: int):
        """Resolved program for a built cell, or None if there's no build / the
        cell isn't programmed.

        Returns a dict::
            {"entry": int, "memory": [32 words],
             "disasm": [(addr, word, mnemonic), …],
             "face": "<dir>"|None, "routing_only": bool}
        """
        build = self.cached_build()
        if build is None or chip not in build.chips:
            return None
        info = build.chips[chip].cells.get((x, y))
        if info is None:
            return None
        words = info["memory"]
        block_name = info.get("block")
        cell_id = info.get("cell_id")
        classes = info.get("classes") or {}
        return {
            "entry": info["entry"],
            "memory": words,
            "disasm": _disassemble(words),
            "face": info.get("face"),
            "routing_only": info.get("routing_only", False),
            "kind": info.get("kind"),    # "broker" for a programmed routing cell
            "block": block_name,
            "cell_id": cell_id,
            # {addr: {"role", "name"}} — data/state/instruction classification.
            "classes": classes,
            "instructions": self._cell_instructions(
                words, block_name, cell_id, classes),
        }

    def _cell_instructions(self, words: list, block_name, cell_id,
                           classes: dict | None = None) -> list:
        """Per-WRITE/JUMP handoff metadata for a block cell (§3.3).

        Returns one dict per *executable* WRITE/JUMP instruction word::

            {"addr": int, "kind": "WRITE"|"JUMP",
             "hop": <@N hops away currently encoded>,
             "field": <dest reg (WRITE) | entry addr (JUMP) currently encoded>,
             "hop_override": int|None, "field_override": int|None,
             "field_label": "Dest reg"|"Entry addr",
             "field_kind": "reg"|"reg_or_config"|"entry",
             "field_options": [valid dest regs | entry addrs]}

        Words classified as DATA/STATE are NOT instructions — they are skipped
        even when their bits match a WRITE/JUMP opcode (a coefficient that
        happens to look like a JUMP is data, not a handoff to configure).

        ``hop``/``field`` reflect the BUILT word (route auto-fill + any applied
        override). ``*_override`` are the stored manual overrides (None = auto).
        ``field_options`` come from the downstream block's interface so the UI
        can offer a dropdown when there is more than one valid choice.
        ``field_kind`` tells the UI which control to render: a register spinbox
        (JUMP entry / WRITE dest), or register-or-config (WRITE may target a
        CONFIG address C0–C31 as well as a data register).
        """
        from engine.build import decode_hop_cnt

        if block_name is None:
            # A PROGRAMMED ROUTING CELL (a bus BROKER or CROSSOVER) — it has no owning
            # block, but it DOES carry a flip→relay→restore / demux program. Decode its
            # WRITE/JUMP instructions read-only (no per-cell override metadata, since it
            # isn't a block cell) so the Inspector shows the routing control logic
            # instead of an empty panel. Plain transit cells (no WRITE/JUMP words)
            # yield [] here — correctly, they are FACE-only.
            ro: list = []
            for addr, word in enumerate(words):
                opcode = word & 0xF000
                if opcode == 0x6000:
                    kind, label = "WRITE", "Dest reg"
                elif opcode == 0x7000:
                    kind, label = "JUMP", "Entry addr"
                else:
                    continue
                ro.append({
                    "addr": addr, "kind": kind,
                    "hop": decode_hop_cnt((word >> 5) & 0x1F),
                    "field": word & 0x1F,
                    "hop_override": None, "field_override": None,
                    "field_label": label, "field_kind": "broker",
                    "field_options": [], "broker": True,
                })
            return ro
        classes = classes or {}
        blk = self.project.block(block_name)
        placement = blk.placement if blk else None
        regs, entries = self._downstream_interface(block_name)
        out: list = []
        for addr, word in enumerate(words):
            # Skip non-instruction words (data/state) even if their bits look
            # like a WRITE/JUMP — they aren't handoffs to configure.
            role = (classes.get(addr) or {}).get("role")
            if role in ("data", "state", "input", "output"):
                continue
            opcode = word & 0xF000
            if opcode == 0x6000:
                kind, label, options = "WRITE", "Dest reg", regs
                field_kind = "reg_or_config"
            elif opcode == 0x7000:
                kind, label, options = "JUMP", "Entry addr", entries
                field_kind = "entry"
            else:
                continue
            ov = (placement.override(cell_id, addr)
                  if placement and cell_id is not None else None)
            out.append({
                "addr": addr,
                "kind": kind,
                "hop": decode_hop_cnt((word >> 5) & 0x1F),
                "field": word & 0x1F,
                # Current WRITE config-bit state (bit 10): True → dest is C0–C31.
                "field_config": bool(word & 0x0400) if kind == "WRITE" else False,
                "hop_override": ov.hop if ov else None,
                "field_override": (
                    (ov.dest if kind == "WRITE" else ov.entry) if ov else None),
                "config_override": (ov.dest_config if (ov and kind == "WRITE")
                                    else None),
                "field_label": label,
                "field_kind": field_kind,
                "field_options": list(options),
            })
        return out

    def _downstream_interface(self, block_name: str):
        """(input_registers, entry_addresses) of the block(s) this one feeds.

        Resolved from the project's block→block connections sourced at
        ``block_name``. Uses the build-RESOLVED entry + input register of each
        downstream block (v2 lays memory out dynamically — the DFE's entry is
        15 and inputs R5–R7, NOT the static interface's R31/entry 1). Empty
        tuples when the block only targets a chip port (no register interface).
        """
        from model.connection import BlockEndpoint

        regs: list = []
        entries: list = []
        for conn in self.project.connections:
            src, tgt = conn.source, conn.target
            if (isinstance(src, BlockEndpoint) and src.block == block_name
                    and isinstance(tgt, BlockEndpoint)):
                tb = self.project.block(tgt.block)
                if tb is None:
                    continue
                entry, in_regs = self.catalog.resolved_io(
                    tb.type, tb.params, library=tb.library)
                for r in in_regs:
                    if r not in regs:
                        regs.append(r)
                if entry not in entries:
                    entries.append(entry)
        return tuple(regs), tuple(entries)

    def export_bitstream(self, result, path: str | Path) -> None:
        """Write a built :class:`BuildResult` to a ``.kbs`` file."""
        from engine.io.kbs import Kbs, KbsChip, chip_type_hash, write_kbs

        chips = []
        for cid in sorted(result.chips):
            chip = self.project.chip(cid)
            type_name = (chip.type_name if chip and chip.type_name
                         else self.project.chip_type)
            chips.append(KbsChip(chip_type_hash(type_name), result.words(cid)))
        kbs = Kbs(chips=chips, metadata={
            "project_name": self.project.metadata.name,
            "ide_version": "1.0.0",
            "blocks": [b.name for b in self.project.blocks],
        })
        write_kbs(kbs, path)


def _default_registry() -> ChipTypeRegistry:
    """Registry scanning the bundled resources and the user chip dir (§9.2)."""
    dirs = [resource_path("resources/chips"), Path.home() / ".placekyt" / "chips"]
    return ChipTypeRegistry.from_dirs(dirs)


def _disassemble(words: list) -> list:
    """Disassemble 32 words → ``[(addr, word, mnemonic), …]`` (one per word).

    Uses ``simkyt.Program.from_words(...).disassemble()`` and parses its
    ``"  NN: WWWW  Mnemonic { … }"`` lines. Falls back to a raw hex listing if
    simkyt is unavailable or the format is unexpected.
    """
    try:
        import simkyt

        text = simkyt.Program.from_words("inspector", list(words)).disassemble()
    except Exception:  # noqa: BLE001
        return [(a, w, "") for a, w in enumerate(words)]

    rows: list = []
    for line in text.splitlines():
        s = line.strip()
        if not s or ":" not in s:
            continue
        addr_str, _, rest = s.partition(":")
        rest = rest.strip()
        word_str, _, mnem = rest.partition(" ")
        try:
            addr = int(addr_str.strip(), 16)
            word = int(word_str.strip(), 16)
        except ValueError:
            continue
        rows.append((addr, word, mnem.strip()))
    return rows or [(a, w, "") for a, w in enumerate(words)]


def _default_name(type_name: str) -> str:
    """A short instance name from a block type, e.g. AGCBlock → 'agc'."""
    base = type_name[:-5] if type_name.endswith("Block") else type_name
    return base.lower() or "block"
