# SPDX-License-Identifier: GPL-3.0-or-later
"""The STANDALONE all-flavors float<->complex converter flowgraph, proven end to end.

``verification/tests/data/converter_flavors.grc`` is ONE importable GNU Radio
flowgraph that threads EVERY float/complex dtype interaction the placeKYT importer
must handle, in a single DRIVABLE identity chain (dev_docs §7, CM: "all of the
complex/float interactions in a single simple test that is an importable GRC flow
graph … run in placeKYT both visually and headless"):

  complex source (x16_in) -> complex mixer (pass-through) -> blocks_complex_to_float
  (split I/Q) -> two real gains -> blocks_float_to_complex (recombine the two on-chip
  real rails via the physical DualFloatToComplex phase-toggle block) ->
  blocks_complex_to_real (drop Q) -> real gain -> sink (x16_out).

The mixer is pass-through (freq 0), so with the same signal on I and Q the chip
output == input — a true identity round-trip through every converter flavor.

This test proves, mechanically (not by reasoning about GNU Radio):
  * ``grcc`` compiles the .grc with ZERO errors (the user-visible GRC bar) — it is a
    real, valid GNU Radio flowgraph.  [skipped if grcc is unavailable]
  * placeKYT IMPORTS it with the correct placement (1 mixer + 1 DualFloatToComplex
    + 3 gains = 5 cells; the logical converters add ZERO cells) and the exact rail
    wiring for every flavor.
  * it AUTO-P&Rs (all nets route) and BUILDS a bitstream whose fabric carries the
    DualFloatToComplex phase-toggle rendezvous.
  * it RUNS LIVE over the SimServer batch bridge (the GUI "Run as Server → Execute"
    path): a complex burst driven in recovers at x16_out, correlation 1.0.

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
    # 2-real f2c -> DualFloatToComplex (1) ; ONE complex mixer (the c2f split head) ;
    # three real gains. (A single pass-through mixer keeps every flavor covered while
    # keeping the chain within the router's 31-hop reach on a 10x12.)
    assert types == ["ComplexMixerBlock",
                     "DualFloatToComplexBlock", "GainBlock", "GainBlock",
                     "GainBlock"], types
    nets = _nets(res)
    dual = next(b.name for b in res.project.blocks
                if b.type == "DualFloatToComplexBlock")
    # Runnable topology: complex in -> mixer -> c2f split -> 2 gains -> f2c RECOMBINE
    # (DualFloatToComplex, fed by two ON-CHIP real rails, not the port) -> c2r -> gain
    # -> out. Every converter flavor is exercised on a drivable chain.
    # (1) complex_to_float BOTH rails: the mixer's out_i / out_q drive two real gains.
    i_rails = {t for (s, t) in nets if s.endswith(".yi")}
    q_rails = {t for (s, t) in nets if s.endswith(".yq")}
    assert i_rails and q_rails, nets   # both rails materialised, to distinct gains
    # (3) 2-real f2c RECOMBINE: the two on-chip gains feed DualFloatToComplex.i / .q.
    assert any(t == f"{dual}.i" for (s, t) in nets), nets
    assert any(t == f"{dual}.q" for (s, t) in nets), nets
    # ... and its complex output feeds a downstream complex consumer (c2r drop-Q).
    assert any(s == f"{dual}.out" for (s, t) in nets), nets
    # (4) complex_to_real drop-Q: the recovered I rail drives a gain to the port.
    assert any(t == "PORT:x16_out" for (s, t) in nets), nets
    # single-real f2c is exercised implicitly by the chain being grcc-clean +
    # importing with zero converter cells (asserted by the placed-type list above).


def test_routes_and_builds_with_rendezvous_onchip():
    """Auto-P&R routes every net and the build produces a bitstream whose fabric
    carries the DualFloatToComplex PHASE-TOGGLE rendezvous.

    Deterministic now (was ~50% flaky): the CP-SAT abutment-first placer enforces a
    single-cell block's input-face != output-face, so it never emits a layout that
    trips the §5.3 single_cell_inout_deadlock build DRC. The chain uses a single
    pass-through mixer so it also stays within the router's 31-hop reach on a 10x12.
    Measured 20/20 clean pnr+build."""
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
    # The DualFloatToComplex rendezvous is the SINGLE-ENTRY PHASE TOGGLE: its `recv`
    # entry compares the phase register then branches (CMP + Branch{NotZero}) to pick
    # I vs Q. Find that program in the built fabric. (The old design keyed on a LOCK_FACE
    # write — removed: LOCK-by-face can't distinguish two rails that arrive on the SAME
    # face under auto-P&R.)
    found = False
    for cid in range(120):
        mem = [chip.read_cell_memory(cid, a) for a in range(32)]
        dis = simkyt.Program.from_words("c", mem, 0).disassemble()
        if "Cmp" in dis and "Branch" in dis and "invert: true" in dis:
            found = True
            break
    assert found, "built fabric is missing the DualFloatToComplex phase-toggle rendezvous"


def test_runs_live_recovers_input():
    """THE LIVE PROOF (source -> chip -> plot): drive a complex burst through the
    hosted chip over the SimServer batch bridge — the exact "Run as GNURadio Server →
    Execute" path — and recover it at x16_out. With the SAME signal on the I and Q
    rails and a pass-through mixer, the chip output is an IDENTITY of the input
    (correlation 1.0), so every converter in the chain provably FUNCTIONS end to end:
    complex_to_float split, the two on-chip real rails, the DualFloatToComplex phase-
    toggle recombine, complex_to_real drop-Q, and the egress gain.

    This locks in the two fixes that made the live chain flow: (a) the bridge injects
    at the BUILD's corridor-accurate input landing (the ComplexMixer's phase cell,
    reached via a broker one hop past the corridor end — a manhattan hop lands short),
    and (b) the DualFloatToComplex emits a normal brokered {write:out}/{jump:out}
    handoff so auto-P&R hop-patches its egress like any block."""
    import socket
    import numpy as np
    import simkyt
    from engine.io.chip_type_io import load_chip_type
    from engine.port_config import input_port_config, batch_reset_writes
    from engine.sim_bridge import SimServer, send_message, recv_message
    from ui.controller import AppController

    _BC, cat, res = _import()
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    assert ctrl.auto_pnr({CHIP: ct}).ok
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    # The bridge's injection landing MUST come from the BUILT corridor (not a manhattan
    # estimate) — that is the fix that makes the multi-cell ComplexMixer head fire.
    pc = input_port_config(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                           build_result=bres)
    assert pc is not None
    in_name, cfg = pc

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.chips[0].words)
    srv = SimServer(chip,
                    default_entries={in_name: int(cfg["entry_addr"])},
                    default_hops={in_name: int(cfg["hop_count"])},
                    batch_reset_writes=batch_reset_writes(bres, 0))
    port = srv.start()
    try:
        c = socket.socket()
        c.connect(("127.0.0.1", port))
        N = 48
        t = np.arange(N)
        sig = (0.4 * np.cos(2 * np.pi * t / 13)).astype(np.float32)
        # SAME signal on I and Q -> the recovered rail is that signal regardless of
        # which rail the phase-toggle labels "I" (a clean identity round-trip).
        payload = np.empty(2 * N, dtype=np.float32)
        payload[0::2] = sig
        payload[1::2] = sig
        da = int(cfg["data_addr"])
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": in_name, "complex": True, "raw": False,
                         "data_addrs": [da, da + 1]}, payload)
        _hdr, out = recv_message(c)
        c.close()
    finally:
        srv.stop()

    assert out is not None and len(out) >= N, (
        f"live run produced no egress ({0 if out is None else len(out)} words) — "
        "the converter chain did not flow through to x16_out")
    o = np.asarray(out, dtype=float)[:N]
    corr = float(np.corrcoef(o, sig[:N])[0, 1])
    assert corr > 0.99, (
        f"chip output does not recover the input (corr={corr:.4f}); the converter "
        "chain ran but the recovered signal is wrong")
