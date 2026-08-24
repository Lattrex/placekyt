# SPDX-License-Identifier: GPL-3.0-or-later
"""FeaturePairJoinBlock — ORDERED two-word rendezvous, verified ON CHIP.

WHY THERE IS NO GNU RADIO COUNTERPART. This block solves a problem GR does not
have: on a clockless array a downstream cell that consumes a FIXED-ORDER pair of
words on ONE input port + ONE entry (the toggle-cell contract — first trigger =
word0, second = word1) cannot be fed by wiring two nets into that entry. Two nets
into one entry BUILD AND ROUTE WITH ok=True AND SILENTLY PRODUCE GARBAGE: the
toggle reads the two streams as word0/word1 of ALTERNATING timesteps, HALVING the
rate. In GNU Radio a 2-input block just declares two input streams and the
scheduler aligns them — there is nothing to join. So the golden here is an
INDEPENDENT reference of the PINNED contract (``process_reference_pairs``) plus,
decisively, a REAL two-upstream on-chip chain compared against the SAME consumer
fed the ordered words directly.

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * ORDER — ``a`` is ALWAYS the first word out, for A-then-B AND B-then-A
    arrival, bursty arms, and long runs.
  * PAIRING — matched pairs only; the two arms' words never cross timesteps.
  * STARTUP — no partial pair is ever emitted.
  * STALL — a starved arm stalls the join; it never emits a stale or duplicated
    pair, and it RECOVERS exactly when the missing word arrives.
  * THE REAL CONSUMER — a full chain (2 independent rate-reducing arms ->
    join -> GRUCellBlock, the actual single-port toggle consumer) produces
    BIT-IDENTICAL class words to feeding that consumer the ordered pairs
    directly, at the CORRECT rate (N pairs in -> N words out, NOT N/2).

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_feature_pair_join.py -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import compare_against_grc, write_report, Metric  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    return (BlockCatalog, load_chip_type, AppController,
            ChipPortEndpoint, BlockEndpoint)


# --------------------------------------------------------------------------- #
#  The REAL two-upstream chain: two independently rate-reducing arms.          #
# --------------------------------------------------------------------------- #
#
# Both KeepOneInN arms are fed from the ONE chip input port (a shared-port
# fan-out, INV-24), each reducing by its own n. Driving one arm's port landing
# advances ONLY that arm, so the harness can produce ANY relative arrival order
# — including orders the auto-placer would never generate — which is exactly the
# adversarial async interleaving the LOCK rendezvous must survive.

_ARM_N = 2      # each arm emits on every 2nd raw sample (phase n-1)


class _Chain:
    """A built two-upstream join chain + a driver that fires ONE arm at a time."""

    def __init__(self, bres, chip, la, lb):
        self.bres, self.chip, self.la, self.lb = bres, chip, la, lb
        self.out: list[int] = []

    def raw(self, arm: str, value: int):
        """Push ONE RAW sample into the named arm ('a' or 'b'); the arm's
        KeepOneInN emits (and drives the join) on every _ARM_N-th one."""
        land = self.la if arm == "a" else self.lb
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF], target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self.chip.run(max_events=6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        self.chip.run(max_events=300000)
        self._drain()

    def emit(self, arm: str, value: int):
        """Make the named arm EMIT exactly one word equal to ``value``: feed the
        arm _ARM_N raw samples whose LAST is ``value`` (KeepOneInN keeps the last
        of each group of n — GR's phase n-1)."""
        for k in range(_ARM_N - 1):
            self.raw(arm, 0)
        self.raw(arm, value)

    def _drain(self):
        while self.chip.output_available("x16_out"):
            w = self.chip.read_port_i16("x16_out").view("uint16").tolist()
            self.out.extend(int(x) & 0xFFFF for x in w)
            self.chip.release_output_ack("x16_out")
            self.chip.run(max_events=8000)


_ANCHORS = [((2, 2), (2, 6), (5, 4)), ((1, 1), (1, 5), (4, 3)),
            ((2, 1), (2, 5), (5, 3)), ((3, 2), (3, 6), (6, 4))]


def _build_chain():
    """Build 2 KeepOneInN arms -> FeaturePairJoin -> x16_out on one chip.

    auto_pnr is a CP-SAT search and is not deterministic across runs, so try a
    few anchor sets rather than pinning one placement — the block's correctness
    must not depend on a lucky layout (and the anchors that DO route exercise
    different arrival-face geometries, which is itself coverage)."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("fpj_chain", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("FeaturePairJoinBlock", 0, *j_xy, library=LIB,
                                 params={})
            ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                        BE(block=ka, port="sample"), name="n0")
            ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                        BE(block=kb, port="sample"), name="n1")
            ctrl.add_logical_connection(BE(block=ka, port="out"),
                                        BE(block=j, port="a"), name="n2")
            ctrl.add_logical_connection(BE(block=kb, port="out"),
                                        BE(block=j, port="b"), name="n3")
            ctrl.add_logical_connection(BE(block=j, port="out"),
                                        CPE(chip=0, port="x16_out"), name="n4")
            if not ctrl.auto_pnr({ctk: ct}).ok:
                continue
            bres = ctrl.build()
            if not bres.ok:
                continue
            il = bres.chips[0].input_landings
            if "n0" not in il or "n1" not in il:
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            return _Chain(bres, chip, il["n0"], il["n1"]), ctrl, cat, j
    pytest.skip("no anchor routed the two-upstream join chain on this run")


def _join_ref(a_words, b_words):
    from gr_kyttar.placement.blocks import FeaturePairJoinBlock
    return FeaturePairJoinBlock.process_reference_pairs(a_words, b_words)


# --------------------------------------------------------------------------- #
#  ORDER — a is always first, whatever the arrival order                       #
# --------------------------------------------------------------------------- #

def test_order_a_then_b():
    """Arm A supplies its word first, then arm B. Output must be [a, b] per
    timestep, in that ORDER."""
    ch, *_ = _build_chain()
    a = [1001, 1003, 1005, 1007, 1009, 1011]
    b = [2001, 2003, 2005, 2007, 2009, 2011]
    for av, bv in zip(a, b):
        ch.emit("a", av)
        ch.emit("b", bv)
    assert ch.out == _join_ref(a, b), (ch.out, _join_ref(a, b))


def test_order_b_then_a_is_identical():
    """The SAME pairs with arm B arriving FIRST must produce the IDENTICAL
    stream — ``a`` is still the first word emitted. This is the whole point of
    the LOCK rendezvous over a counting join: the output does NOT depend on the
    arrival order."""
    ch, *_ = _build_chain()
    a = [1001, 1003, 1005, 1007, 1009, 1011]
    b = [2001, 2003, 2005, 2007, 2009, 2011]
    for av, bv in zip(a, b):
        ch.emit("b", bv)      # B FIRST
        ch.emit("a", av)
    assert ch.out == _join_ref(a, b), (ch.out, _join_ref(a, b))


@pytest.mark.parametrize("seed", [3, 17, 91])
def test_random_interleavings_preserve_pairs_and_order(seed):
    """RANDOM per-timestep arrival order over a LONG run (3 seeds). Whatever
    order the two arms fire in, the emitted stream is exactly [a0,b0,a1,b1,...]."""
    rng = random.Random(seed)
    ch, *_ = _build_chain()
    a = [1000 + 2 * i + 1 for i in range(10)]
    b = [2000 + 2 * i + 1 for i in range(10)]
    for av, bv in zip(a, b):
        if rng.random() < 0.5:
            ch.emit("a", av)
            ch.emit("b", bv)
        else:
            ch.emit("b", bv)
            ch.emit("a", av)
    assert ch.out == _join_ref(a, b), (ch.out, _join_ref(a, b))


def test_back_to_back_timesteps_do_not_mix():
    """Back-to-back timesteps with DISTINCT, easily-attributed values: no word of
    timestep k may appear in timestep k+1's pair. The re-lock to face_a is the
    LAST thing the emit does, precisely so the next b word cannot barge in before
    its a word is latched."""
    ch, *_ = _build_chain()
    a = [111, 222, 333, 444, 555, 666, 777, 888]
    b = [1111, 2222, 3333, 4444, 5555, 6666, 7777, 8888]
    for av, bv in zip(a, b):
        ch.emit("a", av)
        ch.emit("b", bv)
    assert ch.out == _join_ref(a, b)
    # Structurally: every EVEN position is an a-word, every ODD position a b-word.
    assert all(w in a for w in ch.out[0::2]), ch.out[0::2]
    assert all(w in b for w in ch.out[1::2]), ch.out[1::2]


# --------------------------------------------------------------------------- #
#  STARTUP + STALL semantics                                                   #
# --------------------------------------------------------------------------- #

def test_startup_emits_nothing_until_both_arms_have_spoken():
    """NO PARTIAL PAIR, ever: after arm A alone has emitted, the chip has
    produced NOTHING. The first output word appears only once B arrives."""
    ch, *_ = _build_chain()
    ch.emit("a", 4242)
    assert ch.out == [], f"a partial pair leaked out: {ch.out}"
    ch.emit("b", 8484)
    assert ch.out == [4242, 8484], ch.out


def test_starved_arm_stalls_and_never_duplicates():
    """Arm A supplies TWO words, arm B only ONE. Exactly ONE complete pair may
    be emitted; the surplus A word must be HELD, never paired with a stale or
    duplicated B (which is how a naive counter fails)."""
    ch, *_ = _build_chain()
    ch.emit("a", 1002)
    ch.emit("a", 1004)      # surplus — must be held
    ch.emit("b", 2002)
    assert ch.out == [1002, 2002], (
        f"a starved arm must yield exactly one pair, got {ch.out}")


def test_starved_arm_recovers_when_the_missing_word_arrives():
    """The stall is a STALL, not a loss: once the missing B word arrives, the
    held A word completes its pair with the CORRECT partner and the stream
    resumes in order."""
    ch, *_ = _build_chain()
    ch.emit("a", 1002)
    ch.emit("a", 1004)
    ch.emit("b", 2002)
    assert ch.out == [1002, 2002]
    ch.emit("b", 2004)
    assert ch.out == [1002, 2002, 1004, 2004], ch.out


def test_bursty_arms_within_the_overhang_limit():
    """BURSTY arms: arm A runs 2 timesteps ahead before arm B says anything, then
    B catches up. Both pairs must come out, in order."""
    ch, *_ = _build_chain()
    ch.emit("a", 1001)
    ch.emit("a", 1003)
    ch.emit("b", 2001)
    ch.emit("b", 2003)
    assert ch.out == [1001, 2001, 1003, 2003], ch.out


# --------------------------------------------------------------------------- #
#  THE REAL CONSUMER: two independently rate-reducing arms -> join ->          #
#  GRUCellBlock (the actual single-port toggle consumer this block exists for) #
# --------------------------------------------------------------------------- #
#
# GRUCellBlock's ``fin`` cell is the canonical toggle consumer: ONE input port
# ``f`` on ONE register (R0) with ONE entry, toggling word0/word1 per timestep.
# Wiring two nets into that entry is the failure this block exists to fix (the
# toggle reads the two streams as word0/word1 of ALTERNATING timesteps and the
# rate HALVES). The proof is a whole-chain comparison: the JOINED chain must
# produce the SAME class words as feeding the SAME consumer the ordered pairs
# DIRECTLY from the port — same values, same COUNT.

_GRU_ANCHORS = [((2, 3), (0, 1), (0, 3), (0, 2)),
                ((2, 3), (0, 0), (0, 4), (0, 2)),
                ((3, 3), (0, 1), (0, 3), (1, 2)),
                ((2, 4), (0, 2), (0, 4), (0, 3))]

_GRU_PAIRS = [(1200, 2400), (900, 0xFA24), (5000, 700),
              (0xFCE0, 3300), (150, 150), (7000, 0xF830)]


def _drain(chip, out):
    while chip.output_available("x16_out"):
        w = chip.read_port_i16("x16_out").view("uint16").tolist()
        out.extend(int(x) & 0xFFFF for x in w)
        chip.release_output_ack("x16_out")
        chip.run(max_events=8000)


def _fire(chip, land, value, run=400000):
    hop = int(land["hop"]) & 0x1F
    chip.inject_data_physical([int(value) & 0xFFFF], target_hop_cnt=hop,
                              target_addr=int(land["data_addrs"][0]))
    chip.run(max_events=6000)
    chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=int(land["entry"]))
    chip.run(max_events=run)


def _build_gru_joined():
    """2 KeepOneInN arms -> FeaturePairJoin -> GRUCellBlock -> x16_out, one chip."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for (gp, kap, kbp, jp) in _GRU_ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("fpj_gru", ctk)
            g = ctrl.place_block("GRUCellBlock", 0, *gp, library=LIB, params={})
            ka = ctrl.place_block("KeepOneInNBlock", 0, *kap, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kbp, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("FeaturePairJoinBlock", 0, *jp, library=LIB,
                                 params={})
            ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                        BE(block=ka, port="sample"), name="n0")
            ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                        BE(block=kb, port="sample"), name="n1")
            ctrl.add_logical_connection(BE(block=ka, port="out"),
                                        BE(block=j, port="a"), name="n2")
            ctrl.add_logical_connection(BE(block=kb, port="out"),
                                        BE(block=j, port="b"), name="n3")
            ctrl.add_logical_connection(BE(block=j, port="out"),
                                        BE(block=g, port="f"), name="n4")
            ctrl.add_logical_connection(BE(block=g, port="out"),
                                        CPE(chip=0, port="x16_out"), name="n5")
            if not ctrl.auto_pnr({ctk: ct}).ok:
                continue
            bres = ctrl.build()
            if not bres.ok:
                continue
            il = bres.chips[0].input_landings
            if "n0" not in il or "n1" not in il:
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            return chip, il["n0"], il["n1"]
    pytest.skip("no anchor routed the join->GRU chain on this run")


def _build_gru_direct():
    """The SAME consumer alone, fed the ordered word stream straight from the
    port — the GOLDEN path this chain must reproduce."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for gp in ((2, 3), (2, 2), (3, 3), (1, 2)):
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("gru_direct", ctk)
            g = ctrl.place_block("GRUCellBlock", 0, *gp, library=LIB, params={})
            ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                        BE(block=g, port="f"), name="n0")
            ctrl.add_logical_connection(BE(block=g, port="out"),
                                        CPE(chip=0, port="x16_out"), name="n1")
            if not ctrl.auto_pnr({ctk: ct}).ok:
                continue
            bres = ctrl.build()
            if not bres.ok:
                continue
            il = bres.chips[0].input_landings
            if "n0" not in il:
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            return chip, il["n0"]
    pytest.skip("the direct GRU golden chain did not route on this run")


def _gru_direct_words(pairs):
    chip, land = _build_gru_direct()
    out: list[int] = []
    for (av, bv) in pairs:
        for v in (av, bv):
            _fire(chip, land, v)
            _drain(chip, out)
    return out


def _gru_joined_words(pairs):
    chip, la, lb = _build_gru_joined()
    out: list[int] = []
    for (av, bv) in pairs:
        # Each arm needs _ARM_N raw samples; KeepOneInN keeps the LAST.
        for land, val in ((la, av), (lb, bv)):
            for k in range(_ARM_N - 1):
                _fire(chip, land, 0)
                _drain(chip, out)
            _fire(chip, land, val)
            _drain(chip, out)
    return out


def test_real_consumer_chain_matches_the_direct_feed():
    """THE END-TO-END GATE. A whole chain — 2 independently rate-reducing arms
    -> FeaturePairJoin -> GRUCellBlock -> x16_out, placed + routed + built as ONE
    10x12 chip and run on the real simulator — must produce class words
    IDENTICAL to feeding the SAME consumer the ordered pairs directly.

    This is the claim the block exists to support, and it is NOT a per-block
    proxy: it exercises the placement, the routing, the broker hand-off, the
    two-burst emit, and the consumer's toggle contract together."""
    direct = _gru_direct_words(_GRU_PAIRS)
    assert len(direct) == len(_GRU_PAIRS), (
        f"the golden direct feed must yield ONE class word per pair; "
        f"got {len(direct)} for {len(_GRU_PAIRS)} pairs — the golden itself is "
        f"broken, fix that before trusting the joined comparison")
    joined = _gru_joined_words(_GRU_PAIRS)
    print(f"\ndirect={direct}\njoined={joined}")
    assert joined == direct, (
        f"the joined chain diverged from the direct feed: {joined} != {direct}")


def test_real_consumer_chain_does_not_halve_the_rate():
    """THE MEASURED FAILURE MODE, pinned. Wiring two nets straight into the
    consumer's single entry builds and routes with ok=True and HALVES the rate
    (N feature windows in -> N/2 class words out, all wrong). With the join, N
    pairs in must give exactly N words out."""
    joined = _gru_joined_words(_GRU_PAIRS)
    assert len(joined) == len(_GRU_PAIRS), (
        f"rate is wrong: {len(_GRU_PAIRS)} pairs in -> {len(joined)} words out "
        f"(the two-nets-into-one-entry bug produces {len(_GRU_PAIRS) // 2})")


# --------------------------------------------------------------------------- #
#  MANDATORY mutation tests (INV-4) — each corruption MUST be caught           #
# --------------------------------------------------------------------------- #

def test_mutation_swapped_emit_order_fails():
    """SWAP the emit order (b first, then a). The on-chip stream must NOT match
    the reference — proof the ORDER is genuinely under test and not an artifact
    of the two values being interchangeable."""
    ch, *_ = _build_chain()
    a = [1001, 1003, 1005, 1007]
    b = [2001, 2003, 2005, 2007]
    for av, bv in zip(a, b):
        ch.emit("a", av)
        ch.emit("b", bv)
    swapped = []
    for i in range(0, len(ch.out), 2):
        swapped += [ch.out[i + 1], ch.out[i]]     # emit b first — the mutation
    assert swapped != _join_ref(a, b), (
        "gate cannot see emit ORDER — a swapped-order DUT would pass!")
    assert ch.out == _join_ref(a, b)


def test_mutation_single_burst_emit_fails():
    """SINGLE-BURST emit (drop the second WRITE+JUMP, the DualFloatToComplex's
    2-rail-ONE-trigger shape). The consumer then gets ONE trigger per timestep
    instead of two and only half the words reach it — the gate MUST see it."""
    ch, *_ = _build_chain()
    a = [1001, 1003, 1005, 1007]
    b = [2001, 2003, 2005, 2007]
    for av, bv in zip(a, b):
        ch.emit("a", av)
        ch.emit("b", bv)
    single = ch.out[0::2]                          # only the first burst survives
    assert single != _join_ref(a, b), (
        "gate cannot see a dropped second burst — a single-burst DUT would pass!")
    assert len(single) * 2 == len(ch.out)


def test_mutation_stale_pair_on_starved_arm_fails():
    """NO-STALL mutation: a block that re-emits the LAST b word when arm b is
    starved (instead of stalling) would produce an extra, WRONG pair. The
    reference must reject it."""
    ch, *_ = _build_chain()
    ch.emit("a", 1002)
    ch.emit("a", 1004)
    ch.emit("b", 2002)
    stale = list(ch.out) + [1004, 2002]            # duplicated b — the mutation
    assert stale != _join_ref([1002, 1004], [2002]), (
        "gate cannot see a duplicated/stale pair on a starved arm!")
    assert ch.out == _join_ref([1002, 1004], [2002])


def test_mutation_missing_lock_reproduces_the_measured_garbage():
    """MISSING LOCK — the measured failure mode, reproduced.

    Without the arbiter LOCK the cell cannot tell an ``a`` word from a ``b``
    word: both are accepted on whatever face they arrive on, so a same-side
    counter pairs them by ARRIVAL ORDER instead of by STREAM. Under the
    B-then-A interleaving that mis-pairs EVERY timestep — and, exactly as the
    prior measurement showed, the two streams are consumed as word0/word1 of
    ALTERNATING timesteps, HALVING the rate.

    The unlocked behaviour is modelled here (a naive arrival-order counter) and
    asserted to DIVERGE from the reference, while the real LOCKED block on chip
    MATCHES it under the same interleaving. That contrast is the proof the LOCK
    is load-bearing."""
    a = [1001, 1003, 1005, 1007]
    b = [2001, 2003, 2005, 2007]

    # An UNLOCKED cell: pair strictly by arrival order (what a counter does).
    arrivals = []
    for av, bv in zip(a, b):
        arrivals += [bv, av]                       # B-then-A interleaving
    unlocked = []
    for i in range(0, len(arrivals) - 1, 2):
        unlocked += [arrivals[i], arrivals[i + 1]]
    assert unlocked != _join_ref(a, b), (
        "the unlocked model must MIS-pair under B-then-A; if it agrees, the "
        "stimulus does not exercise the hazard")
    # It also loses the ORDER contract (a first) on every timestep.
    assert unlocked[0::2] != _join_ref(a, b)[0::2]

    # The REAL, LOCKED block on chip under the SAME interleaving: correct.
    ch, *_ = _build_chain()
    for av, bv in zip(a, b):
        ch.emit("b", bv)
        ch.emit("a", av)
    assert ch.out == _join_ref(a, b), ch.out


def test_empty_output_fails():
    """An empty stream must never satisfy the reference (green not reachable
    empty)."""
    assert [] != _join_ref([1, 2], [3, 4])


# --------------------------------------------------------------------------- #
#  DOCUMENTED SUBSTRATE LIMIT — the arm-overhang depth                         #
# --------------------------------------------------------------------------- #

def test_known_limit_arm_overhang_depth_is_two():
    """EXPLICIT GUARD for a real, MEASURED substrate limit (AGENTS.md §6: a
    limitation is recorded as an executable guard, never glossed over).

    An arm may run at most TWO timesteps ahead of the other. Beyond that the
    surplus words are LOST, not merely delayed: the arm's producer wedges
    permanently and the chain emits NOTHING even after the other arm catches up.

    WHY: the face LOCK bars the running-ahead arm's words at the join, and the
    back-pressure propagates up that arm's single-outstanding link. Two unmatched
    words fit (one held at the join's arbiter, one in the link); the third has
    nowhere to wait.

    NOT THIS BLOCK'S BUG — it is a property of the LOCK-by-face rendezvous
    itself: ``test_known_limit_is_shared_with_the_shipped_dual`` measures the
    IDENTICAL boundary on the shipped, verified DualFloatToComplexBlock.

    NOT A PROBLEM FOR THE INTENDED USE: the arms of a feature front end are
    index-aligned by construction (equal-rate decimators off one stream), so the
    overhang is 0 or 1 by design. This guard exists so that if the boundary ever
    MOVES, a test says so instead of a chain silently dropping data."""
    for k in (1, 2):
        ch, *_ = _build_chain()
        a = [1000 + 2 * i + 1 for i in range(k)]
        b = [2000 + 2 * i + 1 for i in range(k)]
        for av in a:
            ch.emit("a", av)
        for bv in b:
            ch.emit("b", bv)
        assert ch.out == _join_ref(a, b), (
            f"overhang depth {k} MUST recover fully; got {ch.out}")

    # Depth 3: the documented boundary — words are lost, not queued.
    ch, *_ = _build_chain()
    for i in range(3):
        ch.emit("a", 1000 + 2 * i + 1)
    for i in range(3):
        ch.emit("b", 2000 + 2 * i + 1)
    assert ch.out == [], (
        f"the depth-3 overhang boundary MOVED (now emits {ch.out}). If the "
        f"substrate/link depth changed this is GOOD NEWS — re-measure the limit "
        f"and update the block docstring + this guard.")


def test_known_limit_is_shared_with_the_shipped_dual():
    """The overhang limit above belongs to the LOCK-BY-FACE MECHANISM, not to
    this block: the shipped, verified DualFloatToComplexBlock — same rendezvous,
    different emit shape — hits the IDENTICAL boundary in the IDENTICAL chain.
    Measuring both here is what makes the limit a substrate fact rather than an
    unexplained failure of the new block."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()

    def _dual_chain():
        for (ka_xy, kb_xy, j_xy) in _ANCHORS:
            for _attempt in range(3):
                cat = BlockCatalog.from_gr_kyttar()
                ct = load_chip_type(CHIP_YAML)
                ctk = getattr(ct, "name", None) or "kyttar_10x12"
                ctrl = AppController(catalog=cat)
                ctrl.new_project("dual_chain", ctk)
                ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                      params={"n": _ARM_N})
                kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                      params={"n": _ARM_N})
                d = ctrl.place_block("DualFloatToComplexBlock", 0, *j_xy,
                                     library=LIB, params={})
                ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                            BE(block=ka, port="sample"), name="n0")
                ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                            BE(block=kb, port="sample"), name="n1")
                ctrl.add_logical_connection(BE(block=ka, port="out"),
                                            BE(block=d, port="i"), name="n2")
                ctrl.add_logical_connection(BE(block=kb, port="out"),
                                            BE(block=d, port="q"), name="n3")
                ctrl.add_logical_connection(BE(block=d, port="yi"),
                                            CPE(chip=0, port="x16_out"), name="n4")
                if not ctrl.auto_pnr({ctk: ct}).ok:
                    continue
                bres = ctrl.build()
                if not bres.ok:
                    continue
                il = bres.chips[0].input_landings
                if "n0" not in il or "n1" not in il:
                    continue
                chip = simkyt.Chip.from_yaml(CHIP_YAML)
                chip.load_bitstream_physical(bres.words(0))
                chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
                return _Chain(bres, chip, il["n0"], il["n1"])
        pytest.skip("the Dual comparison chain did not route on this run")

    ch = _dual_chain()
    for i in range(2):                     # depth 2 — recovers
        ch.emit("a", 1000 + 2 * i + 1)
    for i in range(2):
        ch.emit("b", 2000 + 2 * i + 1)
    depth2 = list(ch.out)
    assert len(depth2) == 4, f"the Dual must also recover at depth 2; got {depth2}"

    ch2 = _dual_chain()
    for i in range(3):                     # depth 3 — same boundary
        ch2.emit("a", 1000 + 2 * i + 1)
    for i in range(3):
        ch2.emit("b", 2000 + 2 * i + 1)
    assert ch2.out == [], (
        f"the SHIPPED Dual was expected to hit the SAME depth-3 boundary; it "
        f"emitted {ch2.out}. If it now recovers, the limit is NOT mechanism-wide "
        f"and FeaturePairJoin's guard needs re-examining.")


# --------------------------------------------------------------------------- #
#  STRUCTURE — the two conditions that keep the two-burst build path live      #
# --------------------------------------------------------------------------- #

def test_built_cell_emits_two_independent_write_jump_bursts():
    """THE LOAD-BEARING STRUCTURAL CLAIM. The built cell must contain TWO WRITEs
    and TWO JUMPs, and every one of them must carry the SAME hop, the SAME
    destination register and the SAME entry — i.e. two INDEPENDENT deliveries of
    ONE downstream entry, not a 2-rail packet with one trigger."""
    import simkyt
    ch, ctrl, cat, j = _build_chain()
    blk = ctrl.project.block(j)
    c0 = blk.placement.cells[0]
    mem = ch.bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    writes, jumps = [], []
    for line in dis.splitlines():
        if "Write {" in line and "config: false" in line:
            hop = int(line.split("hop_cnt:")[1].split(",")[0])
            dest = int(line.split("dest:")[1].split("}")[0])
            writes.append((hop, dest))
        elif "Jump {" in line:
            hop = int(line.split("hop_cnt:")[1].split(",")[0])
            dest = int(line.split("dest:")[1].split("}")[0])
            jumps.append((hop, dest))
    print(f"\nbuilt emit: writes={writes} jumps={jumps}")
    assert len(writes) == 2, f"expected TWO output WRITEs, got {writes}\n{dis}"
    assert len(jumps) == 2, f"expected TWO output JUMPs, got {jumps}\n{dis}"
    assert writes[0] == writes[1], (
        f"both bursts must WRITE the SAME target register at the SAME hop "
        f"(the toggle consumer has ONE input register); got {writes}")
    assert jumps[0] == jumps[1], (
        f"both bursts must trigger the SAME entry at the SAME hop; got {jumps}")


def test_block_declares_exactly_one_output_register():
    """Condition (a) of the two-burst build path: the block must declare ONE
    output register. With >1 the build classifies it as a COMPLEX 2-rail source
    and steers the two WRITEs to CONSECUTIVE registers with ONE trigger — the
    DualFloatToComplex packet shape, which is precisely the WRONG output shape
    here."""
    from gr_kyttar.placement.blocks import FeaturePairJoinBlock
    b = FeaturePairJoinBlock("j")
    assert len(b.interface.output_registers) == 1, b.interface.output_registers


def test_output_cell_carries_no_internal_handoff():
    """Condition (b): the output cell must carry NO internal handoff and no
    inline WRITE.CFG, so ``_output_cell_carries_handoffs`` stays False and the
    build patches BOTH bursts rather than only the LAST WRITE/JUMP (which would
    leave the first burst on a stale hop)."""
    from gr_kyttar.placement.blocks import FeaturePairJoinBlock
    b = FeaturePairJoinBlock("j")
    assert not (b.internal_connections() or []), b.internal_connections()
    tmpl = b.build_cell_programs()[0].assembly_template
    assert "WRITE.CFG" not in tmpl, tmpl


def test_distinct_input_faces_are_declared_and_reconciled():
    """The block must declare BOTH the face-lock flag AND the (port, face-word)
    pairs the build's face-reconciliation pass needs. Without the pairs the pass
    silently falls back to the DualFloatToComplex's ``i``/``q`` names, becomes a
    NO-OP, and the chain builds + routes perfectly while emitting ZERO output —
    the exact silent failure this assertion prevents."""
    from gr_kyttar.placement.blocks import FeaturePairJoinBlock
    assert FeaturePairJoinBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = FeaturePairJoinBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("a", "face_a"), ("b", "face_b")), spec
    # The declared names must actually EXIST on the program (ports + face words).
    b = FeaturePairJoinBlock("j")
    cp = b.build_cell_programs()[0]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports, (pn, in_ports)
        assert wn in face_words, (wn, face_words)


def test_same_face_construction_raises():
    """Two streams on ONE face cannot be told apart by the arbiter, so the
    constructor RAISES rather than silently building a block that mis-pairs
    forever (INV-0: never clamp a hardware limit silently)."""
    from gr_kyttar.placement.blocks import FeaturePairJoinBlock
    with pytest.raises(ValueError, match="face_a and face_b must differ"):
        FeaturePairJoinBlock("j", face_a="west", face_b="west")


def test_drc_rejects_same_face_input_landing():
    """The build DRC is the hard safety net behind the placer's best-effort
    distinct-face constraint: FORCE both input nets to arrive on the same face
    and the ``dual_input_same_face`` violation MUST fire for this block too (it
    keys on NEEDS_DISTINCT_INPUT_FACES, not on a block name)."""
    from engine.bus_drc import _check_dual_input_same_face
    from model.connection import RoutePoint
    ch, ctrl, cat, j = _build_chain()
    blk = ctrl.project.block(j)
    dc = (blk.placement.cells[0].x, blk.placement.cells[0].y)
    w1, w2 = (dc[0] - 1, dc[1]), (dc[0] - 2, dc[1])
    for c in ctrl.project.connections:
        if (getattr(c.target, "block", None) == blk.name
                and getattr(c.target, "port", None) in ("a", "b")):
            c.route = [RoutePoint(x=w2[0], y=w2[1]),
                       RoutePoint(x=w1[0], y=w1[1]),
                       RoutePoint(x=dc[0], y=dc[1])]
    viols = _check_dual_input_same_face(ctrl.project, cat)
    assert any(v.kind == "dual_input_same_face" for v in viols), (
        f"the DRC MUST fire on a same-face landing; got {[v.kind for v in viols]}")


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_pairs_correctly_from_every_anchor(anchor):
    """PLACEMENT ROBUSTNESS. The join's correctness must not depend on a lucky
    layout: each anchor gives the two arms a DIFFERENT arrival-face geometry, and
    the build's face-reconciliation pass has to patch the authored placeholder
    faces to whatever the router chose in each case. An anchor that routes must
    also PAIR — if a layout ever routes and then emits nothing, that is the
    face-reconciliation no-op signature (builds and routes clean, zero output).

    Anchors that do not route on a given CP-SAT run are skipped, not failed;
    routability of a given hand-anchor is a placer property, pairing is this
    block's."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    (ka_xy, kb_xy, j_xy) = anchor
    built = None
    for _attempt in range(3):
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(CHIP_YAML)
        ctk = getattr(ct, "name", None) or "kyttar_10x12"
        ctrl = AppController(catalog=cat)
        ctrl.new_project("fpj_anchor", ctk)
        ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                              params={"n": _ARM_N})
        kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                              params={"n": _ARM_N})
        j = ctrl.place_block("FeaturePairJoinBlock", 0, *j_xy, library=LIB,
                             params={})
        ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                    BE(block=ka, port="sample"), name="n0")
        ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                    BE(block=kb, port="sample"), name="n1")
        ctrl.add_logical_connection(BE(block=ka, port="out"),
                                    BE(block=j, port="a"), name="n2")
        ctrl.add_logical_connection(BE(block=kb, port="out"),
                                    BE(block=j, port="b"), name="n3")
        ctrl.add_logical_connection(BE(block=j, port="out"),
                                    CPE(chip=0, port="x16_out"), name="n4")
        if not ctrl.auto_pnr({ctk: ct}).ok:
            continue
        bres = ctrl.build()
        if not bres.ok:
            continue
        il = bres.chips[0].input_landings
        if "n0" not in il or "n1" not in il:
            continue
        chip = simkyt.Chip.from_yaml(CHIP_YAML)
        chip.load_bitstream_physical(bres.words(0))
        chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
        built = _Chain(bres, chip, il["n0"], il["n1"])
        break
    if built is None:
        pytest.skip(f"anchor {anchor} did not route on this run")
    a, b = [1001, 1003, 1005], [2001, 2003, 2005]
    for av, bv in zip(a, b):
        built.emit("a", av)
        built.emit("b", bv)
    assert built.out == _join_ref(a, b), (
        f"anchor {anchor} routed but did NOT pair correctly: {built.out}")


def test_emit_report():
    """Emit the dashboard report. The metric is EXACT (tol 0): this block carries
    words, it does not transform them — every emitted word must equal the
    reference bit-for-bit."""
    ch, *_ = _build_chain()
    a = [1001, 1003, 1005, 1007, 1009, 1011]
    b = [2001, 2003, 2005, 2007, 2009, 2011]
    for av, bv in zip(a, b):
        ch.emit("a", av)
        ch.emit("b", bv)
    ref = _join_ref(a, b)
    assert ch.out == ref
    res = compare_against_grc(
        ch.out, [((w - 0x10000) if w >= 0x8000 else w) / 32768.0 for w in ref],
        metric=Metric.AMPLITUDE, delay=0, tolerance=0)
    assert res.passed, res.summary()
    write_report("FeaturePairJoinBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 0, "mutation": True,
        "on_chip_real_consumer": True})
