# SPDX-License-Identifier: GPL-3.0-or-later
"""CORDIC polar example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

ONE complex stimulus through TWO placed CORDIC vectoring chains sharing the
chip via the stream-id duplex (streams 'mag'/'arg' on x16_in/x16_out): the
chip output of EACH stream must be BIT-EXACT to the block's
``process_reference`` (GR equivalence of the references is proven with
derived tolerances in test_cordic_blocks.py), and the float-truth error must
sit inside the same gate bounds. Also gates the shipped-.kyt parity and that
the guided anchors keep routing (the packer alone cannot corridor 47 block
cells + two duplex corridors — the anchors are part of the example
contract)."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "cordic_polar"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cordic_polar_demo import (  # noqa: E402
    CHIP_YAML, IQ_STIM, KYT_PATH, import_and_pnr, reference_outputs,
    run_streams)

pytestmark = pytest.mark.skipif(not Path(CHIP_YAML).exists(),
                                reason="chip yaml absent")


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


@pytest.fixture(scope="module")
def outputs(built):
    project, bres, _cat, _ct = built
    return run_streams(project, bres, IQ_STIM)


def test_import_route_build_ok(built):
    project, bres, _cat, _ct = built
    assert len(project.blocks) == 2
    used = sum(c.cell_count for c in bres.chips.values())
    assert used >= 47                     # 17 + 30 block cells + corridors


def test_both_streams_bit_exact(outputs):
    got_mag, got_arg = outputs
    ref_mag, ref_arg = reference_outputs(IQ_STIM)
    assert got_mag == ref_mag, "mag stream diverges from the block reference"
    assert got_arg == ref_arg, "arg stream diverges from the block reference"


def test_float_truth_within_gate_bounds(outputs):
    got_mag, got_arg = outputs
    wm = max(abs(m / 32768.0 - abs(c)) * 32768
             for m, c in zip(got_mag, IQ_STIM))
    wa = max(abs((a / 32768.0 * math.pi - math.atan2(c.imag, c.real)
                  + math.pi) % (2 * math.pi) - math.pi)
             for a, c in zip(got_arg, IQ_STIM) if abs(c) >= 0.1)
    assert wm <= 40, f"mag worst {wm:.1f} LSB"
    assert wa <= 0.006, f"arg worst {wa:.5f} rad"


def test_shipped_kyt_matches_flow(built):
    """The shipped .kyt must be regenerable-equal in shape: same blocks,
    placements resolvable, and a clean build."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    assert KYT_PATH.exists(), "shipped cordic_polar.kyt missing"
    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    got_mag, got_arg = run_streams(project, bres, IQ_STIM)
    ref_mag, ref_arg = reference_outputs(IQ_STIM)
    assert got_mag == ref_mag and got_arg == ref_arg
