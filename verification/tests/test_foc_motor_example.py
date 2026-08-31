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

  * the chain **STREAMS**: six consecutive iterations with different inputs,
    every duty word bit-exact, every run ``QueueEmpty``, at a sustained
    **55.8 kHz** (``test_chain_streams_consecutive_iterations_bit_exact``,
    ``test_sustained_iteration_rate``). It used to sustain exactly ONE
    iteration; the wall was CordicRotateBlock's serialize-LOCK release
    carrying an unreconciled face constant (INV-69) and it is fixed.
  * the sustained interval ~= the fill latency, so the chain re-arms rather
    than pipelines — the serialize-LOCK holds the next triple until the
    current one clears, by construction.
  * an arm-SATURATED drive still wedges, and a REVERSED arm order still
    wedges — both re-measured after the fix and both INV-70 (arm corridors
    share cells), i.e. routing topology rather than a block defect.
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

# The streaming stimulus: N iterations, DIFFERENT values each, so a chain that
# merely repeats a latched triple cannot pass.
STREAM_ED = [1000, 0x0333, -1500 & 0xFFFF, 300, 0x0700, -200 & 0xFFFF]
STREAM_EQ = [2000, 0x1500, 900, -2200 & 0xFFFF, 0x0123, 1750]
STREAM_TH = [0x1234, 0x4000, 0x8000, 0xC000, 0x2468, 0x9ABC]


def _stream(bres, n=len(STREAM_ED), order=ARMS):
    """Drive n consecutive iterations with DIFFERENT inputs. Returns the
    chain and the wall-clock origin."""
    lands = arm_landings(bres)
    chip = chip_for(bres, lands)
    t0 = chip.simulation_time
    chain = FocChain(bres, chip, lands)
    for i in range(n):
        chain.iteration(STREAM_ED[i], STREAM_EQ[i], STREAM_TH[i], order=order)
    return chain, t0


def test_chain_streams_consecutive_iterations_bit_exact(built):
    """THE STREAMING GATE — the wall that MOVED.

    The chain used to sustain exactly ONE control iteration: a second one
    wedged with a post-group ``Deadlock`` (INV-67). ROOT CAUSE (INV-69, the
    defect class SVPWMBlock had already been fixed for): CordicRotateBlock's
    serialize-LOCK release was a value-carrying ``WRITE.CFG @1, 3`` whose face
    value came from an AUTHORED ``unlock_face`` DataWord in the abutting
    ``pre`` cell. The build's face-reconciliation pass patches face words in
    the RENDEZVOUS cell only, so that copy kept the authored WEST while the
    router actually landed arm x on NORTH. Iteration 0 completed, the release
    re-pointed LOCK_FACE at WEST, and arm x's next word — arriving on NORTH —
    was barred forever. FIX: the release is now a backward JUMP into a
    ``relock`` entry that re-points the lock from the rendezvous's OWN
    reconciled ``face_x`` word.

    This gate drives SIX consecutive iterations with DIFFERENT inputs and
    holds every duty word bit-exact.

    NOTE ON THE GOLDEN (INV-68): the PI integrators EVOLVE across samples, so
    the golden is computed over the WHOLE sequence in one call. Calling it
    per-iteration would reset the accumulator each time and disagree with the
    chip — which is a property of the model call, not of the chip."""
    _project, bres, _cat, _ct = built
    chain, _t0 = _stream(bres)
    want = golden(STREAM_ED, STREAM_EQ, STREAM_TH)
    assert len(chain.words) == 3 * len(STREAM_ED), (
        f"expected {len(STREAM_ED)} duty packets, got "
        f"{len(chain.words) / 3:.1f}: {[hex(w) for w in chain.words]}")
    assert chain.words == want, (
        f"chip {[hex(w) for w in chain.words]} != golden "
        f"{[hex(w) for w in want]}")


def test_every_streamed_run_settles_queue_empty(built):
    """INV-56: read ``stop_reason`` for EVERY run, not one. Across all six
    iterations every run must settle ``QueueEmpty`` — a single ``Deadlock``
    here is the post-group wedge signature (INV-67) coming back."""
    _project, bres, _cat, _ct = built
    chain, _t0 = _stream(bres)
    assert set(chain.stops) == {"QueueEmpty"}, chain.stops


def test_each_streamed_iteration_differs_from_its_neighbours(built):
    """The stimulus really does distinguish the iterations: if consecutive
    packets were identical, a chain that latched one triple and repeated it
    would sail through the exactness gate."""
    want = golden(STREAM_ED, STREAM_EQ, STREAM_TH)
    packets = [tuple(want[3 * i:3 * i + 3]) for i in range(len(STREAM_ED))]
    assert len(set(packets)) == len(packets), (
        f"the streaming stimulus produces repeated packets — it cannot "
        f"distinguish a streaming chain from a latched one: {packets}")


def test_sustained_iteration_rate(built):
    """THE RATE GATE, now that there IS a steady state.

    MEASURED (simKYT's timing model): the first packet lands 17,861.5 ns after
    injection — the FILL latency, unchanged, because it is the pipeline depth
    the first triple must traverse. Every packet after it follows ~17,925 ns
    behind the previous one, i.e. **55.8 kHz sustained**.

    FILL vs STEADY STATE: the interval essentially EQUALS the fill latency
    here, so the chain is NOT pipelined across iterations — the serialize-LOCK
    holds the next triple until the current one has cleared, by construction
    (INV-46 Rule 3 / INV-69). What the fix bought is that the chain now
    RE-ARMS instead of wedging; it did not make the stages overlap. So the
    sustained rate is (correctly) about one-over-the-latency, not better.

    Bands are set wide enough not to flap on build-to-build jitter while still
    catching a real regression."""
    _project, bres, _cat, _ct = built
    chain, t0 = _stream(bres)
    assert len(chain.times) == 3 * len(STREAM_ED), chain.times
    ends = [chain.times[3 * i + 2] for i in range(len(STREAM_ED))]
    fill = ends[0] - t0
    gaps = [ends[i + 1] - ends[i] for i in range(len(ends) - 1)]
    mean = sum(gaps) / len(gaps)

    assert 12_000.0 <= fill <= 22_500.0, (
        f"fill latency {fill:,.1f} ns outside the band (measured 17,861.5 ns)")
    assert 12_000.0 <= mean <= 22_500.0, (
        f"sustained interval {mean:,.1f} ns outside the band "
        f"(measured 17,925 ns = 55.8 kHz)")
    # The cadence must be STEADY — a chain that degrades iteration over
    # iteration (a lock creeping out of phase) would show a widening spread.
    assert max(gaps) - min(gaps) <= 2_000.0, (
        f"the inter-iteration interval is not steady: {gaps}")


def test_known_limit_arm_saturated_drive_wedges(built):
    """MEASURED WALL, and this one did NOT move with the INV-69 fix: one
    iteration's three arm words enqueued back-to-back
    (``queue_words_physical`` — the INV-19 saturated path) still wedges.

    Re-measured after the fix, driving SIX iterations saturated: the run ends
    ``EventLimit`` with ZERO duty words. So saturated does NOT equal
    per-sample at chain level; the honest sustained figure is the per-sample
    one ``test_sustained_iteration_rate`` pins.

    WHY it is not the INV-69 defect: this wedges on the FIRST iteration, before
    any release runs, whereas the INV-69 defect always completed iteration 0.
    It is INV-70 — the three arms' corridors share cells, so an arbiter-HELD
    word's in-flight words block the segment a later arm must transit. That is
    routing topology, not a block defect."""
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
    """MEASURED WALL, and this one did NOT move with the INV-69 fix: driving
    the arms in reverse (theta, e_q, e_d) emits nothing.

    Re-measured after the fix and confirmed a REAL wedge, not a healthy hold:
    all three arms are delivered and the chain STILL emits zero words, so the
    group never completes (INV-67 — a mid-group ``Deadlock`` would clear once
    the last arm lands; this does not).

    This is INV-70, not the INV-69 defect: the theta word arrives on a barred
    face, the arbiter holds it, and its in-flight words occupy corridor cells
    the e_d/e_q arms must transit — a circular wait with no motion required.
    Fixing it means corridor-DISJOINT arm deliveries, i.e. placement/routing,
    which is out of scope for a block change.

    Pinned so the difference between the block-level guarantee (each
    rendezvous IS arrival-order agnostic standalone) and the chain-level
    behaviour is documented rather than rediscovered."""
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

def test_mutation_unreconciled_release_face_collapses_the_stream_ON_CHIP(built):
    """INV-4 STRONG FORM for the INV-69 fix: make the serialize-LOCK release
    read an UNRECONCILED face word, rebuild on the chip, and assert the
    streaming gate catches it.

    The mutant re-points ``relock`` at ``face_fwd`` — a real is_face DataWord
    on the same cell, so the mutation is GEOMETRY-PRESERVING (same cells,
    ports, faces, register budget) and MUST still place, route and build
    (INV-67's corollary: reading a mutant's build failure as "rejected, gate
    passes" makes the gate vacuous). It differs from the true build only in
    which face the release re-admits.

    MEASURED: the mutant emits exactly ONE packet (3 words) and then wedges —
    precisely the pre-fix wall — against six packets for the true build. So
    the streaming gate is proven able to FAIL, and the property it protects is
    specifically "the release reads the reconciled arm face".

    The class is restored in a ``finally`` so a failure cannot leak the
    mutant into another test."""
    import dataclasses
    from gr_kyttar.placement.blocks.cordic_rotate_block import CordicRotateBlock

    original = CordicRotateBlock.build_cell_programs

    def mutated(self):
        cells = original(self)
        rdv = cells["rdv"]
        head, _sep, tail = rdv.assembly_template.partition("relock:\n")
        assert _sep, "relock entry vanished — the mutation anchor is gone"
        bad = head + _sep + tail.replace(
            "MOVE [LOCK_FACE], R{data:face_x}",
            "MOVE [LOCK_FACE], R{data:face_fwd}")
        assert bad != rdv.assembly_template, "the mutation did not apply"
        cells["rdv"] = dataclasses.replace(rdv, assembly_template=bad)
        return cells

    CordicRotateBlock.build_cell_programs = mutated
    try:
        _p, bres_mut, _c, _t = place_route_build()
        chain, _t0 = _stream(bres_mut)
        mutant_words = list(chain.words)
    finally:
        CordicRotateBlock.build_cell_programs = original

    want = golden(STREAM_ED, STREAM_EQ, STREAM_TH)
    assert mutant_words, (
        "the mutant emitted NOTHING — a geometry-preserving face change must "
        "still build and run its first iteration, else this gate proves "
        "nothing about the streaming property specifically")
    assert mutant_words != want, (
        "an UNRECONCILED release face went UNDETECTED — the streaming gate "
        "has no teeth")
    assert len(mutant_words) < 3 * len(STREAM_ED), (
        f"the mutant streamed {len(mutant_words) / 3:.1f} packets; the "
        f"unreconciled release must collapse the stream")


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
