"""SramInspectorView — a live 2D map of an SRAM panel's contents.

The panel stores up to 65 536 words; showing that as a 1-D list is useless, so
this lays the array out as a **256×256 grid** (one cell per word). It reads like
a real SRAM die — dark = untouched, written words lit; when an address is
written or read its cell **blinks** (write vs read in different colours) and
fades. Zooming in reveals the **hex value** per address with the same highlight.

Built as a ``QGraphicsView`` (like the chip canvas) so it gets scrollbars and
cursor-anchored zoom for free, and matches the chip canvas wheel semantics:
plain scroll = zoom, Ctrl+scroll = horizontal pan, Shift+scroll = vertical pan.
A single :class:`_GridItem` paints the visible cells. Fed a ``{addr: value}``
snapshot + recent ``(addr, kind)`` activity; no model reference.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView

GRID = 256                      # 256 × 256 = 65 536 words
_CELL = 12.0                    # scene units per word cell (fixed; view scales)
_BG = QColor(18, 18, 22)
_STORED = QColor(70, 110, 160)  # a word holding a non-zero value: blue-grey
_STORED_ZERO = QColor(44, 48, 56)  # touched but currently zero
_GRIDLINE = QColor(40, 44, 52)
_HEX_FG = QColor(225, 228, 235)
_WRITE_FLASH = QColor(120, 230, 120)   # write activity → green
_READ_FLASH = QColor(255, 170, 60)     # read activity → amber (distinct)

_ZOOM_STEP = 1.15
_MIN_SCALE = 0.05               # fit-the-whole-grid floor (never blanks out)
_MAX_SCALE = 8.0
_HEX_SCALE = 3.2                # view scale at/above which hex values are drawn


class _GridItem(QGraphicsItem):
    """Paints the 256×256 word grid (culled to the exposed rect)."""

    def __init__(self, view: "SramInspectorView"):
        super().__init__()
        self._view = view

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, GRID * _CELL, GRID * _CELL)

    def paint(self, p: QPainter, option, widget=None) -> None:  # noqa: N802
        view = self._view
        scale = view.transform().m11() or 1.0
        draw_hex = scale >= _HEX_SCALE
        draw_grid = scale * _CELL >= 8
        # Cull to the exposed rect for the zoomed-in case.
        exposed = option.exposedRect
        gx0 = max(0, int(exposed.left() / _CELL))
        gy0 = max(0, int(exposed.top() / _CELL))
        gx1 = min(GRID, int(exposed.right() / _CELL) + 1)
        gy1 = min(GRID, int(exposed.bottom() / _CELL) + 1)
        if draw_hex:
            f = QFont("monospace")
            f.setPointSizeF(max(2.0, _CELL * 0.26))
            p.setFont(f)
        mem = view._mem
        flash = view._flash
        for gy in range(gy0, gy1):
            base_addr = gy * GRID
            for gx in range(gx0, gx1):
                addr = base_addr + gx
                rect = QRectF(gx * _CELL, gy * _CELL, _CELL, _CELL)
                val = mem.get(addr)
                if val is None:
                    base = _BG
                elif val == 0:
                    base = _STORED_ZERO
                else:
                    base = _STORED
                p.fillRect(rect, base)
                fl = flash.get(addr)
                if fl is not None:
                    intensity, kind = fl
                    c = QColor(_WRITE_FLASH if kind == "w" else _READ_FLASH)
                    c.setAlphaF(max(0.0, min(1.0, intensity)))
                    p.fillRect(rect, c)
                if draw_grid:
                    p.setPen(QPen(_GRIDLINE, 0))
                    p.drawRect(rect)
                if draw_hex and val is not None:
                    p.setPen(QPen(_HEX_FG))
                    p.drawText(rect, Qt.AlignCenter, f"{val:04X}")


class SramInspectorView(QGraphicsView):
    """Zoomable 256×256 SRAM map with write/read activity blinks."""

    hover_addr = Signal(int, int)   # (addr, value) under the cursor

    def __init__(self, size_words: int = GRID * GRID, parent=None):
        super().__init__(parent)
        self._size_words = size_words
        self._mem: dict[int, int] = {}
        self._flash: dict[int, tuple[float, str]] = {}
        self._scale = 1.0

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(_BG)
        self._scene.setSceneRect(0, 0, GRID * _CELL, GRID * _CELL)
        self._grid = _GridItem(self)
        self._scene.addItem(self._grid)
        self.setScene(self._scene)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 320)

    # -- data feed ------------------------------------------------------------

    def set_contents(self, mem: dict) -> None:
        self._mem = mem
        self._grid.update()

    def add_activity(self, activity) -> None:
        for addr, kind in activity:
            self._flash[addr] = (1.0, kind)
        if activity:
            self._grid.update()

    def decay(self, amount: float = 0.12) -> bool:
        """Fade all flashes a step. Returns True while any remain lit."""
        if not self._flash:
            return False
        nxt = {}
        for addr, (intensity, kind) in self._flash.items():
            v = intensity - amount
            if v > 0:
                nxt[addr] = (v, kind)
        self._flash = nxt
        self._grid.update()
        return bool(self._flash)

    def clear(self) -> None:
        self._mem = {}
        self._flash = {}
        self._grid.update()

    # -- view control (matches ChipCanvas) ------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802
        mods = event.modifiers()
        delta = event.angleDelta().y()
        # Ctrl+scroll pans horizontally, Shift+scroll pans vertically — same as
        # the chip canvas.
        if mods & Qt.ControlModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        if mods & Qt.ShiftModifier:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        # Plain scroll zooms around the cursor (clamped so it never blanks out).
        factor = _ZOOM_STEP if delta > 0 else 1 / _ZOOM_STEP
        new_scale = self._scale * factor
        if not (_MIN_SCALE <= new_scale <= _MAX_SCALE):
            return
        self._scale = new_scale
        self.scale(factor, factor)
        event.accept()

    def fit(self) -> None:
        """Show the whole grid (the minimum zoom — never blank)."""
        self.resetTransform()
        self._scale = 1.0
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._scale = self.transform().m11() or 1.0

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        # Default to a fit-to-window view so the full array is visible.
        self.fit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pt = self.mapToScene(event.position().toPoint())
        gx = int(pt.x() / _CELL)
        gy = int(pt.y() / _CELL)
        if 0 <= gx < GRID and 0 <= gy < GRID:
            addr = gy * GRID + gx
            self.hover_addr.emit(addr, self._mem.get(addr, 0))
        super().mouseMoveEvent(event)
