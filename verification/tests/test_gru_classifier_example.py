# SPDX-License-Identifier: GPL-3.0-or-later
"""Gates for the GRU modulation-classifier example.

STATUS — read this before trusting anything here. The feature front end is
DERIVED and verified against the trained model's own offline definition, and the
join->GRU tail routes on a real chip. The WHOLE chain does **not** place and
route as one chip: it is always exactly one net short (see the lessons_log entry
"gru_classifier example ... BLOCKED one net short"). So this file gates:

* the feature front end, bit-exact / within a DERIVED bound vs ``ml/features.py``;
* the offline chain into the chip-exact GRU golden, on the shipped stimulus;
* the stimulus' load-bearing properties (headroom, trained distribution);
* the mutations that must fail (swapped word order, wrong weights);
* the placement wall itself, as an explicit KNOWN-LIMIT guard — so the day the
  geometry gives, the guard fails and tells us to finish the example.

There is deliberately NO test asserting a working end-to-end on-chip run. That
would be the example's real gate and it does not pass yet.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "gru_classifier"
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python"),
           str(_EX), str(_EX / "ml")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips"
                / "kyttar_10x12.yaml")

pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

WINDOW_N = 32
#: derived RMS budget — see the module docstring of gru_classifier.py and the
#: lessons_log entry. 2 LSB (magsq's two truncating products) + 32 LSB (the 32
#: truncating MovingAverage taps) of POWER deficit, then Sqrt's own [-4, +1].
POWER_DEFICIT_LSB = 2 + WINDOW_N
SQRT_LSB_LO, SQRT_LSB_HI = -4, +1


def _rms_bound_lo(y_q15: int) -> float:
    """Lower bound on (chip - ideal) in Q15 LSB for an RMS word ``y_q15``.

    A power deficit dP propagates through the square root as
    ``dy = dP / (2*sqrt(P))``; in Q15 LSB that is ``dP * 16384 / y``.
    """
    return -(POWER_DEFICIT_LSB * 16384.0 / max(int(y_q15), 1)) - abs(SQRT_LSB_LO)


def _q15(x):
    return np.clip(np.round(np.asarray(x, dtype=np.float64) * 32768.0),
                   -32768, 32767).astype(np.int64)


@pytest.fixture(scope="module")
def stim():
    from gru_stimulus import make_stimulus
    iq, truth = make_stimulus()
    return iq, truth


# --------------------------------------------------------------------------- #
#  The stimulus' load-bearing properties                                       #
# --------------------------------------------------------------------------- #
def test_stimulus_leaves_q15_power_headroom(stim):
    """|z| < 1 everywhere, else ComplexToMagSquared SATURATES and the window's
    mean power reads low (measured up to -1247 LSB when it does)."""
    from gru_stimulus import peak_magnitude
    iq, _ = stim
    assert peak_magnitude(iq) < 1.0
    # and with real margin, not marginally
    assert peak_magnitude(iq) < 0.95


def test_stimulus_gains_stay_in_the_trained_distribution():
    from gru_stimulus import CONFIG, SEGMENT_GAIN
    g0, g1 = CONFIG["gain_range"]
    for cls, g in SEGMENT_GAIN.items():
        assert g0 <= g <= g1, (cls, g)


def test_stimulus_is_deterministic(stim):
    from gru_stimulus import make_stimulus
    iq, truth = stim
    iq2, truth2 = make_stimulus()
    assert np.array_equal(iq, iq2)
    assert np.array_equal(truth, truth2)


def test_stimulus_walks_all_four_classes(stim):
    from gru_stimulus import CLASSES
    _iq, truth = stim
    # one contiguous segment per class, in order
    assert [int(v) for v in np.unique(truth)] == list(range(len(CLASSES)))
    changes = int(np.sum(np.diff(truth) != 0))
    assert changes == len(CLASSES) - 1


# --------------------------------------------------------------------------- #
#  Feature front end vs ml/features.py (the model's own definition)            #
# --------------------------------------------------------------------------- #
def _zcr_pinned_ref(re_q15, wn=WINDOW_N):
    """``features.py``'s ZCR under the BLOCK's pinned convention: one implicit
    non-negative predecessor, and each window counts the ``wn`` pairs ENDING at
    its samples (so the inter-window boundary pair is included)."""
    s = np.where(np.asarray(re_q15) >= 0, 1, -1)
    s = np.concatenate([[1], s])
    cross = (s[1:] != s[:-1]).astype(np.int64)
    n = (len(cross) // wn) * wn
    cnt = cross[:n].reshape(-1, wn).sum(axis=1)
    return np.minimum(cnt * (32768 // wn), 32767)


def test_zcr_arm_is_bit_exact_under_its_pinned_convention(stim):
    from gru_classifier import zcr_feature_words
    iq, _ = stim
    got = np.asarray(zcr_feature_words(iq), dtype=np.int64)
    ref = _zcr_pinned_ref(_q15(np.real(iq)))
    n = min(len(got), len(ref))
    assert n > 0
    assert np.array_equal(got[:n], ref[:n]), \
        f"{int(np.sum(got[:n] != ref[:n]))}/{n} ZCR words differ"


def test_zcr_delta_vs_plain_features_is_exactly_the_boundary_pair(stim):
    """The documented, DERIVED difference vs plain ml/features.py: +1 crossing
    (= +1024 Q15 LSB) exactly on the windows whose boundary pair crosses —
    never any other value, and never negative."""
    import features as F
    from gru_classifier import zcr_feature_words
    iq, _ = stim
    got = np.asarray(zcr_feature_words(iq), dtype=np.int64)
    plain = _q15(F.compute_features(iq, WINDOW_N)["zcr"])
    n = min(len(got), len(plain))
    d = got[:n] - plain[:n]
    assert set(np.unique(d).tolist()) <= {0, 1024}, sorted(set(d.tolist()))


def test_rms_arm_is_inside_the_derived_truncation_bound(stim):
    """RMS is biased DOWNWARD by truncation only, within the derived,
    input-level-dependent bound. Not a tuned tolerance — see _rms_bound_lo."""
    import features as F
    from gru_classifier import rms_feature_words
    iq, _ = stim
    got = np.asarray(rms_feature_words(iq), dtype=np.int64)
    ref = _q15(F.compute_features(iq, WINDOW_N)["rms"])
    n = min(len(got), len(ref))
    assert n > 0
    d = (got[:n] - ref[:n]).astype(np.float64)
    lo = np.array([_rms_bound_lo(v) for v in ref[:n]])
    assert np.all(d <= SQRT_LSB_HI), f"max err {d.max()} > {SQRT_LSB_HI}"
    assert np.all(d >= lo), (
        f"{int(np.sum(d < lo))}/{n} windows below the derived bound; "
        f"worst ratio {float(np.max(d / lo)):.3f}")


def test_rms_bound_is_not_vacuous(stim):
    """A bound that nothing could violate proves nothing: assert the measured
    errors actually USE a real fraction of it (they ran 0.639 when derived)."""
    import features as F
    from gru_classifier import rms_feature_words
    iq, _ = stim
    got = np.asarray(rms_feature_words(iq), dtype=np.int64)
    ref = _q15(F.compute_features(iq, WINDOW_N)["rms"])
    n = min(len(got), len(ref))
    d = (got[:n] - ref[:n]).astype(np.float64)
    lo = np.array([_rms_bound_lo(v) for v in ref[:n]])
    # errors are genuinely negative (truncation), not ~0
    assert d.max() < 0, f"expected a downward bias, got max {d.max()}"
    assert float(np.max(d / lo)) > 0.05, "bound is orders of magnitude too loose"


def test_the_two_arms_are_index_aligned(stim):
    """KeepOneInN(32) keeps phase 31 and ZeroCrossingRate(32) emits on indices
    31, 63, ... — equal counts, one word each per window, no re-sync needed."""
    from gru_classifier import rms_feature_words, zcr_feature_words
    iq, _ = stim
    a, b = rms_feature_words(iq), zcr_feature_words(iq)
    assert len(a) == len(b) == len(iq) // WINDOW_N


# --------------------------------------------------------------------------- #
#  The classifier: offline front end -> the chip-exact GRU golden              #
# --------------------------------------------------------------------------- #
def test_weights_used_by_the_chain_are_the_shipped_trained_weights():
    """BLOCK_SPECS pins weights_file="" (the block's BUNDLED default), which is
    only correct while that file IS this example's weights_single.json."""
    from gru_classifier import BLOCK_SPECS, WEIGHTS
    bundled = (_ROOT / "runtime" / "python" / "gr_kyttar" / "placement"
               / "blocks" / "gru_weights_default.json")
    spec = dict(BLOCK_SPECS)["gru"] if False else \
        [p for n, _c, p in BLOCK_SPECS if n == "gru"][0]
    assert spec["weights_file"] == ""
    assert (hashlib.sha256(bundled.read_bytes()).hexdigest()
            == hashlib.sha256(WEIGHTS.read_bytes()).hexdigest())


def test_every_segment_is_classified_correctly_offline(stim):
    """The shipped stimulus, through the shipped feature front end, into the
    bit-exact GRU golden: every segment's majority vote is its true class."""
    from gru_classifier import golden_classes, segment_votes
    iq, _truth = stim
    votes = segment_votes(golden_classes(iq))
    assert votes == [0, 1, 2, 3], votes


def test_offline_step_accuracy_is_recorded(stim):
    """Per-step accuracy after the per-segment burn-in. Pinned well below the
    measured value so it gates a REGRESSION, not the exact number."""
    from gru_classifier import SEGMENT_STEPS, golden_classes
    from gru_stimulus import CLASSES
    iq, _ = stim
    cls = np.asarray(golden_classes(iq))
    accs = []
    for ci in range(len(CLASSES)):
        seg = cls[ci * SEGMENT_STEPS + 30:(ci + 1) * SEGMENT_STEPS]
        accs.append(float(np.mean(seg == ci)))
    assert min(accs) > 0.60, dict(zip(CLASSES, accs))
    assert float(np.mean(accs)) > 0.85, accs


# --------------------------------------------------------------------------- #
#  MUTATIONS — these must FAIL (INV-4)                                         #
# --------------------------------------------------------------------------- #
def test_mutation_swapped_feature_word_order_fails(stim):
    """word 0 = RMS, word 1 = ZCR is the trained contract. Feeding the pair
    transposed must NOT still classify correctly."""
    from gru_classifier import WEIGHTS, feature_words, segment_votes
    from gru_reference_chip import GRUChipModel
    iq, _ = stim
    pairs = np.asarray(feature_words(iq), dtype=np.int64)
    swapped = pairs[:, ::-1]
    cls, _h = GRUChipModel.load(WEIGHTS).forward(swapped)
    votes = segment_votes([int(c) for c in cls])
    assert votes != [0, 1, 2, 3], \
        "swapping the feature word order still classified correctly — the " \
        "gate cannot distinguish the trained contract from its transpose"


def test_mutation_wrong_weights_fails(stim):
    """A different weights file must not reproduce the correct verdict."""
    from gru_classifier import feature_words, segment_votes
    from gru_reference_chip import GRUChipModel
    iq, _ = stim
    pairs = np.asarray(feature_words(iq), dtype=np.int64)
    params = json.loads((_EX / "ml" / "weights_single.json").read_text())
    # corrupt the readout head: negate it, so the argmax inverts
    params["head"]["quant"]["Wo_q"] = [[-int(v) for v in row]
                                       for row in params["head"]["quant"]["Wo_q"]]
    cls, _h = GRUChipModel(params).forward(pairs)
    votes = segment_votes([int(c) for c in cls])
    assert votes != [0, 1, 2, 3], \
        "a negated readout head still classified correctly"


def test_mutation_zero_features_fails(stim):
    """An empty/zero feature stream must not produce the right verdict."""
    from gru_classifier import WEIGHTS, feature_words, segment_votes
    from gru_reference_chip import GRUChipModel
    iq, _ = stim
    pairs = np.zeros_like(np.asarray(feature_words(iq), dtype=np.int64))
    cls, _h = GRUChipModel.load(WEIGHTS).forward(pairs)
    assert segment_votes([int(c) for c in cls]) != [0, 1, 2, 3]


def test_mutation_saturating_stimulus_breaks_the_rms_bound(stim):
    """The headroom precondition is REAL: scale the clip past the Q15 power
    rail and the derived RMS bound must be violated. Proves the bound gates
    the saturation failure rather than absorbing it."""
    import features as F
    from gru_classifier import rms_feature_words
    iq, _ = stim
    hot = iq * (1.6 / max(float(np.max(np.abs(iq))), 1e-9))   # peak |z| = 1.6
    got = np.asarray(rms_feature_words(hot), dtype=np.int64)
    ref = _q15(F.compute_features(hot, WINDOW_N)["rms"])
    n = min(len(got), len(ref))
    d = (got[:n] - ref[:n]).astype(np.float64)
    lo = np.array([_rms_bound_lo(v) for v in ref[:n]])
    assert np.any(d < lo), \
        "a saturating clip stayed inside the derived bound — the bound is " \
        "too loose to catch |z| >= 1"


# --------------------------------------------------------------------------- #
#  KNOWN LIMIT — the whole chain does not route as one chip                    #
# --------------------------------------------------------------------------- #
#
# GUARD tests in the FIRFilterBlock tap-ceiling sense: they pin the wall so it
# cannot be forgotten, and they FAIL the day it is lifted — which is the signal
# to finish the example. These run a REAL place + route (a few seconds each).

def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController
    return (BlockCatalog, load_chip_type, AppController, ChipPortEndpoint,
            BlockEndpoint)


def _hand_place(ctrl, anchors, specs, ids_out):
    import gru_classifier as G
    from model.placement import Placement
    for nm, cls, params in specs:
        ids_out[nm] = ctrl.place_block(cls, 0, *anchors[nm], library=G.LIB,
                                       params=dict(params))
    by_name = {b.name: b for b in ctrl.project.blocks}
    for nm, _c, _p in specs:
        b = by_name[ids_out[nm]]
        cells, _ = ctrl.default_cells(b.type, b.library, 0, *anchors[nm],
                                      b.params)
        b.placement = Placement(0, cells)


def test_known_limit_the_full_chain_does_not_route():
    """KNOWN LIMIT. The whole chain has never routed on one 10x12.

    Measured across dispatches: ~2500 layouts (three search strategies), then
    5039 (anchor sweep + four routing models incl. auto_orient, single-backbone
    bus/ring and CP-SAT), then — after GRUCellBlock was RE-FOLDED to 8x7 so its
    input and egress span the north edge facing the chip's two row-0 ports —
    a further ~3200 (exhaustive lane enumeration + guided perturbation from
    every one- and two-net-short seed). The best result is always exactly ONE
    failing net, and WHICH net fails rotates as blocks move: a saturated array,
    not one bad anchor.

    The re-fold measurably HELPED and did not close it: on the identical lane
    search the as-authored 7x8 fold bottomed out two nets short, the 8x7 one
    net short. The hop ceiling was ruled out earlier (INV-36; no `hop_overflow`
    in 5039 layouts), and shrinking the RMS arm was measured as a second lever
    that also does not close it (a 4-tap boxcar with Sqrt dropped — 56 block
    cells against the shipped 65 — is still one net short).

    If this test FAILS (the chain routed), the wall is lifted — finish the
    example: build the .kyt, run it end to end, and replace this guard with the
    real on-chip gate.
    """
    import gru_classifier as G
    (BC, lct, AC, CPE, BE) = _engine()
    cat = BC.from_gr_kyttar()
    ct = lct(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AC(catalog=cat)
    ctrl.new_project("gru_classifier_guard", ctk)
    ids = {}
    _hand_place(ctrl, G.BEST_KNOWN_ANCHORS, G.BLOCK_SPECS, ids)
    G._connect(ctrl, CPE, BE, ids)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    bad = [r.name for r in rep.results if not r.ok]
    assert bad, ("the full chain ROUTED — the documented placement wall is "
                 "lifted; finish the example and delete this guard")
    assert len(bad) == 1, (
        f"expected the documented ONE-net-short wall, got {bad}")


#: the companion front end the GRU must leave room for: the real RMS arm
#: (ComplexToMagSquared -> MovingAverage(32) -> Sqrt -> KeepOneInN(32)) plus
#: ZeroCrossingRate and FeaturePairJoin. 14 cells in 6 blocks.
COMPANION = ["power", "boxcar", "root", "decim", "zcr", "join"]


def test_coplacement_the_gru_leaves_room_for_the_companion_front_end():
    """CO-PLACEMENT GATE — the one that decides whether the example can ship.

    Places the RE-FOLDED GRUCellBlock together with the whole companion front
    end on ONE 10x12 with both 16-bit ports, and reports how much of the chain
    routes. Today it is exactly ONE net short (see
    ``test_known_limit_the_full_chain_does_not_route`` for the search volume
    behind that claim), so this gate asserts the measured state:

      * every block PLACES legally together — the geometry fits, with room to
        spare (65 of 120 cells), which is why the wall is corridors and not
        capacity; and
      * the chain gets to within one net of routing.

    The moment the last net closes, the first assert below fails and says so.
    That is the signal to build the .kyt and finish the example.
    """
    import gru_classifier as G
    (BC, lct, AC, CPE, BE) = _engine()
    cat = BC.from_gr_kyttar()
    ct = lct(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AC(catalog=cat)
    ctrl.new_project("gru_coplacement", ctk)
    ids = {}
    _hand_place(ctrl, G.BEST_KNOWN_ANCHORS, G.BLOCK_SPECS, ids)

    # 1. every block is placed, on-fabric, and pairwise non-overlapping.
    placed = {}
    for b in ctrl.project.blocks:
        assert b.placement is not None, f"{b.name} did not place"
        for c in b.placement.cells:
            assert 0 <= c.x < 10 and 0 <= c.y < 12, (
                f"{b.name} cell ({c.x},{c.y}) is off the 10x12 fabric")
            assert (c.x, c.y) not in placed, (
                f"{b.name} overlaps {placed[(c.x, c.y)]} at ({c.x},{c.y})")
            placed[(c.x, c.y)] = b.name
    assert len(placed) == 65, f"expected 65 block cells, got {len(placed)}"
    assert len(placed) < 120, "the block set alone must fit with room to spare"

    # 2. the whole set routes to within one net.
    G._connect(ctrl, CPE, BE, ids)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    bad = [r.name for r in rep.results if not r.ok]
    assert bad, (
        "CO-PLACEMENT ACHIEVED — the GRU and the whole companion front end "
        "routed together on one chip. The wall is lifted: build the .kyt, run "
        "it end to end, and turn this gate into the real on-chip assertion.")
    assert len(bad) == 1, (
        f"the re-folded GRU should leave the chain ONE net short; got "
        f"{len(bad)}: {bad}")


def test_known_limit_the_join_tail_alone_does_route():
    """The wall is the RMS arm's corridors, NOT the join->GRU tail: the tail
    (both ingress nets, the join, the GRU, the egress) routes on its own —
    measured at 81 of 120 cells with the re-folded 8x7 GRU."""
    import gru_classifier as G
    from engine.build import BuildEngine
    (BC, lct, AC, CPE, BE) = _engine()
    cat = BC.from_gr_kyttar()
    ct = lct(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AC(catalog=cat)
    ctrl.new_project("gru_tail", ctk)
    anchors = dict(G.TAIL_ANCHORS)
    specs = [s for s in G.BLOCK_SPECS if s[0] in anchors]
    ids = {}
    _hand_place(ctrl, anchors, specs, ids)
    for src, dst, name in [
            (CPE(chip=0, port="x16_in"), BE(block=ids["decim"], port="x"),
             "in_d"),
            (CPE(chip=0, port="x16_in"), BE(block=ids["zcr"], port="sample"),
             "in_z"),
            (BE(block=ids["decim"], port="out"), BE(block=ids["join"], port="a"),
             "rms_join"),
            (BE(block=ids["zcr"], port="out"), BE(block=ids["join"], port="b"),
             "zcr_join"),
            (BE(block=ids["join"], port="out"), BE(block=ids["gru"], port="f"),
             "join_gru"),
            (BE(block=ids["gru"], port="out"), CPE(chip=0, port="x16_out"),
             "gru_out")]:
        ctrl.add_logical_connection(src, dst, name=name)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    assert rep.ok, [f"{r.name}:{r.reason}" for r in rep.results if not r.ok]
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    assert bres.ok
    used = sum(c.cell_count for c in bres.chips.values())
    assert 60 <= used <= 95, used


def test_known_limit_the_gru_alone_dominates_the_array():
    """Why the chain does not fit: GRUCellBlock is 51 cells — 43% of the array
    — in an 8x7 fold, and it still costs several more cells for its own two
    port corridors, leaving too little for the 14-cell front end and the four
    extra nets that front end needs.

    The re-fold cut those two corridors materially (the block's own port cost,
    min over anchors of |fin - x16_in| + |oout - x16_out|, went from 11 cells
    to 7 by moving the egress onto the north edge beside the input), which is
    why this bound is stated as a RANGE that the re-fold moved down, not the
    single number the 7x8 fold pinned.
    """
    import gru_classifier as G
    from engine.build import BuildEngine
    from model.placement import Placement
    (BC, lct, AC, CPE, BE) = _engine()
    cat = BC.from_gr_kyttar()
    ct = lct(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AC(catalog=cat)
    ctrl.new_project("gru_only", ctk)
    g = ctrl.place_block("GRUCellBlock", 0, 0, 0, library=G.LIB, params={})
    b0 = ctrl.project.blocks[0]
    cells, _ = ctrl.default_cells(b0.type, b0.library, 0, 0, 0, b0.params)
    b0.placement = Placement(0, cells)
    ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                BE(block=g, port="f"), name="fin")
    ctrl.add_logical_connection(BE(block=g, port="out"),
                                CPE(chip=0, port="x16_out"), name="fout")
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    assert rep.ok, [f"{r.name}:{r.reason}" for r in rep.results if not r.ok]
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    assert bres.ok
    used = sum(c.cell_count for c in bres.chips.values())
    assert 51 < used <= 64, (
        f"GRU + its two port corridors measured {used}; the 7x8 fold cost 64 "
        f"and the 8x7 re-fold must not regress past it")
