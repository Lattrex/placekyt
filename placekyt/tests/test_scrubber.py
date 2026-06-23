"""Timeline scrubber tests (DEBUG step 4): span, markers, drag → shared cursor.

Offscreen Qt. Exercises the TimelineScrubber widget directly and its MainWindow
wiring (auto span/markers on run, drag drives the shared cursor + all views).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.widgets.timeline_scrubber import TimelineScrubber  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and DEMO.exists()), reason="chip yaml / demo absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def controller(qapp, catalog):
    return AppController(catalog=catalog)


class TestTimelineScrubber:
    def test_span_normalises_inverted(self, qapp):
        sc = TimelineScrubber()
        sc.set_span(100.0, 50.0)  # inverted → normalised to a 1ns span
        assert sc._t1 > sc._t0

    def test_t_x_roundtrip_and_clamp(self, qapp):
        sc = TimelineScrubber()
        sc.resize(400, 30)
        sc.set_span(0.0, 1000.0)
        # mid pixel ≈ mid time
        x = sc._t_to_x(500.0)
        assert sc._x_to_t(x) == pytest.approx(500.0, abs=5.0)
        # out-of-range x clamps to the span
        assert sc._x_to_t(-9999) == pytest.approx(0.0)
        assert sc._x_to_t(99999) == pytest.approx(1000.0)

    def test_scrub_emits_cursor(self, qapp):
        sc = TimelineScrubber()
        sc.resize(400, 30)
        sc.set_span(0.0, 1000.0)
        got = []
        sc.cursor_requested.connect(got.append)
        sc._scrub(sc._t_to_x(250.0), force=True)
        assert got and got[0] == pytest.approx(250.0, abs=5.0)
        assert sc._cursor == pytest.approx(250.0, abs=5.0)

    def test_scrub_throttles_subpixel(self, qapp):
        sc = TimelineScrubber()
        sc.resize(400, 30)
        sc.set_span(0.0, 1000.0)
        got = []
        sc.cursor_requested.connect(got.append)
        sc._scrub(200.0, force=True)   # emits (force)
        sc._scrub(200.4)               # <1px move → throttled, no emit
        assert len(got) == 1

    def test_set_cursor_does_not_emit(self, qapp):
        sc = TimelineScrubber()
        got = []
        sc.cursor_requested.connect(got.append)
        sc.set_cursor(123.0)  # inbound (shared) direction — no signal
        assert got == []
        assert sc._cursor == 123.0

    def test_markers_and_render(self, qapp):
        sc = TimelineScrubber()
        sc.resize(400, 30)
        sc.set_span(0.0, 100.0)
        sc.set_markers([(10.0, "in"), (40.0, "out"), (60.0, "bp")])
        sc.set_cursor(50.0)
        img = QImage(400, 30, QImage.Format_ARGB32)
        sc.render(img, QPoint(0, 0))  # no crash, markers + playhead drawn
        assert len(sc._markers) == 3

    def test_clear(self, qapp):
        sc = TimelineScrubber()
        sc.set_span(0.0, 100.0)
        sc.set_markers([(1.0, "in")])
        sc.set_cursor(5.0)
        sc.clear()
        assert sc._markers == [] and sc._cursor is None


class TestScrubberWiring:
    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def _run(self, w):
        w.sim.set_speed_index(6)
        w.sim.start()
        for _ in range(60):
            w.sim._run_batch()
            if not w.sim.running:
                break
        QApplication.processEvents()

    def test_span_and_markers_after_run(self, controller):
        w = self._window(controller)
        self._run(w)
        sc = w.scrubber
        assert sc._t1 > sc._t0
        # I/O markers for the input + output port samples.
        kinds = {k for _t, k in sc._markers}
        assert "in" in kinds and "out" in kinds

    def test_drag_moves_shared_cursor_and_views(self, controller):
        w = self._window(controller)
        self._run(w)
        sc = w.scrubber
        sc.resize(800, 30)
        mid_x = sc.width() / 2
        sc._scrub(mid_x, force=True)
        t = sc._x_to_t(mid_x)
        assert w.sim.trace_model.cursor_ns == pytest.approx(t)
        # the waveform playhead followed too.
        assert w.waveform_panel.view._main_ns == pytest.approx(t)

    def test_reset_clears_scrubber(self, controller):
        w = self._window(controller)
        self._run(w)
        assert w.scrubber._markers
        w.sim.reset()
        QApplication.processEvents()
        assert w.scrubber._markers == []
        assert w.scrubber._cursor is None

    def test_log_click_moves_scrubber_playhead(self, controller):
        # The shared cursor flows to the scrubber too: a Transaction-Log row
        # click moves the scrubber playhead.
        w = self._window(controller)
        self._run(w)
        t = w.sim.trace_model.transactions[3].time_ns
        w._on_cursor_requested(t)
        assert w.scrubber._cursor == pytest.approx(t)
