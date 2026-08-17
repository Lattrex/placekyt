# SPDX-License-Identifier: GPL-3.0-or-later
"""Complex channel selector — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

FloatToComplex → FreqXlatingFIR(9 taps, −9 kHz) → ComplexLowPassFilter(firdes,
gain 0.9, 1.2 kHz) → MultiplyConstComplex(0.6+0.35j) → Conjugate →
ComplexToImag, on one array, vs the IDENTICAL stock-GNU-Radio chain, within
the DERIVED per-block bound (0+16+32+13+0+0 = 61 LSB — never tuned).
Whole-chain proof for FloatToComplex, FreqXlatingFIR, ComplexLowPassFilter,
MultiplyConstComplex, ConjugateBlock, ComplexToImag — and for the importer's
``re``/``im`` I/Q-rail synthesis (the converter-class Q rails silently never
wired before this session's fix). The Conjugate stage was restored once the
complex-handoff mis-delivery defect was fixed (test_conjugate_chain.py pins
both the abutment and routed topologies).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "channel_selector"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"), str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from channel_selector_demo import (  # noqa: E402
    CHIP_YAML, GR_PYTHON, KYT_PATH, SIG, TOL_LSB, _q15, _s16, gr_golden,
    import_and_pnr, run_chain)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


@pytest.fixture(scope="module")
def golden():
    return gr_golden(SIG)


def _worst(got, gold):
    n = min(len(got), len(gold))
    return max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(n))


def test_import_expands_all_iq_rails(built):
    """Every complex block→block edge must carry BOTH rails — the re/im
    converter naming regression (Q rails silently unwired → all-zero output)."""
    from model.connection import BlockEndpoint
    project, bres, cat, ct = built
    pairs = {}
    for c in project.connections:
        if isinstance(c.source, BlockEndpoint) and isinstance(c.target,
                                                              BlockEndpoint):
            pairs.setdefault((c.source.block, c.target.block), []).append(
                c.target.port)
    # f2c→fxf, fxf→clpf, clpf→rot, rot→conj, conj→c2i are all complex edges:
    # 2 rails each
    complex_edges = [k for k, v in pairs.items() if len(v) == 2]
    assert len(complex_edges) == 5, pairs


def test_chain_within_derived_bound(built, golden):
    project, bres, cat, ct = built
    got = run_chain(project, bres, SIG)
    assert len(got) == len(golden)
    assert _worst(got, golden) <= TOL_LSB


def test_interferers_actually_rejected(golden):
    """The selected channel must NOT contain the 4/14 kHz interferers — the
    golden's out-of-band energy is tiny relative to the in-band tones (the
    example demonstrates selection, not just filtering identity)."""
    import math
    n = len(golden)
    def tone_mag(f):
        c = sum(golden[t] * math.cos(2 * math.pi * f * t / 32000.0)
                for t in range(n // 2, n))
        s = sum(golden[t] * math.sin(2 * math.pi * f * t / 32000.0)
                for t in range(n // 2, n))
        return math.hypot(c, s) / (n / 2)
    # after the −9 kHz shift the wanted tones sit at ±400 Hz
    assert tone_mag(400.0) > 10 * max(tone_mag(5000.0), tone_mag(4000.0))


def test_shipped_kyt_runs_end_to_end(golden):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    got = run_chain(project, bres, SIG)
    assert len(got) == len(golden)
    assert _worst(got, golden) <= TOL_LSB


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_wrong_center_freq_FAILS(built, golden):
    """Shifting the FreqXlatingFIR center by one channel must blow the bound —
    the gate sees the down-shift frequency, it is not decorative."""
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    project, bres, cat, ct = built
    fxf = next(b for b in project.blocks if b.type == "FreqXlatingFIRBlock")
    old = dict(fxf.params)
    try:
        fxf.params["center_freq"] = 4000.0
        ct2 = load_chip_type(CHIP_YAML)
        bres2 = BuildEngine(cat, CHIP_YAML).build(project,
                                                  {project.chip_type: ct2})
        assert bres2.ok
        got = run_chain(project, bres2, SIG)
        assert _worst(got, golden) > TOL_LSB, \
            "gate blind to a wrong down-shift frequency"
    finally:
        fxf.params.clear()
        fxf.params.update(old)
