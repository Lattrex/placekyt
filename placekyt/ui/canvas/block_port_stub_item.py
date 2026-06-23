"""BlockPortStubItem — a labelled stub marker for a block's external port.

The auto-P&R schematic front-end (P2.3) shows each placed block's external INPUT
and OUTPUT ports as small labelled stubs at the port cell's bus-facing edge, so a
user can see — and (Phase-3) wire — the named ports rather than guessing which
cell is the input/output. Derived from the block's :class:`~engine.portmap.PortMap`
(the canvas resolves the port cell + face via its ``port_cell_provider``).

A stub is a small triangle just outside the port cell's outer face, pointing in
the data-flow direction (OUTPUT points away from the block, INPUT points into it),
with the port name beside it. Input stubs are blue-ish, output stubs amber — the
same input/output cue the chip ``PortItem`` uses, scaled down for a block port.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem

from model.enums import Face

from .cell_item import CELL_PX
from .port_item import _FACE_OUT

_IN_COLOR = QColor(110, 180, 240)      # input stub: blue
_OUT_COLOR = QColor(230, 180, 80)      # output stub: amber (matches chip ports)
_LABEL_COLOR = QColor(220, 220, 220)
_MARK = CELL_PX * 0.22                  # stub marker size (smaller than chip port)


class BlockPortStubItem(QGraphicsItem):
    """A small labelled port stub at a placed block's external-port cell."""

    def __init__(self, port_name: str, direction: str, anchor: QPointF,
                 face: Face, *, block_name: str = "",
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self.port_name = port_name
        self.direction = direction          # "in" | "out"
        self.block_name = block_name
        self._anchor = anchor
        out_x, out_y = _FACE_OUT.get(face, (1, 0))
        # Data-flow direction: an OUTPUT points OUT of the block (data leaves), an
        # INPUT points IN (data enters) — same convention as the chip PortItem.
        is_in = direction == "in"
        self._dir = (-out_x, -out_y) if is_in else (out_x, out_y)
        self._out = (out_x, out_y)          # outward dir for label placement
        self.setZValue(7)                   # above cells/routes, below chip ports
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setToolTip(f"{block_name}.{port_name} ({direction})")

    @property
    def endpoint(self) -> tuple[str, str, str]:
        """(block_name, port_name, direction) — identifies this stub's port."""
        return (self.block_name, self.port_name, self.direction)

    def _color(self) -> QColor:
        return _IN_COLOR if self.direction == "in" else _OUT_COLOR

    def _triangle(self) -> QPolygonF:
        dx, dy = self._dir
        tip = QPointF(self._anchor.x() + dx * _MARK,
                      self._anchor.y() + dy * _MARK)
        # base perpendicular to the direction
        px, py = -dy, dx
        b1 = QPointF(self._anchor.x() + px * _MARK * 0.6,
                     self._anchor.y() + py * _MARK * 0.6)
        b2 = QPointF(self._anchor.x() - px * _MARK * 0.6,
                     self._anchor.y() - py * _MARK * 0.6)
        return QPolygonF([tip, b1, b2])

    def boundingRect(self) -> QRectF:  # noqa: N802
        # Generous box for the marker + the label beside it.
        pad = _MARK * 2 + 60
        return QRectF(self._anchor.x() - pad, self._anchor.y() - pad,
                      2 * pad, 2 * pad)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing, True)
        col = self._color()
        painter.setPen(QPen(col, 1.2))
        painter.setBrush(QBrush(col))
        painter.drawPolygon(self._triangle())
        # Label just outside the marker, in the outward face direction.
        ox, oy = self._out
        lx = self._anchor.x() + ox * (_MARK + 3)
        ly = self._anchor.y() + oy * (_MARK + 3)
        painter.setPen(QPen(_LABEL_COLOR))
        f = painter.font()
        f.setPointSizeF(max(5.0, CELL_PX * 0.16))
        painter.setFont(f)
        flags = Qt.AlignVCenter
        # Place the label to the right for E/N/S, to the left for W.
        if ox < 0:
            painter.drawText(QRectF(lx - 60, ly - 8, 56, 16),
                             Qt.AlignRight | Qt.AlignVCenter, self.port_name)
        else:
            painter.drawText(QRectF(lx, ly - 8, 60, 16),
                             flags | Qt.AlignLeft, self.port_name)
