"""FootprintItem — a translucent preview of where cells will land (§3.2).

Shows light-blue cell outlines while dragging a block (move) or dropping one
from the library; turns amber on the cells that would overlap an existing
placement. Lives only on the scene during the drag — never in the model.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from .cell_item import CELL_PX

_OK_PEN = QColor(120, 200, 255)        # light blue (valid drop)
_OK_FILL = QColor(120, 200, 255, 60)
_BAD_PEN = QColor(235, 170, 60)        # amber (would overlap, §3.2)
_BAD_FILL = QColor(235, 170, 60, 70)


class FootprintItem(QGraphicsItem):
    """Outlines for a set of grid cells at a chip origin."""

    def __init__(self, cells: list[tuple[int, int]], chip_origin: tuple[float, float],
                 *, bad_cells: set[tuple[int, int]] | None = None,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self._cells = list(cells)
        self._origin = chip_origin
        self._bad = bad_cells or set()
        self.setZValue(50)  # above everything during a drag

    def boundingRect(self) -> QRectF:  # noqa: N802
        if not self._cells:
            return QRectF()
        ox, oy = self._origin
        xs = [ox + cx * CELL_PX for cx, _ in self._cells]
        ys = [oy + cy * CELL_PX for _, cy in self._cells]
        return QRectF(min(xs), min(ys),
                      max(xs) - min(xs) + CELL_PX, max(ys) - min(ys) + CELL_PX)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        ox, oy = self._origin
        painter.setRenderHint(QPainter.Antialiasing, False)
        for cx, cy in self._cells:
            bad = (cx, cy) in self._bad
            pen = QPen(_BAD_PEN if bad else _OK_PEN)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QBrush(_BAD_FILL if bad else _OK_FILL))
            painter.drawRect(QRectF(ox + cx * CELL_PX, oy + cy * CELL_PX,
                                    CELL_PX, CELL_PX))
