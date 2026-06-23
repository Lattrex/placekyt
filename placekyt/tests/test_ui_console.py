"""Embedded console tests (the architecture notes §3.1). Offscreen Qt.

Drive the REPL via ``ConsolePanel.submit(line)`` (what Enter calls) and assert
on the captured widget text / namespace effects.
"""

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
from ui.panels.console_panel import ConsolePanel  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# Standalone REPL (no project needed)
# --------------------------------------------------------------------------- #


class TestRepl:
    def test_expression(self, qapp):
        con = ConsolePanel({})
        con.submit("1 + 1")
        assert "2" in con.toPlainText().splitlines()[-2]

    def test_namespace_access(self, qapp):
        con = ConsolePanel({"answer": 42})
        con.submit("answer")
        assert "42" in con.toPlainText()

    def test_assignment_persists(self, qapp):
        con = ConsolePanel({})
        con.submit("x = 10")
        con.submit("x * 3")
        assert "30" in con.toPlainText()

    def test_multiline_block(self, qapp):
        con = ConsolePanel({})
        con.submit("def f(n):")
        con.submit("    return n + 1")
        con.submit("")  # blank line ends the block
        con.submit("f(41)")
        assert "42" in con.toPlainText()

    def test_error_shows_traceback_not_crash(self, qapp):
        con = ConsolePanel({})
        con.submit("1 / 0")
        assert "ZeroDivisionError" in con.toPlainText()

    def test_syntax_error_recovers(self, qapp):
        con = ConsolePanel({})
        con.submit("def (:")          # invalid
        con.submit("2 + 2")           # REPL still usable afterward
        assert "4" in con.toPlainText()


# --------------------------------------------------------------------------- #
# History + completion
# --------------------------------------------------------------------------- #


class TestHistoryCompletion:
    def test_history_records_submitted_lines(self, qapp):
        con = ConsolePanel({})
        con.submit("a = 1")
        con.submit("b = 2")
        assert con._history == ["a = 1", "b = 2"]

    def test_history_prev_recalls(self, qapp):
        con = ConsolePanel({})
        con.submit("first")
        con.submit("second")
        con._history_prev()
        assert con._current_input() == "second"
        con._history_prev()
        assert con._current_input() == "first"

    def test_completion_candidates(self, qapp):
        con = ConsolePanel({"project_name": 1, "project_id": 2})
        cands = con.completions("project_")
        assert "project_name" in cands and "project_id" in cands

    def test_no_completion_for_unknown(self, qapp):
        con = ConsolePanel({})
        assert con.completions("zzz_nope") == []


class TestKeyHandling:
    """Drive the real keyPressEvent dispatch (QKeyEvent is safe to construct)."""

    def _key(self, key, text=""):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text)

    def _type(self, con, text):
        for ch in text:
            con.keyPressEvent(self._key(0, ch))

    def test_enter_submits_typed_line(self, qapp):
        from PySide6.QtCore import Qt

        con = ConsolePanel({})
        self._type(con, "3 + 4")
        con.keyPressEvent(self._key(Qt.Key_Return))
        assert "7" in con.toPlainText()

    def test_up_arrow_recalls_history(self, qapp):
        from PySide6.QtCore import Qt

        con = ConsolePanel({})
        self._type(con, "x = 5")
        con.keyPressEvent(self._key(Qt.Key_Return))
        con.keyPressEvent(self._key(Qt.Key_Up))
        assert con._current_input() == "x = 5"
        con.keyPressEvent(self._key(Qt.Key_Down))
        assert con._current_input() == ""

    def test_tab_completes_unique_token(self, qapp):
        from PySide6.QtCore import Qt

        con = ConsolePanel({"unique_var": 99})
        self._type(con, "uniqu")
        con.keyPressEvent(self._key(Qt.Key_Tab))
        assert con._current_input() == "unique_var"

    def test_backspace_does_not_eat_prompt(self, qapp):
        from PySide6.QtCore import Qt

        con = ConsolePanel({})
        start = con._input_start
        # Backspace at the prompt boundary is a no-op.
        con.keyPressEvent(self._key(Qt.Key_Backspace))
        assert con._input_start == start
        assert con.toPlainText().endswith(">>> ")


# --------------------------------------------------------------------------- #
# Bound to the live API via MainWindow
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not (CT_PATH.exists() and DEMO.exists()),
                    reason="chip yaml / demo absent")
class TestConsoleApi:
    @pytest.fixture(scope="class")
    def catalog(self):
        return BlockCatalog.from_gr_kyttar()

    def _window(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_namespace_has_api(self, qapp, catalog):
        w = self._window(catalog)
        w.console.submit("project.metadata.name")
        assert "Gain Demo" in w.console.toPlainText()

    def test_command_from_console_mutates_model(self, qapp, catalog):
        w = self._window(catalog)
        before = len(w.controller.project.blocks)
        w.console.submit("place('GainBlock', 0, 8, 8, library='lattrex.official')")
        assert len(w.controller.project.blocks) == before + 1

    def test_namespace_rebinds_on_open(self, qapp, catalog):
        w = self._window(catalog)
        # open again (same project) → namespace points at the live project
        w.controller.new_project("Fresh", "kyttar_10x12")
        w._after_project_loaded()
        w.console.submit("project.metadata.name")
        assert "Fresh" in w.console.toPlainText()
