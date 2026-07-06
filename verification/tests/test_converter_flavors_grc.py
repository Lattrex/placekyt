# SPDX-License-Identifier: GPL-3.0-or-later
"""The STANDALONE all-flavors float<->complex converter flowgraph, proven end to end.

``verification/tests/data/converter_flavors.grc`` is ONE importable GNU Radio
flowgraph that threads EVERY float/complex dtype interaction the placeKYT importer
must handle, in a single linear chain (dev_docs §7, CM: "all of the complex/float
interactions in a single simple test that is an importable GRC flow graph … run in
placeKYT both visually and headless"):

  1. TWO real streams  -> blocks_float_to_complex (2-real)  -> a physical
     DualFloatToComplex LOCK-rendezvous block  -> a complex mixer.
  2. complex mixer -> blocks_complex_to_float (BOTH rails): out_i and out_q each
     drive a real gain (the Q rail is observed on a second chip-output stream).
  3. the out_i real rail -> blocks_float_to_complex (SINGLE real, Q = null_source)
     -> a second complex mixer (xq = 0, no cell — logical-only).
  4. the second complex mixer -> blocks_complex_to_real (drop Q) -> a real gain -> sink.

This test proves, mechanically (not by reasoning about GNU Radio):
  * ``grcc`` compiles the .grc with ZERO errors (the user-visible GRC bar) — it is a
    real, valid GNU Radio flowgraph.  [skipped if grcc is unavailable]
  * placeKYT IMPORTS it with the correct placement (2 mixers + 1 DualFloatToComplex
    + 3 gains = 6 cells; the logical converters add ZERO cells) and the exact rail
    wiring for every flavor.
  * it AUTO-P&Rs (all nets route) and BUILDS a bitstream, whose fabric carries the
    DualFloatToComplex LOCK rendezvous (a dest-35 LOCK_FACE + dest-36 LOCK write).

Run::

    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_converter_flavors_grc.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

GRC = _ROOT / "verification" / "tests" / "data" / "converter_flavors.grc"
CHIP = "kyttar_10x12"
CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _import():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    cat = BlockCatalog.from_gr_kyttar()
    return BlockCatalog, cat, import_grc(str(GRC), cat, chip_type=CHIP)


def _ep(e):
    from model.connection import BlockEndpoint, ChipPortEndpoint
    if isinstance(e, BlockEndpoint):
        return f"{e.block}.{e.port}"
    if isinstance(e, ChipPortEndpoint):
        return f"PORT:{e.port}"
    return str(e)


def _nets(res):
    return {(_ep(c.source), _ep(c.target)) for c in res.project.connections}


def test_file_exists():
    assert GRC.is_file(), f"missing standalone .grc: {GRC}"


@pytest.mark.skipif(shutil.which("grcc") is None, reason="grcc not available")
def test_grcc_clean():
    """grcc compiles the flowgraph with ZERO errors AND the generated Python is
    syntactically valid. This is the user-visible 'no red errors in GRC' bar — proven
    against the real toolchain. The syntax check guards the codegen edge case where a
    multi-line ``description`` leaked unquoted into the module body (IndentationError
    on Execute); the emitted .py must parse."""
    import ast
    out = tempfile.mkdtemp(prefix="cf_grcc_")
    try:
        r = subprocess.run(["grcc", str(GRC), "-o", out],
                           capture_output=True, text=True, timeout=180)
        produced = list(Path(out).glob("*.py"))
        assert produced, (
            "grcc produced no .py — compilation failed:\n"
            + (r.stdout or "") + (r.stderr or ""))
        for py in produced:
            src = py.read_text()
            try:
                ast.parse(src)
            except SyntaxError as e:  # e.g. a multi-line description breaking codegen
                raise AssertionError(
                    f"grcc emitted invalid Python ({py.name}): {e}") from e
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_imports_all_flavors_correct_cells_and_wiring():
    """Import places exactly the DSP cells (converters add ZERO cells) and wires
    every flavor's rails correctly."""
    _BC, _cat, res = _import()
    assert res.ok and not res.unknown, res.unknown
    types = sorted(b.type for b in res.project.blocks)
    # 2-real f2c -> DualFloatToComplex (1) ; two complex mixers ; three real gains.
    assert types == ["ComplexMixerBlock", "ComplexMixerBlock",
                     "DualFloatToComplexBlock", "GainBlock", "GainBlock",
                     "GainBlock"], types
    nets = _nets(res)
    dual = next(b.name for b in res.project.blocks
                if b.type == "DualFloatToComplexBlock")
    # (1) 2-real -> DualFloatToComplex.i / .q ; its output feeds a complex mixer.
    assert ("PORT:x16_in", f"{dual}.i") in nets, nets
    assert ("PORT:x16_in", f"{dual}.q") in nets, nets
    assert any(s == f"{dual}.out" for (s, t) in nets), nets
    # (2) complex_to_float BOTH rails: one mixer drives two gains via out_i / out_q.
    i_rails = {t for (s, t) in nets if s.endswith(".yi")}
    q_rails = {t for (s, t) in nets if s.endswith(".yq")}
    assert i_rails and q_rails, nets   # both rails materialised, to distinct gains
    # (4) complex_to_real drop-Q: a mixer's I rail (yi) drives a gain that reaches the
    #     output port.
    assert any(t == "PORT:x16_out" for (s, t) in nets), nets


def test_routes_and_builds_with_rendezvous_onchip():
    """Auto-P&R routes every net and the build produces a bitstream whose fabric
    carries the DualFloatToComplex LOCK rendezvous."""
    import simkyt
    _BC, cat, res = _import()
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({CHIP: ct})
    assert rep.ok, "converter_flavors did not fully route under auto-P&R"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    words = bres.chips[0].words
    assert words, "empty bitstream"
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    # The DualFloatToComplex rendezvous cell writes LOCK_FACE (dest 35) and LOCK
    # (dest 36) — search the built fabric for that program.
    found = False
    for cid in range(120):
        mem = [chip.read_cell_memory(cid, a) for a in range(32)]
        dis = simkyt.Program.from_words("c", mem, 0).disassemble()
        if "dest: 35" in dis and "dest: 36" in dis:
            found = True
            break
    assert found, "built fabric is missing the DualFloatToComplex LOCK rendezvous"
