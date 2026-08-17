# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio effects rack — WHOLE-CHAIN end-to-end gates (AGENTS.md §5b).

Three placed effects, each a dataflow JOIN (two independent arms into a
multi-input block), vs the IDENTICAL stock-GNU-Radio chains within DERIVED
per-block bounds (never tuned):

  echo:    x + 0.5·Delay(8) → Gain(0.5) → IIRBiquad → KeepOneInN(2)  (25 LSB)
  tremolo: x · (0.5 + 0.45·cos 250 Hz)  via NCO→C2R→AddConst→Multiply (16 LSB)
  comb:    x − 0.3·Delay(5)                                           (4 LSB)

Whole-chain proof for Delay, Gain, Add, Subtract, Multiply, IIRBiquad, NCO,
AddConst, KeepOneInN (+ ComplexToReal mid-chain) — and the FIRST gates for
single-fire JOINS: the join blocks' data-only ``sink`` entry + the importer's
trigger-arm election (grc_import._elect_join_triggers). The mutation test
proves the election is load-bearing: stripping the overrides double-fires the
combiner and the sample count itself breaks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "audio_effects"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"), str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from audio_effects_demo import (  # noqa: E402
    CHIP_YAML, EFFECTS, GR_PYTHON, HERE, SIG, _q15, _s16, gr_golden,
    import_and_pnr, run_chain)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")


def _worst(got, gold):
    n = min(len(got), len(gold))
    return max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(n))


@pytest.mark.parametrize("which", list(EFFECTS))
def test_effect_within_derived_bound(which):
    grc_name, tol, n_expect = EFFECTS[which]
    project, bres, cat, ct = import_and_pnr(grc_name)
    got = run_chain(bres, SIG)
    gold = gr_golden(which, SIG)
    assert len(got) == len(gold) == n_expect
    assert _worst(got, gold) <= tol


@pytest.mark.parametrize("which", list(EFFECTS))
def test_join_election_applied(which):
    """Each effect's import must wire its join as a COUNTING join: EVERY arm
    of the (single) 2-arm combiner carries the SAME entry_override — the
    block's ``join`` entry, which fires on the SECOND arrival in ANY arm
    order. (Replaces the deepest-arm election, which could not order
    equal-depth sibling arms.)"""
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(HERE / EFFECTS[which][0]), cat)
    ov = [c for c in res.project.connections
          if getattr(c, "entry_override", None) is not None]
    assert len(ov) == 2, [(c.name, c.entry_override) for c in ov]
    assert len({c.entry_override for c in ov}) == 1, \
        "both arms must target the one counting-join entry"


def test_shipped_kyts_run_end_to_end():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    for which, (grc_name, tol, n_expect) in EFFECTS.items():
        kyt = HERE / grc_name.replace(".grc", ".kyt")
        project = load_project(kyt)
        bres = BuildEngine(cat, CHIP_YAML).build(project,
                                                 {project.chip_type: ct})
        assert bres.ok, (which, [str(e) for e in bres.errors[:3]])
        got = run_chain(bres, SIG)
        gold = gr_golden(which, SIG)
        assert len(got) == len(gold) == n_expect, which
        assert _worst(got, gold) <= tol, which


# ------------------------------------------------- KNOWN LIMIT (INV-20, guard)
def test_saturated_join_skew_KNOWN_LIMIT():
    """EXECUTABLE KNOWN-LIMIT GUARD (flips when fixed): the effects are
    dataflow JOINS whose two arms have UNEQUAL depth, reconverging on one
    combiner — the CROSS-BLOCK instance of the INV-20 reconvergent fan-in.
    Under SATURATED drive (whole burst queued back-to-back) sample k of the
    short arm co-arrives with sample k-Δ of the long arm and the counting join
    combines MISALIGNED samples: the output COUNT stays right but the VALUES
    are wrong (measured on echo: 200/200 samples, every value differs). The
    per-block serialize-LOCK (INV-19/20) is a BLOCK-internal mechanism; no
    cross-block fork→join lock exists yet, so the shipped .grcs run these
    per-sample (``pipelined: 'no'``) and the GUI server paces them.

    If this test ever FAILS (saturated == per-sample), the substrate gained
    cross-block join serialization — flip the .grcs to Full-speed, replace
    this guard with a saturated equality gate, and update the READMEs.
    """
    import simkyt

    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    kyt = HERE / "effect_echo.kyt"
    project = load_project(kyt)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    per_sample = run_chain(bres, SIG)

    from audio_effects_demo import _jp, _wr
    lands = list(bres.chips[0].input_landings.values())
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    words = []
    for v in SIG:
        for lin in lands:
            words += [_wr(lin["hop"], lin["data_addrs"][0]), _q15(v),
                      _jp(lin["hop"], lin["entry"])]
    chip.queue_words_physical("x16_in", words)
    out = []
    idle = 0
    for _ in range(400000):
        chip.run(max_events=256)   # BOUNDED — a deadlock must fail clean
        got = chip.read_port_words_timed("x16_out")
        if got:
            idle = 0
            out.extend(_s16(w) for w, _d, _t in got)
        else:
            idle += 1
        if idle > 2000 or len(out) > len(per_sample):
            break
    assert out != per_sample, (
        "saturated echo now MATCHES per-sample — the cross-block join skew is "
        "fixed! Flip the audio_effects .grcs to Full-speed (pipelined: 'yes'), "
        "replace this known-limit guard with a saturated equality gate, and "
        "update README/KB (INV-20 cross-block).")


# ------------------------------------------------------------- MUTATION (INV-4)
def test_mutation_no_election_double_fires_FAILS():
    """Stripping the election (entry_override → None on every arm) must break
    the echo — the combiner fires once per arm and the output count/values
    diverge. Proves the sink-entry election is load-bearing, not decorative."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(HERE / EFFECTS["echo"][0]), cat)
    for c in res.project.connections:
        c.entry_override = None                     # the mutation
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct}, time_budget_s=120.0)
    assert rep.ok, rep.reason
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    assert bres.ok
    got = run_chain(bres, SIG)
    gold = gr_golden("echo", SIG)
    tol = EFFECTS["echo"][1]
    broken = (len(got) != len(gold)) or _worst(got, gold) > tol
    assert broken, "gate blind to a stripped join election (double-fire)"
