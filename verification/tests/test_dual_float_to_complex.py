# SPDX-License-Identifier: GPL-3.0-or-later
"""DualFloatToComplexBlock — structural on-chip proof of the LOCK-by-face rendezvous.

The physical block for the TWO-independent-real-producer float_to_complex case
(dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md §4): a 1-cell LOCK-BY-FACE
rendezvous that pairs two independent, ASYNCHRONOUSLY-timed real streams into ONE
complex sample, matched-pairs-only regardless of interleaving. The two producers
arrive on TWO DISTINCT faces; the cell uses the arbiter LOCK (LOCK/LOCK_FACE) to
accept ONLY the I face, latch I, then accept ONLY the Q face, latch Q + emit. The
face IS the stream identity — a slow/bursty producer on the other face is ignored
until it is that face's turn, so async re-ordering can never mis-pair.

(A same-face phase-toggle counter was tried and is BROKEN — merging both rails onto
one serialized face destroys the stream identity; see project_dual_f2c_lock_by_face.)

This test proves the block is REAL and builds correctly:
  * the catalog discovers it,
  * it places + routes + BUILDS on a 10x12 chip, and
  * the built cell's program IS the LOCK-by-face rendezvous — it writes LOCK_FACE
    (CONFIG 3, dest 35) and LOCK (CONFIG 4, dest 36) to gate the arbiter by face, and
    its output handoff is a normal brokered WRITE+JUMP (no RAW_OUTPUT_HOPS), so it
    egresses through auto-P&R like any block.

The FUNCTIONAL end-to-end proof (complex in -> converters -> chip out, corr 1.0) is
in test_converter_flavors_grc.py's live-run path. The adversarial ASYNC-interleave
proof (2 producers on 2 faces -> matched pairs only, with mutation gates) is in
proto_dual_f2c_rendezvous.py.

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
    ctrl.add_logical_connection(BE(block=d, port="yi"),
                                CPE(chip=0, port="x16_out"), name="no")
    rep = ctrl.auto_pnr({ctk: ct}, use_bus="never")
    assert rep.ok, "DualFloatToComplex must route on one chip"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    # The built cell's program IS the LOCK-by-face rendezvous: it writes LOCK_FACE
    # (CONFIG 3 = dest 35) and LOCK (CONFIG 4 = dest 36) to gate the arbiter by face,
    # and it emits a normal brokered handoff (WRITE + JUMP the build patched). The
    # phase-toggle (Cmp + Branch{invert}) is GONE — a same-face counter can't pair
    # async streams.
    blk = ctrl.project.block(d)
    c0 = blk.placement.cells[0]
    mem = bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    assert "dest: 35" in dis, f"LOCK rendezvous missing its LOCK_FACE write:\n{dis}"
    assert "dest: 36" in dis, f"LOCK rendezvous missing its LOCK write:\n{dis}"
    assert "Write" in dis and "Jump" in dis, f"missing output handoff:\n{dis}"
    # The broken phase-toggle is GONE.
    assert "Cmp" not in dis, f"unexpected phase-toggle Cmp — should be LOCK now:\n{dis}"


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


# --------------------------------------------------------------------------- #
#  Task #438: the dual emits a 2-rail COMPLEX PACKET (yi + yq) so a GENUINE    #
#  2-input complex consumer receives BOTH rails — the Q rail is NOT lost.      #
# --------------------------------------------------------------------------- #

# Two independent real producers -> float_to_complex (-> DualFloatToComplex) ->
# a GENUINE 2-input complex block (complex mixer, pass-through) -> complex_to_float
# split -> two real sinks. The dual feeds the mixer's xi AND xq; the mixer is a real
# 2-input complex consumer (not a Q-dropping complex_to_real), so BOTH the recovered
# I and Q rails must be delivered — this is the case the single-`out` dual could not
# serve (it lost Q).
_DUAL_TO_COMPLEX_GRC = """options:
  parameters: {id: dual_to_cplx, generate_options: qt_gui}
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
- name: mix
  id: kyttar_complex_mixer
  parameters: {frequency: '0', sample_rate: '48000'}
  states: {coordinate: [460, 140], rotation: 0, state: enabled}
- name: c2f
  id: blocks_complex_to_float
  parameters: {}
  states: {coordinate: [640, 140], rotation: 0, state: enabled}
- name: snki
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [820, 100], rotation: 0, state: enabled}
- name: snkq
  id: kyttar_sink
  parameters: {device_id: '"kyttar_0"'}
  states: {coordinate: [820, 200], rotation: 0, state: enabled}
connections:
- [si, '0', f2c, '0']
- [sq, '0', f2c, '1']
- [f2c, '0', mix, '0']
- [mix, '0', c2f, '0']
- [c2f, '0', snki, '0']
- [c2f, '1', snkq, '0']
"""


def test_dual_delivers_both_rails_to_complex_consumer():
    """A DualFloatToComplex feeding a GENUINE 2-input complex block wires BOTH rails:
    dual.yi -> mixer.xi AND dual.yq -> mixer.xq. This is the whole point of the 2-rail
    complex-packet emit (#438) — the imaginary rail is NOT dropped. The importer's I/Q
    split synthesises the Q net because yi/yq (dual out) and xi/xq (mixer in) are each
    an on-cell I/Q pair. Then it must auto-P&R and BUILD (both rails route)."""
    _BlockCatalog, load_chip_type, AppController, _CPE, _BE = _engine()
    cat, res = _import_grc_text(_DUAL_TO_COMPLEX_GRC)
    assert res.ok and not res.unknown, res.unknown
    dual = next((b.name for b in res.project.blocks
                 if b.type == "DualFloatToComplexBlock"), None)
    mix = next((b.name for b in res.project.blocks
                if b.type == "ComplexMixerBlock"), None)
    assert dual and mix, [b.type for b in res.project.blocks]

    def _ep(e):
        return getattr(e, "port", None), getattr(e, "block", None)

    nets = {(c.source.block, c.source.port, c.target.block, c.target.port)
            for c in res.project.connections
            if hasattr(c.source, "block") and hasattr(c.target, "block")}
    # BOTH rails of the dual reach the mixer's two complex input regs.
    assert (dual, "yi", mix, "xi") in nets, (
        f"the recovered I rail (yi) must feed mixer.xi; nets={sorted(nets)}")
    assert (dual, "yq", mix, "xq") in nets, (
        f"the recovered Q rail (yq) must feed mixer.xq — the 2-rail emit's whole "
        f"purpose is that Q is NOT lost; nets={sorted(nets)}")

    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({ctk: ct}).ok, "dual->complex chain did not route (both rails)"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)


def test_dual_program_emits_two_output_writes():
    """STRUCTURAL proof the built dual emits a 2-rail complex PACKET, not a single rail:
    its `recv` program's emit arm has TWO Write instructions (yi then yq) plus the
    trigger Jump. A single-`out` dual (the pre-#438 design) had ONE Write and would drop
    Q at a genuine complex consumer. Counting the Writes in the phase-toggle cell is the
    load-bearing distinction."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("d2c2", ctk)
    d = ctrl.place_block("DualFloatToComplexBlock", 0, 5, 5, library=LIB, params={})
    # Two real chip-input feeds (i, q) and a complex-consuming egress is not needed for
    # the structural count; wire i/q in and the yi rail out so the block places+builds.
    ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                BE(block=d, port="i"), name="ni")
    ctrl.add_logical_connection(BE(block=d, port="yi"),
                                CPE(chip=0, port="x16_out"), name="no")
    assert ctrl.auto_pnr({ctk: ct}, use_bus="never").ok
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    blk = ctrl.project.block(d)
    c0 = blk.placement.cells[0]
    mem = bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    n_write = dis.count("Write")
    assert n_write >= 2, (
        f"the 2-rail dual must emit TWO Writes (yi + yq); got {n_write}:\n{dis}")
    assert "Jump" in dis, f"missing the downstream trigger Jump:\n{dis}"
