# SPDX-License-Identifier: GPL-3.0-or-later
"""TMR-pipeline example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

Two co-resident streams on ONE 10x12 array, one run:

  tmr  : ramp -> StreamSplitter -> 3x (identity worker -> AddConst injector
         0/f/0) -> TMRVoter -> [value, status] packets on x16_out
  solo : ramp -> GainBlock(0.5) -> x16_out   (an ordinary single-path chain)

GOLDEN: the voter has no stock GNU Radio counterpart; its pinned spec is
``TMRVoterBlock.vote`` (proven word-for-word on-chip by test_tmr_voter.py).
Gated here, all on the real placed+routed chip:

  * f=0 (healthy): 256 packets, every one [ramp byte, 0].
  * f=1 LSB on path B (the shipped default): 256 packets, every one
    [ramp byte, 2] — the value is STILL the correct ramp byte (TMR corrects
    the fault) and the status names path B on every sample.
  * the solo stream is 0.5x the ramp throughout, same array, same run.
  * INV-56: every per-sample settle stop_reason is "QueueEmpty".
  * INV-42: the flags are asserted on the GENERATED Python (a .grc enum
    matching no option is silently replaced by the default).
  * INV-4 mutations: a fault on the WRONG arm and a healthy-claim under
    fault must both break the gate's expectations.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "tmr_pipeline"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tmr_pipeline_demo import (  # noqa: E402
    CHIP_YAML, FAULT_LSB, GRC_PATH, KYT_PATH, RAMP, load_and_build,
    place_route_build, run_streams, solo_golden, tmr_golden)

GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")


def _inj_b(project):
    """The path-B injector: the AddConst wired into the voter's ``b`` arm."""
    b_net = next(c for c in project.connections
                 if getattr(c.target, "port", None) == "b")
    return next(blk for blk in project.blocks if blk.name == b_net.source.block)


def _rebuild(project, cat):
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    return bres


@pytest.fixture(scope="module")
def built():
    project, bres, cat, ct = place_route_build()
    return project, bres, cat


def test_route_build_ok(built):
    project, bres, cat = built
    assert bres.ok
    assert len(project.blocks) == 9
    voter = next(b for b in project.blocks if b.type == "TMRVoterBlock")
    assert len(voter.placement.cells) == 4
    # 12 block cells: 8 single-cell blocks + the 4-cell voter.
    assert sum(len(b.placement.cells) for b in project.blocks) == 12


def test_fault_on_b_is_corrected_and_named(built):
    """f = 1 LSB on path B (the shipped default): every packet's VALUE is the
    correct ramp byte (TMR corrects the fault) and every STATUS is 2 (path B
    named). The co-resident solo stream is exact in the SAME run."""
    project, bres, cat = built
    assert float(_inj_b(project).params["const"]) == FAULT_LSB
    tmr, solo, reasons = run_streams(project, bres, RAMP)
    assert tmr == tmr_golden(RAMP, 1)
    assert tmr[0::2] == RAMP                     # values: the ramp, corrected
    assert set(tmr[1::2]) == {2}                 # status: path B, every sample
    assert solo == solo_golden(RAMP)
    assert set(reasons) == {"QueueEmpty"}, sorted(set(map(str, reasons)))


def test_healthy_run_reports_status_zero(built):
    """f = 0: every packet is [ramp byte, 0]; solo unchanged."""
    project, bres, cat = built
    inj = _inj_b(project)
    old = dict(inj.params)
    try:
        inj.params["const"] = 0.0
        bres2 = _rebuild(project, cat)
        tmr, solo, reasons = run_streams(project, bres2, RAMP)
        assert tmr == tmr_golden(RAMP, 0)
        assert tmr[0::2] == RAMP
        assert set(tmr[1::2]) == {0}
        assert solo == solo_golden(RAMP)
        assert set(reasons) == {"QueueEmpty"}
    finally:
        inj.params.clear()
        inj.params.update(old)


def test_large_corruption_is_still_corrected(built):
    """TMR corrects by MAJORITY, not by magnitude: a 100-LSB path-B fault
    still yields the exact ramp values with status 2."""
    project, bres, cat = built
    inj = _inj_b(project)
    old = dict(inj.params)
    payload = RAMP[:32]
    try:
        inj.params["const"] = 100.0 / 32768.0
        bres2 = _rebuild(project, cat)
        tmr, solo, reasons = run_streams(project, bres2, payload)
        assert tmr == tmr_golden(payload, 100)
        assert tmr[0::2] == payload
        assert set(tmr[1::2]) == {2}
        assert set(reasons) == {"QueueEmpty"}
    finally:
        inj.params.clear()
        inj.params.update(old)


def test_shipped_kyt_runs_end_to_end():
    """The SHIPPED .kyt (the file the GUI hosts): full-ramp parity on both
    streams, with the 1-LSB path-B fault baked in."""
    project, bres, cat, ct = load_and_build()
    assert float(_inj_b(project).params["const"]) == FAULT_LSB
    tmr, solo, reasons = run_streams(project, bres, RAMP)
    assert tmr == tmr_golden(RAMP, 1)
    assert solo == solo_golden(RAMP)
    assert set(reasons) == {"QueueEmpty"}


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_fault_on_wrong_arm_FAILS(built):
    """Moving the SAME fault to path A must break the shipped expectation —
    the status word distinguishes arms; the gate is not blind to WHICH path
    faulted (every status flips 2 -> 1, values stay corrected)."""
    project, bres, cat = built
    inj_b = _inj_b(project)
    a_net = next(c for c in project.connections
                 if getattr(c.target, "port", None) == "a")
    inj_a = next(b for b in project.blocks if b.name == a_net.source.block)
    old_a, old_b = dict(inj_a.params), dict(inj_b.params)
    payload = RAMP[:32]
    try:
        inj_a.params["const"] = FAULT_LSB
        inj_b.params["const"] = 0.0
        bres2 = _rebuild(project, cat)
        tmr, _solo, reasons = run_streams(project, bres2, payload)
        assert set(reasons) == {"QueueEmpty"}
        assert tmr != tmr_golden(payload, 1), \
            "gate blind to WHICH arm carries the fault"
        assert tmr == [w for v in payload for w in (v, 1)], \
            "wrong-arm fault did not produce the status-1 packets"
    finally:
        inj_a.params.clear(), inj_a.params.update(old_a)
        inj_b.params.clear(), inj_b.params.update(old_b)


def test_mutation_healthy_claim_under_fault_FAILS(built):
    """The shipped (faulted) build must NOT satisfy the healthy golden — the
    status rail carries real information; a gate asserting [value, 0] here
    fails. (The value rail ALONE is identical in both cases: that is TMR
    doing its job, and exactly why the gate asserts whole packets.)"""
    project, bres, cat = built
    payload = RAMP[:32]
    tmr, _solo, reasons = run_streams(project, bres, payload)
    assert set(reasons) == {"QueueEmpty"}
    assert tmr, "no packets at all"
    assert tmr != tmr_golden(payload, 0), \
        "faulted build satisfied the healthy expectation"
    assert tmr[0::2] == payload              # ...while the values ARE corrected


# --------------------------------------------------- GENERATED PYTHON (INV-42)
_GEN_SCRIPT = r"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from gnuradio import gr
from gnuradio.grc.core.platform import Platform
platform = Platform(name="tmr gen check", prefs=gr.prefs(), version=gr.version(),
                    version_parts=(gr.major_version(), gr.api_version(),
                                   gr.minor_version()))
platform.build_library(["/usr/share/gnuradio/grc/blocks", sys.argv[2]])
out = tempfile.mkdtemp(prefix="tmrgen_")
fg, file_path = platform.load_and_generate_flow_graph(
    os.path.abspath(sys.argv[1]), os.path.abspath(out))
assert file_path, "generation failed"
sys.stdout.write(open(file_path).read())
"""


@pytest.mark.skipif(not os.path.exists(GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_generated_python_carries_the_shipped_flags():
    """INV-42: the .grc text is NOT the authority — an enum value matching no
    option is silently replaced by the default. Assert the flags on the
    GENERATED Python: raw output words on BOTH sources (byte/status streams
    are not Q15), the GUI's default server port on all four markers, and the
    looped display batch on both sinks."""
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "tmr_gen_check.py"
    script.write_text(_GEN_SCRIPT)
    r = subprocess.run(
        [GR_PYTHON, str(script), str(GRC_PATH),
         str(_ROOT / "gr-kyttar" / "grc")],
        capture_output=True, text=True, timeout=300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 0, r.stderr[-800:]
    py = r.stdout
    assert py.count('output_words="raw"') == 2, \
        "both kyttar sources must set output_words='raw' EXPLICITLY"
    assert py.count("server_port=58950") == 4, \
        "all four kyttar markers must bind the GUI's default port 58950"
    assert py.count("server_repeat=True") == 2, \
        "both sinks must loop the display batch (a full-size QT buffer " \
        "never paints from one finite burst)"
    assert 'stream_id="tmr"' in py and 'stream_id="solo"' in py
    # The path-B injector's constant must survive generation as one word LSB.
    assert "3.0517578125e-05" in py


# ------------------------------------------------------------ USER PATH (§5b)
@pytest.mark.skipif(not os.path.exists(GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_shipped_grc_user_path():
    """Host the SHIPPED .kyt exactly as the GUI's "Run as GNURadio Server"
    does (port 58950), GRC-generate and run the SHIPPED .grc under the real
    GNU Radio interpreter, and assert what the kyttar sinks recovered: the
    [value, status] packet stream (values = the ramp, status = 2 on every
    sample) and the solo 0.5x ramp, both as RAW word floats, with clean
    server_repeat repetition."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(KYT_PATH)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=58950)
    assert bound == 58950, f"port 58950 busy (bound {bound})"
    try:
        runner = _ROOT / "verification" / "grc_userpath_run.py"
        r = subprocess.run(
            [GR_PYTHON, str(runner), str(GRC_PATH), "90"],
            capture_output=True, text=True, timeout=330,
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        sinks = {}
        for line in r.stdout.splitlines():
            if line.startswith("SINK "):
                parts = line.split()
                sinks[parts[1]] = [float(x) for x in parts[2:]]
        assert r.returncode == 0 and sinks, (
            f"generated flowgraph failed (rc={r.returncode}):\n"
            f"{r.stdout[-800:]}\n{r.stderr[-800:]}")
    finally:
        sim.stop_gnuradio_server()
    # output_words="raw": the recovered floats ARE the words, no q15 rescale.
    tmr = [int(round(v)) & 0xFFFF for v in sinks.get("tmr_sink", [])]
    solo = [int(round(v)) & 0xFFFF for v in sinks.get("solo_sink", [])]
    exp_tmr = tmr_golden(RAMP, 1)
    exp_solo = solo_golden(RAMP)
    assert len(tmr) >= len(exp_tmr), \
        f"tmr_sink recovered only {len(tmr)}/{len(exp_tmr)} words"
    assert tmr[:len(exp_tmr)] == exp_tmr, \
        "user-path packet stream diverges from the pinned golden"
    assert len(solo) >= len(exp_solo), \
        f"solo_sink recovered only {len(solo)}/{len(exp_solo)} words"
    assert solo[:len(exp_solo)] == exp_solo, \
        "user-path solo stream diverges from the 0.5x ramp"
    # server_repeat integrity: every full repetition is the SAME batch.
    for name, got, exp in (("tmr", tmr, exp_tmr), ("solo", solo, exp_solo)):
        for rep in range(1, len(got) // len(exp)):
            assert got[rep * len(exp):(rep + 1) * len(exp)] == exp, \
                f"{name}: repetition {rep} diverges (display loop corrupt)"
