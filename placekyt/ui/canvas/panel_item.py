"""PanelItem — an SRAM / peripheral panel on the canvas (the SRAM panel notes).

A panel renders like a small chip: a labelled rounded box with x16/x1 port
markers on its edges. It is movable + selectable. Unlike a chip it has no cell
grid — it's an off-array memory device — so it draws a compact fixed-size box
with a memory glyph and its size (words). Port anchors are exposed so the canvas
can draw panel↔chip wires to/from them.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem

from model.enums import Face, PortDirection

from .cell_item import CELL_PX

# Panel box size (scene px). Compact — a panel is not a cell grid.
_PANEL_W = CELL_PX * 4
_PANEL_H = CELL_PX * 3
_LABEL_H = 22

_BODY = QColor(52, 48, 64)              # deep violet-grey (distinct from chips)
_BORDER = QColor(170, 150, 210)
_LABEL_COLOR = QColor(220, 215, 230)
_SUB_COLOR = QColor(160, 156, 174)
_PORT_COLOR = QColor(210, 170, 240)     # panel port: light violet
_PORT_LABEL = QColor(235, 235, 240)
_SELECT_COLOR = QColor(120, 220, 255)
_WRITE_FLASH = QColor(120, 230, 120)    # write activity → green
_READ_FLASH = QColor(255, 170, 60)      # read activity → amber
_MARK = CELL_PX * 0.30

_FACE_OUT = {
    Face.NORTH: (0, -1),
    Face.SOUTH: (0, 1),
    Face.EAST: (1, 0),
    Face.WEST: (-1, 0),
}


class PanelItem(QGraphicsItem):
    """A movable SRAM/peripheral panel box with edge port markers."""

    def __init__(self, panel, *, connected_ports: set[str] | None = None,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self.panel = panel
        self.panel_id = panel.id
        self._connected = connected_ports or set()
        # Activity flash (high-level "panel is working" indicator): write = green,
        # read = amber, each decays independently.
        self._flash_w = 0.0
        self._flash_r = 0.0
        self.setZValue(-8)  # behind chip cells, like a chip outline
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setPos(panel.position_x, panel.position_y)
        self.setToolTip(
            f"{panel.label or f'Panel {panel.id}'} — "
            f"{panel.size_words} words")

    # -- activity flash -------------------------------------------------------

    def flash(self, activity) -> None:
        """Light the panel for this batch's activity (``[(addr, "w"|"r"), …]``):
        a write lights the write glow, a read the read glow."""
        for _addr, kind in activity:
            if kind == "w":
                self._flash_w = 1.0
            else:
                self._flash_r = 1.0
        self.update()

    def decay_flash(self, amount: float = 0.12) -> bool:
        """Fade both glows a step. Returns True while either is still lit."""
        lit = False
        if self._flash_w > 0:
            self._flash_w = max(0.0, self._flash_w - amount)
            lit = True
        if self._flash_r > 0:
            self._flash_r = max(0.0, self._flash_r - amount)
            lit = True
        if lit:
            self.update()
        return self._flash_w > 0 or self._flash_r > 0

    # -- geometry -------------------------------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802
        m = _MARK + 14
        return QRectF(-m, -_LABEL_H - 2, _PANEL_W + 2 * m,
                      _PANEL_H + _LABEL_H + 2 * m)

    def _port_edge_point(self, port) -> QPointF:
        """The midpoint on the panel edge for a port (before the outward nudge),
        in ITEM coords. Ports are spread along their edge by index."""
        face = port.face
        # Group ports by face and spread them evenly along that edge.
        same = [p for p in self.panel.ports if p.face == face]
        idx = same.index(port)
        n = len(same)
        frac = (idx + 1) / (n + 1)
        if face in (Face.WEST, Face.EAST):
            y = _PANEL_H * frac
            x = 0.0 if face == Face.WEST else _PANEL_W
            return QPointF(x, y)
        x = _PANEL_W * frac
        y = 0.0 if face == Face.NORTH else _PANEL_H
        return QPointF(x, y)

    def port_anchor_scene(self, port_name: str) -> QPointF | None:
        """Scene point of a port marker (its outward tip midpoint) — for wires."""
        port = self.panel.port(port_name)
        if port is None:
            return None
        edge = self._port_edge_point(port)
        ox, oy = _FACE_OUT.get(port.face, (0, 0))
        local = QPointF(edge.x() + ox * _MARK, edge.y() + oy * _MARK)
        return self.mapToScene(local)

    # -- painting -------------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = QRectF(0, 0, _PANEL_W, _PANEL_H)
        # Activity halo (drawn under the box): write = green, read = amber. Both
        # can be lit at once — the brighter wins the outer glow.
        for glow_c, level in ((_WRITE_FLASH, self._flash_w),
                              (_READ_FLASH, self._flash_r)):
            if level > 0:
                halo = QColor(glow_c)
                halo.setAlphaF(max(0.0, min(1.0, level)) * 0.6)
                painter.setPen(QPen(halo, 8))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(body, 6, 6)
        # Box.
        pen = QPen(_SELECT_COLOR if self.isSelected() else _BORDER)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QBrush(_BODY))
        painter.drawRoundedRect(body, 6, 6)
        # Label band + size.
        painter.setPen(QPen(_LABEL_COLOR))
        painter.drawText(QRectF(0, -_LABEL_H, _PANEL_W, _LABEL_H),
                         Qt.AlignVCenter | Qt.AlignLeft,
                         f"  {self.panel.label or f'Panel {self.panel.id}'}")
        painter.setPen(QPen(_SUB_COLOR))
        painter.drawText(body.adjusted(0, 0, 0, -4),
                         Qt.AlignCenter, self._size_label())
        # Memory glyph: a few horizontal "rows" suggesting a memory array.
        painter.setPen(QPen(_SUB_COLOR, 1))
        gx0, gx1 = _PANEL_W * 0.2, _PANEL_W * 0.8
        for k in range(3):
            gy = _PANEL_H * (0.30 + 0.14 * k)
            painter.drawLine(QPointF(gx0, gy), QPointF(gx1, gy))
        # Ports.
        for port in self.panel.ports:
            self._paint_port(painter, port)

    def _size_label(self) -> str:
        w = self.panel.size_words
        if w >= 1024 and w % 1024 == 0:
            return f"{w // 1024}k×16"
        return f"{w}×16"

    def _paint_port(self, painter: QPainter, port) -> None:
        edge = self._port_edge_point(port)
        ox, oy = _FACE_OUT.get(port.face, (0, 0))
        # OUTPUT points out of the panel; INPUT points in.
        is_input = port.direction == PortDirection.INPUT
        dx, dy = (-ox, -oy) if is_input else (ox, oy)
        tip = QPointF(edge.x() + dx * _MARK + ox * _MARK,
                      edge.y() + dy * _MARK + oy * _MARK)
        base_c = QPointF(edge.x() + ox * _MARK, edge.y() + oy * _MARK)
        px, py = -dy, dx
        half = _MARK * 0.7
        b1 = QPointF(base_c.x() + px * half, base_c.y() + py * half)
        b2 = QPointF(base_c.x() - px * half, base_c.y() - py * half)
        tri = QPolygonF([tip, b1, b2])
        pen = QPen(_PORT_COLOR)
        pen.setWidth(2)
        painter.setPen(pen)
        filled = port.name in self._connected
        painter.setBrush(QBrush(_PORT_COLOR) if filled else Qt.NoBrush)
        painter.drawPolygon(tri)
        # Label (x16/x1) just outside.
        painter.setPen(QPen(_PORT_LABEL))
        lx = edge.x() + ox * (_MARK + 8)
        ly = edge.y() + oy * (_MARK + 8)
        label = "x16" if port.is_x16 else "x1"
        painter.drawText(QRectF(lx - 14, ly - 8, 28, 16),
                         Qt.AlignCenter, label)
