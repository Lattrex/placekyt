"""ChipOutlineItem — the labelled boundary rectangle around a chip's grid.

Drawn beneath the cells (low Z) so cells paint on top (the architecture notes §3.2).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from .cell_item import CELL_PX

_OUTLINE_PEN = QColor(150, 155, 160)
_LABEL_COLOR = QColor(200, 205, 210)
_LABEL_H = 22  # label band height above the grid (scene px)


class ChipOutlineItem(QGraphicsItem):
    """Boundary + label for one chip; positioned at the chip's scene origin."""

    def __init__(self, label: str, width_cells: int, height_cells: int,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self.label = label
        self.width_cells = width_cells
        self.height_cells = height_cells
        self.setZValue(-10)  # behind cells

    def boundingRect(self) -> QRectF:  # noqa: N802
        w = self.width_cells * CELL_PX
        h = self.height_cells * CELL_PX
        return QRectF(-2, -_LABEL_H, w + 4, h + _LABEL_H + 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        w = self.width_cells * CELL_PX
        h = self.height_cells * CELL_PX
        pen = QPen(_OUTLINE_PEN)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(0, 0, w, h))
        # label band
        painter.setPen(QPen(_LABEL_COLOR))
        painter.drawText(QRectF(0, -_LABEL_H, w, _LABEL_H),
                         Qt.AlignVCenter | Qt.AlignLeft, f"  {self.label}")
