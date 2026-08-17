# SPDX-License-Identifier: GPL-3.0-or-later
"""Selecting a block lights its WHOLE cross-chip stream path (#22).

On a multi-chip board (gain_2p2s: two serial pairs), a stream's physical path
spans chips: the far-die input rides the near die's transparent-wire transit
corridor and the inter-chip wire before it ever reaches its block, and the near
die's output transits the far die to the board output. Selecting a gain cell
must highlight ONE continuous path:

  * every model connection in the stream's chain (related-highlight, #266),
  * every inter-chip wire the stream crosses (new related state on the wire),
  * overlay polylines for the synthesized segments that have no design-level
    route — the transparent-wire transit corridors and the far-die delivery.

Driven on the REAL MainWindow open path against the shipped example."""
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

_KYT = _ROOT / "examples" / "gain_2p2s" / "gain_2p2s.kyt"

pytestmark = pytest.mark.skipif(not _KYT.exists(), reason="example kyt absent")


@pytest.fixture()
def win():
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.controller.open_project(str(_KYT))
    w._after_project_loaded()
    app.processEvents()
    yield w
    app.processEvents()


def _select_block_cell(win, label: str):
    from PySide6.QtWidgets import QApplication

    canvas = win.canvas
    canvas._scene.clearSelection()
    for it in canvas.cell_items():
        if it.label == label:
            it.setSelected(True)
            break
    else:
        raise AssertionError(f"no cell item labeled {label!r}")
    QApplication.processEvents()
    return canvas


def _related_conn_names(canvas) -> set:
    return {it.connection_name for it in canvas.connection_items()
            if it.is_related and it.connection_name}


def _related_wires(canvas) -> list:
    return [w for w in getattr(canvas, "_wire_items", []) if w.is_related]


def test_far_die_gain_highlights_full_input_path(win):
    """gain_1 lives on chip 1 but is fed from chip 0's x16_in: selecting it must
    light its input connection, its egress connection, the chip0→chip1 wire, AND
    overlay the chip-0 transit corridor + the chip-1 delivery segment."""
    canvas = _select_block_cell(win, "gain_1")

    names = _related_conn_names(canvas)
    assert "x16_in_to_gain_1" in names, names
    assert "gain_1_to_x16_out" in names, names

    wires = _related_wires(canvas)
    assert any(w.inter_chip.from_chip == 0 and w.inter_chip.to_chip == 1
               for w in wires), "the chip0→chip1 wire is not highlighted"

    overlays = getattr(canvas, "_xchip_overlay_items", [])
    assert overlays, ("no transit/delivery overlay drawn — the far-die input "
                      "path renders as a broken fly line again")


def test_near_die_gain_highlights_egress_transit(win):
    """gain (chip 0) egresses to chip0.x16_out → wire → chip1, then transits
    chip 1 transparently to the board output: the wire must light and the chip-1
    transit corridor must be overlaid."""
    canvas = _select_block_cell(win, "gain")

    names = _related_conn_names(canvas)
    assert "gain_to_x16_out" in names, names
    assert "x16_in_to_gain" in names, names

    wires = _related_wires(canvas)
    assert any(w.inter_chip.from_chip == 0 and w.inter_chip.to_chip == 1
               for w in wires), "the chip0→chip1 wire is not highlighted"

    overlays = getattr(canvas, "_xchip_overlay_items", [])
    assert overlays, "no chip-1 egress transit overlay drawn"
    # The stream never touches the second serial pair (chips 2/3).
    assert not any(w.inter_chip.from_chip == 2 for w in _related_wires(canvas))


def test_deselect_clears_cross_chip_highlight(win):
    from PySide6.QtWidgets import QApplication

    canvas = _select_block_cell(win, "gain_1")
    assert getattr(canvas, "_xchip_overlay_items", [])
    canvas._scene.clearSelection()
    QApplication.processEvents()
    assert not _related_conn_names(canvas)
    assert not _related_wires(canvas)
    assert not getattr(canvas, "_xchip_overlay_items", [])


def test_other_pair_stays_dark(win):
    """Selecting gain_3 (the second serial pair) must not light pair-1 wires."""
    canvas = _select_block_cell(win, "gain_3")
    names = _related_conn_names(canvas)
    assert "x16_in_to_gain_3" in names and "gain_3_to_x16_out" in names, names
    wires = _related_wires(canvas)
    assert wires and all(w.inter_chip.from_chip == 2 for w in wires), (
        [(w.inter_chip.from_chip, w.inter_chip.to_chip) for w in wires])
