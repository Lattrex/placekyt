"""ConnectionItem — draws a connection's route or fly line (§3.2).

A routed connection is a solid poly-line through its waypoint cell centres; an
unrouted connection (or one being drawn) is a dashed preview line. Drawn beneath
cells so cell content stays legible.

The first and last waypoints of a routed connection ARE the source-output and
target-input block I/O cells, so the line already reaches their centres — it
runs INTO the I/O cell, not merely to its edge (route-into-cell, #266). That
in-cell portion is also a hittable handle: clicking the route where it overlaps
an I/O cell selects THIS connection so it can be grabbed + deleted (#268).

Beyond the normal selected state a connection can be RELATED-highlighted: when
the user selects a block I/O cell, every connection whose route runs through (or
terminates at) that cell is highlighted along its whole physical bus path so the
A.out → B.in link is obvious at a glance (#266).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem

from .cell_item import CELL_PX

_ROUTE_COLOR = QColor(90, 200, 120, 170)   # routed: green-ish, semi-transparent
_PREVIEW_COLOR = QColor(230, 210, 90)      # in-progress draw: amber
_FLY_COLOR = QColor(150, 150, 150)         # unrouted fly line: gray dashed
_SELECT_COLOR = QColor(120, 220, 255)      # selected route highlight
_RELATED_COLOR = QColor(255, 200, 80)      # bus-highlight from an I/O-cell select
_HIT_WIDTH = 14                            # clickable hit area around the line


def _cell_center(ox: float, oy: float, cx: int, cy: int) -> QPointF:
    return QPointF(ox + cx * CELL_PX + CELL_PX / 2,
                   oy + cy * CELL_PX + CELL_PX / 2)


def _cell_rect(ox: float, oy: float, cx: int, cy: int) -> QRectF:
    return QRectF(ox + cx * CELL_PX, oy + cy * CELL_PX, CELL_PX, CELL_PX)


class ConnectionItem(QGraphicsItem):
    """A poly-line through a route's waypoints (scene coords)."""

    def __init__(self, points: list[tuple[int, int]], chip_origin: tuple[float, float],
                 *, preview: bool = False, name: str | None = None,
                 end_point: QPointF | None = None, fly: bool = False,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        ox, oy = chip_origin
        self._pts = [_cell_center(ox, oy, x, y) for x, y in points]
        if end_point is not None:
            # Extend the line to a final scene point (e.g. a chip port marker)
            # so the route visually reaches the port, not the last cell centre.
            self._pts.append(end_point)
        self._preview = preview
        # A FLY line is a logical (unrouted) net — dashed gray, distinct from an
        # in-progress preview (amber) and a routed line (green). P2.3.
        self._fly = fly
        self.connection_name = name  # the model Connection this draws (or None)
        # Scene rects of this route's ENDPOINT I/O cells (source-output + target-
        # input block cells). Used to (a) hit-test the in-cell route segment as a
        # grab handle for this connection (#268), and (b) confirm the line runs
        # INTO the cell. Set from the model grid coords, independent of any
        # end_point extension.
        self._endpoint_rects: list[QRectF] = []
        if points:
            self._endpoint_rects.append(_cell_rect(ox, oy, *points[0]))
            if len(points) > 1:
                self._endpoint_rects.append(_cell_rect(ox, oy, *points[-1]))
        # RELATED highlight: lit when the user selects a block I/O cell that this
        # connection's bus passes through (#266). Distinct from isSelected().
        self._related = False
        self.setZValue(5)  # above cell fills, below selection
        # A drawn (non-preview) route is selectable/clickable so it can be
        # deleted (§3.2 "Click connection line: selects that connection").
        if not preview and name is not None:
            self.setFlag(QGraphicsItem.ItemIsSelectable, True)
            self.setAcceptHoverEvents(True)

    @classmethod
    def fly_line(cls, start: QPointF, end: QPointF, *, name: str | None = None,
                 parent: QGraphicsItem | None = None) -> "ConnectionItem":
        """A logical-net fly line between two raw SCENE points (P2.3): the dashed
        gray line shown for an unrouted connection until the auto-router (Phase 3)
        replaces it with a real route."""
        item = cls([], (0.0, 0.0), name=name, fly=True, parent=parent)
        item._pts = [start, end]
        if name is not None:
            item.setFlag(QGraphicsItem.ItemIsSelectable, True)
            item.setAcceptHoverEvents(True)
        return item

    @property
    def is_fly(self) -> bool:
        """True for a logical-net fly line (unrouted), False for a routed line."""
        return self._fly

    @property
    def is_related(self) -> bool:
        return self._related

    def set_related(self, on: bool) -> None:
        """Set/clear the bus-highlight driven by an I/O-cell selection (#266)."""
        on = bool(on)
        if on != self._related:
            self._related = on
            self.update()

    def covers_io_cell(self, scene_point: QPointF) -> bool:
        """True if ``scene_point`` lies inside one of this route's ENDPOINT I/O
        cells (and the route is drawn). Lets the canvas treat a click on the
        in-cell route segment as a grab handle for this connection (#268)."""
        if self._fly or len(self._pts) < 2:
            return False
        return any(r.contains(scene_point) for r in self._endpoint_rects)

    def _path(self) -> QPainterPath:
        path = QPainterPath(self._pts[0]) if self._pts else QPainterPath()
        for p in self._pts[1:]:
            path.lineTo(p)
        return path

    def shape(self) -> QPainterPath:  # noqa: N802
        # Fat hit area so the thin line is easy to click (§3.2 route selection).
        # Includes the in-I/O-cell endpoint segment so clicking the route where it
        # enters a block I/O cell selects this connection (#268).
        if len(self._pts) < 2:
            return super().shape()
        stroker = QPainterPathStroker()
        stroker.setWidth(_HIT_WIDTH)
        return stroker.createStroke(self._path())

    def boundingRect(self) -> QRectF:  # noqa: N802
        if not self._pts:
            return QRectF()
        xs = [p.x() for p in self._pts]
        ys = [p.y() for p in self._pts]
        pad = _HIT_WIDTH / 2 + 2
        return QRectF(min(xs) - pad, min(ys) - pad,
                      max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        if len(self._pts) < 2:
            return
        path = self._path()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # A glow underlay marks selection (cyan) or a related bus-highlight
        # (amber). Selection wins when both apply. The glow is wider than the line
        # and runs the WHOLE path — including the in-I/O-cell endpoint segments —
        # so it is obvious where the connection goes (#266).
        if self.isSelected() or self._related:
            glow = QPen(_SELECT_COLOR if self.isSelected() else _RELATED_COLOR)
            glow.setWidth(8 if self._related and not self.isSelected() else 7)
            painter.setPen(glow)
            painter.drawPath(path)
            if self._related and not self.isSelected():
                # Emphasise the endpoint I/O cells so it's clear the route runs
                # INTO the source-output and target-input cells (not just up to
                # the edge): a faint amber fill over each endpoint cell.
                fill = QColor(_RELATED_COLOR)
                fill.setAlphaF(0.18)
                painter.setPen(Qt.NoPen)
                painter.setBrush(fill)
                for r in self._endpoint_rects:
                    painter.drawRect(r)
        if self._fly:
            pen = QPen(_FLY_COLOR)
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
        else:
            pen = QPen(_PREVIEW_COLOR if self._preview else _ROUTE_COLOR)
            pen.setWidth(3)
            if self._preview:
                pen.setStyle(Qt.DashLine)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(pen)
        painter.drawPath(path)
