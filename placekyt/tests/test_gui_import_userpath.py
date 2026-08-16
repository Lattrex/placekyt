# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI import-dialog USER PATH gate (offscreen Qt) — the REAL handler, not a mirror.

Regression for the QPSK import failure that only the GUI path exhibited:
``MainWindow._import_grc`` ran a free-standing ``auto_place`` BEFORE
``auto_pnr``, so auto_pnr snapshotted the already-packed layout as its VIRGIN
geometry and the position-dependent serpentine planner derived overlapping /
off-grid plans from it on every attempt ("blocks 'complexcostasloop' and
'gardnertimingrecovery' overlap at cell (6,1)") — while a controller-level
``auto_pnr``-only harness routed the same design 16/16. The lesson: the gate
must execute the GUI handler itself, because a hand-rolled mirror of its
sequence is exactly what let the pre-place drift in unnoticed.

This drives ``MainWindow._import_grc`` verbatim, stubbing ONLY the modal
inputs (file picker, import-options dialog, discard confirm, message boxes),
and asserts the QPSK modem import places and routes every net.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

GRC = Path(__file__).resolve().parents[2] / "examples" / "qpsk_modem" / "qpsk_modem.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC.exists()), reason="chip yaml / qpsk .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_import_grc_full_pnr_userpath(qapp, monkeypatch):
    """File > Import GRC > 'Full place-and-route' on the QPSK modem must route
    every net through the handler's real sequence (import -> auto_pnr, NO
    pre-place). Failure dialogs are captured and FAIL the test with their text."""
    win = MainWindow()
    errors: list[str] = []
    monkeypatch.setattr(win, "_confirm_discard", lambda: True)
    monkeypatch.setattr(win, "_ask_import_options",
                        lambda: {"route": True, "use_bus": "always"})
    monkeypatch.setattr(win, "_error",
                        lambda title, msg: errors.append(f"{title}: {msg}"))
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(GRC), "GNURadio flowgraphs (*.grc)")))
    # An unmapped-blocks warning is informational; don't let a modal block the run.
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

    win._import_grc()

    assert not errors, f"GUI import surfaced an error dialog: {errors}"
    conns = win.controller.project.connections
    assert conns, "import produced no logical nets"
    unrouted = [c.name for c in conns if not c.is_routed]
    assert not unrouted, (
        f"user-path import left {len(unrouted)}/{len(conns)} nets unrouted: "
        f"{unrouted}")
    win.close()
