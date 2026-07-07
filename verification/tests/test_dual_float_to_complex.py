# SPDX-License-Identifier: GPL-3.0-or-later
"""DualFloatToComplexBlock — structural on-chip proof of the phase-toggle rendezvous.

The physical block for the TWO-independent-real-producer float_to_complex case
(dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md §4): a 1-cell SINGLE-ENTRY
PHASE-TOGGLE rendezvous that pairs two independent real streams into ONE complex
sample, matched-pairs-only regardless of interleaving. Both producers JUMP one
`recv` entry; a persistent `phase` register alternates 0->1->0 (I then Q+emit). This
replaced the LOCK-by-face design, which cannot work under auto-P&R (both rails reach
the cell from the SAME face, so a face lock cannot distinguish them).

This test proves the block is REAL and builds correctly:
  * the catalog discovers it,
  * it places + routes + BUILDS on a 10x12 chip, and
  * the built cell's program IS the phase-toggle rendezvous — its `recv` entry
    compares the phase register and branches on non-zero (CMP + Branch invert) to
    pick the I vs Q arm, and its output handoff is a normal brokered WRITE+JUMP
    (no RAW_OUTPUT_HOPS), so it egresses through auto-P&R like any block.

The FUNCTIONAL end-to-end proof (complex in -> converters -> chip out, corr 1.0) is
in test_converter_flavors_grc.py's live-run path. The adversarial-interleave proof
(2 producers -> matched pairs only, with mutation gates) is tracked separately.

Run::

    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_dual_float_to_complex.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"


def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    return (BlockCatalog, load_chip_type, AppController,
            ChipPortEndpoint, BlockEndpoint)


def test_catalog_discovers_block():
    BlockCatalog, *_ = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    assert cat.get("DualFloatToComplexBlock", LIB) is not None


def test_builds_on_chip_with_phase_toggle_rendezvous():
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("d2c", ctk)
    d = ctrl.place_block("DualFloatToComplexBlock", 0, 5, 5, library=LIB, params={})
    ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                BE(block=d, port="i"), name="ni")
    ctrl.add_logical_connection(BE(block=d, port="out"),
                                CPE(chip=0, port="x16_out"), name="no")
    rep = ctrl.auto_pnr({ctk: ct}, use_bus="never")
    assert rep.ok, "DualFloatToComplex must route on one chip"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    # The built cell's program IS the phase-toggle rendezvous: its `recv` entry does
    # CMP phase, zero ; BR.NZ _q (a Cmp + a Branch{invert:true}) to pick the I/Q arm,
    # and it emits a normal brokered handoff (a WRITE + a JUMP the build patched) — NOT
    # the old LOCK_FACE (dest 35) / LOCK (dest 36) config writes.
    blk = ctrl.project.block(d)
    c0 = blk.placement.cells[0]
    mem = bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    assert "Cmp" in dis, f"phase-toggle rendezvous missing its Cmp:\n{dis}"
    assert "invert: true" in dis, f"phase-toggle missing its BR.NZ branch:\n{dis}"
    assert "Write" in dis and "Jump" in dis, f"missing output handoff:\n{dis}"
    # The old LOCK-based design is GONE (face lock can't pair same-face rails).
    assert "dest: 35" not in dis and "dest: 36" not in dis, (
        f"unexpected LOCK config writes — should be phase-toggle now:\n{dis}")
