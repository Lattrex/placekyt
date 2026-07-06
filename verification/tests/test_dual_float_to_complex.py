# SPDX-License-Identifier: GPL-3.0-or-later
"""DualFloatToComplexBlock — structural on-chip proof of the LOCK rendezvous.

The physical block for the TWO-independent-real-producer float_to_complex case
(dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md §4): a 1-cell arbiter-LOCK
rendezvous that pairs two independent real streams (I on one face, Q on another)
into ONE complex packet, matched-pairs-only regardless of interleaving.

This test proves the block is REAL and builds correctly:
  * the catalog discovers it,
  * it places + routes + BUILDS on a 10x12 chip, and
  * the built cell's program IS the rendezvous — it contains the LOCK_FACE (CONFIG
    addr 3, mem dest 35) and LOCK (addr 4, dest 36) config writes that arm the
    lock, flip it I->Q, and re-arm.

The FUNCTIONAL adversarial-interleave proof (2 producers on 2 faces -> matched
pairs only, with mutation gates) needs a bespoke 2-producer routed topology and is
tracked separately — the block-DUT harness sends matched packets through ONE
port/face, so it cannot exercise the adversarial 2-face case.

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


def test_builds_on_chip_with_lock_rendezvous():
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

    # The built cell's program IS the rendezvous: LOCK_FACE (dest 35) is written
    # three times (arm face_i, got_i -> face_q, got_q re-arm face_i) and LOCK
    # (dest 36) once.
    blk = ctrl.project.block(d)
    c0 = blk.placement.cells[0]
    mem = bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    assert dis.count("dest: 35") == 3, f"expected 3 LOCK_FACE writes:\n{dis}"
    assert dis.count("dest: 36") == 1, f"expected 1 LOCK-enable write:\n{dis}"
