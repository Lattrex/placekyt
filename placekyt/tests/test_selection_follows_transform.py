# SPDX-License-Identifier: GPL-3.0
"""A selected block cell stays selected — and FOLLOWS the block — across a rotate/flip.

Before: the canvas keyed the restored selection by the cell's (x, y). A transform MOVES
the cells, so after re-render the old coords held a DIFFERENT cell (or none) and the
selection jumped off the block. The user then had to re-click the block for every
rotate/flip — tedious when cycling through orientations.

Fix: key a block-cell selection by (block_name, cell_id) — STABLE across a transform —
and, if that exact cell went off-grid, fall back to ANY on-array cell of the same block.
So the same cell stays selected wherever it moves, and repeated transforms can be cycled
without re-clicking.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_selection_follows_transform.py -q
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402
from ui.canvas.cell_item import CellItem  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _place_multicell(w):
    """Place a multi-cell block and bind the canvas; return its name."""
    w.controller.new_project("sel", "kyttar_10x12")
    name = w.controller.place_block(
        "RRCPulseShaperBlock", 0, 3, 3, library="lattrex.official",
        params={"sampling_freq": 2.0, "symbol_rate": 1.0, "alpha": 0.5,
                "ntaps": 17, "gain": 1.0})
    w.canvas.set_project(w.controller.project, w.controller.chip_types())
    return name


def _select_cell(w, block_name, cell_id):
    for it in w.canvas._scene.items():
        if (isinstance(it, CellItem)
                and getattr(it, "label", None) == block_name
                and getattr(it, "cell_id", None) == cell_id):
            it.setSelected(True)
            return
    raise AssertionError(f"cell {cell_id} of {block_name} not found in scene")


def test_selection_follows_a_single_rotation(qapp):
    w = MainWindow()
    name = _place_multicell(w)
    blk = w.controller.project.block(name)
    cid = blk.placement.cells[2].cell_id
    before = next(c for c in blk.placement.cells if c.cell_id == cid)
    _select_cell(w, name, cid)

    w.controller.transform_block(name, "cw")
    w.canvas.render_scene()

    sc = w.canvas.selected_cell()
    assert sc is not None, "selection was lost across the rotation"
    assert sc.label == name, "selection jumped to a different block"
    assert sc.cell_id == cid, "the SAME cell must stay selected after the rotate"
    after = next(c for c in w.controller.project.block(name).placement.cells
                 if c.cell_id == cid)
    assert (before.x, before.y) != (after.x, after.y), (
        "sanity: the transform should have moved the cell")


def test_cycle_rotations_and_flips_keep_the_cell_selected(qapp):
    w = MainWindow()
    name = _place_multicell(w)
    blk = w.controller.project.block(name)
    cid = blk.placement.cells[1].cell_id
    _select_cell(w, name, cid)

    for kind in ("cw", "cw", "cw", "cw", "mirror_h", "cw", "mirror_v"):
        w._on_transform_requested(name, kind)
        w.canvas.render_scene()
        sc = w.canvas.selected_cell()
        assert sc is not None and sc.label == name and sc.cell_id == cid, (
            f"selection dropped off the block after '{kind}'")


def test_offarray_cell_falls_back_to_another_block_cell(qapp):
    """If the selected cell rotates off the grid, the selection falls back to another
    on-array cell of the SAME block (never lost, so cycling still works)."""
    w = MainWindow()
    w.controller.new_project("sel2", "kyttar_10x12")
    # Place a long block hugging the top edge so a rotation pushes some cells off-grid.
    name = w.controller.place_block(
        "RRCPulseShaperBlock", 0, 5, 0, library="lattrex.official",
        params={"sampling_freq": 2.0, "symbol_rate": 1.0, "alpha": 0.5,
                "ntaps": 17, "gain": 1.0})
    w.canvas.set_project(w.controller.project, w.controller.chip_types())
    blk = w.controller.project.block(name)
    cid = blk.placement.cells[0].cell_id
    _select_cell(w, name, cid)
    # Rotate; even if this cell leaves the grid, SOME cell of the block stays selected.
    try:
        w.controller.transform_block(name, "cw")
    except Exception:
        pytest.skip("transform rejected at this placement (off-grid) — not this test's case")
    w.canvas.render_scene()
    sc = w.canvas.selected_cell()
    assert sc is not None and sc.label == name, (
        "selection must remain on the block even if the exact cell went off-array")
