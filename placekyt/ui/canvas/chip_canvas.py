"""ChipCanvas — the QGraphicsView chip display (the architecture notes §3.2).

Renders one or more chips' 10×12 grids with zoom/pan. This first version draws
the grid, chip outlines, and any placed block / transit cells from a
:class:`~model.project.Project`; interaction (drag-drop, routing, selection
commands) and simulation overlays come in later milestones.

§3.2 ChipCanvas contract:
  * ``setFocusPolicy(Qt.StrongFocus)`` for keyboard handling,
  * ``setTransformationAnchor(AnchorUnderMouse)`` so wheel-zoom zooms at cursor,
  * ``setViewportUpdateMode(BoundingRectViewportUpdate)``,
  * antialiasing on,
  * override ``wheelEvent`` for zoom (before the scroll area consumes it).
"""

from __future__ import annotations

import json
from enum import Enum

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from model.chip_type import ChipType
from model.enums import Face
from model.project import Project

from .cell_item import CELL_PX, CellItem, CellKind, block_palette_color
from .chip_outline import ChipOutlineItem
from .connection_item import ConnectionItem
from .footprint_item import FootprintItem
from .port_item import PortItem

# MIME type for library → canvas block drag-drop (§3.4).
BLOCK_MIME = "application/x-placekyt-block"

# Face enum → the single-letter face code the trace uses ("N"/"S"/"E"/"W").
_FACE_LETTERS = {Face.NORTH: "N", Face.SOUTH: "S",
                 Face.EAST: "E", Face.WEST: "W"}


def _face_letter(face) -> str:
    return _FACE_LETTERS.get(face, "")


# Arrow key → (dx, dy) in grid cells.
_ARROW_DELTA = {
    Qt.Key_Left: (-1, 0),
    Qt.Key_Right: (1, 0),
    Qt.Key_Up: (0, -1),
    Qt.Key_Down: (0, 1),
}


class Tool(Enum):
    """Canvas interaction mode (§3.2 canvas tool state)."""

    SELECT = "select"
    ROUTE_DRAW = "route_draw"

ZOOM_STEP = 1.15
MIN_SCALE = 0.1
MAX_SCALE = 6.0


def chip_cell_to_scene(chip_x: float, chip_y: float, cx: int, cy: int) -> tuple[float, float]:
    """Map (chip origin, cell x/y) to scene coords (§3.2 canonical mapping)."""
    return (chip_x + cx * CELL_PX, chip_y + cy * CELL_PX)


class ChipCanvas(QGraphicsView):
    """Scrollable, zoomable view of the project's chips."""

    # (block_type, library, chip_id, cell_x, cell_y) — emitted on a valid drop.
    block_dropped = Signal(str, object, int, int, int)
    # (dx, dy) grid-cell move requested via arrow keys with a selection.
    move_requested = Signal(int, int)
    # selection payload: a dict describing the selected cell, or None.
    selection_changed = Signal(object)
    # block name to delete (Delete key / context menu on a block cell).
    delete_requested = Signal(str)
    # (block_name, cell_id, face_value) — set-face request from context menu.
    set_face_requested = Signal(str, object, str)
    # (block_name, kind) — rotate/mirror a placed block; kind ∈ cw/ccw/
    # mirror_h/mirror_v (§3.2 block transforms).
    transform_requested = Signal(str, str)
    # (block_name, dx, dy) — drag-move of a whole block (committed on release).
    block_moved = Signal(str, int, int)
    # (panel_id, x, y) — drag-move of an SRAM panel (committed on release).
    panel_moved = Signal(int, float, float)
    # (panel_id) — delete an SRAM panel (Delete key / context menu).
    panel_delete_requested = Signal(int)
    # (panel_id) — horizontally mirror an SRAM panel (H key / context menu).
    panel_mirror_requested = Signal(int)
    # (panel_id) — open the SRAM inspector (double-click a panel).
    panel_inspect_requested = Signal(int)
    # (block_name, chip, ax, ay) — drag-move of a whole block to ANOTHER chip,
    # anchored at the drop cell (drop-point placement).
    block_moved_to_chip = Signal(str, int, int, int)
    # (block_name, cell_id, x, y) — Alt+drag of a single cell to a new position.
    cell_moved = Signal(str, object, int, int)
    # (source_block, target_block, points) — a route was drawn to completion.
    route_completed = Signal(object, object, object)
    # live hop count while drawing (int hops, bool overflow).
    route_progress = Signal(int, bool)
    # connection name to delete (Delete key / context menu on a route line).
    delete_connection_requested = Signal(str)
    # InterChipConnection to delete (Delete key on a selected inter-chip wire).
    delete_inter_chip_requested = Signal(object)
    # (chip, x, y, kind, value) — a breakpoint was requested via the cell context
    # menu (DEBUG §3.6). kind ∈ "pc"/"face"; value is a pc int or face string.
    breakpoint_requested = Signal(int, int, int, str, object)
    # (block_name, "#rrggbb" or None) — the user picked a block colour (or reset).
    block_color_requested = Signal(str, object)
    # (src_block, src_port, dst_block, dst_port) — a logical net was wired by
    # clicking one block-port stub then another (auto-P&R P2.3 capture).
    logical_wire_requested = Signal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        # §3.2 view contract.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(Qt.black)
        self.setAcceptDrops(True)
        self._flash_timer = None  # handshake-flash decay timer (lazy)
        # Per-word flash playback queue (#194): each entry is one sim-time STEP
        # {"cells": [...], "ports": [...]} = the words that transacted at one
        # instant. The flash timer releases steps one-at-a-time so consecutive
        # words light SEQUENTIALLY (a rolling wave), not the whole batch at once.
        self._flash_queue: list = []
        # Per-word flash steps released per decay tick (#194 / speed control):
        # a positive value caps playback to that many words per tick (slow-motion
        # at low speeds); 0 = adaptive catch-up (len//8). Set by the speed slider.
        self._flash_per_tick = 0

        self._scale = 1.0
        self._project: Project | None = None
        self._chip_types: dict[str, ChipType] = {}
        self._scene.selectionChanged.connect(self._on_selection_changed)

        # Route-draw tool state (§3.2).
        self._tool = Tool.SELECT
        self._route_points: list[tuple[int, int]] = []     # waypoint cells
        self._route_source = None                          # source block name
        self._route_chip = 0
        self._preview_item: ConnectionItem | None = None

        # Drag-move state (§3.2): set on a press over a block cell, used while
        # dragging to show the footprint preview, committed on release.
        self._drag_block: str | None = None
        self._drag_panel: int | None = None      # panel drag-move state
        self._drag_panel_grab = QPointF(0, 0)    # cursor offset within the panel
        self._drag_port = None                   # (chip_id, port_name) drag-to-waveform
        self._drag_port_start = QPointF(0, 0)    # press point for the drag threshold
        self._drag_route = None                  # connection_name drag-to-waveform
        self._drag_route_start = QPointF(0, 0)
        self._drag_cell_id = None        # for Alt+drag (single cell)
        self._drag_alt = False
        self._drag_start_cell: tuple[int, int] | None = None
        self._drag_chip = 0
        self._footprint: FootprintItem | None = None
        self._dragging = False
        # (block_type, library) -> list of (dx, dy) cell offsets, for the
        # library-drag preview. Set by MainWindow from the catalog.
        self.footprint_provider = None
        # (block_type, library) -> {port_name: (cell_id, direction)} from the
        # PortMap, for anchoring unrouted-connection flylines + port stubs at the
        # right block cell (auto-P&R P2.3). Set by MainWindow.
        self.port_cell_provider = None
        # Show labelled block-port stubs (auto-P&R P2.3). Off by default — the
        # dense edit view stays uncluttered until the user wants to wire blocks.
        self._show_port_stubs = False
        # First-clicked stub of a pending logical wire — (block, port, direction).
        self._pending_wire = None

    # -- model binding --------------------------------------------------------

    def set_project(self, project: Project, chip_types: dict[str, ChipType]) -> None:
        """Bind a project + its chip types and (re)render the scene."""
        self._project = project
        self._chip_types = chip_types
        self.render_scene()

    def render_scene(self) -> None:
        """Rebuild the scene from the bound project. Idempotent.

        ``scene.clear()`` destroys every item, including the selected one — which
        would fire ``selectionChanged`` and collapse the Inspector to "No
        selection" on every model edit (e.g. a handoff override that rebuilds
        the bitstream). To keep the user's selection across the rebuild we
        snapshot a stable key, suppress the intermediate selection signals while
        clearing/rebuilding, then re-select the matching item — emitting at most
        one ``selection_changed`` only if the selection actually changed.
        """
        key = self._selection_key()
        block_signals = self._scene.blockSignals(True)
        self._scene.clear()
        try:
            if self._project is not None:
                chips = self._project.chips or []
                for chip in chips:
                    ct = self._chip_type_for(chip)
                    if ct is None:
                        continue
                    self._render_chip(chip, ct)
                    self._render_ports(chip, ct)
                self._render_panels()
                self._render_connections()
                self._render_inter_chip_connections()
                self._render_panel_connections()
                if self._show_port_stubs:
                    self._render_block_port_stubs()
            self._preview_item = None  # cleared by scene.clear(); redraw on demand
            if self._project is not None:
                # Pad the scene rect so the user can pan the array fully off-
                # screen in all directions (scroll range follows the scene rect).
                items = self._scene.itemsBoundingRect()
                margin = max(items.width(), items.height())  # ~one array of slack
                self._scene.setSceneRect(
                    items.adjusted(-margin, -margin, margin, margin))
            restored = self._restore_selection(key)
        finally:
            self._scene.blockSignals(block_signals)
        if not block_signals:
            # We owned the signal block — surface the (re)selection exactly once
            # so the Inspector tracks the same item after the rebuild.
            if restored:
                self._on_selection_changed()
            elif key is not None:
                # The selected item no longer exists (e.g. block deleted).
                self.selection_changed.emit(None)

    def _selection_key(self):
        """A stable identifier for the current selection, robust to re-render.

        ``("cell", chip_id, cx, cy)`` for a cell item, ``("conn", name)`` for a
        routed connection, or ``None`` when nothing is selected. The chip id is
        part of the key so a re-render re-selects the SAME chip's cell (the same
        local coords exist on every chip).
        """
        cell = self.selected_cell()
        if cell is not None:
            return ("cell", getattr(cell, "chip_id", None), cell.cx, cell.cy)
        conn = self.selected_connection()
        if conn is not None and conn.connection_name:
            return ("conn", conn.connection_name)
        return None

    def _restore_selection(self, key) -> bool:
        """Re-select the item matching ``key`` after a re-render. Returns True
        if a matching item was found and selected."""
        if key is None:
            return False
        kind = key[0]
        for it in self._scene.items():
            if kind == "cell" and isinstance(it, CellItem) \
                    and (getattr(it, "chip_id", None), it.cx, it.cy) \
                    == (key[1], key[2], key[3]):
                it.setSelected(True)
                return True
            if kind == "conn" and isinstance(it, ConnectionItem) \
                    and it.connection_name == key[1]:
                it.setSelected(True)
                return True
        return False

    def _render_connections(self) -> None:
        """Draw each routed connection as a line through its waypoints, plus a
        selectable routing-cell marker on each intermediate waypoint (§3.2)."""
        from model.connection import ChipPortEndpoint

        # Cells occupied by a block, PER CHIP — don't draw a routing marker on a
        # block cell. Must be per-chip: the same local (x, y) exists on every
        # chip, so a block on another chip must NOT blank this route's waypoint.
        block_cells: dict[int, set] = {}
        for b in self._project.blocks:
            if b.placement is not None:
                block_cells.setdefault(b.placement.chip, set()).update(
                    (c.x, c.y) for c in b.placement.cells)

        for conn in self._project.connections:
            if not conn.is_routed:
                # A chip INPUT-port → block net injects DIRECTLY at the port edge
                # cell — it has no physical route by design (the build treats it as
                # a direct port injection; DRC accepts it unrouted). Drawing a fly
                # line for it falsely reads as "not connected", so skip it.
                if (isinstance(conn.source, ChipPortEndpoint)
                        and conn.source.port.endswith("_in")):
                    continue
                # An UNROUTED connection (a logical net / ``route=auto`` / no
                # route yet) draws as a dashed gray FLY LINE between its endpoint
                # anchors — captured wiring the Phase-3 router will materialise.
                self._render_fly_line(conn)
                continue
            chip_id = self._route_chip_of(conn)
            origin = self._chip_origin(chip_id)
            occupied = block_cells.get(chip_id, set())
            pts = [(p.x, p.y) for p in conn.route]
            end = None
            if isinstance(conn.target, ChipPortEndpoint):
                end = self._port_anchor(chip_id, conn.target.port)
            else:
                # Extend the line INTO the target block's input cell (#270) when the
                # route stops SHORT of it — a §1.2 bus/broker route ends at the
                # broker cell ABUTTING the input, so without this the line stops at
                # the cell edge and it isn't apparent where the connection goes. This
                # is a VISUAL extension only (the model route + the built bitstream
                # are untouched); skipped when the route already ends on the input
                # cell (manual/corridor routes), so nothing is double-drawn.
                in_center, in_cell = self._block_input_cell_center(
                    conn.target, chip_id)
                if in_center is not None and (not pts or pts[-1] != in_cell):
                    end = in_center
            self._scene.addItem(
                ConnectionItem(pts, origin, name=conn.name, end_point=end))
            # Routing-cell markers on intermediate waypoints (not the endpoints,
            # which sit on block cells). Face = direction to the next waypoint.
            for i in range(len(pts)):
                x, y = pts[i]
                if (x, y) in occupied:
                    continue
                face = self._waypoint_face(pts, i, conn, chip_id)
                item = CellItem(x, y, kind=CellKind.TRANSIT, face=face,
                                cell_id=("route", conn.name, i))
                item.route_name = conn.name
                item.route_index = i
                item.chip_id = chip_id  # the route lives on this chip
                sx, sy = chip_cell_to_scene(origin[0], origin[1], x, y)
                item.setPos(sx, sy)
                item.setZValue(2)  # above empty cells, below the route line
                self._scene.addItem(item)

    def _endpoint_anchor(self, endpoint):
        """Scene anchor for either endpoint kind of a connection (P2.3)."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        if isinstance(endpoint, ChipPortEndpoint):
            return self._port_anchor(endpoint.chip, endpoint.port)
        if isinstance(endpoint, BlockEndpoint):
            return self._block_port_anchor(endpoint)
        return None

    def _render_fly_line(self, conn) -> None:
        """Draw an unrouted connection as a dashed fly line between its endpoint
        anchors. Skips when either end can't be anchored (e.g. an unplaced
        block) — there is nothing to draw a line to yet."""
        start = self._endpoint_anchor(conn.source)
        end = self._endpoint_anchor(conn.target)
        if start is None or end is None:
            return
        self._scene.addItem(
            ConnectionItem.fly_line(start, end, name=conn.name))

    def set_show_port_stubs(self, show: bool) -> None:
        """Toggle the labelled block-port stubs (auto-P&R P2.3) and re-render."""
        show = bool(show)
        if show == self._show_port_stubs:
            return
        self._show_port_stubs = show
        self._pending_wire = None     # cancel any half-drawn wire
        self.render_scene()

    def _handle_stub_click(self, stub) -> bool:
        """Click-to-wire a logical net between two block-port stubs (P2.3).

        First click records the source stub; a second click on a DIFFERENT
        block's stub emits ``logical_wire_requested`` (normalised producer→
        consumer: the OUTPUT stub is the source). Returns True if the click was
        consumed (so the caller stops further handling)."""
        block, port, direction = stub.endpoint
        if self._pending_wire is None:
            self._pending_wire = (block, port, direction)
            stub.setSelected(True)
            return True
        sb, sp, sd = self._pending_wire
        self._pending_wire = None
        if sb == block:
            return True                # same block — cancel, don't self-wire
        # Normalise to producer (out) -> consumer (in).
        if sd == "out" and direction == "in":
            src, sport, dst, dport = sb, sp, block, port
        elif sd == "in" and direction == "out":
            src, sport, dst, dport = block, port, sb, sp
        else:
            # two outputs or two inputs — not a valid net; ignore.
            return True
        self.logical_wire_requested.emit(src, sport, dst, dport)
        return True

    def _render_block_port_stubs(self) -> None:
        """Draw a labelled stub at each placed block's external INPUT/OUTPUT port
        (auto-P&R P2.3). Port → cell + direction come from the block's PortMap via
        ``port_cell_provider``; the stub sits at that cell's outer face."""
        from .block_port_stub_item import BlockPortStubItem

        if self._project is None or self.port_cell_provider is None:
            return
        for blk in self._project.blocks:
            if blk.placement is None or not blk.placement.cells:
                continue
            try:
                pmap = self.port_cell_provider(blk.type, blk.library) or {}
            except Exception:  # noqa: BLE001
                pmap = {}
            origin = self._chip_origin(blk.placement.chip)
            for port_name, (cell_id, direction) in pmap.items():
                cell = blk.placement.cell(cell_id)
                if cell is None:
                    continue
                from PySide6.QtCore import QPointF
                from .port_item import _FACE_OUT
                cx = origin[0] + cell.x * CELL_PX + CELL_PX / 2
                cy = origin[1] + cell.y * CELL_PX + CELL_PX / 2
                dx, dy = _FACE_OUT.get(cell.face, (0, 0))
                anchor = QPointF(cx + dx * CELL_PX / 2, cy + dy * CELL_PX / 2)
                self._scene.addItem(BlockPortStubItem(
                    port_name, direction, anchor, cell.face,
                    block_name=blk.name))

    def _render_inter_chip_connections(self) -> None:
        """Draw each board-level chip-to-chip wire between its port anchors."""
        from .inter_chip_wire_item import InterChipWireItem

        for ic in self._project.inter_chip_connections:
            start = self._port_anchor(ic.from_chip, ic.from_port)
            end = self._port_anchor(ic.to_chip, ic.to_port)
            if start is None or end is None:
                continue
            self._scene.addItem(InterChipWireItem(start, end, ic=ic))

    def _waypoint_face(self, pts, i, conn=None, chip_id=0):
        """Face of a routing cell = direction to the NEXT waypoint. The FINAL
        waypoint faces its target: a chip-output port → the PORT's exit face
        (e.g. south for x1_out); a target block → toward that block's entry cell
        (so the marker shows the real direction the data leaves the route),
        matching the build's ``_apply_routes`` final-waypoint facing."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        def _dir(x0, y0, x1, y1):
            if x1 > x0:
                return Face.EAST
            if x1 < x0:
                return Face.WEST
            if y1 > y0:
                return Face.SOUTH
            if y1 < y0:
                return Face.NORTH
            return None

        if i + 1 < len(pts):
            f = _dir(*pts[i], *pts[i + 1])
            if f is not None:
                return f
        # Last waypoint: face the target.
        if conn is not None and self._project is not None:
            tgt = conn.target
            if isinstance(tgt, ChipPortEndpoint):
                ct = self._chip_type_for(self._project.chip(chip_id))
                port = ct.port(tgt.port) if ct else None
                if port is not None:
                    return Face.from_str(port.face.value)
            elif isinstance(tgt, BlockEndpoint):
                blk = self._project.block(tgt.block)
                if blk is not None and blk.placement and blk.placement.cells:
                    ec = blk.placement.cells[0]
                    f = _dir(*pts[i], ec.x, ec.y)
                    if f is not None:
                        return f
        return Face.EAST

    def _route_chip_of(self, conn) -> int:
        """The chip a connection's coordinates live on (source's chip, §2.1)."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        src = conn.source
        if isinstance(src, ChipPortEndpoint):
            return src.chip
        if isinstance(src, BlockEndpoint):
            blk = self._project.block(src.block)
            if blk and blk.placement is not None:
                return blk.placement.chip
        return 0

    def _render_ports(self, chip, ct) -> None:
        """Draw every chip I/O port marker (§3.2 port markers)."""
        connected = self._connected_ports(chip.id)
        origin = (chip.position_x, chip.position_y)
        for port in ct.ports:
            self._scene.addItem(PortItem(
                port, chip.id, origin,
                connected=(port.name in connected)))

    def _connected_ports(self, chip_id: int) -> set[str]:
        """Port names referenced by any connection/inter-chip link on a chip."""
        from model.connection import ChipPortEndpoint

        names: set[str] = set()
        for conn in self._project.connections:
            for ep in (conn.source, conn.target):
                if isinstance(ep, ChipPortEndpoint) and ep.chip == chip_id:
                    names.add(ep.port)
        for ic in self._project.inter_chip_connections:
            if ic.from_chip == chip_id:
                names.add(ic.from_port)
            if ic.to_chip == chip_id:
                names.add(ic.to_port)
        return names

    def port_items(self) -> list[PortItem]:
        return [it for it in self._scene.items() if isinstance(it, PortItem)]

    def _render_panels(self) -> None:
        """Draw each SRAM/peripheral panel as a chip-like box (the SRAM panel notes)."""
        from .panel_item import PanelItem

        for panel in (self._project.panels or []):
            connected = {pc.panel_port
                         for pc in self._project.panel_connections
                         if pc.panel == panel.id}
            self._scene.addItem(PanelItem(panel, connected_ports=connected))

    def _render_panel_connections(self) -> None:
        """Draw each panel↔chip wire between the panel port and the chip port."""
        from .inter_chip_wire_item import InterChipWireItem

        for pc in (self._project.panel_connections or []):
            start = self._panel_port_anchor(pc.panel, pc.panel_port)
            end = self._port_anchor(pc.chip, pc.chip_port)
            if start is None or end is None:
                continue
            self._scene.addItem(InterChipWireItem(start, end))

    def _panel_port_anchor(self, panel_id: int, port_name: str):
        """Scene point of a panel's port marker (for wires). Finds the rendered
        PanelItem so its layout (port spread) drives the anchor."""
        from .panel_item import PanelItem

        for it in self._scene.items():
            if isinstance(it, PanelItem) and it.panel_id == panel_id:
                return it.port_anchor_scene(port_name)
        return None

    def _port_anchor(self, chip_id: int, port_name: str):
        """Scene point of a port marker (the edge cell's outer face midpoint)."""
        from PySide6.QtCore import QPointF

        from .port_item import _FACE_OUT

        if self._project is None:
            return None
        chip = self._project.chip(chip_id)
        ct = self._chip_type_for(chip) if chip else None
        if ct is None:
            return None
        port = ct.port(port_name)
        if port is None:
            return None
        ox, oy = chip.position_x, chip.position_y
        cx = ox + port.cell_x * CELL_PX + CELL_PX / 2
        cy = oy + port.cell_y * CELL_PX + CELL_PX / 2
        dx, dy = _FACE_OUT.get(port.face, (0, 0))
        return QPointF(cx + dx * CELL_PX / 2, cy + dy * CELL_PX / 2)

    def _block_input_cell_center(self, endpoint, chip_id):
        """Scene CENTRE of a block-target endpoint's input cell, or None.

        Used to extend a routed connection's line INTO the target block's input
        cell (#270) when the route stops at the abutting broker. Returns None when
        the port→cell can't be resolved (then the line is left as-is). VISUAL only —
        does not touch the model route or the build."""
        from PySide6.QtCore import QPointF

        if self._project is None:
            return None, None
        blk = self._project.block(getattr(endpoint, "block", None))
        if blk is None or blk.placement is None or not blk.placement.cells:
            return None, None
        name = getattr(endpoint, "port", None)
        cell = None
        if self.port_cell_provider is not None and name is not None:
            try:
                pmap = self.port_cell_provider(blk.type, blk.library) or {}
            except Exception:  # noqa: BLE001
                pmap = {}
            entry = pmap.get(name)
            if entry is not None:
                cell = blk.placement.cell(entry[0])
        if cell is None:
            return None, None
        origin = self._chip_origin(chip_id)
        center = QPointF(origin[0] + cell.x * CELL_PX + CELL_PX / 2,
                         origin[1] + cell.y * CELL_PX + CELL_PX / 2)
        return center, (cell.x, cell.y)

    def _block_port_anchor(self, endpoint, port_name: str | None = None):
        """Scene point for a block-port endpoint of a logical net (P2.3 flylines).

        Resolves the BlockEndpoint's port to the placed cell that carries it (via
        the PortMap ``port_cell_provider``) and returns the midpoint of that
        cell's OUTER face. Falls back to the block's bounding-box centre when the
        port→cell map is unavailable or the cell isn't placed."""
        from PySide6.QtCore import QPointF

        if self._project is None:
            return None
        name = port_name if port_name is not None else getattr(endpoint, "port", None)
        blk = self._project.block(getattr(endpoint, "block", None))
        if blk is None or blk.placement is None or not blk.placement.cells:
            return None
        origin = self._chip_origin(blk.placement.chip)

        # Resolve the port → cell_id via the PortMap, then find that placed cell.
        target_cell = None
        if self.port_cell_provider is not None and name is not None:
            try:
                pmap = self.port_cell_provider(blk.type, blk.library) or {}
            except Exception:  # noqa: BLE001
                pmap = {}
            entry = pmap.get(name)
            if entry is not None:
                cell_id = entry[0]
                target_cell = blk.placement.cell(cell_id)

        if target_cell is not None:
            cx = origin[0] + target_cell.x * CELL_PX + CELL_PX / 2
            cy = origin[1] + target_cell.y * CELL_PX + CELL_PX / 2
            from .port_item import _FACE_OUT
            dx, dy = _FACE_OUT.get(target_cell.face, (0, 0))
            return QPointF(cx + dx * CELL_PX / 2, cy + dy * CELL_PX / 2)

        # Fallback: block bounding-box centre.
        bb = blk.placement.bounding_box()
        if bb is None:
            return None
        minx, miny, maxx, maxy = bb
        cx = origin[0] + (minx + maxx + 1) / 2 * CELL_PX
        cy = origin[1] + (miny + maxy + 1) / 2 * CELL_PX
        return QPointF(cx, cy)

    def _chip_type_for(self, chip) -> ChipType | None:
        name = chip.type_name or self._project.chip_type
        return self._chip_types.get(name)

    def _render_chip(self, chip, ct: ChipType) -> None:
        ox, oy = chip.position_x, chip.position_y
        outline = ChipOutlineItem(chip.label or f"Chip {chip.id}", ct.width, ct.height)
        outline.setPos(ox, oy)
        self._scene.addItem(outline)

        # Per-block fill colour: manual override (block.color) wins, else a
        # stable rotation colour by the block's index so placed blocks are
        # visually distinguishable (not all the same green).
        block_colors: dict[str, QColor] = {}
        for i, blk in enumerate(self._project.blocks):
            if blk.color:
                block_colors[blk.name] = QColor(blk.color)
            else:
                block_colors[blk.name] = block_palette_color(i)

        # Index placed/transit cells for this chip.
        block_cells: dict[tuple[int, int], tuple[str, Face, object]] = {}
        transit_cells: dict[tuple[int, int], Face] = {}
        io_roles: dict[tuple[int, int], str] = {}
        for blk in self._project.blocks:
            pl = blk.placement
            if pl is None or pl.chip != chip.id:
                continue
            for c in pl.cells:
                block_cells[(c.x, c.y)] = (blk.name, c.face, c.cell_id)
            for t in pl.transit_cells:
                transit_cells[(t.x, t.y)] = t.face
            # I/O cell indicators (§3.2): mark the block's REAL input/output cells
            # from its PortMap — NOT the first/last placed cell. For a FOLDED
            # multi-cell block the output is a MID-block cell (e.g. Costas `rotate`,
            # Gardner `loop_filter`), so the positional first/last guess put the pink
            # output border on the wrong cell. Resolve port → cell_id via the same
            # PortMap provider the flylines/stubs use; fall back to first/last only
            # when no provider is available.
            if len(pl.cells) > 1:
                pmap = None
                if self.port_cell_provider is not None:
                    try:
                        pmap = self.port_cell_provider(blk.type, blk.library) or {}
                    except Exception:  # noqa: BLE001
                        pmap = None
                if pmap:
                    for pname, entry in pmap.items():
                        cid = entry[0] if isinstance(entry, (tuple, list)) else entry
                        direction = (entry[1] if isinstance(entry, (tuple, list))
                                     and len(entry) > 1 else None)
                        pc = pl.cell(cid)
                        if pc is None or direction not in ("in", "out",
                                                           "input", "output"):
                            continue
                        role = "input" if direction in ("in", "input") else "output"
                        # Don't overwrite an "output" mark with "input" (a folded
                        # block may share a landing edge); output takes precedence.
                        cur = io_roles.get((pc.x, pc.y))
                        if cur != "output":
                            io_roles[(pc.x, pc.y)] = role
                else:
                    io_roles[(pl.cells[0].x, pl.cells[0].y)] = "input"
                    io_roles[(pl.cells[-1].x, pl.cells[-1].y)] = "output"

        for cy in range(ct.height):
            for cx in range(ct.width):
                pos = (cx, cy)
                if pos in block_cells:
                    name, face, cid = block_cells[pos]
                    item = CellItem(cx, cy, kind=CellKind.BLOCK, face=face,
                                    label=name, cell_id=cid,
                                    fill=block_colors.get(name))
                    item.io_role = io_roles.get(pos)
                elif pos in transit_cells:
                    item = CellItem(cx, cy, kind=CellKind.TRANSIT,
                                    face=transit_cells[pos])
                else:
                    item = CellItem(cx, cy, kind=CellKind.EMPTY)
                # Tag the cell with its chip so (cx, cy) lookups stay per-chip —
                # with two chips the same local coords exist on both (§3.2).
                item.chip_id = chip.id
                sx, sy = chip_cell_to_scene(ox, oy, cx, cy)
                item.setPos(sx, sy)
                self._scene.addItem(item)

    # -- zoom / pan -----------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        mods = event.modifiers()
        delta = event.angleDelta().y()
        # Ctrl+scroll pans horizontally, Shift+scroll pans vertically (§3.2).
        if mods & Qt.ControlModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        if mods & Qt.ShiftModifier:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        # Plain scroll zooms around the cursor.
        factor = ZOOM_STEP if delta > 0 else 1 / ZOOM_STEP
        new_scale = self._scale * factor
        if not (MIN_SCALE <= new_scale <= MAX_SCALE):
            return
        self._scale = new_scale
        self.scale(factor, factor)

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._scale = 1.0

    def _grid_fit_rect(self):
        """Bounding rect of the grid CELLS only (excludes port labels/arrows
        that inflate ``itemsBoundingRect`` and waste fit space). Falls back to
        the full items rect when there are no cells yet."""
        from PySide6.QtCore import QRectF

        rect = QRectF()
        for it in self._scene.items():
            if isinstance(it, CellItem):
                r = it.sceneBoundingRect()
                rect = r if rect.isEmpty() else rect.united(r)
        return rect if not rect.isEmpty() else self._scene.itemsBoundingRect()

    def fit_to_view(self) -> None:
        """Fit all chips in view (§3.2 — called on project open).

        Defers if the viewport has no real size yet (the window hasn't been
        shown/laid out) — otherwise fitInView would zoom to a near-zero scale
        and the chip would be an invisible speck. ``showEvent`` refits once the
        view has its real geometry, and the scale is clamped to a usable floor.
        """
        rect = self._grid_fit_rect()
        if rect.isEmpty():
            return
        vp = self.viewport().rect()
        if vp.width() < 50 or vp.height() < 50:
            self._pending_fit = True  # too small to fit meaningfully yet
            return
        self._pending_fit = False
        self.fitInView(rect, Qt.KeepAspectRatio)
        scale = self.transform().m11()
        if scale < MIN_SCALE:  # never crush the chip to an invisible speck
            self.resetTransform()
            self.scale(MIN_SCALE, MIN_SCALE)
            scale = MIN_SCALE
        self._scale = scale

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        # Refit once the view has its real on-screen geometry — but DEFER it to
        # the next event-loop turn. Calling fitInView() (which mutates the view
        # transform) inside showEvent can re-enter Qt's layout/paint machinery
        # and crash on the xcb backend.
        if getattr(self, "_pending_fit", False) or self._scale < 0.2:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self.fit_to_view)

    @property
    def scale_factor(self) -> float:
        return self._scale

    def cell_items(self) -> list[CellItem]:
        return [it for it in self._scene.items() if isinstance(it, CellItem)]

    @property
    def tool(self) -> "Tool":
        return self._tool

    # -- route drawing (§3.2) -------------------------------------------------

    def _cell_at(self, cx: int, cy: int, chip_id: int | None = None) -> CellItem | None:
        """The CellItem at local ``(cx, cy)``. With multiple chips the same local
        coords exist on every chip, so ``chip_id`` MUST be given to disambiguate
        (else routing/placement on one chip can hit another chip's cell)."""
        for it in self.cell_items():
            if it.cx == cx and it.cy == cy:
                if chip_id is None or getattr(it, "chip_id", None) == chip_id:
                    return it
        return None

    def start_route(self, block_name: str, chip: int, x: int, y: int) -> None:
        """Begin drawing a route from a source block cell (§3.2 step 1)."""
        self.cancel_route()
        self._tool = Tool.ROUTE_DRAW
        self._route_source = block_name
        self._route_chip = chip
        self._route_points = [(x, y)]
        self._scene.clearSelection()
        self._emit_progress()

    def start_route_from_port(self, chip: int, port_name: str) -> bool:
        """Begin drawing a route FROM a chip port (§3.2). The port's edge cell is
        the first waypoint; the route source is ``("port", chip, port_name)``.
        Returns False if the port has no resolvable edge cell."""
        edge = None
        chip_inst = self._project.chip(chip) if self._project else None
        ct = self._chip_type_for(chip_inst) if chip_inst else None
        port = ct.port(port_name) if ct else None
        if port is not None:
            edge = (port.cell_x, port.cell_y)
        if edge is None:
            return False
        self.cancel_route()
        self._tool = Tool.ROUTE_DRAW
        self._route_source = ("port", chip, port_name)
        self._route_chip = chip
        self._route_points = [edge]
        self._scene.clearSelection()
        self._emit_progress()
        return True

    def add_waypoint(self, x: int, y: int) -> bool:
        """Add an empty, N/S/E/W-adjacent cell to the path. Returns success.

        Diagonal moves, non-adjacent cells, occupied (block) cells, and repeats
        are rejected (§3.2 step 2/5)."""
        if self._tool is not Tool.ROUTE_DRAW or not self._route_points:
            return False
        last = self._route_points[-1]
        if (x, y) in self._route_points:
            return False
        if abs(x - last[0]) + abs(y - last[1]) != 1:  # not N/S/E/W adjacent
            return False
        cell = self._cell_at(x, y, self._route_chip)
        if cell is not None and cell.kind is CellKind.BLOCK:
            return False  # can't route through a block (target uses complete_route)
        self._route_points.append((x, y))
        self._update_preview()
        self._emit_progress()
        return True

    def undo_waypoint(self) -> None:
        """Backspace — drop the last waypoint (but keep the source, §3.2 step 7)."""
        if self._tool is Tool.ROUTE_DRAW and len(self._route_points) > 1:
            self._route_points.pop()
            self._update_preview()
            self._emit_progress()

    def complete_route(self, target_block: str, target_xy=None) -> None:
        """Finish at a target block cell; emit ``route_completed`` (§3.2 step 6).

        ``target_xy`` (the clicked destination cell) is appended to the path so
        the route reaches the target. It must be adjacent to the last waypoint.
        The emitted target descriptor is the block NAME (str)."""
        self._finish_route(target_block, target_xy)

    def complete_route_to_port(self, port_name: str) -> bool:
        """Finish at a chip I/O port (§3.2). The port's edge cell becomes the
        final waypoint; the build adds the +1 exit hop (§2.6).

        REJECTS (returns False, keeps drawing) unless the port's edge cell is
        the last waypoint or N/S/E/W-adjacent to it — otherwise the line would
        jump diagonally across the array to the port.
        """
        if self._tool is not Tool.ROUTE_DRAW or not self._route_points:
            return False
        edge = self._port_cell(port_name)
        if edge is None:
            return False
        last = self._route_points[-1]
        dist = abs(edge[0] - last[0]) + abs(edge[1] - last[1])
        if dist > 1:
            return False  # not adjacent — keep drawing toward the port
        target = ("port", self._route_chip, port_name)
        # If the edge cell isn't already the last waypoint, append it.
        self._finish_route(target, target_xy=(edge if dist == 1 else None))
        return True

    def _port_cell(self, port_name: str):
        """The (x, y) edge cell of a port on the route's chip, or None."""
        if self._project is None:
            return None
        chip = self._project.chip(self._route_chip)
        ct = self._chip_type_for(chip) if chip else None
        if ct is None:
            return None
        port = ct.port(port_name)
        return (port.cell_x, port.cell_y) if port else None

    def _finish_route(self, target, target_xy) -> None:
        if self._tool is not Tool.ROUTE_DRAW or self._route_source is None:
            return
        points = list(self._route_points)
        if target_xy is not None:
            last = points[-1]
            if (abs(target_xy[0] - last[0]) + abs(target_xy[1] - last[1]) == 1
                    and target_xy not in points):
                points.append(target_xy)
        source = self._route_source
        self.cancel_route()
        self.route_completed.emit(source, target, points)

    def cancel_route(self) -> None:
        """Escape / cleanup — leave route mode, remove the preview (§3.2 step 7)."""
        self._tool = Tool.SELECT
        self._route_points = []
        self._route_source = None
        self._remove_preview()

    @property
    def route_hops(self) -> int:
        """Current distance = waypoints excluding the source (adjacent = 1)."""
        return max(0, len(self._route_points) - 1)

    def _emit_progress(self) -> None:
        hops = self.route_hops
        self.route_progress.emit(hops, hops > 31)

    def _update_preview(self) -> None:
        self._remove_preview()
        if len(self._route_points) >= 2:
            origin = self._chip_origin(self._route_chip)
            self._preview_item = ConnectionItem(
                self._route_points, origin, preview=True)
            self._scene.addItem(self._preview_item)

    def _remove_preview(self) -> None:
        if self._preview_item is not None:
            self._scene.removeItem(self._preview_item)
            self._preview_item = None

    def _chip_origin(self, chip_id: int) -> tuple[float, float]:
        if self._project is not None:
            chip = self._project.chip(chip_id)
            if chip is not None:
                return (chip.position_x, chip.position_y)
        return (0.0, 0.0)

    def _route_chip_of_cell(self, item: CellItem) -> int:
        """The chip a block cell sits on (from the block's placement)."""
        if self._project is not None and item.label:
            blk = self._project.block(item.label)
            if blk is not None and blk.placement is not None:
                return blk.placement.chip
        return 0

    # -- simulation overlay (§3.2) --------------------------------------------

    def apply_cell_states(self, states: dict) -> None:
        """Apply a sim-state overlay to the cells, keyed by ``(chip_id, x, y)``.

        Keying by chip is required for multi-chip projects — the same local
        (x, y) exists on every chip, so a chip-0 overlay must NOT bleed onto
        another chip. Cells absent from ``states`` are cleared, so each frame
        fully describes the current overlay.
        """
        for item in self.cell_items():
            key = (getattr(item, "chip_id", 0) or 0, item.cx, item.cy)
            item.set_sim_state(states.get(key))

    def apply_resolved_faces(self, build) -> None:
        """Sync a BLOCK/TRANSIT cell's arrow to its BUILD-resolved output face
        (#135). A block's effective output face can be set by its program /
        route resolution during build, differing from the placement default the
        scene was rendered from — so the arrow could go stale.

        EMPTY cells are deliberately SKIPPED: the build auto-fills a forwarding
        face on every downstream cell even when the user has placed no block or
        route there, so honouring those would make deleted-route / blank cells
        wrongly show an arrow (they must look unprogrammed). The canvas reflects
        the MODEL, not the build's internal auto-forwarding. ``build`` may be
        None (leave arrows as-is)."""
        if build is None:
            return
        from model.enums import Face
        for item in self.cell_items():
            if item.kind is CellKind.EMPTY:
                continue  # blank cells never show a build-derived arrow
            chip = getattr(item, "chip_id", 0) or 0
            cells = build.chips[chip].cells if chip in build.chips else {}
            info = cells.get((item.cx, item.cy))
            face_str = info.get("face") if info else None
            if face_str:
                try:
                    new_face = Face.from_str(face_str)
                except Exception:  # noqa: BLE001
                    continue
                if item.face != new_face:
                    item.face = new_face
                    item.update()

    def apply_cell_faces(self, faces: dict) -> None:
        """Update cells' arrows to their LIVE simulation output face, keyed by
        ``(chip_id, x, y)`` (§real-time face). A cell that re-points at runtime
        (MOVE [FACE], e.g. the crossover relay) shows its CURRENT direction so
        the user can watch where data flows. Originals are remembered and
        restored by :meth:`clear_sim_states`."""
        from model.enums import Face
        if not hasattr(self, "_live_face_orig"):
            self._live_face_orig = {}
        for item in self.cell_items():
            key = (getattr(item, "chip_id", 0) or 0, item.cx, item.cy)
            face_str = faces.get(key)
            if not face_str:
                continue
            try:
                new_face = Face.from_str(face_str)
            except Exception:  # noqa: BLE001
                continue
            if item.face != new_face:
                self._live_face_orig.setdefault(id(item), (item, item.face))
                item.face = new_face
                item.update()

    def apply_breakpoints(self, bp_set) -> None:
        """Mark every cell that has a breakpoint (DEBUG §3.6). ``bp_set`` is an
        ``engine.breakpoints.BreakpointSet``; a cell is marked if it has any
        breakpoint (enabled or not)."""
        for item in self.cell_items():
            chip = getattr(item, "chip_id", 0) or 0
            item.set_breakpoint(bp_set.has_any(chip, item.cx, item.cy))

    def clear_sim_states(self) -> None:
        for item in self.cell_items():
            item.set_sim_state(None)
            item.clear_flash()
        for p in self.port_items():
            p.clear_flash()
        # Restore any arrows the live sim re-pointed (MOVE [FACE] cells).
        for item, orig in getattr(self, "_live_face_orig", {}).values():
            if item.face != orig:
                item.face = orig
                item.update()
        self._live_face_orig = {}
        self._flash_queue.clear()  # drop any un-played per-word flashes (#194)
        if self._flash_timer is not None:
            self._flash_timer.stop()

    def apply_handshakes(self, transfers) -> None:
        """Queue data transfers for PER-WORD flash playback (§3.2 / #194).

        ``transfers`` is ``{"steps": [{"cells": [(chip,x,y,face),…], "ports":
        [(chip, port_name),…]}, …], …}`` — one step per sim-time = one word
        transacted across the fabric. The steps are ENQUEUED and the shared flash
        timer releases them ONE AT A TIME (a rolling wave) so consecutive words
        light sequentially, not all-at-once. Falls back to the flat
        ``{"cells":…, "ports":…}`` form (one step) for old callers."""
        if not transfers:
            return
        if isinstance(transfers, dict) and "steps" in transfers:
            steps = [s for s in transfers["steps"]
                     if s.get("cells") or s.get("ports")]
        else:
            cell_xfers = transfers.get("cells", []) if isinstance(transfers, dict) \
                else transfers
            port_xfers = transfers.get("ports", []) if isinstance(transfers, dict) \
                else []
            steps = [{"cells": cell_xfers, "ports": port_xfers}] \
                if (cell_xfers or port_xfers) else []
        if not steps:
            return
        # Flash the FIRST step immediately (so a flash is observable right after
        # apply_handshakes, and a single-word frame behaves as before); the rest
        # are queued and the timer rolls them out one word per tick (#194).
        self._flash_step(steps[0])
        if len(steps) > 1:
            self._flash_queue.extend(steps[1:])
        self._ensure_flash_timer()

    def _flash_step(self, step) -> None:
        """Flash one per-word step: the cell exit faces + ports that transacted
        at one instant. Flashes the TOPMOST (visible) CellItem at each position —
        a routing waypoint has TWO stacked items (base grid cell Z=0 + opaque
        route-overlay TRANSIT cell Z=2); the higher-Z item is the one the user
        sees, so flashing the base cell would light a hidden cell."""
        cell_xfers = step.get("cells", [])
        port_xfers = step.get("ports", [])
        if cell_xfers:
            index: dict[tuple[int, int, int], CellItem] = {}
            for it in self.cell_items():
                key = (getattr(it, "chip_id", 0) or 0, it.cx, it.cy)
                cur = index.get(key)
                if cur is None or it.zValue() >= cur.zValue():
                    index[key] = it
            for (chip, x, y, face) in cell_xfers:
                item = index.get((chip, x, y))
                if item is not None:
                    item.flash_face(face)
        if port_xfers:
            pindex = {(p.chip_id, p.name): p for p in self.port_items()}
            for (chip, port_name) in port_xfers:
                p = pindex.get((chip, port_name))
                if p is not None:
                    p.flash()
        panel_xfers = step.get("panels", [])
        if panel_xfers:
            from .panel_item import PanelItem
            panels = {it.panel_id: it for it in self._scene.items()
                      if isinstance(it, PanelItem)}
            for (panel_id, activity) in panel_xfers:
                pan = panels.get(panel_id)
                if pan is not None:
                    pan.flash(activity)

    def panel_items(self) -> list:
        from .panel_item import PanelItem
        return [it for it in self._scene.items() if isinstance(it, PanelItem)]

    def flash_panel(self, panel_id: int, activity) -> None:
        """Blink an SRAM panel box for its read/write activity. ``activity`` is
        ``[(addr, "w"|"r"), …]``. (#194) Each (addr, op) is enqueued as its OWN
        per-word playback step so a burst blinks ONE WORD AT A TIME (a rolling
        write/read), synchronized with the cell/port rolling wave, instead of a
        single coalesced glow for the whole burst."""
        if not activity:
            return
        # First entry blinks immediately; the rest roll out one word per tick.
        self._flash_step({"panels": [(panel_id, [activity[0]])]})
        for entry in activity[1:]:
            self._flash_queue.append({"panels": [(panel_id, [entry])]})
        self._ensure_flash_timer()

    def set_flash_per_tick(self, n: int) -> None:
        """Set how many per-word flash steps are released per decay tick (0 =
        adaptive catch-up). Driven by the speed slider so the slow end shows
        individual transactions one-at-a-time."""
        self._flash_per_tick = max(0, int(n))

    def _ensure_flash_timer(self) -> None:
        if self._flash_timer is None:
            from PySide6.QtCore import QTimer

            self._flash_timer = QTimer(self)
            self._flash_timer.setInterval(40)  # ~25 fps decay
            self._flash_timer.timeout.connect(self._decay_flashes)
        if not self._flash_timer.isActive():
            self._flash_timer.start()

    def _decay_flashes(self) -> None:
        # (#194) First release queued per-word steps so consecutive words light
        # sequentially — ONE step per tick normally. If the backlog is large (a
        # fast run produced many words between frames), release several per tick
        # so playback keeps pace with the run without lagging arbitrarily; small
        # bursts (e.g. the SRAM demo's 6 packets) still roll one word per tick.
        if self._flash_queue:
            # Slow speeds cap to a fixed number of words per tick (so individual
            # transactions are visible); fast/default uses adaptive catch-up so
            # playback never lags arbitrarily behind a fast run.
            if self._flash_per_tick > 0:
                n = self._flash_per_tick
            else:
                n = max(1, len(self._flash_queue) // 8)
            for _ in range(n):
                if not self._flash_queue:
                    break
                self._flash_step(self._flash_queue.pop(0))

        still = bool(self._flash_queue)
        for it in self.cell_items():
            if it.decay_flash():
                still = True
        for p in self.port_items():
            if p.decay_flash():
                still = True
        for pan in self.panel_items():
            if pan.decay_flash():
                still = True
        if not still and self._flash_timer is not None:
            self._flash_timer.stop()

    # -- selection ------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        sel = [it for it in self._scene.selectedItems() if isinstance(it, CellItem)]
        if not sel:
            # No cell selected → clear any I/O-cell route bus-highlight.
            self._update_route_highlights(None)
            # A selected connection line shows connection details (hop override).
            conn = self.selected_connection()
            if conn is not None and conn.connection_name:
                self.selection_changed.emit({"connection": conn.connection_name})
            else:
                self.selection_changed.emit(None)
            return
        item = sel[0]
        # Highlight the whole physical bus of every connection through the
        # selected cell (route highlight, #266). No-op for non-route cells.
        self._update_route_highlights(item)
        self.selection_changed.emit({
            "cell": (item.cx, item.cy),
            "chip": getattr(item, "chip_id", 0) or 0,
            "kind": item.kind.value,
            "block": item.label or None,
            "cell_id": item.cell_id,
            "face": item.face.value if item.face else None,
            # routing-cell selection carries its connection + waypoint index
            "route": getattr(item, "route_name", None),
            "route_index": getattr(item, "route_index", None),
        })

    def connection_items(self) -> list[ConnectionItem]:
        return [it for it in self._scene.items()
                if isinstance(it, ConnectionItem)]

    def _update_route_highlights(self, cell: "CellItem | None") -> None:
        """Bus-highlight (#266): light every routed connection whose physical
        path runs through ``cell`` along its WHOLE path — from the source-output
        cell, through the transit lane, into the target-input cell — so where the
        connection goes is obvious. Connections terminating AT the cell (its block
        I/O is an endpoint) take precedence; if none terminate there, any
        connection merely transiting the cell is highlighted instead. Passing
        ``None`` (or a non-route cell) clears all highlights."""
        names: set[str] = set()
        if cell is not None and self._project is not None:
            from engine.route_analysis import (connections_terminating_at_cell,
                                                connections_through_cell)
            chip = getattr(cell, "chip_id", 0) or 0
            names = set(connections_terminating_at_cell(
                self._project, chip, cell.cx, cell.cy))
            if not names:
                names = set(connections_through_cell(
                    self._project, chip, cell.cx, cell.cy))
        for it in self.connection_items():
            it.set_related(it.connection_name in names if names else False)

    def selected_cell(self) -> CellItem | None:
        for it in self._scene.selectedItems():
            if isinstance(it, CellItem):
                return it
        return None

    def selected_port(self) -> "PortItem | None":
        for it in self._scene.selectedItems():
            if isinstance(it, PortItem):
                return it
        return None

    def current_selection(self) -> dict | None:
        """The current selection as a descriptor (same shape emitted by
        ``selection_changed``), or None. Lets panels re-pull the selection
        without waiting for a fresh signal (e.g. when a dock is re-shown)."""
        cell = self.selected_cell()
        if cell is not None:
            return {
                "cell": (cell.cx, cell.cy),
                "chip": getattr(cell, "chip_id", 0) or 0,
                "kind": cell.kind.value,
                "block": cell.label or None,
                "cell_id": cell.cell_id,
                "face": cell.face.value if cell.face else None,
                "route": getattr(cell, "route_name", None),
                "route_index": getattr(cell, "route_index", None),
            }
        conn = self.selected_connection()
        if conn is not None and conn.connection_name:
            return {"connection": conn.connection_name}
        return None

    # -- arrow-key move (§3.2) ------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._tool is Tool.ROUTE_DRAW:
            if event.key() == Qt.Key_Escape:
                self.cancel_route()
                self.route_progress.emit(0, False)
                event.accept()
                return
            if event.key() == Qt.Key_Backspace:
                self.undo_waypoint()
                event.accept()
                return
        # 'w' = wire/route from the selected block cell OR chip port (§3.2).
        if event.key() == Qt.Key_W:
            cell = self.selected_cell()
            if cell is not None and cell.label and cell.kind is CellKind.BLOCK:
                self.start_route(cell.label, self._route_chip_of_cell(cell),
                                 cell.cx, cell.cy)
                event.accept()
                return
            port = self.selected_port()
            if port is not None:
                self.start_route_from_port(port.chip_id, port.name)
                event.accept()
                return
        # Block transforms on the selected block: ] / [ rotate, H / V mirror.
        _tf_key = {
            Qt.Key_BracketRight: "cw",
            Qt.Key_BracketLeft: "ccw",
            Qt.Key_H: "mirror_h",
            Qt.Key_V: "mirror_v",
        }.get(event.key())
        if _tf_key is not None:
            cell = self.selected_cell()
            if cell is not None and cell.label and cell.kind is CellKind.BLOCK:
                self.transform_requested.emit(cell.label, _tf_key)
                event.accept()
                return
            # H on a selected panel → horizontal mirror (ports swap sides).
            panel = self.selected_panel()
            if panel is not None and _tf_key == "mirror_h":
                self.panel_mirror_requested.emit(panel.panel_id)
                event.accept()
                return
        delta = _ARROW_DELTA.get(event.key())
        if delta is not None and self.selected_cell() is not None:
            # A cell/block is selected → issue a move; suppress scrolling.
            self.move_requested.emit(*delta)
            event.accept()
            return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            cell = self.selected_cell()
            if cell is not None and cell.label:
                self.delete_requested.emit(cell.label)
                event.accept()
                return
            panel = self.selected_panel()
            if panel is not None:
                self.panel_delete_requested.emit(panel.panel_id)
                event.accept()
                return
            conn = self.selected_connection()
            if conn is not None and conn.connection_name:
                self.delete_connection_requested.emit(conn.connection_name)
                event.accept()
                return
            wire = self.selected_inter_chip()
            if wire is not None and wire.inter_chip is not None:
                self.delete_inter_chip_requested.emit(wire.inter_chip)
                event.accept()
                return
        super().keyPressEvent(event)  # no selection → default (scroll)

    def selected_connection(self) -> "ConnectionItem | None":
        for it in self._scene.selectedItems():
            if isinstance(it, ConnectionItem):
                return it
        return None

    def selected_panel(self):
        from .panel_item import PanelItem

        for it in self._scene.selectedItems():
            if isinstance(it, PanelItem):
                return it
        return None

    def selected_inter_chip(self):
        from .inter_chip_wire_item import InterChipWireItem

        for it in self._scene.selectedItems():
            if isinstance(it, InterChipWireItem):
                return it
        return None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Double-click an SRAM panel → open its contents inspector.
        from .panel_item import PanelItem
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, PanelItem):
            self.panel_inspect_requested.emit(item.panel_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._tool is Tool.ROUTE_DRAW and event.button() == Qt.LeftButton:
            view_pt = event.position().toPoint()
            # A clicked PortItem completes the route to that chip port (§3.2) —
            # UNLESS it is the port the route STARTED from. A chip port's marker
            # sits on top of its own edge cell (e.g. x16_in over cell (0,0)), so a
            # click meant for that cell hits the PortItem first. If that port is
            # the route source, do NOT treat the click as a completion (it would
            # try to close a zero-length route back onto the source and get
            # rejected, swallowing the click); fall through to cell handling so
            # the click advances the route from the source cell as the user
            # expects.
            clicked = self.itemAt(view_pt)
            src = self._route_source
            click_is_source_port = (
                isinstance(clicked, PortItem)
                and isinstance(src, tuple) and len(src) == 3 and src[0] == "port"
                and clicked.chip_id == src[1] and clicked.name == src[2])
            if isinstance(clicked, PortItem) \
                    and clicked.chip_id == self._route_chip \
                    and not click_is_source_port:
                if self.complete_route_to_port(clicked.name):
                    event.accept()
                    return
                # non-adjacent port → ignore the click, keep drawing
                self.route_progress.emit(self.route_hops, self.route_hops > 31)
                event.accept()
                return
            pt = self.mapToScene(view_pt)
            hit = self._which_chip_at(pt.x(), pt.y())
            # Only act on clicks on the ROUTE's chip — a route lives on one chip
            # (§2.1). Clicks on another chip are ignored so its cells (which share
            # the same local coords) don't spuriously complete the route.
            if hit is not None and hit[0].id == self._route_chip:
                _chip, _ct, cx, cy = hit
                cell = self._cell_at(cx, cy, self._route_chip)
                if cell is not None and cell.kind is CellKind.BLOCK \
                        and cell.label != self._route_source:
                    self.complete_route(cell.label, (cx, cy))  # clicked target
                else:
                    self.add_waypoint(cx, cy)
            event.accept()
            return

        # Logical-wire click: with port stubs shown, click one stub then another
        # to create a logical net (auto-P&R P2.3). Output→input is normalised so
        # the net always flows producer→consumer regardless of click order.
        if self._show_port_stubs and event.button() == Qt.LeftButton:
            from .block_port_stub_item import BlockPortStubItem
            it = self.itemAt(event.position().toPoint())
            if isinstance(it, BlockPortStubItem):
                if self._handle_stub_click(it):
                    event.accept()
                    return

        # SELECT mode: a left-press on a block cell arms a potential drag-move;
        # a left-press on a panel arms a free-position panel drag.
        if event.button() == Qt.LeftButton:
            view_pt = event.position().toPoint()
            item = self.itemAt(view_pt)
            from .panel_item import PanelItem
            # Grab-route (#268): a click on the route line where it runs INTO a
            # block I/O cell selects THIS connection (so the route between the two
            # block I/O cells can be grabbed + deleted), instead of the cell. The
            # route item sits above the cell (Z=5), so itemAt returns it only when
            # the click lands on the route's hit band; elsewhere in the cell the
            # underlying CellItem is hit and normal cell selection applies.
            if isinstance(item, ConnectionItem) and item.connection_name \
                    and not item.is_fly \
                    and item.covers_io_cell(self.mapToScene(view_pt)):
                self._scene.clearSelection()
                item.setSelected(True)
                event.accept()
                return
            if isinstance(item, CellItem) and item.label \
                    and item.kind is CellKind.BLOCK:
                self._arm_drag(item, event.modifiers())
            elif isinstance(item, PanelItem):
                self._drag_panel = item.panel_id
                pt = self.mapToScene(event.position().toPoint())
                self._drag_panel_grab = pt - item.pos()  # offset within item
            elif isinstance(item, PortItem):
                # Arm a port → waveform-viewer drag (started on threshold move in
                # mouseMoveEvent). Selection still works on a plain click.
                self._drag_port = (item.chip_id, item.name)
                self._drag_port_start = event.position().toPoint()
            elif isinstance(item, ConnectionItem) and item.connection_name \
                    and not item.is_fly:
                # Arm a ROUTE → waveform-viewer drag (plot the channels flowing
                # through the route). Threshold-started in mouseMoveEvent; a plain
                # click still selects the route.
                self._drag_route = item.connection_name
                self._drag_route_start = event.position().toPoint()
        super().mousePressEvent(event)

    def _start_port_drag(self, chip_id: int, port_name: str) -> None:
        """Begin a QDrag carrying a chip port's identity, so the waveform viewer
        can accept the drop and offer its channel/tag picker."""
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag

        mime = QMimeData()
        mime.setData("application/x-placekyt-port",
                     f"{chip_id},{port_name}".encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def _start_route_drag(self, connection_name: str) -> None:
        """Begin a QDrag carrying a route's connection name, so the waveform
        viewer can plot the channels flowing through it (with the same picker)."""
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag

        mime = QMimeData()
        mime.setData("application/x-placekyt-route", connection_name.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def _arm_drag(self, item: CellItem, modifiers) -> None:
        self._drag_block = item.label
        self._drag_cell_id = item.cell_id
        # Ctrl+drag = move a single cell out of the block. (Alt is reserved by
        # most window managers for move-the-window, so Ctrl is used here.)
        self._drag_alt = bool(modifiers & Qt.ControlModifier)
        self._drag_start_cell = (item.cx, item.cy)
        self._drag_chip = self._route_chip_of_cell(item)
        self._dragging = False  # becomes True once the cursor leaves the cell

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Port → waveform-viewer drag: once the cursor moves past the drag
        # threshold, start a QDrag carrying the port identity. The waveform view
        # accepts it and pops a channel/tag picker.
        if self._drag_port is not None and (event.buttons() & Qt.LeftButton):
            from PySide6.QtWidgets import QApplication
            start = self._drag_port_start
            if (event.position().toPoint() - start).manhattanLength() \
                    >= QApplication.startDragDistance():
                self._start_port_drag(*self._drag_port)
                self._drag_port = None
            event.accept()
            return
        if self._drag_route is not None and (event.buttons() & Qt.LeftButton):
            from PySide6.QtWidgets import QApplication
            if (event.position().toPoint() - self._drag_route_start).manhattanLength() \
                    >= QApplication.startDragDistance():
                self._start_route_drag(self._drag_route)
                self._drag_route = None
            event.accept()
            return
        if self._drag_panel is not None and (event.buttons() & Qt.LeftButton):
            from .panel_item import PanelItem
            pt = self.mapToScene(event.position().toPoint())
            new_pos = pt - self._drag_panel_grab
            for it in self._scene.items():
                if isinstance(it, PanelItem) and it.panel_id == self._drag_panel:
                    it.setPos(new_pos)        # live move; committed on release
                    break
            event.accept()
            return
        if self._drag_block is not None and (event.buttons() & Qt.LeftButton):
            pt = self.mapToScene(event.position().toPoint())
            hit = self._which_chip_at(pt.x(), pt.y())
            # A whole-block drag may cross to ANOTHER chip (drop-point placement).
            # A single-cell (Ctrl) breakout stays on its own chip.
            cross_ok = hit is not None and not self._drag_alt
            same_chip = hit is not None and hit[0].id == self._drag_chip
            if same_chip or cross_ok:
                tgt_chip, _ct, cx, cy = hit
                if (tgt_chip.id, cx, cy) != (self._drag_chip, *self._drag_start_cell):
                    self._dragging = True
                self._show_drag_footprint(cx, cy, chip_id=tgt_chip.id)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._drag_port = None        # a plain click on a port → no drag started
        self._drag_route = None       # a plain click on a route → no drag started
        if self._drag_panel is not None:
            from .panel_item import PanelItem
            pid = self._drag_panel
            self._drag_panel = None
            for it in self._scene.items():
                if isinstance(it, PanelItem) and it.panel_id == pid:
                    p = it.pos()
                    self.panel_moved.emit(pid, float(p.x()), float(p.y()))
                    event.accept()
                    return
        if self._drag_block is not None:
            dragging = self._dragging and self._footprint is not None
            # Tear down the drag overlay + state BEFORE emitting the move signal:
            # the move triggers a scene re-render (scene.clear()) which would
            # delete the footprint item out from under us.
            dest = self._drag_dest_cell(event) if dragging else None
            block, alt, cell_id = self._drag_block, self._drag_alt, self._drag_cell_id
            start = self._drag_start_cell
            start_chip = self._drag_chip
            self._end_drag()
            committed = False
            # dest = (chip, cx, cy); a no-op drop = same chip AND same cell.
            if dragging and dest is not None \
                    and dest != (start_chip, start[0], start[1]):
                committed = self._emit_move(block, alt, cell_id, start, dest)
            if committed:
                event.accept()
                return
        super().mouseReleaseEvent(event)

    # -- drag-move helpers ----------------------------------------------------

    def _drag_dest_cell(self, event):
        """Return ``(chip_id, cx, cy)`` for the drop, or None. A Ctrl single-cell
        drag stays on its own chip; a whole-block drag may target any chip."""
        pt = self.mapToScene(event.position().toPoint())
        hit = self._which_chip_at(pt.x(), pt.y())
        if hit is None:
            return None
        if self._drag_alt and hit[0].id != self._drag_chip:
            return None  # single-cell breakout stays on its chip
        return (hit[0].id, hit[2], hit[3])

    def _emit_move(self, block, alt, cell_id, start, dest) -> bool:
        dest_chip, dcx, dcy = dest
        sx, sy = start
        if alt:
            # Move just this one cell to dest on the same chip (if free).
            if not self._footprint_overlaps_for(block, [(dcx, dcy)],
                                               chip_id=dest_chip):
                self.cell_moved.emit(block, cell_id, dcx, dcy)
                return True
            return False
        block_cells = self._block_cells(block)
        if dest_chip != self._drag_chip:
            # Cross-chip: anchor the block's first cell at the drop cell.
            a0x, a0y = block_cells[0] if block_cells else (sx, sy)
            moved = [(x - a0x + dcx, y - a0y + dcy) for x, y in block_cells]
            if self._footprint_overlaps_for(block, moved, chip_id=dest_chip):
                return False
            self.block_moved_to_chip.emit(block, dest_chip, dcx, dcy)
            return True
        # Same-chip whole-block move by (dx, dy).
        dx, dy = dcx - sx, dcy - sy
        moved = [(x + dx, y + dy) for x, y in block_cells]
        if self._footprint_overlaps_for(block, moved, chip_id=dest_chip):
            return False
        self.block_moved.emit(block, dx, dy)
        return True

    def _block_cells(self, block_name: str) -> list[tuple[int, int]]:
        if self._project is None:
            return []
        blk = self._project.block(block_name)
        if blk is None or blk.placement is None:
            return []
        return [(c.x, c.y) for c in blk.placement.cells]

    def _occupied_except(self, block_name: str, chip_id: int) -> set[tuple[int, int]]:
        """All occupied grid cells on ``chip_id`` except ``block_name``'s."""
        occ: set[tuple[int, int]] = set()
        if self._project is None:
            return occ
        for blk in self._project.blocks:
            if blk.name == block_name or blk.placement is None \
                    or blk.placement.chip != chip_id:
                continue
            for c in blk.placement.cells:
                occ.add((c.x, c.y))
            for t in blk.placement.transit_cells:
                occ.add((t.x, t.y))
        return occ

    def _footprint_overlaps_for(self, block_name: str,
                                cells: list[tuple[int, int]],
                                chip_id: int | None = None) -> bool:
        chip_id = self._drag_chip if chip_id is None else chip_id
        occ = self._occupied_except(block_name, chip_id)
        ct = self._chip_type_for(self._project.chip(chip_id)) \
            if self._project else None
        for cx, cy in cells:
            if (cx, cy) in occ:
                return True
            if ct is not None and not ct.in_bounds(cx, cy):
                return True
        return False

    def _show_drag_footprint(self, cx: int, cy: int,
                             chip_id: int | None = None) -> None:
        chip_id = self._drag_chip if chip_id is None else chip_id
        cross = chip_id != self._drag_chip
        if self._drag_alt:
            cells = [(cx, cy)]
        elif cross:
            # Cross-chip: anchor the block's FIRST cell at the drop cell, keeping
            # its shape (offsets relative to the anchor) — drop-point placement.
            block_cells = self._block_cells(self._drag_block)
            if block_cells:
                a0x, a0y = block_cells[0]
                cells = [(x - a0x + cx, y - a0y + cy) for x, y in block_cells]
            else:
                cells = [(cx, cy)]
        else:
            sx, sy = self._drag_start_cell
            dx, dy = cx - sx, cy - sy
            cells = [(x + dx, y + dy) for x, y in self._block_cells(self._drag_block)]
        bad = {c for c in cells
               if self._footprint_overlaps_for(self._drag_block, [c],
                                               chip_id=chip_id)}
        self._remove_footprint()
        origin = self._chip_origin(chip_id)
        self._footprint = FootprintItem(cells, origin, bad_cells=bad)
        self._scene.addItem(self._footprint)

    def _remove_footprint(self) -> None:
        if self._footprint is not None:
            try:
                self._scene.removeItem(self._footprint)
            except RuntimeError:
                pass  # already removed by a scene.clear() (re-render)
            self._footprint = None

    def _end_drag(self) -> None:
        self._remove_footprint()
        self._drag_block = None
        self._drag_cell_id = None
        self._drag_alt = False
        self._drag_start_cell = None
        self._dragging = False

    # -- context menu (§3.2) --------------------------------------------------

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt override)
        from PySide6.QtWidgets import QMenu

        item = self.itemAt(event.pos())
        # A route line → offer to delete the connection (§3.2 route selection).
        if isinstance(item, ConnectionItem) and item.connection_name:
            menu = QMenu(self)
            menu.addAction(
                "Delete Route",
                lambda: self.delete_connection_requested.emit(item.connection_name))
            menu.exec(event.globalPos())
            return
        # An SRAM panel → mirror / delete.
        from .panel_item import PanelItem
        if isinstance(item, PanelItem):
            item.setSelected(True)
            menu = QMenu(self)
            menu.addAction(
                "Mirror Horizontal\tH",
                lambda: self.panel_mirror_requested.emit(item.panel_id))
            menu.addSeparator()
            menu.addAction(
                "Delete Panel",
                lambda: self.panel_delete_requested.emit(item.panel_id))
            menu.exec(event.globalPos())
            return
        if not isinstance(item, CellItem):
            return  # only cells / routes / panels have a context menu
        item.setSelected(True)
        menu = QMenu(self)
        # Block-cell actions (route / delete / face) — only for programmed cells.
        if item.label:
            menu.addAction(
                "Route from here…",
                lambda: self.start_route(item.label,
                                         self._route_chip_of_cell(item),
                                         item.cx, item.cy))
            menu.addSeparator()
            menu.addAction("Delete Block",
                           lambda: self.delete_requested.emit(item.label))
            face_menu = menu.addMenu("Set Face")
            for face in Face:
                face_menu.addAction(
                    face.value.capitalize(),
                    lambda f=face: self.set_face_requested.emit(
                        item.label, item.cell_id, f.value),
                )
            tf_menu = menu.addMenu("Transform")
            for kind, text in (("cw", "Rotate CW\t]"),
                               ("ccw", "Rotate CCW\t["),
                               ("mirror_h", "Mirror Horizontal\tH"),
                               ("mirror_v", "Mirror Vertical\tV")):
                tf_menu.addAction(
                    text,
                    lambda k=kind: self.transform_requested.emit(item.label, k))
            menu.addAction("Change Color…",
                           lambda: self._pick_block_color(item.label))
            menu.addAction("Reset Color",
                           lambda: self.block_color_requested.emit(item.label,
                                                                   None))
            menu.addSeparator()
        # Breakpoint actions (DEBUG §3.6) — available on ANY cell.
        chip = getattr(item, "chip_id", 0) or 0
        bp_menu = menu.addMenu("Add Breakpoint")
        bp_menu.addAction(
            "Break on execute (PC)…",
            lambda: self._request_pc_breakpoint(chip, item.cx, item.cy))
        face_bp = bp_menu.addMenu("Break on arrival @face")
        for face in Face:
            face_bp.addAction(
                face.value.capitalize(),
                lambda f=face: self.breakpoint_requested.emit(
                    chip, item.cx, item.cy, "face", _face_letter(f)))
        menu.exec(event.globalPos())

    def _request_pc_breakpoint(self, chip: int, x: int, y: int) -> None:
        """Prompt for a PC value, then request a PC breakpoint on the cell."""
        from PySide6.QtWidgets import QInputDialog

        pc, ok = QInputDialog.getInt(
            self, "PC breakpoint",
            f"Break when cell ({x},{y}) executes PC ==", 0, 0, 63)
        if ok:
            self.breakpoint_requested.emit(chip, x, y, "pc", int(pc))

    def _pick_block_color(self, block_name: str) -> None:
        """Open a colour picker for a block; emit the chosen "#rrggbb"."""
        from PySide6.QtWidgets import QColorDialog

        # Seed the dialog with the block's current colour if any.
        cur = None
        if self._project is not None:
            blk = self._project.block(block_name)
            cur = QColor(blk.color) if blk and blk.color else None
        chosen = QColorDialog.getColor(
            cur or QColor(110, 160, 110), self, f"Colour for {block_name}")
        if chosen.isValid():
            self.block_color_requested.emit(block_name, chosen.name())

    # -- drag-drop placement (§3.4) -------------------------------------------

    def _which_chip_at(self, scene_x: float, scene_y: float):
        """Return (chip, chip_type, cell_x, cell_y) for a scene point, or None."""
        if self._project is None:
            return None
        for chip in self._project.chips:
            ct = self._chip_type_for(chip)
            if ct is None:
                continue
            lx = scene_x - chip.position_x
            ly = scene_y - chip.position_y
            cx = int(lx // CELL_PX)
            cy = int(ly // CELL_PX)
            if 0 <= cx < ct.width and 0 <= cy < ct.height:
                return (chip, ct, cx, cy)
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(BLOCK_MIME):
            return
        event.acceptProposedAction()
        # Footprint preview at the snapped grid position (§3.2).
        payload = self._block_payload(event.mimeData())
        scene_pt = self.mapToScene(event.position().toPoint())
        hit = self._which_chip_at(scene_pt.x(), scene_pt.y())
        if payload is None or hit is None:
            self._remove_footprint()
            return
        chip, _ct, cx, cy = hit
        offsets = self._lib_footprint(payload)
        cells = [(cx + dx, cy + dy) for dx, dy in offsets]
        self._drag_chip = chip.id  # for the overlap check
        bad = {c for c in cells if self._footprint_overlaps_for("", [c])}
        self._remove_footprint()
        self._footprint = FootprintItem(
            cells, (chip.position_x, chip.position_y), bad_cells=bad)
        self._scene.addItem(self._footprint)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._remove_footprint()
        super().dragLeaveEvent(event)

    def _block_payload(self, mime):
        try:
            return json.loads(bytes(mime.data(BLOCK_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _lib_footprint(self, payload) -> list[tuple[int, int]]:
        """Cell offsets for a library block being dragged (default: a 1-cell
        spot; the provider gives the real footprint when set)."""
        if self.footprint_provider is not None:
            offs = self.footprint_provider(
                payload.get("block_type", ""), payload.get("library"))
            if offs:
                return list(offs)
        return [(0, 0)]

    def dropEvent(self, event) -> None:  # noqa: N802
        self._remove_footprint()
        md = event.mimeData()
        payload = self._block_payload(md) if md.hasFormat(BLOCK_MIME) else None
        if payload is None:
            return
        scene_pt = self.mapToScene(event.position().toPoint())
        hit = self._which_chip_at(scene_pt.x(), scene_pt.y())
        if hit is None:
            return  # dropped off any chip
        chip, _ct, cx, cy = hit
        self.block_dropped.emit(
            payload.get("block_type", ""), payload.get("library"),
            chip.id, cx, cy,
        )
        event.acceptProposedAction()
