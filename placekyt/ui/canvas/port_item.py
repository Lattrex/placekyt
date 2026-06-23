"""PortItem — an I/O port marker on a chip edge (the architecture notes §3.2).

A triangle just outside the port's edge cell, pointing in the port's face
direction, with a small label (e.g. "x16"). Filled when the port is connected,
outline-only otherwise. Clickable so a route can complete at the port
(§3.2 inter-chip / port routing).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem

from model.chip_type import PortSpec
from model.enums import Face

from .cell_item import CELL_PX

_PORT_COLOR = QColor(230, 180, 80)     # amber-gold
_LABEL_COLOR = QColor(230, 230, 230)
_MARK = CELL_PX * 0.32                  # marker size

# Unit vector pointing OUT of the chip for each face (scene coords).
_FACE_OUT = {
    Face.NORTH: (0, -1),
    Face.SOUTH: (0, 1),
    Face.EAST: (1, 0),
    Face.WEST: (-1, 0),
}


class PortItem(QGraphicsItem):
    """A chip I/O port marker positioned just outside its edge cell."""

    def __init__(self, port: PortSpec, chip_id: int,
                 chip_origin: tuple[float, float],
                 *, connected: bool = False, parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self.port = port
        self.chip_id = chip_id
        self._connected = connected
        self._flash = 0.0  # data-flow flash intensity (0..1), decays
        ox, oy = chip_origin
        # Anchor = the edge cell's outer face midpoint, nudged outward.
        cell_cx = ox + port.cell_x * CELL_PX + CELL_PX / 2
        cell_cy = oy + port.cell_y * CELL_PX + CELL_PX / 2
        out_x, out_y = _FACE_OUT.get(port.face, (0, 0))
        # midpoint on the cell's outer edge in the face direction
        self._anchor = QPointF(cell_cx + out_x * CELL_PX / 2,
                               cell_cy + out_y * CELL_PX / 2)
        # The ARROW points in the data-flow direction: OUTPUT ports point OUT of
        # the chip (data exits), INPUT ports point IN (data enters). This is the
        # visual cue that distinguishes inputs from outputs (§3.2).
        is_input = port.direction.value == "input"
        self._dir = (-out_x, -out_y) if is_input else (out_x, out_y)
        self._out = (out_x, out_y)  # outward dir for label placement
        self.setZValue(8)  # above cells + routes
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setToolTip(
            f"{port.name} ({port.direction.value}, {port.width}-bit)")

    @property
    def name(self) -> str:
        return self.port.name

    def set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            self.update()

    def flash(self) -> None:
        """Data just flowed through this port — light it (decays)."""
        self._flash = 1.0
        self.update()

    def decay_flash(self, amount: float = 0.12) -> bool:
        """Fade the flash slowly enough to be seen. Returns True if still lit."""
        if self._flash <= 0:
            return False
        self._flash = max(0.0, self._flash - amount)
        self.update()
        return self._flash > 0

    def clear_flash(self) -> None:
        if self._flash:
            self._flash = 0.0
            self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        # Generous box around the marker + label + flash halo (8px pen).
        m = _MARK + 12
        return QRectF(self._anchor.x() - m, self._anchor.y() - m - 8,
                      2 * m + 32, 2 * m + 16)

    def _triangle(self) -> QPolygonF:
        dx, dy = self._dir
        a = self._anchor
        tip = QPointF(a.x() + dx * _MARK, a.y() + dy * _MARK)
        # base perpendicular to the direction
        px, py = -dy, dx
        half = _MARK * 0.7
        b1 = QPointF(a.x() + px * half, a.y() + py * half)
        b2 = QPointF(a.x() - px * half, a.y() - py * half)
        return QPolygonF([tip, b1, b2])

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Base marker (amber, filled when connected).
        pen = QPen(_PORT_COLOR)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QBrush(_PORT_COLOR) if self._connected else Qt.NoBrush)
        painter.drawPolygon(self._triangle())
        # Data-flow flash: a bright red triangle ON TOP of the marker, plus a
        # red glow halo, so it clearly reads as "data flowing through the port".
        if self._flash > 0:
            a = max(0.0, min(1.0, self._flash))
            halo = QColor(255, 80, 80)
            halo.setAlphaF(a * 0.5)
            painter.setPen(QPen(halo, 8))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(self._triangle())
            flash = QColor(255, 70, 70)
            flash.setAlphaF(a)
            painter.setPen(QPen(flash, 2))
            painter.setBrush(QBrush(flash))
            painter.drawPolygon(self._triangle())
        if self.isSelected():
            sel = QPen(QColor(120, 220, 255))
            sel.setWidth(2)
            painter.setPen(sel)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(self._triangle())
        # short label (x16/x1) always just OUTSIDE the chip edge
        painter.setPen(QPen(_LABEL_COLOR))
        ox, oy = self._out
        lx = self._anchor.x() + ox * (_MARK + 6)
        ly = self._anchor.y() + oy * (_MARK + 6)
        label = "x16" if self.port.width == 16 else "x1"
        painter.drawText(QRectF(lx - 14, ly - 8, 28, 16),
                         Qt.AlignCenter, label)
