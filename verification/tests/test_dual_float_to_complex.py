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


# --------------------------------------------------------------------------- #
#  Task #429: the importer auto-inserts the DualFloatToComplex block from a    #
#  RAW 2-real `float_to_complex` .grc — no special demo scaffolding.           #
# --------------------------------------------------------------------------- #

_TWO_REAL_GRC = """options:
  parameters: {id: min_dual, generate_options: qt_gui}
  states: {coordinate: [8, 8], rotation: 0, state: enabled}
blocks:
- name: si
  id: kyttar_source
  parameters: {device_id: '"kyttar_0"', complex_in: 'False'}
  states: {coordinate: [100, 100], rotation: 0, state: enabled}
- name: sq
  id: kyttar_source
  parameters: {device_id: '"kyttar_0"', complex_in: 'False'}
  states: {coordinate: [100, 200], rotation: 0, state: enabled}
- name: f2c
  id: blocks_float_to_complex
  parameters: {}
  states: {coordinate: [300, 140], rotation: 0, state: enabled}
- name: snk
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [500, 140], rotation: 0, state: enabled}
connections:
- [si, '0', f2c, '0']
- [sq, '0', f2c, '1']
- [f2c, '0', snk, '0']
"""

# The SINGLE-real (mutation) variant: the Q input (port 1) is a null_source, so the f2c
# is a LOGICAL-ONLY converter — the importer must splice it to ZERO cells, NOT place a
# DualFloatToComplex. If a dual is placed here, the 2-real detection is over-eager.
_SINGLE_REAL_GRC = """options:
  parameters: {id: min_single, generate_options: qt_gui}
  states: {coordinate: [8, 8], rotation: 0, state: enabled}
blocks:
- name: si
  id: kyttar_source
  parameters: {device_id: '"kyttar_0"', complex_in: 'False'}
  states: {coordinate: [100, 100], rotation: 0, state: enabled}
- name: nq
  id: blocks_null_source
  parameters: {}
  states: {coordinate: [100, 200], rotation: 0, state: enabled}
- name: f2c
  id: blocks_float_to_complex
  parameters: {}
  states: {coordinate: [300, 140], rotation: 0, state: enabled}
- name: snk
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [500, 140], rotation: 0, state: enabled}
connections:
- [si, '0', f2c, '0']
- [nq, '0', f2c, '1']
- [f2c, '0', snk, '0']
"""


def _import_grc_text(text):
    import tempfile
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    with tempfile.NamedTemporaryFile("w", suffix=".grc", delete=False) as tf:
        tf.write(text)
        path = tf.name
    try:
        return cat, import_grc(path, cat, chip_type="kyttar_10x12")
    finally:
        os.unlink(path)


def test_importer_auto_inserts_dual_from_two_real_f2c():
    """A RAW `float_to_complex` fed by TWO independent real producers (no null_source on
    Q) auto-inserts EXACTLY ONE DualFloatToComplexBlock — no cell for the (logical) f2c
    itself — then auto-P&Rs and BUILDS. This is the general importer path (task #429):
    it does NOT require the converter_flavors scaffolding, just a bare 2-real f2c."""
    _BlockCatalog, load_chip_type, AppController, _CPE, _BE = _engine()
    cat, res = _import_grc_text(_TWO_REAL_GRC)
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    assert types == ["DualFloatToComplexBlock"], (
        f"a 2-real f2c must place exactly ONE DualFloatToComplex (the f2c adds no cell "
        f"of its own); got {types}")
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({ctk: ct}).ok, "2-real f2c did not route under auto-P&R"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)


def test_importer_single_real_f2c_places_no_dual():
    """MUTATION / boundary gate (INV-4): a `float_to_complex` whose Q input is a
    null_source is a LOGICAL-ONLY converter — the importer must splice it to ZERO cells,
    NOT place a DualFloatToComplex. This proves the 2-real detection is not over-eager
    (it keys on TWO real producers, not merely on the f2c block existing)."""
    _BlockCatalog, _load_chip_type, _AppController, _CPE, _BE = _engine()
    _cat, res = _import_grc_text(_SINGLE_REAL_GRC)
    assert res.ok and not res.unknown, res.unknown
    types = [b.type for b in res.project.blocks]
    assert "DualFloatToComplexBlock" not in types, (
        f"single-real (null_source Q) f2c must NOT place a dual; got {types}")
