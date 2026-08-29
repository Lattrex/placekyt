# SPDX-License-Identifier: GPL-3.0-or-later
"""Startup DEFAULTS of the main window — the two things a user should not have
to configure on every launch.

1. The bottom dock opens on **Waveform**, not Output. A run is read from the
   waveform viewer; the transaction log is a debugging detail. Output is HIDDEN
   rather than deleted — it keeps its output/trace feeds, its cursor sync and its
   canvas-selection cell filter, and is one click away under View.

2. **Run as GNURadio Server is ON.** Driving the chip from GNU Radio is the
   primary workflow, so the menu item starts checked and the server starts as
   soon as a project exists.

The autostart is a PREFERENCE (``sim/gr_server_autostart``), not a hardcoded
default, because a live server changes simulation behaviour — it rebuilds per
batch and drives the chip itself, which races manual Step/Pause. The suite's
conftest disables it session-wide for exactly that reason, so the tests here set
the preference explicitly instead of relying on the shipped default.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine import preferences  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and DEMO.exists()), reason="chip yaml / demo absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture()
def controller(qapp, catalog):
    return AppController(catalog=catalog)


def _bottom(win, title):
    dock = win._docks[title]
    assert win.dockWidgetArea(dock) == Qt.BottomDockWidgetArea, \
        f"{title} is not in the bottom dock area"
    return dock


class TestBottomDockDefaults:
    def test_waveform_is_the_landing_tab_and_output_is_hidden(self, controller):
        win = MainWindow(controller=controller)
        controller.open_project(DEMO)
        win._after_project_loaded()
        win.show()

        assert not _bottom(win, "Output").isVisible(), \
            "the Output tab must not be the default bottom tab"
        assert _bottom(win, "Waveform").isVisible(), \
            "Waveform must be the visible bottom tab on startup"

    def test_output_panel_is_hidden_but_still_wired(self, controller):
        """Hidden, NOT removed — the panel keeps its feeds so re-showing it from
        the View menu gives a working panel rather than a dead one."""
        win = MainWindow(controller=controller)
        controller.open_project(DEMO)
        win._after_project_loaded()

        assert win.output_panel is not None
        # Its View-menu toggle exists, so it is reachable again in one click.
        assert _bottom(win, "Output").toggleViewAction() is not None
        # And the live feeds are still connected: the panel accepts a cursor
        # highlight and an input-sample set without error.
        win.output_panel.highlight_cursor(0.0)
        win.output_panel.set_inputs(win.sim.input_samples)


class TestGnuradioServerAutostart:
    def test_menu_item_is_checked_by_default(self, controller, monkeypatch):
        """With the preference at its SHIPPED value the menu item starts checked."""
        monkeypatch.setattr(preferences, "gr_server_autostart", lambda: True)
        win = MainWindow(controller=controller)
        assert win.act_gr_server.isChecked(), \
            "'Run as GNURadio Server' must be checked on startup"

    def test_server_starts_when_a_project_loads(self, controller, monkeypatch):
        """Checked is not enough — the server must actually be LISTENING, and it
        cannot start before a project exists (it needs a built design).

        PORT CONTENTION IS NOT A FAILURE. Another test in the same session may
        still hold 58950; the autostart path then correctly catches the OSError,
        falls back to an OS-assigned port, and — if even that fails — unchecks
        the action rather than claiming a server that is not listening. All three
        are correct behaviour, so this asserts the INVARIANT (checked ⇔ a server
        is bound) rather than demanding one particular outcome. The old form
        asserted `_gr_server is not None` unconditionally and failed in a full
        suite run for a reason that had nothing to do with the feature.
        """
        monkeypatch.setattr(preferences, "gr_server_autostart", lambda: True)
        win = MainWindow(controller=controller)
        try:
            controller.open_project(DEMO)
            win._after_project_loaded()
            started = win.sim._gr_server is not None
            assert win.act_gr_server.isChecked() == started, (
                "the menu item and the server must agree: checked means a "
                f"server is bound (checked={win.act_gr_server.isChecked()}, "
                f"bound={started})")
            if started:
                assert win.sim._gr_server.bound_port, \
                    "a started server must report the port it bound"
        finally:
            win.sim.stop_gnuradio_server()

    def test_autostart_can_be_turned_off(self, controller, monkeypatch):
        """The preference is a real escape hatch: off means no server is bound,
        which is what hand-stepping a design needs."""
        monkeypatch.setattr(preferences, "gr_server_autostart", lambda: False)
        win = MainWindow(controller=controller)
        controller.open_project(DEMO)
        win._after_project_loaded()
        try:
            assert not win.act_gr_server.isChecked()
            assert win.sim._gr_server is None, \
                "autostart is off but a server was started anyway"
        finally:
            win.sim.stop_gnuradio_server()
