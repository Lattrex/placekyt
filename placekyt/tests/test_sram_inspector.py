"""SRAM inspector view + panel activity/flash tests."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class TestDeviceActivity:
    def test_write_records_activity(self):
        from engine.sram_panel import (
            REG_ADDR_BASE,
            REG_PAYLOAD,
            REG_WRITE_TRIGGER,
            SramPanelDevice,
        )
        dev = SramPanelDevice()
        dev.on_write(REG_PAYLOAD, 0x1234)
        dev.on_write(REG_ADDR_BASE, 0x07)
        dev.on_jump(REG_WRITE_TRIGGER)
        acts = dev.take_activity()
        assert acts == [(0x07, "w")]
        # draining clears it
        assert dev.take_activity() == []

    def test_read_records_activity(self):
        from engine.sram_panel import (
            REG_ADDR_BASE,
            REG_READ_TRIGGER,
            REG_READ_WR_DESC,
            SramPanelDevice,
        )
        dev = SramPanelDevice()
        dev.mem[5] = 0xABCD
        dev.on_write(REG_READ_WR_DESC, (0x6 << 12) | (10 << 5) | 9)  # valid desc
        dev.on_write(REG_ADDR_BASE, 5)
        dev.on_jump(REG_READ_TRIGGER)
        assert dev.take_activity() == [(5, "r")]


class TestInspectorView:
    def test_renders_and_activity(self, qapp):
        from ui.widgets.sram_inspector import SramInspectorView
        v = SramInspectorView()
        v.resize(300, 300)
        v.set_contents({0: 0xAAAA, 65535: 0xBBBB, 100: 0x0})
        v.add_activity([(0, "w"), (100, "r")])
        # decay returns True while flashes remain
        assert v.decay(0.1) is True
        # render the view to an image (drives the grid item paint) without crash
        img = QImage(300, 300, QImage.Format_ARGB32)
        from PySide6.QtGui import QPainter
        p = QPainter(img)
        v.render(p)
        p.end()

    def test_zoom_clamps(self, qapp):
        from ui.widgets.sram_inspector import (
            _MAX_SCALE,
            _MIN_SCALE,
            SramInspectorView,
        )
        v = SramInspectorView()
        v.resize(300, 300)
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        def wheel(dy):
            ev = QWheelEvent(QPointF(150, 150), QPointF(150, 150),
                             QPoint(0, 0), QPoint(0, dy), Qt.NoButton,
                             Qt.NoModifier, Qt.NoScrollPhase, False)
            v.wheelEvent(ev)
        for _ in range(60):
            wheel(-120)                     # zoom out hard
        assert v._scale >= _MIN_SCALE       # never collapses to nothing
        for _ in range(60):
            wheel(120)                      # zoom in hard
        assert v._scale <= _MAX_SCALE

    def test_ctrl_shift_scroll_pan(self, qapp):
        from ui.widgets.sram_inspector import SramInspectorView
        v = SramInspectorView()
        v.resize(200, 200)
        v.show()
        v.scale(4.0, 4.0)                   # zoom in so there's room to pan
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        hbar = v.horizontalScrollBar()
        before = hbar.value()
        ev = QWheelEvent(QPointF(100, 100), QPointF(100, 100),
                         QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                         Qt.ControlModifier, Qt.NoScrollPhase, False)
        v.wheelEvent(ev)
        assert hbar.value() != before      # Ctrl+scroll panned horizontally

    def test_decay_returns_false_when_clear(self, qapp):
        from ui.widgets.sram_inspector import SramInspectorView
        v = SramInspectorView()
        assert v.decay() is False           # nothing lit


class TestPanelItemFlash:
    def test_flash_and_decay(self, qapp):
        from model.panel import SramPanel
        from ui.canvas.panel_item import PanelItem
        item = PanelItem(SramPanel(id=0))
        item.flash([(0, "w"), (1, "r")])
        assert item._flash_w == 1.0 and item._flash_r == 1.0
        # decays toward zero, stays lit until both reach 0
        for _ in range(20):
            item.decay_flash()
        assert item.decay_flash() is False
