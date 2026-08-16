# SPDX-License-Identifier: GPL-3.0-or-later
"""LMS equalizer example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

Multipath QPSK through the placed decision-directed LMS equalizer, driven
per-sample (the LMS contract): the chip output must be BIT-EXACT to the
block's ``process_reference`` (GR scale-covariant equivalence proven in
test_lms_equalizer.py), the converged tail must decide every transmitted
symbol (tail BER 0 through the multipath), and the tail must sit on the
+-0.7071 decision constellation — the demo's visible "snap". Also gates the
shipped-.kyt parity."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "lms_equalizer"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lms_eq_demo import (  # noqa: E402
    BURST_LEN, CHIP_YAML, IQ_STIM, KYT_PATH, SYMS, TAIL, _dec,
    import_and_pnr, reference_output, run_chain)

pytestmark = pytest.mark.skipif(not Path(CHIP_YAML).exists(),
                                reason="chip yaml absent")


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


@pytest.fixture(scope="module")
def chip_words(built):
    _project, bres, _cat, _ct = built
    return run_chain(bres, IQ_STIM)


def test_import_pnr_build_ok(built):
    project, bres, _cat, _ct = built
    assert any(b.type == "LMSEqualizerBlock" for b in project.blocks)


def test_chip_bit_exact_to_reference(chip_words):
    exp = reference_output(IQ_STIM)
    assert len(chip_words) == len(exp) == 2 * BURST_LEN
    assert chip_words == exp, "chip diverges from the verified LMS reference"


def test_converged_tail_ber_zero_and_snapped(chip_words):
    y = np.array([complex(chip_words[2 * k], chip_words[2 * k + 1]) / 32768.0
                  for k in range(len(chip_words) // 2)])
    tail = slice(BURST_LEN - TAIL, len(y))
    errs = int(np.sum(_dec(y[tail]) != _dec(SYMS[tail])))
    assert errs == 0, f"tail symbol errors: {errs}/{TAIL}"
    rad = np.abs(np.abs(y[tail]) - 1.0)
    assert rad.mean() < 0.1, (
        f"tail not on the unit constellation (mean |dev| {rad.mean():.3f})")


def test_shipped_kyt_matches_flow():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    assert KYT_PATH.exists(), "shipped lms_equalizer.kyt missing"
    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    got = run_chain(bres, IQ_STIM)
    assert got == reference_output(IQ_STIM)
