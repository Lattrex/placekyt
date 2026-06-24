"""File + Build menu wiring tests (controller-level, offscreen Qt).

These exercise the controller methods the menus call (open/save/new/build/drc/
export) plus the MainWindow's non-dialog handlers. File-dialog-driven slots
(_open_project etc.) are not driven here — QFileDialog can't run headless; the
underlying controller methods they call ARE tested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.kbs import read_kbs  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
BPSK_DEMO = Path(__file__).parent / "data" / "demo" / "bpsk_demo.kyt"
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
    # Uses the default registry — must find the bundled resources/chips/.
    return AppController(catalog=catalog)


# --------------------------------------------------------------------------- #
# Registry / bundled chip types
# --------------------------------------------------------------------------- #


class TestBundledRegistry:
    def test_default_registry_finds_bundled_chip(self, controller):
        # resources/chips/kyttar_10x12.yaml must be discoverable by the GUI.
        assert "kyttar_10x12" in controller.registry.names()


# --------------------------------------------------------------------------- #
# File operations
# --------------------------------------------------------------------------- #


class TestFileOps:
    def test_open_demo(self, controller):
        controller.open_project(DEMO)
        assert controller.project.metadata.name == "Gain Demo"
        assert controller.project_path == DEMO
        assert not controller.project.project_dirty

    def test_new_project(self, controller):
        controller.new_project("Fresh", "kyttar_10x12", n_chips=2)
        assert controller.project.metadata.name == "Fresh"
        assert len(controller.project.chips) == 2
        assert controller.project_path is None

    def test_save_then_reload_round_trip(self, controller, catalog, tmp_path):
        controller.open_project(DEMO)
        out = tmp_path / "copy.kyt"
        controller.save_project(out)
        assert not controller.project.project_dirty

        other = AppController(catalog=catalog)
        other.open_project(out)
        assert other.project.metadata.name == "Gain Demo"
        assert [b.name for b in other.project.blocks] == \
            [b.name for b in controller.project.blocks]

    def test_save_without_path_raises(self, controller):
        controller.new_project("X", "kyttar_10x12")
        with pytest.raises(ValueError):
            controller.save_project()  # never saved, no path


# --------------------------------------------------------------------------- #
# Build / DRC / export
# --------------------------------------------------------------------------- #


class TestBuildOps:
    def test_drc_clean_on_demo(self, controller):
        controller.open_project(DEMO)
        result = controller.run_drc()
        assert result.ok

    def test_build_demo(self, controller):
        controller.open_project(DEMO)
        result = controller.build()
        assert result.ok
        assert len(result.words(0)) > 0

    def test_export_writes_valid_kbs(self, controller, tmp_path):
        controller.open_project(DEMO)
        result = controller.build()
        out = tmp_path / "demo.kbs"
        controller.export_bitstream(result, out)
        kbs = read_kbs(out)  # round-trips through the hardened reader
        assert len(kbs.chips) == 1
        assert kbs.metadata["project_name"] == "Gain Demo"

    def test_bpsk_demo_opens_builds_and_disassembles_clean(self, controller):
        """The BPSK demo opens + builds clean, and its built bitstream
        disassembles with real mnemonics (CMP/BR present, no '??' garbage)."""
        from engine.disasm import disassemble_bitstream

        controller.open_project(BPSK_DEMO)
        assert controller.project.metadata.name == "BPSK Modem Demo"
        assert controller.run_drc().ok
        result = controller.build()
        assert result.ok
        listing = disassemble_bitstream(result.words(0), stateful=False)
        # The slicer's conditional logic must decode, not show as garbage.
        assert "CMP" in listing
        assert "BR.NN" in listing
        assert "??" not in listing


# --------------------------------------------------------------------------- #
# MainWindow handlers (non-dialog paths)
# --------------------------------------------------------------------------- #


class TestMainWindowMenus:
    @pytest.fixture(autouse=True)
    def _no_modal(self, monkeypatch):
        """Stub the modal dialogs so handlers don't block waiting for input."""
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        monkeypatch.setattr(QMessageBox, "information",
                            staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(QMessageBox, "critical",
                            staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(QMessageBox, "about",
                            staticmethod(lambda *a, **k: None))

    def test_generate_bitstream_sets_last_build(self, controller, qapp):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        w._generate_bitstream()
        assert getattr(w, "_last_build", None) is not None
        assert w._last_build.ok

    def test_check_drc_runs(self, controller, qapp):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        w._check_drc()  # must not raise / block

    def test_title_marks_dirty_after_edit(self, controller, qapp):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        assert "*" not in w.windowTitle()
        controller.place_block("GainBlock", 0, 7, 7, library="lattrex.official")
        assert w.windowTitle().endswith("*")  # dirty marker

    def test_open_project_via_dialog(self, controller, qapp, monkeypatch):
        from PySide6.QtWidgets import QFileDialog

        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(DEMO), "")))
        w = MainWindow(controller=controller)
        w._open_project()
        assert controller.project.metadata.name == "Gain Demo"
        # 120 grid cells (routing-cell markers on the demo route are extra).
        grid = [c for c in w.canvas.cell_items() if c.route_name is None]
        assert len(grid) == 120

    def test_open_cancelled_is_noop(self, controller, qapp, monkeypatch):
        from PySide6.QtWidgets import QFileDialog

        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: ("", "")))
        w = MainWindow(controller=controller)
        before = controller.project.metadata.name
        w._open_project()  # user cancelled → nothing changes
        assert controller.project.metadata.name == before

    def test_save_as_via_dialog_appends_extension(self, controller, qapp,
                                                  monkeypatch, tmp_path):
        from PySide6.QtWidgets import QFileDialog

        controller.open_project(DEMO)
        target = tmp_path / "saved"  # no extension — handler should add .kyt
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        w = MainWindow(controller=controller)
        w._save_as()
        assert (tmp_path / "saved.kyt").exists()

    def test_new_project_via_dialog(self, controller, qapp, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        monkeypatch.setattr(QInputDialog, "getItem",
                            staticmethod(lambda *a, **k: ("kyttar_10x12", True)))
        w = MainWindow(controller=controller)
        w._new_project()
        assert controller.project.metadata.name == "Untitled"
        assert len(controller.project.chips) == 1

    def test_export_via_dialog(self, controller, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QFileDialog

        controller.open_project(DEMO)
        out = tmp_path / "x"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(out), "")))
        w = MainWindow(controller=controller)
        w._generate_bitstream()  # populate _last_build
        w._export_bitstream()
        assert (tmp_path / "x.kbs").exists()

    def test_show_findings_with_errors(self, controller, qapp):
        # A project that fails DRC → findings dialog summarizes errors.
        controller.new_project("Bad", "kyttar_10x12")
        controller.place_block("GainBlock", 0, 3, 3, library="lattrex.official")
        controller.place_block("DCBlockerBlock", 0, 3, 3, library="lattrex.official", params={"length": 2, "long_form": False})
        w = MainWindow(controller=controller)
        w._check_drc()  # has overlap error; dialog stubbed by _no_modal

    def test_save_failure_surfaces_error(self, controller, qapp, monkeypatch):
        controller.open_project(DEMO)
        w = MainWindow(controller=controller)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(controller, "save_project", boom)
        w._do_save("/whatever.kyt")  # must not raise; routes to _error (stubbed)

    def test_about_dialog(self, controller, qapp):
        w = MainWindow(controller=controller)
        w._about()  # QMessageBox.about stubbed via _no_modal? about() is separate

    def test_namespace_shims_callable(self, controller, qapp):
        controller.open_project(DEMO)
        w = MainWindow(controller=controller)
        ns = w._api_namespace()
        assert callable(ns["build"]) and callable(ns["drc"])
        assert ns["project"] is controller.project
        # the drc shim returns a real result
        assert ns["drc"]().ok
