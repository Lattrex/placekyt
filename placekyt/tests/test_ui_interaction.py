"""Interactive-canvas tests: controller + drag-drop + selection + undo/redo.

Offscreen Qt. These need gr_kyttar (the controller builds a BlockCatalog) and
the chip-type YAML for rendering.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.registry import ChipTypeRegistry  # noqa: E402
from model.chip import ChipInstance  # noqa: E402
from model.enums import Face  # noqa: E402
from model.project import Project  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.panels.library_panel import LibraryPanel  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip-type yaml absent")


def _pump() -> None:
    """Process pending Qt events (deferred QTimer.singleShot(0, …) callbacks)."""
    QApplication.processEvents()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def controller(qapp, catalog):
    reg = ChipTypeRegistry()
    reg.register_file(CT_PATH)
    return AppController(catalog=catalog, registry=reg)


@pytest.fixture
def project():
    p = Project(chip_type="kyttar_10x12")
    p.chips = [ChipInstance(0, "C0")]
    return p


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #


class TestController:
    def test_place_block_issues_command(self, controller, project):
        controller.set_project(project)
        name = controller.place_block("GainBlock", 0, 3, 4,
                                      library="lattrex.official")
        blk = project.block(name)
        assert blk is not None and blk.is_placed
        assert blk.placement.cells[0].pos == (3, 4)
        assert controller.can_undo()

    def test_unique_names(self, controller, project):
        controller.set_project(project)
        n1 = controller.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        n2 = controller.place_block("GainBlock", 0, 5, 0, library="lattrex.official")
        assert n1 != n2  # auto-deduped

    def test_move_and_undo(self, controller, project):
        controller.set_project(project)
        name = controller.place_block("GainBlock", 0, 2, 2,
                                      library="lattrex.official")
        controller.move_block(name, 1, 1)
        assert project.block(name).placement.cells[0].pos == (3, 3)
        controller.undo()
        assert project.block(name).placement.cells[0].pos == (2, 2)

    def test_transform_rotate_and_undo(self, controller, project):
        controller.set_project(project)
        name = controller.place_block("GainBlock", 0, 2, 2,
                                      library="lattrex.official")
        before = [(c.pos, c.face) for c in project.block(name).placement.cells]
        controller.transform_block(name, "cw")
        after = [(c.pos, c.face) for c in project.block(name).placement.cells]
        assert after != before                 # something changed
        # top-left corner preserved (block didn't wander)
        box = project.block(name).placement.full_bounding_box()
        assert box[0] == 2 and box[1] == 2
        controller.undo()
        assert [(c.pos, c.face)
                for c in project.block(name).placement.cells] == before

    def test_add_panel_and_undo(self, controller, project):
        from model.enums import Face
        controller.set_project(project)
        pid = controller.add_panel("Symbols")
        assert project.panel(pid) is not None
        assert project.panel(pid).size_words == 1 << 16
        # new panels default to MIRRORED (inputs EAST, outputs WEST) so they
        # connect chip-output → panel-input naturally.
        assert project.panel(pid).mirrored is True
        assert project.panel(pid).port("x16_in").face is Face.EAST
        controller.undo()
        assert project.panel(pid) is None

    def test_move_panel_and_undo(self, controller, project):
        controller.set_project(project)
        pid = controller.add_panel()
        controller.move_panel(pid, 123.0, 45.0)
        assert project.panel(pid).position == (123.0, 45.0)
        controller.undo()
        assert project.panel(pid).position != (123.0, 45.0)

    def test_connect_panel_out_to_chip_in(self, controller, project):
        controller.set_project(project)
        pid = controller.add_panel()
        # panel 'out' (output, x16) → chip x16_in (input, x16): valid
        pc = controller.connect_panel(pid, "x16_out", 0, "x16_in")
        assert pc in project.panel_connections
        controller.undo()
        assert pc not in project.panel_connections

    def test_connect_panel_rejects_same_direction(self, controller, project):
        controller.set_project(project)
        pid = controller.add_panel()
        # panel 'out' (output) → chip x16_out (output): both outputs → error
        with pytest.raises(ValueError):
            controller.connect_panel(pid, "x16_out", 0, "x16_out")

    def test_mirror_panel_and_undo(self, controller, project):
        from model.enums import Face
        controller.set_project(project)
        pid = controller.add_panel()
        # added mirrored (inputs EAST); mirroring again flips back to WEST.
        assert project.panel(pid).port("x16_in").face is Face.EAST
        controller.mirror_panel(pid)
        assert project.panel(pid).mirrored is False
        assert project.panel(pid).port("x16_in").face is Face.WEST
        controller.undo()
        assert project.panel(pid).mirrored is True
        assert project.panel(pid).port("x16_in").face is Face.EAST

    def test_mirror_then_connect_x1(self, controller, project):
        # After a mirror, panel x1_in (now EAST) can wire to a chip x1 output —
        # exercise the x1 ports added to the panel.
        controller.set_project(project)
        pid = controller.add_panel()
        # chip x1 ports exist on kyttar_10x12 (x1_in / x1_out)
        pc = controller.connect_panel(pid, "x1_in", 0, "x1_out")
        assert pc in project.panel_connections

    def test_remove_panel_drops_links(self, controller, project):
        controller.set_project(project)
        pid = controller.add_panel()
        controller.connect_panel(pid, "x16_out", 0, "x16_in")
        controller.remove_panel(pid)
        assert project.panel(pid) is None
        assert project.panel_connections == []
        controller.undo()                          # restores panel + link
        assert project.panel(pid) is not None
        assert len(project.panel_connections) == 1

    def test_changed_signal_fires(self, controller, project):
        controller.set_project(project)
        fired = []
        controller.changed.connect(lambda: fired.append(1))
        controller.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        assert fired  # at least one refresh emitted (post-flush)

    def test_unknown_block_raises(self, controller, project):
        controller.set_project(project)
        with pytest.raises(KeyError):
            controller.place_block("NoSuchBlock", 0, 0, 0)


# --------------------------------------------------------------------------- #
# Library panel
# --------------------------------------------------------------------------- #


class TestLibraryPanel:
    def test_lists_all_blocks(self, qapp, catalog):
        # The library panel shows the CURATED palette (manifest blocks only) — a
        # smaller, trustworthy set than every discovered class. It must list the
        # verified production blocks and exclude the unverified leftovers.
        panel = LibraryPanel(catalog)
        n = panel.block_count()
        assert n >= 12, "the verified+POC palette should list at least a dozen blocks"
        assert n < len(catalog.all(include_hidden=True)), "palette is a curated subset"

    def test_search_filters(self, qapp, catalog):
        panel = LibraryPanel(catalog)
        panel.search.setText("costas")
        assert panel.block_count() >= 1
        assert panel.block_count() < 25  # filtered down


# --------------------------------------------------------------------------- #
# MainWindow interaction
# --------------------------------------------------------------------------- #


class TestMainWindowInteraction:
    def _window(self, controller, project):
        w = MainWindow(controller=controller)
        w.set_project(project)
        return w

    def test_drop_places_block_and_rerenders(self, controller, project):
        w = self._window(controller, project)
        before = sum(1 for c in w.canvas.cell_items() if c.label)
        w._on_block_dropped("GainBlock", "lattrex.official", 0, 6, 6)
        after = sum(1 for c in w.canvas.cell_items() if c.label)
        assert after == before + 1
        # the placed cell is at the drop position
        placed = [c for c in w.canvas.cell_items() if c.label]
        assert (placed[0].cx, placed[0].cy) == (6, 6)

    def test_drop_off_chip_is_ignored(self, controller, project):
        w = self._window(controller, project)
        # which_chip_at returns None off-grid; simulate by dropping at a bad cell
        # via the canvas helper directly.
        assert w.canvas._which_chip_at(99999, 99999) is None

    def test_selection_updates_inspector(self, controller, project):
        w = self._window(controller, project)
        w._on_block_dropped("GainBlock", "lattrex.official", 0, 1, 1)
        cell = [c for c in w.canvas.cell_items() if c.label][0]
        cell.setSelected(True)
        # inspector reflects the selected cell
        assert "1, 1" in w.inspector._title.text()

    def test_arrow_move_via_handler(self, controller, project):
        w = self._window(controller, project)
        name = controller.place_block("GainBlock", 0, 4, 4,
                                      library="lattrex.official")
        w.canvas.render_scene()
        cell = [c for c in w.canvas.cell_items() if c.label][0]
        cell.setSelected(True)
        w._on_move_requested(0, 1)
        assert project.block(name).placement.cells[0].pos == (4, 5)

    def test_undo_redo_actions_enabled_state(self, controller, project):
        w = self._window(controller, project)
        assert not w.act_undo.isEnabled()  # nothing to undo yet
        w._on_block_dropped("GainBlock", "lattrex.official", 0, 2, 2)
        assert w.act_undo.isEnabled()
        assert "Place block" in w.act_undo.text()
        w._undo()
        assert w.act_redo.isEnabled()
        assert not w.act_undo.isEnabled()

    def test_empty_selection_clears_inspector(self, controller, project):
        w = self._window(controller, project)
        w._on_block_dropped("GainBlock", "lattrex.official", 0, 0, 0)
        cell = [c for c in w.canvas.cell_items() if c.label][0]
        cell.setSelected(True)
        cell.setSelected(False)
        assert w.inspector._title.text() == "No selection"


class TestCanvasQtEvents:
    """Exercise the real keyPressEvent path and the scene→cell mapping.

    (Synthetic QDragEnterEvent/QDropEvent construction is omitted — PySide6's
    drag-event constructors are fragile to hand-build and crash the interpreter;
    the drop LOGIC is covered via the _on_block_dropped / _which_chip_at handler
    tests above, which is the behavior that matters.)"""

    def _canvas(self, controller, project):
        from ui.canvas import ChipCanvas

        canvas = ChipCanvas()
        canvas.set_project(project, controller.chip_types())
        return canvas

    def test_which_chip_maps_scene_to_cell(self, controller, project):
        canvas = self._canvas(controller, project)
        hit = canvas._which_chip_at(0.5 * 64, 1.5 * 64)  # cell (0,1)
        assert hit is not None
        _chip, _ct, cx, cy = hit
        assert (cx, cy) == (0, 1)

    def test_context_menu_on_empty_cell_is_noop(self, controller, project):
        from PySide6.QtGui import QContextMenuEvent
        from PySide6.QtCore import QPoint

        canvas = self._canvas(controller, project)
        deleted = []
        canvas.delete_requested.connect(deleted.append)
        # An empty cell has no label → contextMenuEvent returns early, no menu.
        ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), QPoint(5, 5))
        canvas.contextMenuEvent(ev)
        assert deleted == []

    def test_arrow_key_with_selection_emits_move(self, controller, project):
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent, Qt

        canvas = self._canvas(controller, project)
        # place + select a block cell
        controller.set_project(project)
        canvas.set_project(project, controller.chip_types())
        controller.place_block("GainBlock", 0, 3, 3, library="lattrex.official")
        canvas.render_scene()
        [c for c in canvas.cell_items() if c.label][0].setSelected(True)
        moves = []
        canvas.move_requested.connect(lambda dx, dy: moves.append((dx, dy)))
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
        canvas.keyPressEvent(ev)
        assert moves == [(1, 0)]
        assert ev.isAccepted()

    def test_arrow_key_without_selection_propagates(self, controller, project):
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent, Qt

        canvas = self._canvas(controller, project)
        moves = []
        canvas.move_requested.connect(lambda dx, dy: moves.append((dx, dy)))
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
        canvas.keyPressEvent(ev)  # no selection → no move emitted
        assert moves == []

    def test_delete_key_emits_delete(self, controller, project):
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent, Qt

        controller.set_project(project)
        canvas = self._canvas(controller, project)
        controller.place_block("GainBlock", 0, 3, 3, library="lattrex.official")
        canvas.render_scene()
        [c for c in canvas.cell_items() if c.label][0].setSelected(True)
        deleted = []
        canvas.delete_requested.connect(deleted.append)
        canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        assert deleted == ["gain"]


class TestDeleteAndFaceEdit:
    def _window(self, controller, project):
        w = MainWindow(controller=controller)
        w.set_project(project)
        return w

    def test_delete_removes_block_and_undo_restores(self, controller, project):
        w = self._window(controller, project)
        controller.place_block("GainBlock", 0, 4, 4, library="lattrex.official")
        w._on_delete_requested("gain")
        assert controller.project.block("gain") is None
        controller.undo()
        assert controller.project.block("gain") is not None

    def test_set_face_via_signal(self, controller, project):
        from model.enums import Face

        w = self._window(controller, project)
        controller.place_block("GainBlock", 0, 2, 2, library="lattrex.official")
        w._on_set_face_requested("gain", 0, "north")
        assert controller.project.block("gain").placement.cell(0).face is Face.NORTH

    def test_inspector_face_combo_issues_command(self, controller, project):
        from model.enums import Face

        w = self._window(controller, project)
        controller.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
        w.inspector.show_selection(
            {"cell": (1, 1), "kind": "block", "block": "gain",
             "cell_id": 0, "face": "east"})
        assert w.inspector._face_combo is not None
        w.inspector._on_face_combo("west")
        _pump()
        assert controller.project.block("gain").placement.cell(0).face is Face.WEST

    def test_face_combo_full_chain_no_crash(self, controller, project):
        """Regression: changing the face combo on a SELECTED cell used to crash.

        The combo's currentTextChanged synchronously ran the command → model
        change → canvas re-render → selection change → show_selection() →
        _clear_rows(), deleting the combo mid-signal (use-after-free → segfault).
        Driving the REAL widget + pumping the event loop reproduces the path."""
        from model.enums import Face

        w = self._window(controller, project)
        controller.place_block("GainBlock", 0, 2, 2, library="lattrex.official")
        w.canvas.render_scene()
        cell = [c for c in w.canvas.cell_items() if c.label][0]
        cell.setSelected(True)
        _pump()
        assert w.inspector._face_combo is not None
        # Drive the actual combo widget, as a user would.
        w.inspector._face_combo.setCurrentText("west")
        _pump()
        _pump()  # deferred command + re-render settle
        assert controller.project.block("gain").placement.cell(0).face is Face.WEST
