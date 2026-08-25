# SPDX-License-Identifier: GPL-3.0-or-later
"""Gates for the GRU modulation-classifier example.

STATUS: SHIPPED and verified END TO END on a real placed + routed + built chip.
This file gates:

* the feature front end, bit-exact / within a DERIVED bound vs ``ml/features.py``;
* the stimulus' load-bearing properties (headroom, trained distribution);
* the offline chain into the chip-exact GRU golden;
* **the ON-CHIP run** — the shipped stimulus driven through the real bitstream,
  its class stream asserted against the shipped golden AND against the offline
  chip-exact model, plus the per-class verdict;
* the shipped ``.kyt`` / ``.grc`` / golden and their agreement with the design
  this module builds;
* the mutations that must fail (swapped word order, wrong weights, zero
  features, a saturating stimulus).

WHAT CHANGED. For four dispatches the chain was always exactly ONE net short and
this file carried KNOWN-LIMIT guards pinning that wall. The wall was lifted by
re-folding ``GRUCellBlock`` WIDE-FLAT under the ``CHIP_SCALE`` placement class
(10 wide x 6 tall, I/O on one edge), which leaves six full-width free rows as a
contiguous through-channel. The guards are now the real on-chip assertions they
were always meant to become; the history is preserved in
``gru_classifier.py``'s ROUTING HISTORY note and the lessons_log entry.

WHAT THIS FILE DOES **NOT** GATE, stated so the coverage is not overread: the
end-to-end evidence below is the headless on-chip run through the real built
bitstream — the strongest claim about the CHIP, but NOT a claim about the hosted
GUI/GRC path. That path is gated separately by
``test_examples_grc_userpath.py::test_gru_classifier_shipped_grc_user_path``,
which hosts the shipped ``.kyt`` on the GUI's default server port and runs the
shipped ``.grc`` the way a user does.

That separation is not academic: for one release this file was fully green while
the user path returned NOTHING. ``run_on_chip`` reads the build's
``input_landings`` and drives the three ingress arms itself, so it never touched
the server's stream resolution — where all three faults lived (a ``.kyt`` with
no ``stream_id``, a bridge that could not drive a multi-arm complex stream, and
a ``.grc`` rescaling a RAW stream off-axis). A headless on-chip gate does not
imply a hosted gate; keep both.

Still unasserted anywhere: a pixel-probe of the scopes. The userpath gate asserts
the stream the scope RECEIVES, not the image it renders.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
#  THE EXAMPLE'S REAL GATE — the whole chain, on a real placed + routed chip   #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def onchip(stim):
    """The shipped stimulus driven through the REAL built bitstream.

    Module-scoped because it is the expensive one (15360 samples through 102
    cells, ~40 s); every on-chip assertion below reads this ONE run, so they
    are all statements about the same measured artefact rather than about
    separate re-runs that could disagree.
    """
    from gru_classifier import run_on_chip
    iq, _ = stim
    words, cells = run_on_chip(iq)
    return words, cells


def test_the_whole_chain_routes_and_builds_as_one_chip():
    """The wall is LIFTED: every net routes and the design builds, as ONE chip.

    This is the assertion the four KNOWN-LIMIT guards this file used to carry
    were waiting to become. If it fails, the chain has regressed to the
    one-net-short state — check ``GRUCellBlock``'s fold first (see
    ``gru_classifier.py``'s ROUTING HISTORY).
    """
    from gru_classifier import route_chain
    bad, used = route_chain()
    assert not bad, f"the chain no longer routes: {bad}"
    assert used is not None
    # 65 block cells + the corridors. Bounded, not pinned to the exact number,
    # so a routing improvement is not a failure — but a blow-out is.
    assert 65 < used <= 120, used
    assert used <= 110, (
        f"the build used {used}/120 cells; it measured 102 when shipped, and "
        f"the headroom above that is small — investigate before accepting")


def test_on_chip_matches_the_offline_chip_exact_golden(stim, onchip):
    """THE END-TO-END GATE. The class stream the REAL chip emits must equal the
    offline chip-exact model's, word for word.

    This is what makes the example an example rather than a composition: the
    stimulus enters through the chip's real input port, crosses the placed
    corridors into the placed blocks, and the class words come out of the real
    output port. Running each block separately and composing in Python would
    still pass with a broken ``.kyt``; this would not.
    """
    from gru_classifier import golden_classes
    iq, _ = stim
    words, _cells = onchip
    gold = golden_classes(iq)
    assert len(words) == len(gold), (
        f"RATE: chip emitted {len(words)} class words, the model expects "
        f"{len(gold)} (one per {WINDOW_N}-sample window)")
    bad = [(i, a, b) for i, (a, b) in enumerate(zip(words, gold)) if a != b]
    assert not bad, (
        f"{len(bad)}/{len(gold)} class words differ from the golden; "
        f"first few: {bad[:8]}")


def test_on_chip_classifies_every_segment_correctly(stim, onchip):
    """Every class segment's majority vote is its TRUE class, ON CHIP."""
    from gru_classifier import segment_votes
    from gru_stimulus import CLASSES
    _iq, _truth = stim
    words, _cells = onchip
    votes = segment_votes(words)
    assert votes == list(range(len(CLASSES))), (
        f"on-chip segment votes {votes}, expected {list(range(len(CLASSES)))}")


def test_on_chip_step_accuracy_is_recorded(stim, onchip):
    """Per-step accuracy after the per-segment burn-in, ON CHIP. Pinned below
    the measured values so it gates a REGRESSION, not the exact numbers
    (measured when shipped: ssb 1.000, bpsk 0.811, fsk4 0.856, noise 1.000)."""
    from gru_classifier import SEGMENT_STEPS
    from gru_stimulus import CLASSES
    words, _cells = onchip
    cls = np.asarray(words)
    accs = []
    for ci in range(len(CLASSES)):
        seg = cls[ci * SEGMENT_STEPS + 30:(ci + 1) * SEGMENT_STEPS]
        accs.append(float(np.mean(seg == ci)))
    assert min(accs) > 0.60, dict(zip(CLASSES, accs))
    assert float(np.mean(accs)) > 0.85, accs


def test_on_chip_equals_offline_accuracy_exactly(stim, onchip):
    """The chip is not merely 'about as good' as the offline model — it is the
    SAME. Stated as its own gate because 'on-chip vs offline accuracy' is the
    number the example reports, and an equal-accuracy claim built from two
    different word streams would be a coincidence, not a proof."""
    from gru_classifier import SEGMENT_STEPS, golden_classes
    from gru_stimulus import CLASSES
    iq, _ = stim
    words, _cells = onchip
    gold = golden_classes(iq)

    def accs(stream):
        s = np.asarray(stream)
        return [float(np.mean(s[ci * SEGMENT_STEPS + 30:
                                (ci + 1) * SEGMENT_STEPS] == ci))
                for ci in range(len(CLASSES))]

    assert accs(words) == accs(gold)


# --------------------------------------------------------------------------- #
#  ON-CHIP MUTATIONS — the on-chip gate must FAIL on a corrupted run (INV-4)  #
# --------------------------------------------------------------------------- #
#
# The offline mutations above prove the MODEL gate discriminates. These prove
# the ON-CHIP gate does — a gate never shown to fail certifies nothing, and the
# on-chip path has its own failure modes the offline one cannot reach.

def _run_on_chip_mutated(iq, mutate=None, n_samples=32 * 40):
    """Drive a SHORT clip through the real bitstream, optionally corrupting the
    per-sample ingress burst. Returns the emitted class words."""
    import simkyt

    import gru_classifier as G
    from gru_stimulus import to_q15
    clip = iq[:n_samples]
    _ctrl, bres, _ids, bad = G.build_chain()
    assert not bad, bad
    land = dict(bres.chips[0].input_landings)
    chip = simkyt.Chip.from_yaml(G.CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["in_re"]["entry"]))
    re = to_q15(np.real(clip)).tolist()
    im = to_q15(np.imag(clip)).tolist()
    stream = []
    for r, i in zip(re, im):
        if mutate == "swap_rails":
            burst = G._sample_burst(land, int(i), int(r))
        else:
            burst = G._sample_burst(land, int(r), int(i))
            if mutate == "drop_zcr_trigger":
                burst = burst[:-1]     # ZCR word written but never fired
        stream += burst
    chip.queue_words_physical("x16_in", stream)
    chip.run(max_events=6_000_000)
    return [int(v) & 0xFFFF
            for (v, _d, _t) in chip.read_port_words_timed("x16_out")]


@pytest.fixture(scope="module")
def short_golden(stim):
    from gru_classifier import golden_classes
    iq, _ = stim
    return golden_classes(iq[:32 * 40])


def test_on_chip_short_clip_is_exact_before_mutating(stim, short_golden):
    """The mutations below are only meaningful against an EXACT baseline."""
    iq, _ = stim
    got = _run_on_chip_mutated(iq)
    assert got == short_golden, "the unmutated short clip is not exact"


def test_on_chip_mutation_swapped_iq_rails_fails(stim, short_golden):
    """Feeding Im where Re belongs must NOT still classify correctly. This is
    the real bug this example's driver hit: the complex pair is ONE delivery
    into a shared broker, and getting it wrong left Im stuck at 0 while every
    downstream stage still looked plausible."""
    iq, _ = stim
    got = _run_on_chip_mutated(iq, "swap_rails")
    assert got != short_golden, (
        "swapping the I/Q rails still produced the golden class stream — the "
        "on-chip gate cannot tell the correct ingress from a transposed one")


def test_on_chip_mutation_starved_zcr_arm_fails(stim, short_golden):
    """Withholding the ZCR arm's trigger must break the run, not silently emit
    a stale or half-formed pair. The rendezvous is specified to STALL rather
    than mis-pair, so the expected outcome is NO output at all."""
    iq, _ = stim
    got = _run_on_chip_mutated(iq, "drop_zcr_trigger")
    assert got != short_golden
    assert not got, (
        f"a starved rendezvous emitted {len(got)} words; it is specified to "
        f"STALL, never to emit a partial or stale pair")


# --------------------------------------------------------------------------- #
#  The shipped artefacts                                                      #
# --------------------------------------------------------------------------- #
def test_shipped_golden_matches_the_on_chip_run(onchip):
    """The committed golden must BE the measured on-chip stream — not a
    plausible-looking file that nothing re-derives."""
    from gru_classifier import GOLDEN
    words, cells = onchip
    g = json.loads(GOLDEN.read_text())
    assert g["class_words"] == [int(v) for v in words], (
        "the shipped golden differs from the current on-chip run")
    assert g["n_windows"] == len(words)
    assert g["cells_used"] == cells
    assert g["agreement_vs_offline_golden"] == 1.0


def test_shipped_kyt_exists_and_is_the_design_this_module_builds():
    """The ``.kyt`` must be the SAME placement this module routes — a stale
    ``.kyt`` beside a working builder is exactly the "ships a demo that cannot
    run" failure the example bar forbids."""
    from engine.io.project_io import load_project

    from gru_classifier import BEST_KNOWN_ANCHORS, KYT
    assert KYT.is_file(), f"{KYT} missing — run build_kyt.py"
    proj = load_project(str(KYT))
    assert len(proj.blocks) == len(BEST_KNOWN_ANCHORS), (
        f"{KYT} has {len(proj.blocks)} blocks, the design has "
        f"{len(BEST_KNOWN_ANCHORS)}")
    # every block is placed, on fabric, and pairwise non-overlapping
    seen = {}
    for b in proj.blocks:
        assert b.placement is not None, f"{b.name} is unplaced in the .kyt"
        for c in b.placement.cells:
            assert 0 <= c.x < 10 and 0 <= c.y < 12, (b.name, c.x, c.y)
            assert (c.x, c.y) not in seen, (b.name, seen[(c.x, c.y)], c.x, c.y)
            seen[(c.x, c.y)] = b.name
    assert len(seen) == 65, f"expected 65 block cells in the .kyt, got {len(seen)}"


def test_shipped_kyt_carries_the_stream_metadata_the_hosted_server_needs():
    """A FAST structural guard for the fault that shipped this example broken.

    The hosted server resolves an input net's injection landing ONLY for nets
    that carry a ``stream_id`` (``engine.port_config.stream_targets`` skips the
    rest), and demuxes the recovered words by the egress net's ``out_tag``.
    Without them the server falls back to the single-net path, resolves the
    FIRST arm alone, and the other two ingress arms are never injected — the
    chain starves and the user sees nothing, while every headless gate stays
    green (``run_on_chip`` reads the landings itself).

    ``test_gru_classifier_shipped_grc_user_path`` proves the whole path, but it
    needs a live server and 100s. This costs milliseconds and fails the instant
    a regenerated ``.kyt`` drops the metadata, so the expensive gate never has
    to be the first thing that notices.
    """
    from engine.io.project_io import load_project
    from model.connection import BlockEndpoint, ChipPortEndpoint

    import gru_classifier as G
    proj = load_project(str(G.KYT))

    ingress = [c for c in proj.connections
               if isinstance(c.source, ChipPortEndpoint)
               and c.source.port == "x16_in"
               and isinstance(c.target, BlockEndpoint)]
    assert len(ingress) == 3, (
        f"expected the 3 ingress arms (re, im, zcr), got {len(ingress)}")
    missing = [c.name for c in ingress if not getattr(c, "stream_id", None)]
    assert not missing, (
        f"ingress nets without a stream_id: {missing} — the hosted server will "
        f"resolve only the first arm and starve the rest (this is exactly how "
        f"the example shipped broken)")
    sids = {c.stream_id for c in ingress}
    assert sids == {G.STREAM_ID}, (
        f"ingress arms must share the ONE stream_id the .grc names "
        f"({G.STREAM_ID!r}); got {sids}")

    egress = [c for c in proj.connections
              if isinstance(c.source, BlockEndpoint)
              and isinstance(c.target, ChipPortEndpoint)
              and c.target.port == "x16_out"]
    assert len(egress) == 1, f"expected 1 egress net, got {len(egress)}"
    assert getattr(egress[0], "out_tag", None) is not None, (
        "the egress net carries no out_tag — the sink cannot demux its words")


def test_the_shipped_kyt_FILE_itself_computes_the_golden():
    """THE STRONGEST ARTEFACT CLAIM: load the committed ``.kyt`` FROM DISK,
    build THAT project, and run the shipped stimulus through the resulting
    bitstream — the class stream must equal the shipped golden.

    ``test_shipped_kyt_exists_and_is_the_design_this_module_builds`` compares
    the file's GEOMETRY to the design; this one closes the remaining gap by
    proving the FILE computes. A ``.kyt`` that matched on geometry but had, say,
    a stale routed corridor would pass that gate and fail this one. It is what
    the user actually opens, so it is what has to work.
    """
    import simkyt
    from engine.build import BuildEngine
    from engine.io.project_io import load_project

    import gru_classifier as G
    from gru_stimulus import make_stimulus, to_q15
    (BC, lct, _AC, _CPE, _BE) = _engine()
    cat = BC.from_gr_kyttar()
    ct = lct(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"

    proj = load_project(str(G.KYT))
    res = BuildEngine(cat, CHIP_YAML).build(proj, {ctk: ct})
    assert res.ok, f"the shipped .kyt does not build: {res.errors[:3]}"
    used = sum(c.cell_count for c in res.chips.values())

    land = dict(res.chips[0].input_landings)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", int(land["in_re"]["entry"]))
    iq, _truth = make_stimulus()
    re_q = to_q15(np.real(iq)).tolist()
    im_q = to_q15(np.imag(iq)).tolist()
    stream = []
    for r, i in zip(re_q, im_q):
        stream += G._sample_burst(land, int(r), int(i))
    chip.queue_words_physical("x16_in", stream)
    chip.run(max_events=40_000_000)
    words = [int(v) & 0xFFFF
             for (v, _d, _t) in chip.read_port_words_timed("x16_out")]

    golden = json.loads(G.GOLDEN.read_text())
    assert used == golden["cells_used"], (
        f"the shipped .kyt builds in {used} cells, the golden records "
        f"{golden['cells_used']}")
    assert words == golden["class_words"], (
        "the SHIPPED .kyt does not reproduce the shipped golden — the file a "
        "user opens is not the design that was verified")


def test_shipped_grc_targets_the_gui_default_server_port():
    """``server_port: 0`` silently no-ops (the kyttar_source never connects and
    the window stays blank with a plausible axis). Every kyttar source/sink in
    the shipped flowgraph must name the GUI's default host port."""
    from gru_classifier import GRC
    assert GRC.is_file(), f"{GRC} missing"
    text = GRC.read_text()
    ports = re.findall(r"^\s*server_port:\s*'?(\d+)'?\s*$", text, flags=re.M)
    assert ports, "no server_port found in the shipped .grc"
    assert set(ports) == {"58950"}, (
        f"the shipped .grc must use the GUI's default port 58950; found "
        f"{sorted(set(ports))}")


def test_shipped_grc_scopes_are_sized_to_actually_paint():
    """A QT time_sink draws NOTHING until a FULL ``size`` buffer arrives, and
    the GR scheduler strands the tail of a finite stream — so a scope sized
    >= its burst NEVER paints. Both scopes must be sized BELOW the burst."""
    from gru_classifier import GRC
    text = GRC.read_text()
    sizes = re.findall(r"^\s*size:\s*(.+)$", text, flags=re.M)
    assert sizes, "no time-sink size found in the shipped .grc"
    for s in sizes:
        assert "n_windows" in s and "-" in s, (
            f"scope size {s!r} is not derived BELOW the burst length; a scope "
            f"sized >= its burst never draws")


def test_shipped_grc_does_not_rescale_the_raw_class_stream():
    """The class scope must be fed the sink's stream DIRECTLY.

    This chain's source is COMPLEX, so ``output_words='auto'`` ties the recovered
    stream to the RAW convention: the class index 0..3 arrives as 0.0..3.0,
    already the value the scope should plot. The shipped .grc used to put a
    ``x32768`` multiply in front of it — the q15 convention, which applies to
    REAL-input chains — driving every sample to 0/32768/65536/98304, far outside
    the scope's ``[-0.5, 3.5]`` y-axis. Correct chip data, unreadable window.

    Guarded statically because it is a one-line edit away from returning, and
    the expensive user-path gate is the only other thing that would catch it.
    """
    import yaml

    from gru_classifier import GRC
    doc = yaml.safe_load(GRC.read_text())
    ids = {b["name"]: b["id"] for b in doc["blocks"]}
    scalers = [n for n, i in ids.items()
               if i in ("blocks_multiply_const_vxx", "blocks_divide_xx",
                        "blocks_multiply_xx")]
    assert not scalers, (
        f"the shipped .grc rescales again ({scalers}) — a RAW (complex-input) "
        f"class stream must reach the scope unscaled")

    # and the sink must feed the class scope DIRECTLY (nothing spliced between)
    conns = [(a, b) for a, _pa, b, _pb in
             (tuple(c) for c in doc["connections"])]
    assert ("chip_sink", "cls_scope") in conns, (
        f"chip_sink no longer feeds cls_scope directly; the class stream is "
        f"RAW and must not pass through a converter. Connections: {conns}")


def test_the_installed_stimulus_module_is_the_examples_stimulus():
    """The flowgraph imports ``kyttar.gru_demo_stim`` (the installed package
    cannot reach the example tree), so that copy MUST produce the identical
    clip. Asserted rather than trusted — a drifted copy would make the GUI demo
    show a different signal from the one every gate here measures."""
    import importlib.util

    from gru_stimulus import make_stimulus
    src = (_ROOT / "gr-kyttar" / "python" / "kyttar" / "gru_demo_stim.py")
    assert src.is_file(), f"{src} missing — the shipped .grc imports it"
    # load the FILE directly: importing the package pulls in gnuradio, which
    # the verification venv deliberately does not have.
    spec = importlib.util.spec_from_file_location("_gru_demo_stim", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a_iq, a_truth = make_stimulus()
    b_iq, b_truth = mod.make_stimulus()
    assert np.array_equal(a_iq, b_iq), (
        "the installed stimulus module produces a DIFFERENT clip from "
        "examples/gru_classifier/gru_stimulus.py")
    assert np.array_equal(a_truth, b_truth)
    assert mod.n_samples() == len(a_iq)
    assert mod.n_windows() == len(a_truth)


# --------------------------------------------------------------------------- #
#  Placement detail                                                           #
# --------------------------------------------------------------------------- #

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


def test_coplacement_the_gru_leaves_room_for_the_front_end():
    """CO-PLACEMENT — the gate that used to decide whether the example could
    ship, now asserting that it does.

    Places the wide-flat GRUCellBlock together with the whole feature front end
    on ONE 10x12 with both 16-bit ports, and asserts:

      * every block places legally together — on fabric, pairwise
        non-overlapping, 65 of 120 cells; and
      * EVERY net routes.

    For four dispatches the second assertion was its negation: the chain was
    always exactly ONE net short, and this gate pinned that wall so the day it
    lifted would be visible. It lifted when GRUCellBlock was re-folded WIDE-FLAT
    under the CHIP_SCALE placement class (see gru_classifier.py's ROUTING
    HISTORY). The block-cell count is UNCHANGED at 65 — the fix was the shape
    of the free space, not its size, which is why the count is still asserted
    exactly here.
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

    # 2. and EVERY net routes.
    G._connect(ctrl, CPE, BE, ids)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    bad = [f"{r.name}:{r.reason}" for r in rep.results if not r.ok]
    assert not bad, f"the chain no longer co-places and routes: {bad}"


def test_the_wide_fold_is_what_makes_the_front_end_fit():
    """WHY it fits, asserted as geometry rather than narrated.

    The GRU is 10 wide x 6 tall, so it leaves six FULL-WIDTH free rows as one
    contiguous through-channel — and the six front-end blocks all live in that
    band. Under the previous <= 8-across fold the block was 8 wide and its free
    space was fragmented perimeter, which is the difference the ROUTING HISTORY
    note explains. If the fold narrows again, this fails with the reason.
    """
    import gru_classifier as G
    from gr_kyttar.placement.blocks import GRUCellBlock
    assert GRUCellBlock.CHIP_SCALE is True, (
        "GRUCellBlock is no longer chip-scale; the wide-flat fold is what "
        "makes this example fit")
    lay = GRUCellBlock("g").default_layout()
    xs = [v[0] for v in lay.values()]
    ys = [v[1] for v in lay.values()]
    w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    assert (w, h) == (10, 6), f"the GRU fold moved to {w}x{h}"
    # the GRU occupies a solid band; the front end lives entirely outside it.
    gy = G.BEST_KNOWN_ANCHORS["gru"][1]
    band = range(gy, gy + h)
    for nm, (ax, ay) in G.BEST_KNOWN_ANCHORS.items():
        if nm == "gru":
            continue
        assert ay not in band, (
            f"front-end block {nm} is anchored inside the GRU's row band "
            f"{list(band)}")


def test_known_limit_the_join_tail_alone_does_route():
    """The wall is the RMS arm's corridors, NOT the join->GRU tail: the tail
    (both ingress nets, the join, the GRU, the egress) routes on its own —
    measured at 81 of 120 cells. Kept as a REGRESSION SPLIT: if the whole chain
    ever stops routing, this says immediately whether the tail or the RMS arm
    is responsible."""
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


def test_the_gru_alone_dominates_the_array():
    """The scale of the problem, pinned: GRUCellBlock is 51 cells — 43% of the
    array — and costs several more for its own two port corridors, leaving the
    14-cell front end and its four extra nets to fit in what remains. That is
    why the example was blocked for four dispatches, and why it is still tight
    (the whole chain builds at 102 of 120).

    MEASURED AT THE BLOCK'S BEST ANCHOR, which is the honest way to state a
    fold's port cost: seated at row 0 the wide-flat fold and its two corridors
    build in 58 cells, against 64 for the 8x7 fold it replaced.

    The example does NOT use that anchor — it seats the block at row 6 (58 ->
    70 cells for this measurement, +2 per row) precisely so the front end gets
    the six port-side rows. That trade is deliberate and it is what
    ``test_the_wide_fold_is_what_makes_the_front_end_fit`` covers: the wide
    fold wins on the SHAPE of the free space it leaves, not on its own corridor
    cost, and the whole-chain figure (102/120) is the number that settles it.
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
    # the block's BEST anchor: a 10-wide block has exactly one legal column,
    # and row 0 puts its single I/O edge alongside the chip's two row-0 ports.
    gx, gy = 0, 0
    g = ctrl.place_block("GRUCellBlock", 0, gx, gy, library=G.LIB, params={})
    b0 = ctrl.project.blocks[0]
    cells, _ = ctrl.default_cells(b0.type, b0.library, 0, gx, gy, b0.params)
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
        f"GRU + its two port corridors measured {used} at its best anchor; the "
        f"7x8 fold cost 64 and the wide-flat fold measured 58, so no re-fold "
        f"may regress past that ceiling")
    # and the trade the example actually takes is +2 cells per row of descent,
    # which is why seating it low is affordable at all.
    assert used <= 60, (
        f"the wide-flat fold measured 58 at row 0; {used} means the block's "
        f"own corridors got materially more expensive")
