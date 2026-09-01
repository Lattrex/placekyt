# SPDX-License-Identifier: GPL-3.0-or-later
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
# FACE-CONFLICT guidance: a rail that IS routed but lands on an already-taken face of
# a face-locking (NEEDS_DISTINCT_INPUT_FACES) block. Magenta so it reads as "attention"
# and is unmistakable against BOTH the green routed line and the gray fly line.
_CONFLICT_COLOR = QColor(255, 70, 190)
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
                 conflict: bool = False, offset: QPointF | None = None,
                 parent: QGraphicsItem | None = None):
        super().__init__(parent)
        ox, oy = chip_origin
        self._pts = [_cell_center(ox, oy, x, y) for x, y in points]
        # COINCIDENT-ROUTE separation: two nets sharing a waypoint path paint exactly
        # on top of each other and read as ONE wire (the synthesised I/Q Q-rail hides
        # under the I-rail the user drew). A per-segment perpendicular nudge makes the
        # second net visibly a second net. Purely visual: the model route, the hit
        # shape's endpoint cells and the build are untouched, and the offset is small
        # enough that the line still reads as running through its cells.
        self._offset_px = 0.0
        if end_point is not None:
            # Extend the line to a final scene point (e.g. a chip port marker)
            # so the route visually reaches the port, not the last cell centre.
            self._pts.append(end_point)
        self._preview = preview
        # A routed rail whose ARRIVAL FACE collides with a sibling input of a
        # face-locking block: still guidance, NOT completion (see _CONFLICT_COLOR).
        self._conflict = bool(conflict)
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

    @classmethod
    def solid_link(cls, start: QPointF, end: QPointF, *, name: str | None = None,
                   parent: QGraphicsItem | None = None) -> "ConnectionItem":
        """A SOLID (routed) line between two raw SCENE points — used for an ABUTMENT
        connection, whose two I/O cells physically touch so there are no corridor
        waypoints, yet the net IS routed (the build synthesises the @1 handoff). Drawn
        green + selectable like any realised route, distinct from the dashed
        (unrouted) fly line."""
        item = cls([], (0.0, 0.0), name=name, fly=False, parent=parent)
        item._pts = [start, end]
        if name is not None:
            item.setFlag(QGraphicsItem.ItemIsSelectable, True)
            item.setAcceptHoverEvents(True)
        return item

    @classmethod
    def face_conflict_line(cls, start: QPointF, end: QPointF, *,
                           name: str | None = None,
                           parent: QGraphicsItem | None = None) -> "ConnectionItem":
        """A magenta dashed ATTENTION line for a rail that IS routed but arrives on a
        face of a face-locking block that a sibling input already occupies.

        Drawing nothing (the routed-line default) makes the second rail of a complex
        pair look FINISHED the moment it shares the first rail's corridor — the two
        identical routes paint as one green line and the only feedback is a DRC error
        naming a face the user has no way to see. This keeps guidance ON the net until
        its face is actually legal: a third style, distinct from the green routed line
        and the gray unrouted fly line, so "routed" and "legal" stay separate states."""
        item = cls([], (0.0, 0.0), name=name, fly=False, conflict=True, parent=parent)
        item._pts = [start, end]
        if name is not None:
            item.setFlag(QGraphicsItem.ItemIsSelectable, True)
            item.setAcceptHoverEvents(True)
        return item

    def set_parallel_offset(self, px: float) -> None:
        """Nudge this line ``px`` perpendicular to each of its segments, so a route
        that COINCIDES with another net's route is visibly a separate wire.

        Visual only — ``covers_io_cell`` and the model route are unaffected — but the
        hit ``shape()`` follows the drawn line so clicking either of two overlapping
        nets still selects the right one."""
        px = float(px)
        if px != self._offset_px:
            self._offset_px = px
            self.prepareGeometryChange()
            self.update()

    @property
    def parallel_offset(self) -> float:
        """The perpendicular nudge applied for coincident-route separation."""
        return self._offset_px

    @property
    def is_conflict(self) -> bool:
        """True for the face-conflict ATTENTION line (routed but illegally faced)."""
        return self._conflict

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

    def _drawn_pts(self) -> list[QPointF]:
        """The points actually painted — ``self._pts`` shifted perpendicular by
        ``_offset_px`` when a coincident-route separation is set.

        Each segment is offset along its own normal and consecutive offset segments
        are joined at the shifted vertex, so a poly-line keeps its corners instead of
        breaking apart at every turn."""
        import math

        pts = self._pts
        if not self._offset_px or len(pts) < 2:
            return pts
        d = self._offset_px
        shifted: list[QPointF] = []
        for i, p in enumerate(pts):
            # Normal of the segment(s) meeting at p: average the incoming and
            # outgoing segment normals so a corner shifts diagonally.
            nx = ny = 0.0
            segs = []
            if i > 0:
                segs.append((p.x() - pts[i - 1].x(), p.y() - pts[i - 1].y()))
            if i < len(pts) - 1:
                segs.append((pts[i + 1].x() - p.x(), pts[i + 1].y() - p.y()))
            for dx, dy in segs:
                ln = math.hypot(dx, dy)
                if ln:
                    nx += -dy / ln
                    ny += dx / ln
            ln = math.hypot(nx, ny)
            if ln:
                shifted.append(QPointF(p.x() + d * nx / ln, p.y() + d * ny / ln))
            else:
                shifted.append(p)
        return shifted

    def _path(self) -> QPainterPath:
        pts = self._drawn_pts()
        path = QPainterPath(pts[0]) if pts else QPainterPath()
        for p in pts[1:]:
            path.lineTo(p)
        return path

    def shape(self) -> QPainterPath:  # noqa: N802
        # Fat hit area so the thin line is easy to click (§3.2 route selection).
        # Includes the in-I/O-cell endpoint segment so clicking the route where it
        # enters a block I/O cell selects this connection (#268).
        if len(self._pts) < 2:
            return super().shape()
        stroker = QPainterPathStroker()
        # A route separated from a COINCIDENT sibling gets a narrower hit band, so the
        # two fat bands stop overlapping and a click near one line resolves to THAT
        # net rather than whichever happened to be added last. Never narrower than the
        # drawn line plus a small margin — the line stays easy to grab.
        width = _HIT_WIDTH
        if self._offset_px:
            width = max(6.0, min(_HIT_WIDTH, 2.0 * abs(self._offset_px)))
        stroker.setWidth(width)
        return stroker.createStroke(self._path())

    def boundingRect(self) -> QRectF:  # noqa: N802
        if not self._pts:
            return QRectF()
        xs = [p.x() for p in self._pts]
        ys = [p.y() for p in self._pts]
        pad = _HIT_WIDTH / 2 + 2 + abs(self._offset_px)
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
        if self._conflict:
            # Routed, but its arrival face collides with a sibling input of a
            # face-locking block — draw ATTENTION, not completion.
            pen = QPen(_CONFLICT_COLOR)
            pen.setWidth(3)
            pen.setStyle(Qt.DashDotLine)
        elif self._fly:
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
