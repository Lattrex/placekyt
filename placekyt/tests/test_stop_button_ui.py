# SPDX-License-Identifier: GPL-3.0-or-later
"""The Stop toolbar button exists, is gated on an active run, and stops it.

Wires the placeKYT Stop QAction (next to Run) to SimController.stop_batch(). It
must be DISABLED when nothing is running and ENABLED while a GRC server hosts the
chip (a batch can arrive/run); triggering it must call stop_batch() and settle.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def controller(qapp, catalog):
    return AppController(catalog=catalog)


def test_stop_action_exists_and_disabled_initially(qapp, controller):
    w = MainWindow(controller=controller)
    assert hasattr(w, "act_sim_stop"), "a Stop toolbar action must exist"
    assert w.act_sim_stop.text() == "Stop"
    assert not w.act_sim_stop.isEnabled(), \
        "Stop must be disabled when nothing is running"
    w.close()


def test_stop_enabled_while_batch_active_and_calls_stop_batch(qapp, controller,
                                                              monkeypatch):
    w = MainWindow(controller=controller)

    # Pretend a GRC server batch is hostable/active.
    monkeypatch.setattr(w.sim, "batch_run_active", lambda: True)
    w._refresh_stop_enabled()
    assert w.act_sim_stop.isEnabled(), \
        "Stop must be enabled while a batch run is active"

    called = {"stop": 0, "settle": 0}
    monkeypatch.setattr(w.sim, "stop_batch",
                        lambda: called.__setitem__("stop", called["stop"] + 1))
    monkeypatch.setattr(w.sim, "settle_pending",
                        lambda *a, **k: called.__setitem__(
                            "settle", called["settle"] + 1))
    monkeypatch.setattr(w.sim, "_running", False, raising=False)

    w.act_sim_stop.trigger()
    assert called["stop"] == 1, "Stop must call SimController.stop_batch()"
    assert called["settle"] == 1, "Stop must settle the residual trace"
    w.close()


def test_stop_disabled_again_when_no_run(qapp, controller, monkeypatch):
    w = MainWindow(controller=controller)
    monkeypatch.setattr(w.sim, "batch_run_active", lambda: False)
    monkeypatch.setattr(w.sim, "_running", False, raising=False)
    w._refresh_stop_enabled()
    assert not w.act_sim_stop.isEnabled()
    w.close()
