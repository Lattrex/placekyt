# SPDX-License-Identifier: GPL-3.0-or-later
"""Bug B: Placement.transform must CANONICALISE the stored ``orientation`` D4 stack.

A user nudging a block's orientation by hand builds a redundant transform history
(e.g. mirror -> rotate -> un-rotate -> un-mirror = identity). Left un-reduced, the
build re-applies that whole stack op-by-op to the block's in-program FACE constants —
a latent mis-face bug. transform() now canonicalises ``orientation`` to the shortest
D4-equivalent after each op. These tests prove:

  * the canonicaliser reduces any sequence to its minimal form with the IDENTICAL net
    face permutation (so the build's ``face_code_after`` is unchanged);
  * transform() stores the canonical form (the SSB regression: [mirror_v,cw,ccw,
    mirror_v] -> []);
  * canonicalisation NEVER changes the resulting cell coordinates or faces (only the
    stored history shrinks — the geometry is already applied in-place per op).
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

from model.enums import Face, _FACE_CODE, face_code_after  # noqa: E402
from model.placement import (  # noqa: E402
    PlacedCell, Placement, canonicalize_orientation)

_OPS = ("cw", "ccw", "mirror_h", "mirror_v")


def _net_perm(kinds):
    order = (Face.SOUTH, Face.EAST, Face.WEST, Face.NORTH)
    out = list(order)
    for k in kinds:
        out = [{"cw": f.rotated_cw, "ccw": f.rotated_ccw,
                "mirror_h": f.mirrored_h, "mirror_v": f.mirrored_v}[k] for f in out]
    return tuple(f.value for f in out)


def test_canon_reduces_ssb_regression_to_identity():
    """The exact SSB hand-place stack collapses to nothing (bug B repro)."""
    assert canonicalize_orientation(["mirror_v", "cw", "ccw", "mirror_v"]) == []


def test_canon_preserves_net_face_perm_for_all_sequences_up_to_len4():
    """For EVERY op sequence up to length 4, the canonical form has the identical net
    face permutation (so the build's per-op face-constant map is unchanged), and is no
    longer than the original."""
    for n in range(0, 5):
        for seq in itertools.product(_OPS, repeat=n):
            seq = list(seq)
            canon = canonicalize_orientation(seq)
            assert _net_perm(canon) == _net_perm(seq), (seq, canon)
            assert len(canon) <= len(seq)
            # face_code_after (what the build uses) must also match for every face.
            for f in Face:
                code = _FACE_CODE[f]
                assert face_code_after(code, canon) == face_code_after(code, seq)


def test_canon_is_idempotent_and_minimal():
    """Canonicalising a canonical form is a fixed point, and the result is the SHORTEST
    op list for its D4 element (≤2 ops: D4's diameter over these 4 generators — a mirror
    axis + a rotation covers every element)."""
    for n in range(0, 6):
        for seq in itertools.product(_OPS, repeat=n):
            c = canonicalize_orientation(list(seq))
            assert canonicalize_orientation(c) == c            # idempotent
            assert len(c) <= 2, c                               # genuinely minimal


def _sample_placement():
    # An L-shaped 3-cell block with distinct faces, off-origin.
    return Placement(chip=0, cells=[
        PlacedCell(cell_id="a", x=3, y=2, face=Face.EAST),
        PlacedCell(cell_id="b", x=4, y=2, face=Face.NORTH),
        PlacedCell(cell_id="c", x=4, y=3, face=Face.SOUTH),
    ])


def test_transform_stores_canonical_history():
    """transform() records the canonical (shortest) orientation, not the raw stack."""
    pl = _sample_placement()
    for k in ("mirror_v", "cw", "ccw", "mirror_v"):
        pl.transform(k)
    assert pl.orientation == [], pl.orientation  # net identity


def test_transform_geometry_unaffected_by_canonicalisation():
    """Canonicalising the HISTORY must not change WHERE cells land or their faces:
    a net-identity op sequence returns every cell to its exact start pose."""
    pl = _sample_placement()
    start = [(c.cell_id, c.x, c.y, c.face) for c in pl.cells]
    for k in ("mirror_v", "cw", "ccw", "mirror_v"):   # net identity
        pl.transform(k)
    end = [(c.cell_id, c.x, c.y, c.face) for c in pl.cells]
    assert end == start, (start, end)


def test_four_cw_rotations_return_to_start_and_clear_history():
    """4× cw = identity: cells return to start AND the stored orientation is empty."""
    pl = _sample_placement()
    start = [(c.cell_id, c.x, c.y, c.face) for c in pl.cells]
    for _ in range(4):
        pl.transform("cw")
    assert pl.orientation == []
    assert [(c.cell_id, c.x, c.y, c.face) for c in pl.cells] == start
