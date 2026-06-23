"""TimelineScrubber — the shared time cursor made draggable (DEBUG §3.4).

A thin horizontal time axis spanning the whole run. Dragging it moves the one
global time cursor; every debug view (Cell Inspector PC/registers, Waveform
playhead, Transaction-Log highlight) snaps to that time. Markers show port I/O
events (and, later, breakpoint hits). This is the post-run "replay" control.

Pure Qt/QPainter (same approach as the waveform/canvas). Holds no model — just
the run span, the cursor, and the marker times. Live drag is throttled so a big
trace doesn't stack redundant view rebuilds.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

_BG = QColor(32, 34, 38)
_AXIS = QColor(90, 95, 102)
_TICK = QColor(150, 155, 162)
_IN_MARK = QColor(90, 200, 255)       # input port I/O marker (cyan)
_OUT_MARK = QColor(120, 220, 140)     # output port I/O marker (green)
_BP_MARK = QColor(255, 90, 90)        # breakpoint-hit marker (red)
_PLAYHEAD = QColor(255, 215, 60)      # the cursor (yellow)
_PAD = 8                              # left/right padding (px)


class TimelineScrubber(QWidget):
    """Draggable run timeline that drives the shared cursor."""

    # The user scrubbed the cursor to this time (ns).
    cursor_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setMouseTracking(True)
        self._t0 = 0.0          # run start (ns)
        self._t1 = 1.0          # run end (ns)
        self._cursor: float | None = None
        # Markers: list of (time_ns, kind) where kind ∈ {"in","out","bp"}.
        self._markers: list[tuple[float, str]] = []
        self._dragging = False
        # Throttle: only re-emit when the cursor moves at least this many px,
        # so a drag across a big trace doesn't stack redundant view rebuilds.
        self._last_emit_x: float | None = None

    # -- data -----------------------------------------------------------------

    def set_span(self, t0: float, t1: float) -> None:
        """Set the run's time span (ns). A zero/inverted span is normalised."""
        self._t0 = float(t0)
        self._t1 = float(t1) if t1 > t0 else float(t0) + 1.0
        self.update()

    def set_markers(self, markers: list[tuple[float, str]]) -> None:
        """Port-I/O / breakpoint markers: ``[(time_ns, kind), …]``."""
        self._markers = list(markers)
        self.update()

    def set_cursor(self, ns: float | None) -> None:
        """Move the playhead WITHOUT emitting (the inbound/shared direction)."""
        self._cursor = None if ns is None else float(ns)
        self.update()

    def clear(self) -> None:
        self._t0, self._t1 = 0.0, 1.0
        self._cursor = None
        self._markers = []
        self.update()

    def set_from_trace_model(self, model) -> None:
        """Populate span + markers from a TraceModel (port_injection/capture)."""
        if model is None or not model.transactions:
            self.clear()
            return
        txns = model.transactions
        self.set_span(txns[0].time_ns, txns[-1].time_ns)
        marks: list[tuple[float, str]] = []
        for s in model.port_streams().items():
            (_chip, port), samples = s
            kind = "in" if str(port).endswith("_in") else "out"
            for t, _v in samples:
                marks.append((t, kind))
        self.set_markers(marks)
        self.set_cursor(model.cursor_ns)

    # -- mapping --------------------------------------------------------------

    def _t_to_x(self, t: float) -> float:
        span = self._t1 - self._t0 or 1.0
        w = max(1, self.width() - 2 * _PAD)
        return _PAD + (t - self._t0) / span * w

    def _x_to_t(self, x: float) -> float:
        span = self._t1 - self._t0 or 1.0
        w = max(1, self.width() - 2 * _PAD)
        t = self._t0 + (x - _PAD) / w * span
        return min(self._t1, max(self._t0, t))  # clamp to the run span

    # -- painting -------------------------------------------------------------

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), _BG)
        mid = self.height() * 0.55

        # Axis line.
        p.setPen(QPen(_AXIS, 1))
        p.drawLine(int(_PAD), int(mid), int(self.width() - _PAD), int(mid))

        # Ruler ticks: dense minor ticks + labelled major ticks, the same
        # 'nice' strategy as the waveform header — more labels fill in as the
        # span shrinks (zoom). Minor ticks are short; majors carry the number.
        from PySide6.QtCore import QRectF

        from ui.widgets.ruler_ticks import nice_ticks

        w = max(1, self.width() - 2 * _PAD)
        minors = nice_ticks(self._t0, self._t1, w, min_px_per_tick=14.0)
        majors = nice_ticks(self._t0, self._t1, w, min_px_per_tick=85.0)
        major_set = set(majors)
        p.setPen(QPen(_TICK))
        for t in minors:
            x = self._t_to_x(t)
            hh = 4 if t in major_set else 2
            p.drawLine(int(x), int(mid - hh), int(x), int(mid + hh))
        for t in majors:
            x = self._t_to_x(t)
            label = f"{t:.0f}" if abs(t) >= 100 else f"{t:.3g}"
            p.drawText(QRectF(x - 50, 0, 100, mid - 4),
                       Qt.AlignHCenter | Qt.AlignVCenter, f"{label} ns")

        # I/O + breakpoint markers as short ticks below the axis.
        for t, kind in self._markers:
            x = self._t_to_x(t)
            color = (_IN_MARK if kind == "in" else _OUT_MARK if kind == "out"
                     else _BP_MARK)
            p.setPen(QPen(color, 2))
            p.drawLine(int(x), int(mid + 2), int(x), int(self.height() - 3))

        # Playhead.
        if self._cursor is not None:
            x = self._t_to_x(self._cursor)
            p.setPen(QPen(_PLAYHEAD, 1.5))
            p.drawLine(int(x), 0, int(x), self.height())
            # a small grab handle triangle at the top
            from PySide6.QtGui import QPolygonF
            from PySide6.QtCore import QPointF
            p.setBrush(_PLAYHEAD)
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([
                QPointF(x - 4, 0), QPointF(x + 4, 0), QPointF(x, 6)]))
        p.end()

    # -- interaction ----------------------------------------------------------

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._dragging = True
            self._scrub(ev.position().x(), force=True)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if self._dragging:
            self._scrub(ev.position().x())

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._scrub(ev.position().x(), force=True)  # land exactly
            self._last_emit_x = None

    def _scrub(self, x: float, *, force: bool = False) -> None:
        """Move the playhead to pixel ``x`` and emit the new cursor time.

        Throttled: during a drag we only emit when the cursor moved ≥1px, so a
        sweep across a large trace doesn't stack a view-rebuild per mouse event.
        ``force`` (press/release) always emits so the final position lands."""
        x = min(self.width() - _PAD, max(_PAD, x))
        if not force and self._last_emit_x is not None \
                and abs(x - self._last_emit_x) < 1.0:
            return
        self._last_emit_x = x
        t = self._x_to_t(x)
        self._cursor = t
        self.update()
        self.cursor_requested.emit(t)
