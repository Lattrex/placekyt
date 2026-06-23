"""InterChipWireItem — a board-level chip-to-chip wire (§3.2).

Drawn as a thick dark line between two chips' port anchors (the from-port output
marker and the to-port input marker). Unlike a routed connection it has no cell
waypoints — the wire is defined by the board, not by on-chip cells. Selectable so
it can be deleted; carries the model :class:`InterChipConnection` it represents.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem

_WIRE_COLOR = QColor(180, 180, 190)     # board wire: light gray, thick
_SELECT_COLOR = QColor(120, 220, 255)   # selected highlight
_HIT_WIDTH = 16                         # clickable hit area around the wire


class InterChipWireItem(QGraphicsItem):
    """A thick line between two port anchors (scene coords)."""

    def __init__(self, start: QPointF, end: QPointF, *, ic=None,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self._a = QPointF(start)
        self._b = QPointF(end)
        # The model InterChipConnection this draws (for selection → delete).
        self.inter_chip = ic
        self.setZValue(4)  # above cells, below intra-chip routes/selection
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

    def _path(self) -> QPainterPath:
        path = QPainterPath(self._a)
        # A gentle horizontal S-bend reads as a board wire rather than a cell
        # route: leave each port horizontally, meet in the middle.
        mid_x = (self._a.x() + self._b.x()) / 2
        path.cubicTo(QPointF(mid_x, self._a.y()),
                     QPointF(mid_x, self._b.y()), self._b)
        return path

    def shape(self) -> QPainterPath:  # noqa: N802
        stroker = QPainterPathStroker()
        stroker.setWidth(_HIT_WIDTH)
        return stroker.createStroke(self._path())

    def boundingRect(self) -> QRectF:  # noqa: N802
        pad = _HIT_WIDTH / 2 + 2
        x0, x1 = sorted((self._a.x(), self._b.x()))
        y0, y1 = sorted((self._a.y(), self._b.y()))
        return QRectF(x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = self._path()
        if self.isSelected():
            glow = QPen(_SELECT_COLOR)
            glow.setWidth(9)
            painter.setPen(glow)
            painter.drawPath(path)
        pen = QPen(_WIRE_COLOR)
        pen.setWidth(4)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawPath(path)
