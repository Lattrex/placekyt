# SPDX-License-Identifier: GPL-3.0-or-later
"""Block I/O cell markers cover EVERY block, both id schemes, both roles.

User-reported on the psk31 transceiver: the RaisedCosineEnvelope showed no
input/output borders at all (its PortMap names cells 'ingest'/'shape' while
the .kyt placement stores positional ids — the named-cell resolution gap),
and the Varicode encoder's emit cell showed output-only when it is BOTH the
block's byte input and its bit output (the input role was silently dropped).
Single-cell blocks showed nothing (skipped outright).

Now: named ids resolve via the provider's positional bridge; a dual-role cell
gets the combined 'inout' marker; single-cell blocks are marked too."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_KYT = _ROOT / "examples" / "psk31_transceiver" / "psk31_transceiver.kyt"

pytestmark = pytest.mark.skipif(not _KYT.exists(), reason="example kyt absent")


@pytest.fixture(scope="module")
def roles():
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.controller.open_project(str(_KYT))
    win._after_project_loaded()
    app.processEvents()
    out: dict = {}
    for it in win.canvas.cell_items():
        if it.kind.name == "BLOCK" and it.io_role:
            out.setdefault(it.label, {})[(it.cx, it.cy)] = it.io_role
    return out


def test_named_cell_block_gets_both_roles(roles):
    """RaisedCosineEnvelope (named cells 'ingest'/'shape', positional .kyt ids)
    must show a distinct input cell AND a distinct output cell."""
    rce = roles.get("raisedcosineenvelope", {})
    assert "input" in rce.values(), rce
    assert "output" in rce.values(), rce


def test_fused_in_out_cell_shows_combined_marker(roles):
    """The Varicode encoder's emit cell carries the byte input AND the bit
    output — it must show the combined marker, not output-only."""
    enc = roles.get("varicodeencoder", {})
    assert "inout" in enc.values(), enc
    assert "input" in enc.values(), enc  # the controller cell keeps its input


def test_single_cell_blocks_are_marked(roles):
    """Every single-cell DSP block shows the combined in+out marker (they were
    previously skipped and showed nothing)."""
    for name in ("diffencoder", "diffdecoder", "psksymbolmapper",
                 "bpskslicer", "repeat"):
        assert roles.get(name, {}), f"{name} has no I/O marker at all"
        assert "inout" in roles[name].values(), (name, roles[name])
