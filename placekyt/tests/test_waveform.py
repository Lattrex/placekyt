"""Waveform viewer tests (DEBUG step 3): streams, radix, cursors, measurement.

Offscreen Qt. Exercises the WaveformView widget directly and the WaveformPanel
+ MainWindow wiring (auto-populate on run, shared cursor, latency/amplitude).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.widgets.waveform_view import (  # noqa: E402
    RADIX_ANALOG,
    RADIX_HEX,
    WaveformView,
    _fmt_value,
    _q15,
)

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


# --------------------------------------------------------------------------- #
# WaveformView unit behaviour
# --------------------------------------------------------------------------- #


class TestWaveformView:
    def test_value_formatting(self):
        assert _fmt_value(0x4000, RADIX_HEX) == "0x4000"
        assert _fmt_value(0x4000, "Dec") == "16384"
        assert _fmt_value(0xC000, "Dec") == "-16384"  # signed
        assert _q15(0x4000) == pytest.approx(0.5)
        assert _q15(0xC000) == pytest.approx(-0.5)

    def test_set_streams_one_row_each(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (10.0, 0x2000)],
                       (0, "x16_out"): [(5.0, 0x1000)]})
        assert v.stream_count() == 2
        assert v.stream_labels() == ["chip0.x16_in", "chip0.x16_out"]
        assert v.radix_of(0) == RADIX_ANALOG  # default

    def test_value_at_is_step_held(self, qapp):
        v = WaveformView()
        samples = [(0.0, 0x4000), (10.0, 0x2000), (20.0, 0x1000)]
        v.set_streams({(0, "x16_in"): samples})
        assert v._value_at(samples, -1.0) is None       # before first
        assert v._value_at(samples, 5.0) == 0x4000       # held
        assert v._value_at(samples, 10.0) == 0x2000      # at edge
        assert v._value_at(samples, 99.0) == 0x1000      # last held

    def test_radix_change_and_keep_on_refresh(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 1)]})
        v.set_radix(0, RADIX_HEX)
        assert v.radix_of(0) == RADIX_HEX
        # A refresh of the same stream keeps the user's radix choice.
        v.set_streams({(0, "x16_in"): [(0.0, 1), (1.0, 2)]})
        assert v.radix_of(0) == RADIX_HEX

    def test_amplitude(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (1.0, 0xC000)]})
        assert v.amplitude_of(0) == (-0x4000, 0x4000)  # signed min/max

    def test_measurement_dt(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 1), (100.0, 2)]})
        v.set_main_cursor(10.0)
        v._meas_ns = 60.0
        assert v.measurement_dt == pytest.approx(50.0)

    def test_latency_in_before_out(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(10.0, 1)],
                       (0, "x16_out"): [(40.0, 2)]})
        assert v.latency_ns() == pytest.approx(30.0)

    def test_renders_analog_and_bus(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (10.0, 0x2000)]})
        v.resize(600, 120)
        img = QImage(600, 120, QImage.Format_ARGB32)
        v.render(img, QPoint(0, 0))  # analog (no crash)
        v.set_radix(0, RADIX_HEX)
        v.render(img, QPoint(0, 0))  # bus trace (no crash)

    def test_empty_renders(self, qapp):
        v = WaveformView()
        v.resize(400, 100)
        img = QImage(400, 100, QImage.Format_ARGB32)
        v.render(img, QPoint(0, 0))  # "No streams" message, no crash
        assert v.stream_count() == 0

    def test_gutter_is_opaque(self, qapp):
        # Traces must not be seen through the left value gutter even when the
        # view is scrolled so a trace would extend left into it.
        from ui.widgets.waveform_view import _GUTTER_W, _TRACE
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (10.0, 0x2000),
                                       (20.0, 0x6000)]})
        v.resize(600, 120)
        v._t0, v._t1 = 12.0, 25.0  # scroll so early samples are left of view
        img = QImage(600, 120, QImage.Format_ARGB32)
        v.render(img, QPoint(0, 0))
        trace_rgb = (_TRACE.red(), _TRACE.green(), _TRACE.blue())
        for y in range(5, 115, 4):
            c = img.pixelColor(_GUTTER_W // 2, y)
            assert not all(abs(a - b) <= 30 for a, b in
                           zip((c.red(), c.green(), c.blue()), trace_rgb))

    def test_row_at_y(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "a_in"): [(0.0, 1)], (0, "b_out"): [(0.0, 2)]})
        from ui.widgets.waveform_view import _ROW_H, _ROW_GAP, _TOP_PAD
        assert v._stream_at_y(_TOP_PAD + 2) == 0
        assert v._stream_at_y(_TOP_PAD + _ROW_H + _ROW_GAP + 2) == 1
        assert v._stream_at_y(99999) == -1


# --------------------------------------------------------------------------- #
# Panel + MainWindow wiring
# --------------------------------------------------------------------------- #


class TestWaveformPanel:
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

    def test_autopopulates_on_run(self, controller):
        w = self._window(controller)
        self._run(w)
        wv = w.waveform_panel.view
        assert "chip0.x16_in" in wv.stream_labels()
        assert "chip0.x16_out" in wv.stream_labels()

    def test_latency_measured(self, controller):
        w = self._window(controller)
        self._run(w)
        assert w.waveform_panel.view.latency_ns() is not None

    def test_radix_via_view_signal_refreshes_readout(self, controller):
        # Radix is set by right-clicking the gutter (set_radix), not a toolbar.
        # The panel listens to radix_changed to refresh its readout.
        w = self._window(controller)
        self._run(w)
        wp = w.waveform_panel
        got = []
        wp.view.radix_changed.connect(lambda r, rx: got.append((r, rx)))
        wp.view.set_radix(0, RADIX_HEX)
        assert got == [(0, RADIX_HEX)]
        assert wp.view.radix_of(0) == RADIX_HEX

    def test_wave_cursor_drives_shared_cursor(self, controller):
        w = self._window(controller)
        self._run(w)
        got = []
        w.waveform_panel.cursor_requested.connect(lambda ns: got.append(ns))
        # Emit as if the user left-clicked the wave at a known time.
        t = list(w.sim.trace_model.port_streams().values())[0][0][0]
        w.waveform_panel.view.cursor_requested.emit(t)
        assert got and got[0] == t

    def test_shared_cursor_moves_wave_playhead(self, controller):
        w = self._window(controller)
        self._run(w)
        w.sim.set_cursor(123.0)
        assert w.waveform_panel.view._main_ns == 123.0

    def test_wave_click_highlights_log_row(self, controller):
        # Bidirectional cursor: a waveform click (→ _on_cursor_requested) selects
        # and scrolls the Transaction Log to the row at that time.
        w = self._window(controller)
        self._run(w)
        p = w.output_panel
        p._detail.setChecked(True)
        QApplication.processEvents()
        mid = w.sim.trace_model.transactions[
            len(w.sim.trace_model.transactions) // 2].time_ns
        w._on_cursor_requested(mid)
        sel = p._txn_table.selectionModel().selectedRows()
        assert sel
        # the selected row's time is at/<= the cursor (nearest preceding row).
        assert p._rows[sel[0].row()].time_ns <= mid

    def test_reset_clears_wave(self, controller):
        w = self._window(controller)
        self._run(w)
        assert w.waveform_panel.view.stream_count() > 0
        w.sim.reset()
        QApplication.processEvents()
        assert w.waveform_panel.view.stream_count() == 0


class TestWaveformInteractions:
    """Per-trace colour/height/amplitude, measurement readout, delete (GUI feedback)."""

    def _view(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (10.0, 0x2000)],
                       (0, "x16_out"): [(5.0, 0x1000), (15.0, 0x3000)]})
        return v

    def test_traces_get_rotating_colors(self, qapp):
        v = self._view(qapp)
        c0 = v._streams[0]["color"].name()
        c1 = v._streams[1]["color"].name()
        assert c0 != c1  # distinct (rotating palette)

    def test_per_trace_height_and_row_tops(self, qapp):
        v = self._view(qapp)
        v._streams[0]["height"] = 100
        tops = list(v._row_tops())
        assert tops[0][2] == 100               # row 0 height honoured
        assert tops[1][1] == tops[0][1] + 100 + 6  # row 1 sits below it

    def test_measurement_cursor_signal_and_dt(self, qapp):
        v = self._view(qapp)
        fired = []
        v.measurement_changed.connect(lambda: fired.append(1))
        v.set_main_cursor(2.0)
        # simulate a middle-click setting the measurement cursor
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        v.resize(400, 120)
        x = v._t_to_x(12.0)
        ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(x, 30),
                         Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier)
        v.mousePressEvent(ev)
        assert fired                              # readout refresh fired
        assert v.measurement_dt == pytest.approx(10.0, abs=1.0)

    def test_amplitude_scale_via_ctrl_wheel(self, qapp):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        v = self._view(qapp)
        v.resize(400, 120)
        before = v._streams[0]["amp_scale"]
        # Ctrl+wheel-up over row 0 magnifies its amplitude.
        y = list(v._row_tops())[0][1] + 10
        ev = QWheelEvent(QPointF(200, y), QPointF(200, y), QPoint(0, 0),
                         QPoint(0, 120), Qt.NoButton, Qt.ControlModifier,
                         Qt.NoScrollPhase, False)
        v.wheelEvent(ev)
        assert v._streams[0]["amp_scale"] > before

    def test_delete_trace(self, qapp):
        v = self._view(qapp)
        changed = []
        v.streams_changed.connect(lambda: changed.append(1))
        v.remove_stream(0)
        assert v.stream_count() == 1 and changed

    def test_resize_grip_detection(self, qapp):
        v = self._view(qapp)
        _i, top, h = list(v._row_tops())[0]
        assert v._resize_grip_at_y(top + h) == 0      # on the bottom edge
        assert v._resize_grip_at_y(top + h / 2) == -1  # in the middle


class TestWaveformAdvanced:
    """Overlay drag-drop, register traces, YAML signal-list save/load."""

    def _view(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(0.0, 0x4000), (10.0, 0x2000)],
                       (0, "x16_out"): [(5.0, 0x1000), (15.0, 0x3000)]})
        return v

    def test_port_settings_survive_refresh(self, qapp):
        v = self._view(qapp)
        v.set_radix(0, RADIX_HEX)
        v._streams[0]["height"] = 90
        # a port refresh updates samples but keeps the user's radix + height
        v.set_streams({(0, "x16_in"): [(0.0, 1), (1.0, 2), (2.0, 3)],
                       (0, "x16_out"): [(5.0, 4)]})
        assert v._streams[0]["radix"] == RADIX_HEX
        assert v._streams[0]["height"] == 90

    def test_register_trace_defaults_hex_and_survives_refresh(self, qapp):
        v = self._view(qapp)
        v.add_register_stream(0, 1, 1, 5, [(2.0, 0x100), (8.0, 0x200)])
        assert v.stream_count() == 3
        reg = v._streams[-1]
        assert reg["radix"] == RADIX_HEX
        assert reg["source"]["type"] == "register"
        # a port refresh must NOT drop the register trace
        v.set_streams({(0, "x16_in"): [(0.0, 1)], (0, "x16_out"): [(5.0, 2)]})
        assert any(s["source"]["type"] == "register" for s in v._streams)

    def test_overlay_via_drop(self, qapp):
        v = self._view(qapp)
        # Both port streams default to ANALOG. Drop analog trace 1 into the
        # MIDDLE of analog row 0 → stacked (one rendered row with 2 members).
        _grp0, top0, h0 = list(v._group_rows())[0]
        v._drop_trace(1, top0 + h0 * 0.5)
        rows = [g for g, _t, _h in v._group_rows()]
        assert any(len(g) == 2 for g in rows)

    def test_drop_in_fringe_does_not_stack(self, qapp):
        v = self._view(qapp)
        # Dropping in the top 10% "gap" reorders into its own pane, no stack.
        _grp0, top0, h0 = list(v._group_rows())[0]
        v._drop_trace(1, top0 + h0 * 0.02)
        rows = [g for g, _t, _h in v._group_rows()]
        assert all(len(g) == 1 for g in rows)

    def test_digital_never_stacks(self, qapp):
        v = self._view(qapp)
        # A HEX (digital) register trace dropped in the middle of an analog row
        # must NOT stack — digital traces only reorder.
        v.add_register_stream(0, 1, 1, 5, [(2.0, 0x100)])  # index 2, HEX
        _grp0, top0, h0 = list(v._group_rows())[0]
        v._drop_trace(2, top0 + h0 * 0.5)
        rows = [g for g, _t, _h in v._group_rows()]
        assert all(len(g) == 1 for g in rows)

    def test_digital_radix_splits_out_of_pane(self, qapp):
        v = self._view(qapp)
        # Stack two analog traces, then switch one to HEX → it splits out.
        _grp0, top0, h0 = list(v._group_rows())[0]
        v._drop_trace(1, top0 + h0 * 0.5)
        assert any(len(g) == 2 for g in v._groups())
        # The stacked member is now last in its group; switch it to HEX.
        stacked = [g for g in v._groups() if len(g) == 2][0]
        v.set_radix(stacked[-1], RADIX_HEX)
        assert all(len(g) == 1 for g in v._groups())

    def test_shared_pane_amplitude_on_stack(self, qapp):
        v = self._view(qapp)
        # The pane (row 0) already has a non-default amplitude; a trace stacked
        # onto it inherits that single shared scale (#158).
        _grp0, top0, h0 = list(v._group_rows())[0]
        v._streams[0]["amp_scale"] = 3.0
        v._drop_trace(1, top0 + h0 * 0.5)
        grp = [g for g in v._groups() if len(g) == 2][0]
        assert v._streams[grp[0]]["amp_scale"] == pytest.approx(3.0)
        assert v._streams[grp[1]]["amp_scale"] == pytest.approx(3.0)

    def test_gutter_click_selects_without_moving(self, qapp):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        v = self._view(qapp)
        _grp0, top0, _h0 = list(v._group_rows())[0]
        y = top0 + 8
        order_before = [s["key"] for s in v._streams]
        press = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(10, y),
                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        v.mousePressEvent(press)
        rel = QMouseEvent(QMouseEvent.MouseButtonRelease, QPointF(10, y),
                          Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        v.mouseReleaseEvent(rel)
        # A plain click selects (no reorder/stack).
        assert v._selected == 0
        assert [s["key"] for s in v._streams] == order_before

    def test_signal_list_round_trip(self, qapp, tmp_path):
        from engine.io.waveform_io import load_signal_list, save_signal_list
        v = self._view(qapp)
        v.add_register_stream(0, 1, 1, 5, [(2.0, 0x100)])
        v.set_radix(0, RADIX_HEX)
        out = tmp_path / "sigs.wsig.yaml"
        save_signal_list(v.to_signal_list(), out)
        items = load_signal_list(out)
        assert len(items) == 3
        v2 = WaveformView()
        v2.from_signal_list(items, lambda src: [(0.0, 1)])
        assert v2.stream_count() == 3
        assert v2._streams[0]["radix"] == RADIX_HEX  # settings preserved

    def test_register_dropped_signal(self, qapp):
        v = self._view(qapp)
        got = []
        v.register_dropped.connect(lambda *a: got.append(a))
        # simulate a drop via the MIME path
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDropEvent
        from PySide6.QtCore import QPointF, Qt
        md = QMimeData()
        md.setData("application/x-placekyt-register", b"0,2,3,7")
        ev = QDropEvent(QPointF(200, 30), Qt.CopyAction, md,
                        Qt.LeftButton, Qt.NoModifier)
        v.dropEvent(ev)
        assert got == [(0, 2, 3, 7)]


class TestWaveformRound3:
    """Round-3 fixes: top-drop ordering, pan/zoom clamp, delete key, constant
    gutter value, full-height analog mapping, fixed-header ruler."""

    def _view(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "a"): [(0.0, 0x4000), (10.0, 0x2000)],
                       (0, "b"): [(5.0, 0x1000), (15.0, 0x3000)],
                       (0, "c"): [(2.0, 0x0800), (12.0, 0x6000)]})
        return v

    def test_drop_above_top_row_goes_to_top(self, qapp):
        v = self._view(qapp)
        # Drag the lowest trace (index 2) to y above the first row → it becomes
        # the new top trace (was the can't-drag-above-top bug).
        v._drop_trace(2, 2.0)   # above _TOP_PAD
        assert v._streams[0]["label"] == "chip0.c"

    def test_top_fringe_inserts_above_target(self, qapp):
        v = self._view(qapp)
        _g, top1, h1 = list(v._group_rows())[1]   # second row ('b')
        v._drop_trace(2, top1 + h1 * 0.03)        # top fringe of row 'b'
        labels = [s["label"] for s in v._streams]
        # 'c' now sits immediately above 'b'
        assert labels.index("chip0.c") < labels.index("chip0.b")

    def test_pan_clamped_to_data(self, qapp):
        v = self._view(qapp)
        v._fit_time_window()
        t0, t1 = v.time_window()
        # Pan hard left many times — t0 must not run far past the data start.
        b = v._data_bounds()
        for _ in range(50):
            span = v._t1 - v._t0
            v._t0 -= span
            v._t1 -= span
            v._clamp_window()
        assert v._t0 >= b[0] - (b[1] - b[0])      # stayed near the data

    def test_zoom_out_clamped_to_data(self, qapp):
        v = self._view(qapp)
        for _ in range(20):                        # zoom way out
            c = 0.5 * (v._t0 + v._t1)
            v._t0 = c - (c - v._t0) * 4
            v._t1 = c + (v._t1 - c) * 4
            v._clamp_window()
        b = v._data_bounds()
        full = b[1] - b[0]
        # window collapses to ~ the data range (plus the 4% pad), not infinite
        assert (v._t1 - v._t0) <= full * 1.2

    def test_delete_key_removes_selected(self, qapp):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        v = self._view(qapp)
        v._selected = 1
        n0 = v.stream_count()
        ev = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
        v.keyPressEvent(ev)
        assert v.stream_count() == n0 - 1

    def test_constant_signal_shows_gutter_value(self, qapp):
        v = WaveformView()
        # a constant register trace (single sample), HEX
        v.add_register_stream(0, 1, 1, 5, [(0.0, 0x00AB)])
        s = v._streams[-1]
        # no cursor set → still reports the constant value, not None
        assert v._gutter_value(s) == 0x00AB

    def test_analog_full_height_mapping(self, qapp):
        v = WaveformView()
        # a signal that only swings on the negative side
        v.set_streams({(0, "a"): [(0.0, 0x0000), (1.0, 0xE000)]})  # 0 and -0.25
        grp = v._groups()[0]
        vmin, vmax = v._pane_value_range(grp)
        # range tracks the actual data (min ~ -0.25, max ~ 0.0), not full ±1
        assert vmax == pytest.approx(0.0, abs=1e-3)
        assert vmin < -0.2

    def test_ruler_mirrors_window(self, qapp):
        from ui.widgets.waveform_view import WaveformRuler
        v = self._view(qapp)
        ruler = WaveformRuler(v)
        fired = []
        v.window_changed.connect(lambda: fired.append(1))
        v._fit_time_window()
        assert fired                    # window change notifies the ruler
        assert ruler._view.time_window() == v.time_window()


class TestRulerTicks:
    """Shared nice-tick generation (ruler_ticks)."""

    def test_nice_step_rounds_to_125(self):
        from ui.widgets.ruler_ticks import nice_step
        assert nice_step(0.3) == pytest.approx(0.5)
        assert nice_step(1.1) == pytest.approx(2.0)
        assert nice_step(3.0) == pytest.approx(5.0)
        assert nice_step(7.0) == pytest.approx(10.0)

    def test_more_pixels_yield_more_ticks(self):
        from ui.widgets.ruler_ticks import nice_ticks
        few = nice_ticks(0.0, 1000.0, 200.0)
        many = nice_ticks(0.0, 1000.0, 1600.0)
        assert len(many) > len(few)     # zooming in fills in more ticks

    def test_degenerate_range_empty(self):
        from ui.widgets.ruler_ticks import nice_ticks
        assert nice_ticks(5.0, 5.0, 400.0) == []
        assert nice_ticks(0.0, 100.0, 0.0) == []


class TestWaveformInitialValue:
    """Leading/initial-value display + unknown state."""

    def test_analog_lead_value_default_zero(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "x16_in"): [(10.0, 0x4000)]})
        s = v._streams[0]
        assert s["initial"] == 0
        # before the first sample the lead value is the initial (0)
        lead = v._lead_samples(s)
        assert lead[0][1] == 0 and lead[0][0] <= 10.0

    def test_register_initial_shown_before_first_write(self, qapp):
        v = WaveformView()
        # register with an initial (reset/programmed) value, first write later
        v.add_register_stream(0, 1, 1, 5, [(20.0, 0x00CD)], initial=0x00AB)
        s = v._streams[-1]
        v.set_main_cursor(1.0)          # cursor before the first write
        assert v._gutter_value(s) == 0x00AB

    def test_register_no_samples_no_initial_is_unknown(self, qapp):
        v = WaveformView()
        v.add_register_stream(0, 1, 1, 5, [], initial=None)
        s = v._streams[-1]
        assert v._gutter_value(s) is None      # truly unknown
        # rendering must not raise (draws the hashed 'unknown' band)
        v.resize(400, 120)
        img = QImage(400, 120, QImage.Format_ARGB32)
        v.render(img, QPoint(0, 0))

    def test_initial_round_trips_through_yaml(self, qapp, tmp_path):
        from engine.io.waveform_io import load_signal_list, save_signal_list
        v = WaveformView()
        v.add_register_stream(0, 1, 1, 5, [(2.0, 0x100)], initial=0x00AB)
        out = tmp_path / "sigs.wsig.yaml"
        save_signal_list(v.to_signal_list(), out)
        items = load_signal_list(out)
        v2 = WaveformView()
        v2.from_signal_list(items, lambda src: [(2.0, 0x100)])
        assert v2._streams[0]["initial"] == 0x00AB


class TestWaveformHeights:
    """Digital traces default to a compact text-tall height; radix switch
    re-defaults the height unless hand-resized."""

    def test_register_defaults_to_compact_height(self, qapp):
        from ui.widgets.waveform_view import _ROW_H
        v = WaveformView()
        v.add_register_stream(0, 1, 1, 5, [(2.0, 0x100)])
        h = v._streams[-1]["height"]
        assert h == v._digital_min_height()
        assert h < _ROW_H                       # smaller than the analog default

    def test_radix_switch_redefaults_height(self, qapp):
        from ui.widgets.waveform_view import RADIX_ANALOG, RADIX_HEX, _ROW_H
        v = WaveformView()
        v.set_streams({(0, "a"): [(0.0, 1), (1.0, 2)]})   # analog default
        assert v._streams[0]["height"] == _ROW_H
        v.set_radix(0, RADIX_HEX)                          # → compact
        assert v._streams[0]["height"] == v._digital_min_height()
        v.set_radix(0, RADIX_ANALOG)                       # → tall again
        assert v._streams[0]["height"] == _ROW_H

    def test_hand_resized_height_survives_radix_switch(self, qapp):
        from ui.widgets.waveform_view import RADIX_HEX
        v = WaveformView()
        v.set_streams({(0, "a"): [(0.0, 1), (1.0, 2)]})
        v._streams[0]["height"] = 123            # user hand-resized
        v.set_radix(0, RADIX_HEX)
        assert v._streams[0]["height"] == 123    # preserved, not re-defaulted

    def test_analog_vertical_grid_at_time_majors(self, qapp):
        v = WaveformView()
        v.set_streams({(0, "a"): [(0.0, 0x1000), (100.0, 0x4000)]})
        v.resize(800, 120)
        # the analog grid's vertical lines come from the header ruler's majors
        majors = v._time_major_ticks()
        assert majors                            # there are numbered ticks
        # render must not raise
        img = QImage(800, 120, QImage.Format_ARGB32)
        v.render(img, QPoint(0, 0))
