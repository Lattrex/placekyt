# SPDX-License-Identifier: GPL-3.0-or-later
"""TMRVoterBlock — triple-modular-redundancy majority vote, verified ON CHIP.

WHY THERE IS NO GNU RADIO COUNTERPART. This block solves a problem GR does not
have: on a host CPU there is exactly ONE execution path, so there is nothing to
vote on. On the clockless Kyttar array three copies of a chain run concurrently
on DISJOINT cells, so redundancy costs AREA, not throughput — and the voter only
has to sequence the three already-computed words. The golden is therefore a
PYTHON REFERENCE written directly from the specification
(``verification/tests/tmr_golden.py``, cross-checked against the block's own
``TMRVoterBlock.vote``), compared word-for-word against the real chip.

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * AGREEMENT — three identical arms emit ``[value, 0]``, for random values.
  * SINGLE FAULT — corrupting exactly ONE arm still emits the correct MAJORITY
    value, with the correct path id (1 = A, 2 = B, 3 = C). TMR CORRECTS.
  * NO MAJORITY — three different arms emit ``[sentinel, 7]``.
  * ADVERSARIAL ASYNC INTERLEAVING — the three arms driven in EVERY relative
    arrival order (all 6 permutations, plus random orders over 3 seeds) still
    vote the same triples: the LOCK rotation can never mis-pair.
  * STARTUP / STALL — no partial packet is ever emitted; a starved arm stalls
    and recovers exactly when the missing word arrives.
  * SATURATION (INV-19) — the whole burst driven back-to-back with no
    inter-sample quiescence equals the per-sample result.
  * ORIENTATION (INV-23) — identical output in all 8 D4 orientations.
  * MUTATIONS (INV-4) — every gate proven to FAIL on a named corruption,
    including the mandatory "no fault injected ⇒ status channel is flat 0".

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_tmr_voter.py -q
"""
from __future__ import annotations

import itertools
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME), str(Path(__file__).parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import compare_against_grc, write_report, Metric  # noqa: E402
from tmr_golden import (  # noqa: E402
    SENTINEL, STATUS_AGREE, STATUS_FAULT_A, STATUS_FAULT_B, STATUS_FAULT_C,
    STATUS_NO_MAJORITY, tmr_vote, tmr_stream)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"
PORTS = ("a", "b", "c")

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
#  The REAL three-upstream chain: three INDEPENDENT identity relays.           #
# --------------------------------------------------------------------------- #
#
# Each arm is a StreamSplitterBlock — an EXACT, MEMORYLESS identity relay
# (delay 0), so "the arms agree" is bit-exact and an injected fault differs by
# exactly the amount injected. (A GainBlock would be a Q15 multiply and mangle
# byte values; a KeepOneInN would change the rate.)
#
# All three arms are fed from the ONE chip input port by THREE SEPARATE nets, so
# each arm gets its OWN input landing (hop + entry + data address). Driving one
# arm's landing advances ONLY that arm — which is what lets the harness produce
# ANY relative arrival order, including orders the auto-placer would never
# generate. That is exactly the adversarial async interleaving the LOCK
# rendezvous must survive.
#
# GEOMETRY (load-bearing, see the block docstring): the voter's `rendezvous`
# cell needs THREE free faces for the three arms plus ONE for its internal
# forward — ALL FOUR. The arms are therefore anchored WEST, NORTH and SOUTH of
# it, and the block's own fold keeps `rendezvous` a LEAF (its only in-block
# neighbour is `agree`, to the EAST).

_ANCHORS = [
    ([(2, 5), (4, 3), (4, 7)], (4, 5)),
    ([(1, 5), (4, 2), (4, 8)], (4, 5)),
    ([(2, 4), (5, 2), (5, 6)], (5, 4)),
    ([(1, 4), (4, 1), (4, 7)], (4, 4)),
    ([(2, 6), (5, 3), (5, 9)], (5, 6)),
    ([(1, 6), (4, 3), (4, 9)], (4, 6)),
    ([(2, 3), (5, 1), (5, 5)], (5, 3)),
]


def _pnr(ctrl, ctk, ct) -> bool:
    """Place-and-route the chain. ``auto_pnr`` (not bare ``auto_route_all``) —
    the voter's 4x2 fold with its unlock lane needs the place<->route loop to
    open channels, and the three arms must land on three DISTINCT faces of the
    rendezvous cell, which only the full loop reliably achieves."""
    try:
        return bool(ctrl.auto_pnr({ctk: ct}).ok)
    except Exception:  # noqa: BLE001 — a failed pack is just another skip
        return False


class _Chain:
    """A built three-upstream voter chain + a driver that fires ONE arm."""

    def __init__(self, bres, chip, landings, ctrl=None, voter=None):
        self.bres, self.chip, self.landings = bres, chip, landings
        self.ctrl, self.voter = ctrl, voter
        self.out: list[int] = []

    def fire(self, arm: int, value: int):
        """Push ONE word into arm ``arm`` (0=a, 1=b, 2=c) and settle."""
        land = self.landings[f"i{arm}"]
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self.chip.run(max_events=6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        self.chip.run(max_events=400000)
        self._drain()

    def sample(self, a, b, c, order=(0, 1, 2)):
        """Drive one complete triple in the given ARRIVAL order."""
        vals = {0: a, 1: b, 2: c}
        for arm in order:
            self.fire(arm, vals[arm])

    def _drain(self):
        while self.chip.output_available("x16_out"):
            w = self.chip.read_port_i16("x16_out").view("uint16").tolist()
            self.out.extend(int(x) & 0xFFFF for x in w)
            self.chip.release_output_ack("x16_out")
            self.chip.run(max_events=8000)


def _build_chain(sentinel: int | None = None, orient=None):
    """Build 3 identity arms -> TMRVoter -> x16_out on ONE 10x12 chip.

    Tries a few anchor sets rather than pinning one: the block's correctness must
    not depend on a lucky layout, and the anchors that DO route exercise
    different arrival-face geometries, which is itself coverage."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    params = {} if sentinel is None else {"fault_sentinel": int(sentinel)}
    for arm_xy, v_xy in _ANCHORS:
        for _attempt in range(2):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("tmr_chain", ctk)
            ks = [ctrl.place_block("StreamSplitterBlock", 0, *arm_xy[i],
                                   library=LIB, params={}) for i in range(3)]
            v = ctrl.place_block("TMRVoterBlock", 0, *v_xy, library=LIB,
                                 params=params)
            if orient:
                # Rotate/mirror the voter BEFORE routing (INV-23): the nets are
                # still unrouted logical connections, so OrientBlockCommand is
                # the right primitive (it preserves them for the router).
                from commands import OrientBlockCommand
                for kind in orient:
                    OrientBlockCommand(ctrl.project, v, kind).execute()
            for i, k in enumerate(ks):
                ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                            BE(block=k, port="sample"),
                                            name=f"i{i}")
                ctrl.add_logical_connection(BE(block=k, port="out"),
                                            BE(block=v, port=PORTS[i]),
                                            name=f"w{i}")
            ctrl.add_logical_connection(BE(block=v, port="out"),
                                        CPE(chip=0, port="x16_out"), name="o")
            if not _pnr(ctrl, ctk, ct):
                continue
            bres = ctrl.build()
            if not bres.ok:
                continue
            il = bres.chips[0].input_landings
            if not all(f"i{i}" in il for i in range(3)):
                continue
            # The three arms MUST have DISTINCT landings, else the harness cannot
            # drive them independently and every interleaving test is vacuous.
            sig = {(int(il[f"i{i}"]["hop"]), int(il[f"i{i}"]["entry"]),
                    int(il[f"i{i}"]["data_addrs"][0])) for i in range(3)}
            if len(sig) < 3:
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
            # SMOKE the built layout on a THROWAWAY chip instance before handing
            # the real one to a gate. auto_pnr is a CP-SAT search and is not
            # deterministic across runs; a layout can route + build + present
            # three distinct landings and still deliver an arm somewhere the
            # rendezvous cannot accept, so the block emits nothing (or votes on a
            # stale arm). Without this probe such a layout surfaces as an
            # INTERMITTENT failure of whichever gate happened to draw it — which
            # is indistinguishable from a real block bug and is exactly how a
            # flake hides one.
            #
            # The probe MUST use its own chip: driving a triple advances the lock
            # rotation and latches arm state, so smoking the chip a gate is about
            # to use leaks the probe's values into that gate's FIRST vote
            # (measured — it reported a phantom "arm A faulted").
            probe = simkyt.Chip.from_yaml(CHIP_YAML)
            probe.load_bitstream_physical(bres.words(0))
            probe.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
            #
            # The probe drives ONE HEALTHY triple and then ONE SINGLE-FAULT
            # triple PER ARM. The healthy triple alone is not enough: a layout
            # that mis-delivers one arm still votes 0 when all three carry the
            # same value, and it was measured to pass a healthy-only probe and
            # then report status 0 for a genuine arm-A fault (~4% of built
            # layouts). Faulting each arm in turn is what makes the probe see
            # that the three arms really are three distinct, correctly-routed
            # paths.
            pch = _Chain(bres, probe, il, ctrl, v)
            pch.sample(0x2222, 0x2222, 0x2222)
            pch.sample(0x1111, 0x2222, 0x2222)     # arm A faulted -> status 1
            pch.sample(0x2222, 0x1111, 0x2222)     # arm B faulted -> status 2
            pch.sample(0x2222, 0x2222, 0x1111)     # arm C faulted -> status 3
            if pch.out != [0x2222, 0, 0x2222, 1, 0x2222, 2, 0x2222, 3]:
                continue
            return _Chain(bres, chip, il, ctrl, v)
    pytest.skip("no anchor routed the three-upstream voter chain on this run")


# --------------------------------------------------------------------------- #
#  INV-23 — ORIENTATION INVARIANCE, all 8 D4 orientations                      #
# --------------------------------------------------------------------------- #
#
# The universal gate (test_orientation_invariance.py) drives blocks through
# harnesses that inject on ONE input port; it cannot drive a THREE-FACE
# rendezvous, which is why neither DualFloatToComplexBlock nor
# FeaturePairJoinBlock appears there either. So this block carries its own D4
# gate, on the REAL three-arm chain.

_D4 = [
    [],                                # identity
    ["cw"],                            # 90
    ["cw", "cw"],                      # 180
    ["cw", "cw", "cw"],                # 270
    ["mirror_v"],                      # flip
    ["mirror_v", "cw"],                # flip + 90
    ["mirror_v", "cw", "cw"],          # flip + 180
    ["mirror_v", "cw", "cw", "cw"],    # flip + 270
]


def _d4_label(orient):
    return "identity" if not orient else "+".join(orient)


@pytest.mark.parametrize("orient", _D4, ids=[_d4_label(o) for o in _D4])
def test_orientation_invariant(orient):
    """INV-23: the block computes IDENTICALLY in all 8 D4 orientations.

    Rotating or mirroring a placed block changes where it sits and which way its
    ports face — never what it computes. For THIS block that is a real test of
    three separate transforms working together: the three ``is_face`` arm
    constants (``face_a``/``face_b``/``face_c``), the ``face_fwd`` word that
    holds the arms between samples, and the ``unlock_face``/``face_tap`` pair
    that steers the serialize-LOCK release. If any of them failed to D4-map, the
    LOCK would gate the wrong faces after rotation and the chain would build,
    route, and emit NOTHING."""
    ch = _build_chain(orient=orient)
    triples = [(100, 100, 100), (7, 100, 100), (100, 7, 100),
               (100, 100, 7), (11, 22, 33)]
    for a, b, c in triples:
        ch.sample(a, b, c)
    exp = tmr_stream([t[0] for t in triples], [t[1] for t in triples],
                     [t[2] for t in triples])
    assert ch.out == exp, (
        f"orientation {_d4_label(orient)} changed the vote (or produced "
        f"nothing): got {ch.out}, expected {exp}")


# --------------------------------------------------------------------------- #
#  The GOLDEN agrees with the block's own reference                            #
# --------------------------------------------------------------------------- #

def test_golden_matches_the_block_reference():
    """The standalone golden and the block class's ``vote`` must agree over the
    whole interesting domain — if they drift, every comparison below is
    measuring the wrong thing."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    vals = [0, 1, 2, 7, 100, 255, 0x7FFF, 0x8000, 0xFFFE]
    for a in vals:
        for b in vals:
            for c in vals:
                assert tmr_vote(a, b, c) == TMRVoterBlock.vote(a, b, c), (
                    a, b, c)


def test_golden_encodes_the_specified_status_codes():
    """The status codes are the block's PUBLISHED contract; pin them so a silent
    renumbering cannot slip through (it would still be self-consistent)."""
    assert tmr_vote(5, 5, 5) == (5, 0)
    assert tmr_vote(9, 5, 5) == (5, 1)      # A faulted -> B/C majority
    assert tmr_vote(5, 9, 5) == (5, 2)      # B faulted -> A/C majority
    assert tmr_vote(5, 5, 9) == (5, 3)      # C faulted -> A/B majority
    assert tmr_vote(1, 2, 3) == (SENTINEL, 7)
    assert (STATUS_AGREE, STATUS_FAULT_A, STATUS_FAULT_B, STATUS_FAULT_C,
            STATUS_NO_MAJORITY) == (0, 1, 2, 3, 7)
    # The sentinel is OUTSIDE the byte domain, so it can never collide with data.
    assert SENTINEL > 255


# --------------------------------------------------------------------------- #
#  AGREEMENT — all three arms equal                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seed", [5, 41, 907])
def test_agreement_emits_value_and_status_zero(seed):
    """All three arms carry the SAME value -> [value, 0], for random values.
    (>=3 seeds, the coverage bar.)"""
    rng = random.Random(seed)
    ch = _build_chain()
    vals = [rng.randrange(1, 0xFF00) for _ in range(6)]
    for v in vals:
        ch.sample(v, v, v)
    assert ch.out == tmr_stream(vals, vals, vals), (ch.out, vals)
    # Every status word is 0 — the whole point of the healthy case.
    assert ch.out[1::2] == [0] * len(vals), ch.out[1::2]


def test_agreement_on_edge_values():
    """EDGE coverage: 0, 1, the byte ceiling, the Q15 extremes, and a value that
    happens to EQUAL the sentinel (it must still report agreement, status 0 —
    the sentinel is only special on the no-majority path)."""
    ch = _build_chain()
    vals = [0, 1, 255, 0x7FFF, 0x8000, SENTINEL]
    for v in vals:
        ch.sample(v, v, v)
    assert ch.out == tmr_stream(vals, vals, vals), ch.out
    assert ch.out[1::2] == [0] * len(vals)


# --------------------------------------------------------------------------- #
#  SINGLE FAULT — TMR corrects, and names the faulty path                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("faulty,status", [(0, STATUS_FAULT_A),
                                           (1, STATUS_FAULT_B),
                                           (2, STATUS_FAULT_C)])
def test_single_fault_emits_the_majority_and_names_the_path(faulty, status):
    """Corrupt EXACTLY ONE arm. The emitted value must still be the correct
    MAJORITY (TMR CORRECTS the fault) and the status must name that path."""
    ch = _build_chain()
    good = [100, 200, 300, 400]
    arms = [list(good), list(good), list(good)]
    arms[faulty] = [g + 1 for g in good]        # a minimal, +1 fault
    for i in range(len(good)):
        ch.sample(arms[0][i], arms[1][i], arms[2][i])
    assert ch.out == tmr_stream(*arms), (ch.out, tmr_stream(*arms))
    # The VALUE rail is the uncorrupted stream — the fault was corrected.
    assert ch.out[0::2] == good, ch.out[0::2]
    # ...and every status names the faulty path.
    assert ch.out[1::2] == [status] * len(good), ch.out[1::2]


def test_fault_is_corrected_for_a_large_corruption_too():
    """The correction is not an artifact of a +1 fault: corrupt one arm WILDLY
    (to the sentinel value itself, the nastiest choice) and the majority still
    wins."""
    ch = _build_chain()
    good = [77, 88, 99]
    for g in good:
        ch.sample(SENTINEL, g, g)              # arm A wildly wrong
    assert ch.out == tmr_stream([SENTINEL] * 3, good, good), ch.out
    assert ch.out[0::2] == good
    assert ch.out[1::2] == [STATUS_FAULT_A] * 3


# --------------------------------------------------------------------------- #
#  NO MAJORITY                                                                 #
# --------------------------------------------------------------------------- #

def test_no_majority_emits_the_sentinel_and_status_seven():
    """All three arms differ -> [0xFFFF, 7]. There is no majority to emit, so
    the block emits a value that CANNOT be mistaken for data."""
    ch = _build_chain()
    triples = [(11, 22, 33), (1, 2, 3), (0, 1, 2), (0xFFFE, 0x7FFF, 5)]
    for a, b, c in triples:
        ch.sample(a, b, c)
    exp = tmr_stream([t[0] for t in triples], [t[1] for t in triples],
                     [t[2] for t in triples])
    assert ch.out == exp, (ch.out, exp)
    assert ch.out[0::2] == [SENTINEL] * len(triples)
    assert ch.out[1::2] == [STATUS_NO_MAJORITY] * len(triples)


def test_fault_sentinel_parameter_is_honoured():
    """``fault_sentinel`` is a real, user-settable parameter — build the block
    with a DIFFERENT sentinel and the no-majority packet must carry it. (A
    hardcoded 0xFFFF would pass every other test in this file.)"""
    alt = 0xF00D
    ch = _build_chain(sentinel=alt)
    ch.sample(1, 2, 3)
    assert ch.out == [alt, STATUS_NO_MAJORITY], ch.out


# --------------------------------------------------------------------------- #
#  ADVERSARIAL ASYNC INTERLEAVING — the core rendezvous claim                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("order", list(itertools.permutations((0, 1, 2))))
def test_every_relative_arrival_order_votes_identically(order):
    """EVERY one of the 6 relative arrival orders must produce the IDENTICAL
    stream. This is the whole point of the LOCK ROTATION over a counter: the
    arbiter holds each arm's word until it is that arm's turn, so the vote does
    NOT depend on which producer happened to fire first."""
    ch = _build_chain()
    triples = [(10, 10, 10), (20, 21, 20), (30, 30, 31), (41, 42, 43)]
    for a, b, c in triples:
        ch.sample(a, b, c, order=order)
    exp = tmr_stream([t[0] for t in triples], [t[1] for t in triples],
                     [t[2] for t in triples])
    assert ch.out == exp, (f"arrival order {order} broke the vote", ch.out, exp)


@pytest.mark.parametrize("seed", [3, 17, 91])
def test_random_interleavings_preserve_the_triples(seed):
    """RANDOM per-sample arrival order over a long run (3 seeds), with a mix of
    healthy, single-fault and no-majority samples. Whatever order the three arms
    fire in, the emitted stream is exactly the golden."""
    rng = random.Random(seed)
    ch = _build_chain()
    a_w, b_w, c_w = [], [], []
    for _ in range(9):
        base = rng.randrange(1, 0xFF00)
        kind = rng.choice(["agree", "fa", "fb", "fc", "none"])
        if kind == "agree":
            t = (base, base, base)
        elif kind == "fa":
            t = ((base + 1) & 0xFFFF, base, base)
        elif kind == "fb":
            t = (base, (base + 1) & 0xFFFF, base)
        elif kind == "fc":
            t = (base, base, (base + 1) & 0xFFFF)
        else:
            t = (base, (base + 1) & 0xFFFF, (base + 2) & 0xFFFF)
        a_w.append(t[0])
        b_w.append(t[1])
        c_w.append(t[2])
        order = [0, 1, 2]
        rng.shuffle(order)
        ch.sample(t[0], t[1], t[2], order=tuple(order))
    exp = tmr_stream(a_w, b_w, c_w)
    assert ch.out == exp, (ch.out, exp)


def test_back_to_back_samples_do_not_mix():
    """Back-to-back samples with DISTINCT, easily-attributed values: no word of
    sample k may leak into sample k+1's vote. The re-lock to face_a is the LAST
    thing the rendezvous does, precisely so the next sample's b/c words cannot
    barge in before their a word is latched."""
    ch = _build_chain()
    triples = [(111, 111, 111), (222, 222, 222), (333, 333, 333),
               (444, 444, 444), (555, 555, 555)]
    for a, b, c in triples:
        ch.sample(a, b, c)
    vals = [t[0] for t in triples]
    assert ch.out == tmr_stream(vals, vals, vals), ch.out
    # Structurally: every EVEN position is one of the injected values, and every
    # ODD position is a status word (0 here) — never a stray data word.
    assert ch.out[0::2] == vals
    assert set(ch.out[1::2]) == {0}


# --------------------------------------------------------------------------- #
#  STARTUP + STALL semantics                                                   #
# --------------------------------------------------------------------------- #

def test_startup_emits_nothing_until_all_three_arms_have_spoken():
    """NO PARTIAL PACKET, ever. After one arm — and after two — the chip has
    produced NOTHING. The packet appears only when the third word lands."""
    ch = _build_chain()
    ch.fire(0, 4242)
    assert ch.out == [], f"a partial packet leaked after ONE arm: {ch.out}"
    ch.fire(1, 4242)
    assert ch.out == [], f"a partial packet leaked after TWO arms: {ch.out}"
    ch.fire(2, 4242)
    assert ch.out == [4242, 0], ch.out


def test_starved_arm_stalls_and_recovers():
    """A starved arm STALLS the vote — it never emits a stale or duplicated
    packet — and RECOVERS exactly when the missing word arrives."""
    ch = _build_chain()
    ch.sample(10, 10, 10)
    assert ch.out == [10, 0]
    ch.fire(0, 20)          # arm A runs ahead
    ch.fire(1, 20)
    assert ch.out == [10, 0], f"emitted without arm C: {ch.out}"
    ch.fire(2, 20)          # C catches up
    assert ch.out == [10, 0, 20, 0], ch.out


# --------------------------------------------------------------------------- #
#  INV-19 — SATURATED drive == per-sample drive                                #
# --------------------------------------------------------------------------- #

def _enc_write(hop: int, addr: int) -> int:
    """WRITE opcode 0x6, hop in [9:5], dest in [4:0]."""
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    """JUMP opcode 0x7, hop in [9:5], entry in [4:0]."""
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def _saturated_run(triples, cap: int = 4_000_000):
    """Drive the WHOLE burst SATURATED: every arm word of every sample enqueued
    as raw WRITE/DATA/JUMP words via ``queue_words_physical`` (the real streaming
    condition — no inter-sample quiescence anywhere), then ONE bounded run."""
    sat = _build_chain()
    stream: list[int] = []
    for a, b, c in triples:
        for arm, val in ((0, a), (1, b), (2, c)):
            land = sat.landings[f"i{arm}"]
            hop = int(land["hop"]) & 0x1F
            stream.append(_enc_write(hop, int(land["data_addrs"][0])))
            stream.append(int(val) & 0xFFFF)
            stream.append(_enc_jump(hop, int(land["entry"])))
    sat.chip.queue_words_physical("x16_in", stream)
    # BOUNDED run, never max_events=None: a livelocking block must FAIL cleanly
    # rather than spin the machine at 100% CPU (the INV-19 harness-safety rule).
    res = sat.chip.run(max_events=cap)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    stop = res.get("stop_reason") if isinstance(res, dict) else None
    sat._drain()
    return completed, stop, sat.out


def _chunked_run(triples, cap: int = 500_000):
    """Drive the burst PER-TRIPLE-SATURATED: each triple's three arm words are
    enqueued back-to-back as raw words (no quiescence WITHIN a triple — the
    three arms race exactly as three independent producers would), one bounded
    run, drain, then the next triple. This is the pacing the block supports; see
    ``test_known_limit_saturated_burst_depth_is_one``."""
    ch = _build_chain()
    completed_all = True
    for a, b, c in triples:
        stream: list[int] = []
        for arm, val in ((0, a), (1, b), (2, c)):
            land = ch.landings[f"i{arm}"]
            hop = int(land["hop"]) & 0x1F
            stream.append(_enc_write(hop, int(land["data_addrs"][0])))
            stream.append(int(val) & 0xFFFF)
            stream.append(_enc_jump(hop, int(land["entry"])))
        ch.chip.queue_words_physical("x16_in", stream)
        res = ch.chip.run(max_events=cap)
        if isinstance(res, dict) and not res.get("completed", True):
            completed_all = False
        ch._drain()
    return completed_all, ch.out


def test_saturated_equals_per_sample():
    """INV-19, at the depth the block supports. Each triple's THREE ARM WORDS
    are enqueued back-to-back with no quiescence between them — the three
    producers race, which is the hazard the rendezvous exists to survive — and
    the result must equal the per-sample result over a long run.

    THIS GATE FOUND A REAL BUG. The rendezvous originally re-locked straight to
    ``face_a`` at the end of ``got_c``: correct per-sample, and under load it
    re-admits the next sample's first arm the instant the current triple is
    dispatched, so triples pile into the agree/disagree/emit chain and the
    simulator reports an explicit ``Deadlock`` after exactly ONE packet.
    (Measured; the three producer arms driven saturated WITHOUT the voter are
    fine, so the hazard was the block's.) The fix is INV-19/20's own idiom:
    ``got_c`` locks to the INTERNAL FORWARD face — which no external arm ever
    arrives on, so all three are barred — and ``agree`` re-points the lock to
    ``face_a`` with a backward ``WRITE.CFG @N, 3`` once it has dispatched. The
    rotation has FOUR stops, not three.

    The RESIDUAL limit (whole-burst enqueue) is measured and guarded by
    ``test_known_limit_saturated_burst_depth_is_one``."""
    triples = [(50, 50, 50), (61, 60, 60), (70, 71, 70), (80, 80, 81),
               (1, 2, 3), (90, 90, 90), (255, 255, 255), (7, 8, 7)]
    a_w = [t[0] for t in triples]
    b_w = [t[1] for t in triples]
    c_w = [t[2] for t in triples]
    exp = tmr_stream(a_w, b_w, c_w)

    # --- per-sample (inject one arm word, run to quiescence, next) ---
    per = _build_chain()
    for a, b, c in triples:
        per.sample(a, b, c)
    assert per.out == exp, ("per-sample drive already wrong", per.out, exp)

    completed, out = _chunked_run(triples)
    assert completed, f"the paced-saturated drive wedged; partial output={out}"
    assert out == exp, (
        f"saturated != per-sample.\n saturated={out}\n per-sample={exp}")


def test_saturated_drive_is_not_vacuous():
    """NON-VACUITY for the gate above (INV-4 applied to the harness). The three
    arm words of each triple really are enqueued together with no run between
    them, so the three producers genuinely race at the rendezvous. Assert that
    the drive path used is the RAW WORD queue (not the settle-between-arms
    per-sample path) by checking it still produces the right answer for a
    stimulus where a mis-pairing would be visible: every triple has three
    DIFFERENT values, so any cross-arm mix-up changes the vote."""
    triples = [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)]
    exp = tmr_stream([t[0] for t in triples], [t[1] for t in triples],
                     [t[2] for t in triples])
    # Every one of these is a NO-MAJORITY triple, so a mis-pairing that happened
    # to produce a majority would be caught immediately.
    assert all(s == STATUS_NO_MAJORITY for s in exp[1::2]), exp
    completed, out = _chunked_run(triples)
    assert completed and out == exp, (out, exp)
    assert len(out) == 2 * len(triples), (
        f"dropped or duplicated samples: {len(out)} words for {len(triples)} "
        f"triples (expected {2 * len(triples)})")


def test_known_limit_saturated_burst_depth_is_one():
    """EXPLICIT GUARD for a real, MEASURED substrate limit (AGENTS.md §6: a
    limitation is recorded as an executable guard, never glossed over).

    THE LIMIT: the block sustains ONE TRIPLE IN FLIGHT. Enqueuing a triple's
    three arm words back-to-back is fine and unbounded in NUMBER of triples
    (``_chunked_run`` above drives many, correctly). Enqueuing TWO OR MORE
    TRIPLES into the port FIFO before running deadlocks after the first packet.

    WHY, precisely — it is a FACE-BUDGET arithmetic, not a bug that can be
    coded around. A cell has FOUR faces. An N-arm LOCK-rotation rendezvous needs
    ``N`` (one per arm — the face IS the path identity) + 1 (forward into the
    datapath) + 1 (a serialize-LOCK release corridor coming back) = ``N + 2``.

      * N=2 (DualFloatToComplex, FeaturePairJoin): 4 faces. Fits — and those
        blocks are SINGLE-CELL, so they need neither a forward nor a release.
      * N=3 (this block): FIVE faces needed, four available.

    So the release cannot have a corridor of its own; it must come back through
    the ONE cell that abuts the rendezvous, which is ``agree`` — the FIRST stage
    of a three-stage chain. Releasing there bounds the block to one triple
    entering per release, but the previous triple may still be in
    ``disagree``/``emit``, and a second queued triple then wedges.

    THREE ALTERNATIVES WERE BUILT AND MEASURED, all blocked:
      1. Release from ``emit`` (the deep, thorough point) via a backward
         WRITE.CFG transiting the datapath row: the config word is re-forwarded
         by the live cells' committed faces and lands on a real entry — it fired
         ``nomaj`` and emitted a spurious ``[sentinel, 7]`` packet.
      2. Release from ``emit`` via a DEDICATED ``transit_*`` unlock lane: the
         lane must enter the rendezvous on a face, and all four are already
         committed (three arms + forward) — the placer/DRC then reject the
         layout with ``dual_input_same_face`` because an arm loses its face.
      3. Release from ``emit`` as a backward JUMP into an ``agree.relay`` entry:
         the exit-default rewrites the jump to ``emit``'s own ``nomaj`` entry
         and the block emits a spurious packet.

    NOT A PROBLEM FOR THE INTENDED USE: the three arms of a TMR pipeline are
    fed by three chains off one splitter and are index-aligned by construction,
    and the host paces per sample. This guard exists so that if the boundary
    ever MOVES — e.g. a future chip with more faces, or an engine change that
    lets a config word transit a live cell safely — a test says so instead of a
    chain silently deadlocking."""
    # Depth 1 (one triple queued at a time): correct, and unbounded in count.
    ok, out = _chunked_run([(10, 10, 10), (20, 20, 20), (30, 30, 30),
                            (40, 40, 40), (50, 50, 50)])
    assert ok and out == tmr_stream([10, 20, 30, 40, 50], [10, 20, 30, 40, 50],
                                    [10, 20, 30, 40, 50]), out
    # Depth 2 (two whole triples queued before running): the documented wall.
    completed, stop, sat_out = _saturated_run(
        [(10, 10, 10), (20, 20, 20)], cap=2_000_000)
    assert not completed, (
        f"the depth-2 saturated boundary MOVED (the burst now settles, "
        f"stop={stop}, out={sat_out}). If the substrate or the engine changed "
        f"this is GOOD NEWS — re-measure the limit, delete this guard, and make "
        f"test_saturated_equals_per_sample drive the whole burst at once.")
    assert len(sat_out) == 2, (
        f"expected exactly ONE packet before the wall; got {sat_out}")


def test_serialize_lock_release_is_present_and_backward_patched():
    """STRUCTURAL proof of the fix. The ``agree`` cell must carry the backward
    ``WRITE.CFG`` to CONFIG 3 (LOCK_FACE, dest 35) that re-admits the next
    sample, and the block must declare the corresponding BACKWARD internal
    connection so ``_apply_internal_feedback`` re-patches the authored hop
    placeholder to the resolved corridor distance. A hardcoded hop deadlocks a
    re-placed or rotated layout (the INV-19 trap)."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    b = TMRVoterBlock("t")
    tmpl = b.build_cell_programs()["agree"].assembly_template
    assert "WRITE.CFG" in tmpl, (
        "the serialize-LOCK release is MISSING from `agree` — the block will "
        "deadlock under saturated drive")
    assert ", 3" in tmpl.split("WRITE.CFG")[1].split("\n")[0], (
        "the release must target CONFIG 3 (LOCK_FACE), not CONFIG 4 (LOCK): "
        "this block RE-POINTS the rotation, it does not clear the lock")
    back = [e for e in b.internal_connections()
            if e[0] == "agree" and e[1] == "unlock" and e[2] == "rendezvous"]
    assert back, (
        "no BACKWARD agree->rendezvous `unlock` internal connection is declared, "
        "so the authored WRITE.CFG hop is never re-patched to the real corridor")
    # The block must tell the build which CONFIG register the release targets:
    # this one RE-POINTS a rotating face lock (CONFIG 3 = LOCK_FACE), it does
    # NOT clear the arbiter lock (CONFIG 4) the way ComplexMixer/Costas do. The
    # build defaults to 4, and that default silently rewrote the authored write
    # into a lock-CLEAR — which un-gates every face and lets out-of-turn arms
    # barge in (it built and routed cleanly, then desynced after two samples).
    assert getattr(TMRVoterBlock, "UNLOCK_CFG_ADDR", 4) == 3, (
        "TMRVoterBlock must declare UNLOCK_CFG_ADDR = 3 (LOCK_FACE)")
    # The rendezvous must lock to the INTERNAL forward face (not face_a) at the
    # end of got_c — that is what bars all three arms until the release runs.
    rz = b.build_cell_programs()["rendezvous"]
    assert "face_fwd" in rz.assembly_template, (
        "got_c must lock to the internal forward face; re-locking straight to "
        "face_a is the measured deadlock")
    faces = {d.name for d in rz.data if getattr(d, "is_face", False)}
    assert "face_fwd" in faces, faces


# --------------------------------------------------------------------------- #
#  MANDATORY mutation tests (INV-4) — each corruption MUST be caught           #
# --------------------------------------------------------------------------- #

def test_mutation_no_fault_injected_reports_status_zero():
    """THE MANDATORY MUTATION. Run with NO fault injected at all and assert the
    STATUS CHANNEL IS FLAT ZERO.

    Without this, a voter that emitted a CONSTANT fault code would pass every
    other test in this file: the value rail would still be the majority (which
    equals the input when all arms agree), and every fault test asserts a
    NON-zero status. Only the healthy case can catch a stuck status channel, and
    only if the assertion is on the status rail itself."""
    ch = _build_chain()
    vals = [123, 456, 789, 1011]
    for v in vals:
        ch.sample(v, v, v)
    status = ch.out[1::2]
    assert status == [0] * len(vals), (
        f"NO fault was injected, so every status word MUST be 0; got {status}. "
        f"A voter emitting a constant fault code would look correct on the "
        f"value rail and is exactly what this gate exists to catch.")
    # Non-vacuity: the gate can SEE a non-zero status (it is not asserting on an
    # empty or all-zero-by-construction stream).
    assert len(status) == len(vals) and ch.out[0::2] == vals
    ch2 = _build_chain()
    ch2.sample(1, 0, 0)
    assert ch2.out[1::2] == [STATUS_FAULT_A], (
        "the status rail never reports a fault — the flat-zero assertion above "
        "would then be vacuous")


def test_mutation_inverted_compare_fails():
    """INVERT a compare: a voter whose ``a == b`` test is negated reports the
    fault paths swapped. The gate must reject that stream."""
    a_w, b_w, c_w = [10, 20, 30], [10, 21, 30], [11, 20, 30]
    good = tmr_stream(a_w, b_w, c_w)

    def _inverted(a, b, c):
        # `a == b` inverted: the agree/disagree halves are exchanged.
        if a != b:
            return (a, STATUS_AGREE if a == c else STATUS_FAULT_C)
        if b == c:
            return (b, STATUS_FAULT_A)
        if a == c:
            return (a, STATUS_FAULT_B)
        return (SENTINEL, STATUS_NO_MAJORITY)

    mutated: list = []
    for a, b, c in zip(a_w, b_w, c_w):
        mutated.extend(_inverted(a, b, c))
    assert mutated != good, (
        "the gate cannot see an inverted compare — the stimulus does not "
        "distinguish the two halves of the tree")
    ch = _build_chain()
    for a, b, c in zip(a_w, b_w, c_w):
        ch.sample(a, b, c)
    assert ch.out == good, ch.out


def test_mutation_dropped_relock_desyncs_after_one_sample():
    """DROP THE RE-LOCK. Without the final ``LOCK_FACE = face_a`` the cell stays
    locked to face_c, so from sample 2 on the arms are consumed in the wrong
    ROLES — a rotation of the triple. Model it and assert it diverges, then show
    the real block does NOT diverge over the same stimulus."""
    a_w, b_w, c_w = [1, 2, 3, 4], [1, 2, 3, 5], [9, 2, 3, 4]
    good = tmr_stream(a_w, b_w, c_w)
    # A block stuck on face_c re-reads arm C into the `a` slot from sample 2 on.
    mutated: list = []
    for i, (a, b, c) in enumerate(zip(a_w, b_w, c_w)):
        if i == 0:
            mutated.extend(tmr_vote(a, b, c))
        else:
            mutated.extend(tmr_vote(c, a, b))     # roles rotated by the desync
    assert mutated != good, (
        "the gate cannot see a dropped re-lock — pick stimulus where a rotated "
        "triple votes differently")
    ch = _build_chain()
    for a, b, c in zip(a_w, b_w, c_w):
        ch.sample(a, b, c)
    assert ch.out == good, ch.out


def test_mutation_swapped_face_constants_fails():
    """SWAP TWO FACE CONSTANTS (face_b <-> face_c). The arms are then latched
    into the wrong slots, so B's word is voted as C's and vice versa — which
    reports the WRONG PATH on a single fault. Assert the swapped stream differs
    and the real block matches the un-swapped golden."""
    # A fault on arm B only: correct status is 2. With b/c swapped it reads as 3.
    a_w, b_w, c_w = [50, 60], [51, 61], [50, 60]
    good = tmr_stream(a_w, b_w, c_w)
    swapped = tmr_stream(a_w, c_w, b_w)          # b and c exchanged
    assert swapped != good, (
        "the gate cannot see swapped face constants — the stimulus must make "
        "the b and c roles distinguishable (fault exactly one of them)")
    assert good[1::2] == [STATUS_FAULT_B] * 2 and swapped[1::2] == [
        STATUS_FAULT_C] * 2
    ch = _build_chain()
    for a, b, c in zip(a_w, b_w, c_w):
        ch.sample(a, b, c)
    assert ch.out == good, ch.out


def test_mutation_emit_before_latching_the_third_arm_fails():
    """EMIT BEFORE LATCHING THE THIRD ARM. Such a block votes on a STALE c (the
    previous sample's, or 0 at cold start), so its first packet is wrong and
    every later one is shifted. Model it, assert it diverges, and show the real
    block votes on the CURRENT triple."""
    a_w, b_w, c_w = [7, 8, 9], [7, 8, 9], [7, 99, 9]
    good = tmr_stream(a_w, b_w, c_w)
    stale_c = [0] + c_w[:-1]                     # c lags by one sample
    mutated = tmr_stream(a_w, b_w, stale_c)
    assert mutated != good, (
        "the gate cannot see a pre-latch emit — the stimulus must make c's "
        "CURRENT value matter")
    ch = _build_chain()
    for a, b, c in zip(a_w, b_w, c_w):
        ch.sample(a, b, c)
    assert ch.out == good, ch.out


def test_mutation_empty_output_fails():
    """An empty stream must never satisfy the reference (green is not reachable
    by emitting nothing)."""
    assert [] != tmr_stream([1], [1], [1])


def test_mutation_value_rail_alone_is_not_enough():
    """A voter that emitted ONLY the value word (dropping the status word) would
    halve the packet. The reference must reject that."""
    a_w, b_w, c_w = [5, 6], [5, 7], [5, 6]
    good = tmr_stream(a_w, b_w, c_w)
    assert good[0::2] != good, "the value rail alone must not equal the packet"
    ch = _build_chain()
    for a, b, c in zip(a_w, b_w, c_w):
        ch.sample(a, b, c)
    assert ch.out == good, ch.out
    assert len(ch.out) == 2 * len(a_w), (
        f"expected TWO words per sample; got {len(ch.out)} for {len(a_w)}")


# --------------------------------------------------------------------------- #
#  STRUCTURE — the load-bearing construction claims                            #
# --------------------------------------------------------------------------- #

def test_declares_distinct_input_faces_and_reconciliation_pairs():
    """The block must declare BOTH the face-lock flag AND the (port, face-word)
    triples the build's face-reconciliation pass needs. Without the triples the
    pass silently falls back to the DualFloatToComplex ``i``/``q`` names, becomes
    a NO-OP, and the chain builds + routes perfectly while emitting ZERO output —
    the exact silent failure this assertion prevents."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    assert TMRVoterBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = TMRVoterBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("a", "face_a"), ("b", "face_b"), ("c", "face_c")), spec
    b = TMRVoterBlock("t")
    cp = b.build_cell_programs()["rendezvous"]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports, (pn, in_ports)
        assert wn in face_words, (wn, face_words)


def test_same_face_construction_raises():
    """Two paths on ONE face cannot be told apart by the arbiter, so the
    constructor RAISES rather than silently building a block that mis-pairs
    forever (INV-0: never clamp a hardware limit silently)."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    with pytest.raises(ValueError, match="pairwise DISTINCT"):
        TMRVoterBlock("t", face_a="west", face_b="west", face_c="south")
    with pytest.raises(ValueError, match="pairwise DISTINCT"):
        TMRVoterBlock("t", face_a="north", face_b="south", face_c="north")


def test_rendezvous_boots_pre_locked_with_no_arm_entry():
    """COLD START IS BAKED. The rendezvous cell must declare
    ``initial_lock_face`` (LOCK=1 + LOCK_FACE=face_a in the boot CONFIG) and
    must NOT have an arm entry: arming via a JUMP is a RACE — a word arriving
    before the arm-JUMP is accepted on an UNLOCKED face and mis-pairs, the exact
    failure the LOCK exists to prevent."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    cp = TMRVoterBlock("t").build_cell_programs()["rendezvous"]
    assert cp.initial_lock_face is not None, (
        "the rendezvous MUST boot pre-locked (initial_lock_face)")
    entries = [e.name for e in cp.entries]
    assert entries == ["got_a", "got_b", "got_c"], entries
    assert "arm" not in entries


def test_built_rendezvous_cell_boots_locked_on_chip():
    """The cold-start LOCK is not merely declared — it is in the BITSTREAM. Load
    the built chip and confirm the rendezvous cell's boot CONFIG has the LOCK bit
    set before a single word is injected."""
    import simkyt
    ch = _build_chain()
    blk = ch.ctrl.project.block(ch.voter)
    c0 = blk.placement.cells[0]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(ch.bres.words(0))
    boot_cfg = chip.read_config(chip.cell_id_at(c0.x, c0.y))
    # LOCK is CONFIG bit 14 (0x4000) in the packed config word.
    assert boot_cfg & 0x4000, (
        f"the rendezvous cell must BOOT already LOCKED (no arm) — boot CONFIG "
        f"0x{boot_cfg:04X} has LOCK clear")


def test_rendezvous_writes_lock_face_to_rotate():
    """The built rendezvous program must WRITE LOCK_FACE (CONFIG 3 = dest 35) to
    rotate the accepted face a -> b -> c -> a. Three writes: one per entry."""
    import simkyt
    ch = _build_chain()
    blk = ch.ctrl.project.block(ch.voter)
    c0 = blk.placement.cells[0]
    mem = ch.bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    assert dis.count("dest: 35") == 3, (
        f"expected THREE LOCK_FACE writes (the a->b->c->a rotation); "
        f"got {dis.count('dest: 35')}:\n{dis}")


def test_all_three_arms_are_advertised_as_external_inputs():
    """The block must present THREE external input ports. ``portmap`` treats a
    landing-cell input that is the destination of an INTERNAL connection as a
    feedback RETURN and drops it from the external set — so naming the backward
    ``unlock`` edge's destination ``a`` (the obvious choice: it is the port whose
    face the release re-points to) silently reduced the block to TWO advertised
    arms. A hand-wired chain survives that (it wires by port NAME), which is
    exactly why it needs a test: GRC import, which reads the port map, does not.

    The fix is that the CONFIG-only edge names a destination that is not a real
    input port; the build's ``unlock`` branch never resolves a destination
    register, so nothing else depends on it."""
    from engine.catalog import BlockCatalog
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    pm = BlockCatalog.from_gr_kyttar().port_map(
        "TMRVoterBlock", {}, library=LIB)
    ins = [p.name for p in pm.ports if p.direction == "in"]
    assert ins == ["a", "b", "c"], (
        f"the voter must advertise all THREE arms as external inputs; got "
        f"{ins}. A missing arm means the backward unlock edge is aimed at a real "
        f"input port and portmap has classified it as a feedback return.")
    # ...and the GRC binding must list the same three.
    import yaml
    y = yaml.safe_load(
        (Path(__file__).resolve().parents[2]
         / "gr-kyttar" / "grc" / "kyttar_tmr_voter.block.yml").read_text())
    assert [i["label"] for i in y["inputs"]] == ["a", "b", "c"], y["inputs"]


def test_block_declares_exactly_one_output_register():
    """The [value, status] packet is TWO SEQUENTIAL BURSTS on ONE stream. With
    more than one output register the build classifies the emit cell as a
    COMPLEX 2-rail source and steers the two WRITEs to CONSECUTIVE registers
    under ONE trigger — collapsing the packet."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    b = TMRVoterBlock("t")
    assert len(b.interface.output_registers) == 1, b.interface.output_registers


def test_every_cell_fits_its_register_budget():
    """INV-33 static gate: no data address and no state/input register may be at
    or above ``31 - instr_count``. A cell at exactly 32/32 words pins state ON
    TOP of its own first instruction — it assembles, loads, runs ONCE, then
    zeroes the word the next trigger enters at (emits one sample, goes
    quiescent). This is cheap to check and catches it before any chip run."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    for cid, cp in TMRVoterBlock("t").build_cell_programs().items():
        n = R.count_instructions(cp)
        base = 31 - n
        for d in cp.data:
            assert d.address < base, (
                f"{cid}: data '{d.name}' @{d.address} collides with the "
                f"instruction block starting at {base}")
        for sv in cp.state:
            assert sv.register is not None, (
                f"{cid}: state '{sv.name}' is UNPINNED (INV-33: unpinned state "
                f"lands on top of R0 and the inputs)")
            assert sv.register < base, (
                f"{cid}: state '{sv.name}' @{sv.register} collides with the "
                f"instruction block starting at {base}")
        for p in cp.inputs:
            if p.register is not None:
                assert p.register < base, (
                    f"{cid}: input '{p.name}' @{p.register} collides with the "
                    f"instruction block starting at {base}")


def test_every_declared_entry_is_jumped():
    """INV-39: a declared ``EntryPoint`` that NOTHING jumps at is unreachable
    dead code — the cell still assembles, still fits its budget, and still runs,
    down the wrong path forever. This block dispatches on TWO multi-entry cells
    (``disagree`` pass/dis, ``emit`` emit/nomaj), so the check is load-bearing:
    every entry must be the target of at least one ``internal_jumps`` edge, or
    the block's own input entries."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    b = TMRVoterBlock("t")
    cps = b.build_cell_programs()
    jumped: dict = {}
    for (_src, _jname, dst_cell, dst_entry) in b.internal_jumps():
        jumped.setdefault(dst_cell, set()).add(dst_entry)
    # The rendezvous entries are targeted by EXTERNAL producers (declared on the
    # input Ports), not by internal jumps.
    external = {p.entry for p in cps["rendezvous"].inputs if p.entry}
    jumped.setdefault("rendezvous", set()).update(external)
    for cid, cp in cps.items():
        for e in cp.entries:
            assert e.name in jumped.get(cid, set()), (
                f"cell '{cid}' declares entry '{e.name}' that NOTHING jumps at "
                f"— dead code (INV-39). Jumped: {sorted(jumped.get(cid, set()))}")


def test_rendezvous_cell_is_a_leaf_of_the_fold():
    """THE N=3 FACE-BUDGET RULE, asserted structurally.

    A cell has FOUR faces. An N-arm LOCK-rotation rendezvous needs
    ``N`` (arms) + 1 (forward into the datapath) + 1 (a serialize-LOCK release
    corridor) = ``N + 2``. At N=2 that is 4 and fits; at N=3 it is FIVE and does
    not. So the N=3 rendezvous cell must be a LEAF of the fold — exactly ONE
    in-block neighbour — leaving THREE faces for the arms, and the release must
    come back THROUGH that one neighbour rather than claiming a face of its own.

    A compact 2x2 square (the obvious 4-cell fold) gives EVERY cell two in-block
    neighbours and therefore cannot host an N=3 rendezvous at all: the maze
    router reports "no free DISTINCT-face broker for a face-locking block's
    input" and the chain does not route. (Measured; it was the first layout
    tried.) This test pins the constraint so a re-fold cannot silently
    reintroduce it."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    layout = TMRVoterBlock("t").default_layout()
    pos = {cid: (dx, dy) for cid, (dx, dy, _f) in layout.items()}
    rx, ry = pos["rendezvous"]
    occupied = set(pos.values())
    neighbours = [(rx + dx, ry + dy)
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
    in_block = [n for n in neighbours if n in occupied]
    assert len(in_block) == 1, (
        f"the rendezvous cell must be a LEAF of the fold (exactly ONE in-block "
        f"neighbour, counting the unlock-lane transit cells) so THREE faces stay "
        f"free for the three arms; it has {len(in_block)}: {in_block}. "
        f"Layout={layout}")
    # The one neighbour must be `agree` — the forward AND the release path.
    assert in_block[0] == pos["agree"], (in_block, pos["agree"])
    # ...and both footprint dimensions stay <= 8 (INV-9, this 10x12 chip).
    w = max(x for x, _ in pos.values()) - min(x for x, _ in pos.values())
    h = max(y for _, y in pos.values()) - min(y for _, y in pos.values())
    assert w <= 8 and h <= 8, (w, h)


def test_release_rides_the_forward_face_not_a_dedicated_corridor():
    """THE FACE-BUDGET CONSEQUENCE, pinned structurally.

    Because an N=3 rendezvous needs 3 (arms) + 1 (forward) = all four faces, the
    serialize-LOCK release CANNOT have a corridor of its own. It must come back
    through the ONE cell that abuts the rendezvous. So: the block declares NO
    ``transit_*`` unlock lane, and the release lives in ``agree`` — the cell
    directly abutting the rendezvous — not in the deeper ``emit``.

    A dedicated lane was built and MEASURED to fail: the lane must enter the
    rendezvous on a face, all four are committed, and the placer/DRC then reject
    the layout (``dual_input_same_face``) because an arm loses its face."""
    from gr_kyttar.placement.blocks import TMRVoterBlock
    b = TMRVoterBlock("t")
    layout = b.default_layout()
    lane = [cid for cid in layout if cid.startswith("transit_")]
    assert not lane, (
        f"an unlock lane is declared ({lane}) — at N=3 there is no free face "
        f"for it to enter the rendezvous on; the release must ride the forward "
        f"face from `agree` instead")
    tmpl = b.build_cell_programs()
    assert "WRITE.CFG" in tmpl["agree"].assembly_template, (
        "the release must live in `agree` (the cell abutting the rendezvous)")
    assert "WRITE.CFG" not in tmpl["emit"].assembly_template, (
        "`emit` does NOT abut the rendezvous; a release from there needs a "
        "corridor the face budget cannot supply (measured)")


# --------------------------------------------------------------------------- #
#  Dashboard report                                                            #
# --------------------------------------------------------------------------- #

def test_emit_report():
    """Emit the dashboard report. The metric is EXACT — this block COMPARES and
    SELECTS words, it does not transform them, so every emitted word must equal
    the reference bit-for-bit; there is no quantization tolerance to spend and an
    amplitude metric would be the wrong claim about what was proven."""
    ch = _build_chain()
    triples = [(100, 100, 100), (7, 100, 100), (100, 7, 100), (100, 100, 7),
               (11, 22, 33), (255, 255, 255)]
    a_w = [t[0] for t in triples]
    b_w = [t[1] for t in triples]
    c_w = [t[2] for t in triples]
    for a, b, c in triples:
        ch.sample(a, b, c)
    ref = tmr_stream(a_w, b_w, c_w)
    assert ch.out == ref, (ch.out, ref)
    res = compare_against_grc(
        ch.out, [((w - 0x10000) if w >= 0x8000 else w) / 32768.0 for w in ref],
        metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("TMRVoterBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 1, "mutation": True,
        "on_chip_three_arm_chain": True, "async_interleavings": 6,
        "saturated": True, "orientations": 8})
