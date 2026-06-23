"""CellItem — a single cell on the chip canvas (the architecture notes §3.2).

A custom ``QGraphicsItem`` (NOT QGraphicsRectItem). One ``paint()`` draws the
fill colour, the face-direction arrow, and the block label, selecting detail by
zoom level (the LOD table in §3.2). Simulation overlays (handshake flash, state
icons) are added in the Phase-B simulation milestone.

The §3.2 Qt contract is mandatory — missing any of it makes the item invisible
or unclickable:
  * override ``boundingRect()`` (else empty rect → never painted/hit-tested),
  * override ``paint()``,
  * ``setFlags(ItemIsSelectable | ItemSendsGeometryChanges)`` — NOT ItemIsMovable
    (movement goes through the command system; the data model is the source of
    truth for positions, §3.2),
  * ``setAcceptHoverEvents(True)`` for tooltips,
  * LOD via ``option.levelOfDetailFromTransform(painter.worldTransform())``
    (DPR-normalised — do NOT use ``painter.transform().m11()``).
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QGraphicsItem

from model.enums import Face

# Logical pixels per cell at zoom=1.0 (§3.2). Qt scales for HiDPI automatically.
CELL_PX = 64

# LOD thresholds (§3.2 paint table).
LOD_FULL = 0.8     # fill + arrow + full label
LOD_MEDIUM = 0.3   # fill + arrow + 3-char label
# below LOD_MEDIUM: solid fill only


class CellKind(Enum):
    """What a cell represents on the canvas (drives its colour)."""

    EMPTY = "empty"
    BLOCK = "block"      # a programmed block cell
    TRANSIT = "transit"  # routing-only cell


_EMPTY_FILL = QColor(48, 52, 58)       # dark slate (reads against the black bg)
_TRANSIT_FILL = QColor(120, 170, 210)  # light blue (§3.2)
_GRID_PEN = QColor(95, 100, 108)       # visible grid lines
_LABEL_COLOR = QColor(235, 235, 235)
_ARROW_COLOR = QColor(30, 30, 30)
_SELECT_COLOR = QColor(80, 160, 255)
_DEFAULT_BLOCK_FILL = QColor(110, 160, 110)  # green-ish default
_IO_INPUT = QColor(80, 220, 255)             # input cell border (cyan)
_IO_OUTPUT = QColor(255, 110, 220)           # output cell border (magenta)

# Per-block colour rotation so placed blocks are distinguishable at a glance.
# Muted/desaturated hues that read against the black bg AND stay clear of the
# transit-cell light blue (120,170,210). Assigned by stable index per block.
_BLOCK_PALETTE = [
    QColor(110, 160, 110),  # sage green
    QColor(180, 140, 90),   # tan
    QColor(160, 120, 170),  # mauve
    QColor(190, 150, 70),   # ochre
    QColor(130, 165, 120),  # olive
    QColor(170, 110, 110),  # dusty red
    QColor(120, 150, 160),  # slate teal (kept distinct from transit blue)
    QColor(150, 145, 100),  # khaki
    QColor(175, 120, 145),  # rose
    QColor(140, 155, 90),   # moss
]


def block_palette_color(index: int) -> QColor:
    """Stable rotation colour for a block by its index (DEBUG/canvas)."""
    return _BLOCK_PALETTE[index % len(_BLOCK_PALETTE)]


def _shift_for_state(base: QColor, state: str) -> QColor:
    """Shift a cell's OWN colour by sim state so blocks stay distinguishable
    while showing activity: muted/darker before it runs, brighter once it has
    executed. Uses HSV value+saturation so the hue (the block's identity) is
    preserved."""
    h, s, v, a = base.getHsv()
    # (value-multiplier, saturation-multiplier) per state.
    vm, sm = {
        "executing": (1.35, 1.15),  # brightest — PC advancing now
        "active": (1.12, 1.05),     # data flowing through
        "idle": (0.62, 0.55),       # muted — waiting / not yet run
        "halted": (0.5, 0.45),      # most muted — done/halted
    }.get(state, (1.0, 1.0))
    nv = max(0, min(255, int(v * vm)))
    ns = max(0, min(255, int(s * sm)))
    out = QColor()
    out.setHsv(h if h >= 0 else 0, ns, nv, a)
    return out

# Simulation-overlay fills (§3.2 cell-state colours). When a sim_state is set it
# overrides the static fill so the canvas shows the "living chip".
_SIM_EXECUTING = QColor(60, 220, 90)    # green bright — PC advancing
_SIM_ACTIVE = QColor(70, 140, 200)      # routing/arrival activity this window
_SIM_IDLE = QColor(150, 150, 150)       # light gray — waiting for input
_SIM_HALTED = QColor(70, 72, 74)        # dark gray — halted
_SIM_FILLS = {
    "executing": _SIM_EXECUTING,
    "active": _SIM_ACTIVE,
    "idle": _SIM_IDLE,
    "halted": _SIM_HALTED,
}

# Handshake-flash: a bright edge on the face a packet exits (data transfer).
_FLASH_COLOR = QColor(255, 80, 80)  # red — a transfer just happened on this face

# Face → unit direction vector (scene coords: +x east, +y south).
_FACE_VEC = {
    Face.NORTH: (0, -1),
    Face.SOUTH: (0, 1),
    Face.EAST: (1, 0),
    Face.WEST: (-1, 0),
}


class CellItem(QGraphicsItem):
    """One cell at grid position ``(cx, cy)`` within a chip."""

    def __init__(
        self,
        cx: int,
        cy: int,
        *,
        kind: CellKind = CellKind.EMPTY,
        face: Face | None = None,
        label: str = "",
        cell_id=None,
        fill: QColor | None = None,
        parent: QGraphicsItem | None = None,
    ):
        super().__init__(parent)
        self.cx = cx
        self.cy = cy
        self.kind = kind
        self.face = face
        self.label = label
        self.cell_id = cell_id  # block-relative id for BLOCK cells (else None)
        self.chip_id = None     # which chip this cell belongs to (set at render)
        self.route_name = None  # set on routing (transit) cells: the connection
        self.route_index = None
        self.io_role = None     # "input" / "output" for a block's interface cells
        self._fill = fill
        self.sim_state: str | None = None  # set during simulation (§3.2 overlay)
        # Handshake-flash intensity per face ("S"/"E"/"W"/"N" → 0..1), decayed
        # each animation tick. A face flashes when a packet exits the cell there.
        self._flash: dict[str, float] = {}
        self.has_breakpoint = False  # red dot marker (DEBUG §3.6)

        # §3.2 mandatory flags. NOT ItemIsMovable.
        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)

    # -- Qt geometry ----------------------------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt override)
        return QRectF(0, 0, CELL_PX, CELL_PX)

    # -- painting -------------------------------------------------------------

    def _base_color(self) -> QColor:
        """The cell's static colour (ignoring any sim-state shift)."""
        if self._fill is not None:
            return self._fill
        if self.kind is CellKind.TRANSIT:
            return _TRANSIT_FILL
        if self.kind is CellKind.BLOCK:
            return _DEFAULT_BLOCK_FILL
        return _EMPTY_FILL

    def _fill_color(self) -> QColor:
        # During simulation the cell-state SHIFTS the cell's own base colour
        # (brighter once it has executed) rather than replacing it with a generic
        # green — so each block keeps its distinct colour while still showing
        # activity. Empty cells (no own colour) fall back to the generic state
        # fills so routing/arrival activity is still visible on blank cells.
        if self.sim_state is not None and self.sim_state in _SIM_FILLS:
            base = self._base_color()
            has_own = (self._fill is not None
                       or self.kind in (CellKind.BLOCK, CellKind.TRANSIT))
            if has_own:
                return _shift_for_state(base, self.sim_state)
            return _SIM_FILLS[self.sim_state]
        return self._base_color()

    def set_sim_state(self, state: str | None) -> None:
        """Set/clear the simulation overlay state and request a repaint."""
        if state != self.sim_state:
            self.sim_state = state
            self.update()

    def set_breakpoint(self, has_bp: bool) -> None:
        """Mark/unmark this cell as having a breakpoint (red dot) and repaint."""
        if has_bp != self.has_breakpoint:
            self.has_breakpoint = has_bp
            self.update()

    def flash_face(self, face: str) -> None:
        """A packet just exited on ``face`` — light that edge fully (decays)."""
        self._flash[face] = 1.0
        self.update()

    def decay_flash(self, amount: float = 0.12) -> bool:
        """Fade all face flashes by ``amount`` (slow enough to be seen against
        the persistent cell-state overlay). Returns True if anything is still
        lit (so the canvas knows to keep animating)."""
        if not self._flash:
            return False
        for f in list(self._flash):
            self._flash[f] -= amount
            if self._flash[f] <= 0.0:
                del self._flash[f]
        self.update()
        return bool(self._flash)

    def clear_flash(self) -> None:
        if self._flash:
            self._flash.clear()
            self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        rect = self.boundingRect()

        # 1. fill (all LOD levels)
        painter.setBrush(QBrush(self._fill_color()))
        painter.setPen(QPen(_GRID_PEN, 0))
        painter.drawRect(rect)

        # 1b. handshake-flash: a bright bar on the face a packet just exited.
        if self._flash:
            self._draw_flash(painter, rect)

        if lod >= LOD_MEDIUM:
            # 2. face arrow
            if self.face is not None:
                self._draw_arrow(painter, rect)
            # 3. label (full at >=0.8, 3-char at >=0.3)
            if self.label:
                text = self.label if lod >= LOD_FULL else self.label[:3]
                painter.setPen(QPen(_LABEL_COLOR))
                painter.drawText(rect, Qt.AlignCenter, text)

        # I/O interface indicator (border): cyan = input, magenta = output.
        # Tells the user where to route on a multi-cell block (§3.2).
        if self.io_role is not None:
            pen = QPen(_IO_INPUT if self.io_role == "input" else _IO_OUTPUT)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect.adjusted(2, 2, -2, -2))

        # breakpoint marker — a red dot in the top-left corner (DEBUG §3.6).
        if self.has_breakpoint:
            d = CELL_PX * 0.16
            painter.setPen(QPen(QColor(20, 20, 20), 1))
            painter.setBrush(QBrush(QColor(255, 70, 70)))
            painter.drawEllipse(rect.left() + 4, rect.top() + 4, d, d)

        # selection highlight (topmost)
        if self.isSelected():
            pen = QPen(_SELECT_COLOR)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect.adjusted(1.5, 1.5, -1.5, -1.5))

    def _draw_flash(self, painter: QPainter, rect: QRectF) -> None:
        """Show a data transfer: a whole-cell glow (visible on any fill,
        including empty/transit cells) plus a bright bar on the exit face."""
        peak = max(self._flash.values()) if self._flash else 0.0
        painter.setPen(Qt.NoPen)
        # Whole-cell glow — strong enough to stand out over the persistent
        # cell-state overlay (blue 'active'), so transit data flow is obvious.
        if peak > 0:
            glow = QColor(_FLASH_COLOR)
            glow.setAlphaF(max(0.0, min(0.85, peak * 0.85)))
            painter.setBrush(QBrush(glow))
            painter.drawRect(rect)
        # Bright bar on each flashed exit face.
        thick = max(3.0, rect.width() * 0.18)
        for face, intensity in self._flash.items():
            if intensity <= 0:
                continue
            color = QColor(_FLASH_COLOR)
            color.setAlphaF(max(0.0, min(1.0, intensity)))
            painter.setBrush(QBrush(color))
            if face == "N":
                painter.drawRect(QRectF(rect.left(), rect.top(),
                                        rect.width(), thick))
            elif face == "S":
                painter.drawRect(QRectF(rect.left(), rect.bottom() - thick,
                                        rect.width(), thick))
            elif face == "W":
                painter.drawRect(QRectF(rect.left(), rect.top(),
                                        thick, rect.height()))
            elif face == "E":
                painter.drawRect(QRectF(rect.right() - thick, rect.top(),
                                        thick, rect.height()))

    def _draw_arrow(self, painter: QPainter, rect: QRectF) -> None:
        dx, dy = _FACE_VEC.get(self.face, (0, 0))
        if dx == 0 and dy == 0:
            return
        cx, cy = rect.center().x(), rect.center().y()
        L = CELL_PX * 0.28  # arrow half-length
        tip = QPointF(cx + dx * L, cy + dy * L)
        # base perpendicular to direction
        px, py = -dy, dx
        W = CELL_PX * 0.14
        base1 = QPointF(cx - dx * L * 0.4 + px * W, cy - dy * L * 0.4 + py * W)
        base2 = QPointF(cx - dx * L * 0.4 - px * W, cy - dy * L * 0.4 - py * W)
        painter.setBrush(QBrush(_ARROW_COLOR))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([tip, base1, base2]))
