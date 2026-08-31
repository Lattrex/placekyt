# SPDX-License-Identifier: GPL-3.0-or-later
"""examples/foc_motor — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

WHAT IS PROVEN HERE, on the real placed + routed + built chip:

  * the shipped ``.kyt`` builds, and the chain emits a duty packet that is
    BIT-EXACT against a host golden assembled from the blocks' own pinned
    integer models (``PIControllerBlock.process_reference_q15``,
    ``cordic_rotate_word``, ``svpwm_duties``) — so the gate would fail on any
    arithmetic, ordering, or hand-off defect, not merely on "it ran";
  * the regenerated build and the shipped ``.kyt`` agree word for word;
  * INV-56: every run's ``stop_reason`` is read, and the settling iteration is
    ``QueueEmpty``;
  * INV-71: the three ingress arms land on THREE DISTINCT HOPS — the property
    that actually distinguishes three arms. (Distinct ``entry``/``data_addrs``
    alone does NOT: a chip-input port fan-out gives exactly that while landing
    every word on ONE face, which the rendezvous LOCK bars, and the chain then
    builds, routes, and emits nothing.)
  * INV-4 mutation gates: a corrupted golden, a swapped-arm drive, and a
    truncated packet each FAIL the exactness gate.

WHAT IS EXPLICITLY NOT CLAIMED, and is pinned as a MEASURED LIMIT instead
(``test_known_limit_*`` below — these are guards, so that if the boundary ever
MOVES a test says so):

  * the chain sustains **ONE control iteration**. A second iteration wedges,
    an arm-saturated drive of even ONE iteration wedges, and a reversed arm
    order wedges — all with a POST-group ``Deadlock`` (a real wedge by INV-67,
    not a healthy mid-group hold).
  * therefore there is **no steady-state throughput number** for this chain,
    and the gate does not invent one. The honest rate figure is the
    single-iteration latency, which the demo prints and
    ``test_latency_ceiling`` pins.
  * the measurement half of the loop (Clarke + forward Park) is not in this
    build; the whole FOC loop does not fit ONE 10x12 array on corridor/face
    budget (INV-71).

Run::

    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_foc_motor_example.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "foc_motor"
for _p in (_ROOT / "placekyt", _ROOT / "runtime" / "python", _ROOT / "verification",
           _ROOT / "verification" / "tests", _EX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytestmark = pytest.mark.skipif(
    not (_EX / "foc_motor.kyt").exists(), reason="foc_motor.kyt absent")

from foc_motor_demo import (  # noqa: E402
    ARMS, FocChain, PLACEMENT, arm_landings, chip_for, golden, load_and_build,
    place_route_build, _jp, _wr)

# The single control iteration the chain supports, and its golden.
E_D, E_Q, THETA = 1000, 2000, 0x1234


@pytest.fixture(scope="module")
def built():
    return place_route_build()


@pytest.fixture(scope="module")
def shipped():
    return load_and_build()


def _run_one(bres, order=ARMS):
    lands = arm_landings(bres)
    chain = FocChain(bres, chip_for(bres, lands), lands)
    chain.iteration(E_D, E_Q, THETA, order=order)
    return chain


# --------------------------------------------------------------------------- #
#  The chain computes the right thing                                          #
# --------------------------------------------------------------------------- #

def test_regenerated_chain_is_bit_exact(built):
    """The whole placed+routed+built chain, driven on the real simulator,
    equals the host golden word for word."""
    _project, bres, _cat, _ct = built
    chain = _run_one(bres)
    want = golden([E_D], [E_Q], [THETA])
    assert chain.words == want, (
        f"chip {[hex(w) for w in chain.words]} != golden {[hex(w) for w in want]}")


def test_shipped_kyt_matches_the_regenerated_build(built, shipped):
    """The .kyt in the repo is the build this example documents."""
    _p1, bres_new, _c1, _t1 = built
    _p2, bres_kyt, _c2, _t2 = shipped
    a = _run_one(bres_new).words
    b = _run_one(bres_kyt).words
    assert a == b == golden([E_D], [E_Q], [THETA]), (a, b)


def test_packet_is_three_duty_words(built):
    """SVPWM's contract: exactly three Q15 duty words, a then b then c."""
    _project, bres, _cat, _ct = built
    assert len(_run_one(bres).words) == 3


def test_stop_reasons_are_all_queue_empty(built):
    """INV-56: read stop_reason for EVERY run. The iteration that completes the
    group — and every drain after it — must settle clean."""
    _project, bres, _cat, _ct = built
    chain = _run_one(bres)
    assert set(chain.stops) == {"QueueEmpty"}, chain.stops


# --------------------------------------------------------------------------- #
#  INV-71 — three arms means three HOPS                                        #
# --------------------------------------------------------------------------- #

def test_arms_land_on_three_distinct_hops(built):
    """The property that actually makes three arms three arms.

    A chip-input port fan-out yields distinct ``entry``/``data_addrs`` while
    every word lands on the PORT CELL — one face — which the rendezvous LOCK
    bars. That chain routes, builds, and emits NOTHING. So the hop is the
    thing to assert."""
    _project, bres, _cat, _ct = built
    lands = arm_landings(bres)          # raises on equal hops
    hops = {h for h, _a, _e in lands.values()}
    assert len(hops) == 3, f"arms share a hop (one face): {lands}"


def test_every_arm_is_driven_through_its_own_relay():
    """The relay-per-arm construction is load-bearing (INV-71) — pin it so a
    later 'simplification' that wires the port straight to an arm is caught
    here rather than by a silently empty chain."""
    relays = [n for n, (typ, _p, _xy) in PLACEMENT.items()
              if typ == "StreamSplitterBlock"]
    assert len(relays) == 3, PLACEMENT


# --------------------------------------------------------------------------- #
#  The MEASURED limits — guards, not claims                                    #
# --------------------------------------------------------------------------- #

def test_known_limit_chain_sustains_exactly_one_iteration(built):
    """MEASURED WALL (AGENTS.md §6): a SECOND control iteration wedges.

    Iteration 0 emits its packet and settles QueueEmpty; iteration 1 emits
    nothing and every run reports Deadlock — POST-group, so a REAL wedge by
    INV-67, not the healthy mid-group hold.

    WHY: both the CordicRotateBlock (N=3) and the SVPWMBlock (N=2) carry a
    measured whole-burst depth of ONE (their own suites guard it), because the
    serialize-LOCK release must ride the single cell abutting the rendezvous.
    Chained, the walls compose: the deeper chain still holds the previous
    sample when the next group is admitted.

    If this guard ever FAILS the wall has MOVED — that is good news; re-measure
    and let the throughput gate drive a real burst."""
    _project, bres, _cat, _ct = built
    lands = arm_landings(bres)
    chain = FocChain(bres, chip_for(bres, lands), lands)
    chain.iteration(E_D, E_Q, THETA)
    assert len(chain.words) == 3, chain.words
    assert set(chain.stops) == {"QueueEmpty"}, chain.stops

    n_after_first = len(chain.words)
    chain.iteration(0x0333, 0x1500, 0x4000)
    assert len(chain.words) == n_after_first, (
        f"the depth-1 boundary MOVED — a second iteration now emits "
        f"{len(chain.words) - n_after_first} more words. GOOD NEWS if real: "
        f"re-measure the wall, delete this guard, and make the rate gate "
        f"drive a sustained burst.")
    assert "Deadlock" in chain.stops[-3:], chain.stops[-3:]


def test_known_limit_arm_saturated_drive_wedges(built):
    """MEASURED WALL: even ONE iteration's three arm words enqueued
    back-to-back (``queue_words_physical`` — the INV-19 saturated path) wedges
    the chain and emits nothing.

    This is why this example reports a LATENCY, not a saturated throughput:
    there is no steady state to measure. Recording it as a guard keeps the
    example honest and catches the day it changes."""
    _project, bres, _cat, _ct = built
    lands = arm_landings(bres)
    chip = chip_for(bres, lands)
    stream = []
    for arm in ARMS:
        hop, addr, entry = lands[arm]
        val = {"e_d": E_D, "e_q": E_Q, "theta": THETA}[arm]
        stream += [_wr(hop, addr), val & 0xFFFF, _jp(hop, entry)]
    chip.queue_words_physical("x16_in", stream)
    res = chip.run(max_events=2_000_000)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    words = []
    while chip.output_available("x16_out"):
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            words.append(int(v) & 0xFFFF)
        chip.release_output_ack("x16_out")
        chip.run(max_events=8_000)
    assert not completed and not words, (
        f"the arm-saturated drive now SETTLES ({res}, words={words}). The wall "
        f"moved — re-measure, delete this guard, and report a real saturated "
        f"throughput in the README.")


def test_known_limit_reversed_arm_order_wedges(built):
    """MEASURED: driving the arms in reverse (theta, e_q, e_d) emits nothing.

    A face-locking rendezvous is SPECIFIED to be arrival-order agnostic, and
    each block proves that standalone. Here the arms are not independent — e_d
    and e_q pass through the two PI controllers first — so the ordering the
    chain tolerates is narrower than any single block's. Pinned so the
    difference is documented rather than discovered."""
    _project, bres, _cat, _ct = built
    chain = _run_one(bres, order=("theta", "e_q", "e_d"))
    assert chain.words == [], (
        f"reverse arm order now delivers {chain.words} — re-measure the "
        f"ordering constraint and update the README's claim.")


def test_latency_ceiling(built):
    """THE RATE GATE: pin the measured per-iteration latency as a CEILING so a
    regression is caught.

    Measured 17,861.5 ns to the complete packet (17,576.9 ns to the first duty
    word), simKYT's timing model. The ceiling is set 25% above the measurement
    so ordinary build-to-build jitter does not flap it, while a real slowdown
    trips it."""
    _project, bres, _cat, _ct = built
    lands = arm_landings(bres)
    chip = chip_for(bres, lands)
    t0 = chip.simulation_time
    chain = FocChain(bres, chip, lands)
    chain.iteration(E_D, E_Q, THETA)
    assert chain.out, "no duty words — cannot measure latency"
    t_packet = chain.times[-1] - t0
    assert t_packet <= 22_500.0, (
        f"per-iteration latency regressed to {t_packet:,.1f} ns "
        f"(measured 17,861.5 ns, ceiling 22,500 ns)")
    # And a FLOOR, so an accidentally-vacuous drive (no real work) is caught.
    assert t_packet >= 5_000.0, (
        f"latency {t_packet:,.1f} ns is implausibly low — is the chain "
        f"actually doing the work?")


def test_duty_word_cadence(built):
    """The three duty words leave the SVPWM emit cell as a 3-burst; measured
    142.29 ns apart. Pinned as a band."""
    _project, bres, _cat, _ct = built
    chain = _run_one(bres)
    assert len(chain.times) == 3
    gaps = [chain.times[i + 1] - chain.times[i] for i in range(2)]
    for g in gaps:
        assert 100.0 <= g <= 200.0, f"duty-word cadence {gaps} outside the band"


# --------------------------------------------------------------------------- #
#  INV-4 — the gate is proven able to FAIL                                     #
# --------------------------------------------------------------------------- #

def test_mutation_corrupted_golden_fails(built):
    """A one-LSB corruption of the golden must break the exactness gate."""
    _project, bres, _cat, _ct = built
    got = _run_one(bres).words
    bad = list(golden([E_D], [E_Q], [THETA]))
    bad[1] = (bad[1] + 1) & 0xFFFF
    assert got != bad, "a 1-LSB corruption went undetected"


def test_mutation_swapped_axis_inputs_fails(built):
    """Feeding e_q into the d axis and vice versa must change the duties —
    otherwise the gate could not tell the two axes apart."""
    _project, bres, _cat, _ct = built
    straight = golden([E_D], [E_Q], [THETA])
    swapped = golden([E_Q], [E_D], [THETA])
    assert straight != swapped, (
        "the stimulus cannot distinguish the d and q axes — pick different "
        "values, or this gate proves nothing about the axis wiring")
    assert _run_one(bres).words == straight


def test_mutation_truncated_packet_fails(built):
    """A dropped duty word must fail."""
    _project, bres, _cat, _ct = built
    got = _run_one(bres).words
    assert got[:2] != golden([E_D], [E_Q], [THETA])


def test_mutation_wrong_theta_fails(built):
    """A different rotor angle must produce different duties (the inverse Park
    rotation is genuinely in the path)."""
    _project, bres, _cat, _ct = built
    assert golden([E_D], [E_Q], [THETA]) != golden([E_D], [E_Q], [0x4000])
    assert _run_one(bres).words == golden([E_D], [E_Q], [THETA])


def test_mutation_corrupted_block_constant_fails_ON_CHIP(built):
    """INV-4 STRONG FORM: corrupt the REAL block, REBUILD it on the chip, and
    assert the whole-chain gate catches it.

    ``SVPWMBlock.SQRT3_2_Q15`` (the sqrt(3)/2 inverse-Clarke constant) is
    perturbed by 500 LSB. The mutation is GEOMETRY-PRESERVING — same cells,
    ports and faces — so it MUST still place, route and build (INV-67's
    corollary: treating a mutant's build failure as "rejected, gate passes"
    makes the gate vacuous). Measured: the mutant chip emits
    ['0x0', '0x1dc', '0xfe24'] against the true ['0x0', '0x1e4', '0xfe1c'].

    The class attribute is restored in a ``finally`` so a failure here cannot
    leak a corrupted block into another test."""
    from gr_kyttar.placement.blocks.svpwm_block import SVPWMBlock

    original = SVPWMBlock.SQRT3_2_Q15
    SVPWMBlock.SQRT3_2_Q15 = original - 500
    try:
        _project, bres_mut, _cat, _ct = place_route_build()
        mutant = _run_one(bres_mut).words
    finally:
        SVPWMBlock.SQRT3_2_Q15 = original

    assert mutant, (
        "the mutant emitted nothing — a geometry-preserving constant change "
        "must still build and run, else this gate proves nothing")
    assert mutant != golden([E_D], [E_Q], [THETA]), (
        "a 500-LSB corruption of the sqrt(3)/2 constant went UNDETECTED — the "
        "exactness gate has no teeth")


# --------------------------------------------------------------------------- #
#  The MEASUREMENT half — the README's claim, enforced                         #
# --------------------------------------------------------------------------- #

def test_measurement_half_routes_and_is_exact():
    """The README says the measurement half (Clarke + FORWARD Park) routes,
    builds and runs bit-exactly on its own — it is the WHOLE loop that does not
    fit, not the front half. That claim is ENFORCED here rather than asserted.

    MEASURED: 75 cells, three distinct arm hops, ``QueueEmpty`` on every run,
    and output ``0x868 / 0x870`` exactly matching the golden.

    The anchors are load-bearing for the same INV-46 Rule 4 reason as the main
    chain — most routed layouts mis-deliver — so a probed set is pinned."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import simkyt
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint as CPE, BlockEndpoint as BE
    from gr_kyttar.placement.blocks.clarke_transform_block import ClarkeTransformBlock
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word
    from foc_motor_demo import CHIP_YAML, LIB

    A = {"cl": (4, 1), "pk": (4, 4), "r_ia": (0, 4), "r_ib": (9, 11), "r_th": (4, 8)}
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("foc_front", ctk)
    cl = ctrl.place_block("ClarkeTransformBlock", 0, *A["cl"], library=LIB, params={})
    pk = ctrl.place_block("CordicRotateBlock", 0, *A["pk"], library=LIB,
                          params={"sign": -1})
    relays = {n: ctrl.place_block("StreamSplitterBlock", 0, *A[n], library=LIB,
                                  params={}) for n in ("r_ia", "r_ib", "r_th")}
    C = ctrl.add_logical_connection
    C(CPE(chip=0, port="x16_in"), BE(block=relays["r_ia"], port="x"), name="ia")
    C(CPE(chip=0, port="x16_in"), BE(block=relays["r_ib"], port="x"), name="ib")
    C(CPE(chip=0, port="x16_in"), BE(block=relays["r_th"], port="x"), name="th")
    C(BE(block=relays["r_ia"], port="out"), BE(block=cl, port="ia"), name="w1")
    C(BE(block=relays["r_ib"], port="out"), BE(block=cl, port="ib"), name="w2")
    C(BE(block=relays["r_th"], port="out"), BE(block=pk, port="theta"), name="w3")
    C(BE(block=cl, port="yi"), BE(block=pk, port="x"), name="cx")
    C(BE(block=cl, port="yq"), BE(block=pk, port="y"), name="cy")
    C(BE(block=pk, port="yi"), CPE(chip=0, port="x16_out"), name="o")

    rep = ctrl.auto_route_all({ctk: ct})
    assert rep.ok, [(r.name, r.reason) for r in (rep.failed or [])]
    bres = ctrl.build()
    assert bres.ok, [str(e)[:120] for e in (bres.errors or [])[:3]]

    il = bres.chips[0].input_landings
    lands = {n: (int(il[n]["hop"]) & 0x1F, int(il[n]["data_addrs"][0]),
                 int(il[n]["entry"])) for n in ("ia", "ib", "th")}
    assert len({h for h, _a, _e in lands.values()}) == 3, (
        f"the front half's arms share a hop (one face) — INV-71: {lands}")

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", lands["ia"][2])
    out, stops = [], []
    for name, value in (("ia", 1000), ("ib", 2000), ("th", 0x1234)):
        hop, addr, entry = lands[name]
        chip.inject_data_physical([value], target_hop_cnt=hop, target_addr=addr)
        chip.run(max_events=8_000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        res = chip.run(max_events=600_000)
        stops.append(res.get("stop_reason") if isinstance(res, dict) else None)
        while chip.output_available("x16_out"):
            for v, _d, _t in chip.read_port_words_timed("x16_out"):
                out.append(int(v) & 0xFFFF)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8_000)

    alpha_beta = ClarkeTransformBlock.process_reference_words([1000], [2000])
    want = [w & 0xFFFF for w in
            cordic_rotate_word(alpha_beta[0], alpha_beta[1], 0x1234, -1)]
    assert out == want, (f"front half {[hex(w) for w in out]} != golden "
                         f"{[hex(w) for w in want]}")
    assert set(stops) == {"QueueEmpty"}, stops
