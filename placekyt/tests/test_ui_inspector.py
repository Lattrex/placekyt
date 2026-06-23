"""Inspector memory/assembly view tests (the architecture notes §3.3). Offscreen Qt."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.widgets.cell_program_view import CellProgramView, _format_value  # noqa: E402

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


# --------------------------------------------------------------------------- #
# Value formatting (pure)
# --------------------------------------------------------------------------- #


class TestFormatValue:
    def test_hex(self):
        assert _format_value(0x4000, "Hex") == "0x4000"

    def test_unsigned(self):
        assert _format_value(0x8000, "Unsigned") == "32768"

    def test_signed(self):
        assert _format_value(0x8000, "Signed") == "-32768"
        assert _format_value(0x4000, "Signed") == "16384"

    def test_q15(self):
        assert _format_value(0x4000, "Q15") == "+0.50000"   # 0.5
        assert _format_value(0x8000, "Q15") == "-1.00000"   # -1.0


# --------------------------------------------------------------------------- #
# Controller cell_program
# --------------------------------------------------------------------------- #


class TestCellProgram:
    def _ctrl(self, catalog):
        ctrl = AppController(catalog=catalog)
        ctrl.open_project(DEMO)
        return ctrl

    def test_gain_cell_program(self, qapp, catalog):
        ctrl = self._ctrl(catalog)
        prog = ctrl.cell_program(0, 0, 0)  # gain block at (0,0)
        assert prog is not None
        # v2 layout: data packed low, instructions high (entry = 31 - n_instr).
        assert len(prog["memory"]) == 32
        assert len(prog["disasm"]) == 32
        # The gain coefficient (0.5 → 0x4000) is a DATA word, classified as such.
        classes = prog["classes"]
        gain_addrs = [a for a, c in classes.items()
                      if c["role"] == "data" and prog["memory"][a] == 0x4000]
        assert gain_addrs, "gain coefficient data word not found"
        # The entry address points at an instruction word.
        assert classes[prog["entry"]]["role"] == "instruction"
        assert any("Mul" in m for _a, _w, m in prog["disasm"])

    def test_empty_cell_no_program(self, qapp, catalog):
        ctrl = self._ctrl(catalog)
        assert ctrl.cell_program(0, 5, 5) is None  # empty cell

    def test_build_failure_returns_none(self, qapp, catalog):
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("Bad", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
        ctrl.place_block("DCBlockerBlock", 0, 1, 1, library="lattrex.official")
        assert ctrl.cell_program(0, 1, 1) is None  # overlap → no build

    def test_cached_build_reused(self, qapp, catalog):
        ctrl = self._ctrl(catalog)
        b1 = ctrl.cached_build()
        b2 = ctrl.cached_build()
        assert b1 is b2  # not rebuilt when clean


# --------------------------------------------------------------------------- #
# CellProgramView widget
# --------------------------------------------------------------------------- #


class TestProgramView:
    def test_set_program_populates(self, qapp):
        view = CellProgramView()
        words = [0] * 32
        words[30] = 0x4000
        disasm = [(0, 0x0000, "Halt"), (1, 0xC7FE, "Mul { MulQ }")]
        view.set_program(1, words, disasm)
        assert view.row_count() == 32           # combined table = 32 registers
        assert view.value_text(30) == "0x4000"
        assert "Mul" in view.instruction_text(1)

    def test_format_toggle_updates_value(self, qapp):
        view = CellProgramView()
        words = [0] * 32
        words[30] = 0x4000
        view.set_program(0, words, [])
        view.fmt.setCurrentText("Q15")
        assert view.value_text(30) == "+0.50000"
        view.fmt.setCurrentText("Signed")
        assert view.value_text(30) == "16384"

    def test_isa_tooltip_set(self, qapp):
        view = CellProgramView()
        view.set_program(0, [0] * 32, [(1, 0xC7FE, "Mul { mode: MulQ }")])
        item = view.table.item(1, 2)            # instruction col, R1
        assert "Q15" in item.toolTip() or "Mul" in item.toolTip()

    def test_routing_face_shown(self, qapp):
        view = CellProgramView()
        view.set_program(0, [0] * 32, [], face="east")
        assert view._face_label.text().endswith("east")


# --------------------------------------------------------------------------- #
# Inspector integration
# --------------------------------------------------------------------------- #


class TestInspectorIntegration:
    def _window(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_program_dock_exists_separately(self, qapp, catalog):
        w = self._window(catalog)
        assert "Program" in w._docks
        assert w._docks["Program"].widget() is w.program_view
        assert w.inspector._external_program

    def test_spurious_clear_keeps_program(self, qapp, catalog):
        # A dock interaction can emit selection_changed(None) while the cell is
        # still selected; the program view must NOT blank in that case.
        w = self._window(catalog)
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        assert w.program_view.value_text(1) == "0x4000"
        w._on_selection_changed(None)  # spurious clear; cell still selected
        QApplication.processEvents()
        assert w.program_view.value_text(1) == "0x4000"

    def test_resync_on_program_dock_shown(self, qapp, catalog):
        # Re-showing the Program dock re-pulls the current selection.
        w = self._window(catalog)
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        w.program_view.clear()  # simulate a blank
        w._resync_program(True)
        assert w.program_view.value_text(1) == "0x4000"

    def test_block_cell_shows_program(self, qapp, catalog):
        # The program view is now its own dock; selecting a block cell populates
        # it (v2: the gain coefficient 0.5 → 0x4000 is the data word at addr 1).
        w = self._window(catalog)
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        assert w.program_view.value_text(1) == "0x4000"
        assert w.program_view.instruction_text(1).startswith("data")

    def test_empty_cell_clears_program(self, qapp, catalog):
        w = self._window(catalog)
        # First select a block cell to populate the program view.
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        assert w.program_view.value_text(1) == "0x4000"  # populated
        # Clearing the selection blanks the (docked) program view.
        w.inspector.show_selection(None)
        QApplication.processEvents()
        assert w.program_view._face_label.text() == "No cell selected"

    def test_routing_cell_shows_program(self, qapp, catalog):
        from ui.canvas.cell_item import CellKind

        w = self._window(catalog)
        routing = [c for c in w.canvas.cell_items()
                   if c.kind is CellKind.TRANSIT][0]
        routing.setSelected(True)
        QApplication.processEvents()
        # routing cells are real programmed cells → routing banner shown
        assert "Routing cell" in w.program_view._face_label.text()

    def test_inspector_has_editable_name_distinct_from_type(self, qapp, catalog):
        # The inspector must show the instance NAME (editable) AND the block TYPE
        # (read-only) as DISTINCT fields — they were conflated before.
        w = self._window(catalog)
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        edit = getattr(w.inspector, "_name_edit", None)
        assert edit is not None and edit.text() == "gain"
        from PySide6.QtWidgets import QLineEdit
        assert isinstance(edit, QLineEdit) and edit.isEnabled()  # editable

    def test_rename_block_via_inspector(self, qapp, catalog):
        w = self._window(catalog)
        gain = [c for c in w.canvas.cell_items() if c.label == "gain"][0]
        gain.setSelected(True)
        QApplication.processEvents()
        edit = w.inspector._name_edit
        edit.setText("rx_gain")
        edit.editingFinished.emit()           # commit (deferred via QTimer)
        QApplication.processEvents()
        assert w.controller.project.block("rx_gain") is not None
        assert w.controller.project.block("gain") is None
