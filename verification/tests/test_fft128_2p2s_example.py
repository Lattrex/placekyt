# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT128 on the 2P2S BOARD — the carrier link, on the real 4-die system.

``test_fft128_split.py`` gates the arithmetic: the composition identity
``whole(x) == die1(die0(x))``, both dies' cell contracts, both folds. It
deliberately stops at the die boundary. **This file is the transport gate for
the 2P2S retarget.**

WHAT IT PROVES. The design is PLACED on chain A of the real 2P2S dev board
(``placekyt/resources/boards/dev2p2s.kdb`` — four dies, two parallel
daisy-chains of two), ROUTED, BUILT, DRC-clean AGAINST THAT BOARD, and DRIVEN
on ``simkyt.MultiChipSimulation``. The words that come out of chain A's tail
are compared to the whole transform's reference. Nothing here is composed in
Python — a run that never crossed the carrier link cannot pass.

MEASURED (the shipped example):
    200 samples driven -> 400 words egress chain A's tail -> 200/200 BIT-EXACT
    (73 non-zero outputs past the latency-127 transient, so not vacuous)

THE CONCURRENCY GATE. ``test_the_dies_are_concurrent_across_the_run`` is new
and is the gate that would have caught the reported "chip 0 works, then chip 1
works" animation. It asserts BOTH dies do real work on every trigger, and it
pins the honest limitation the measurement found: within ONE settle the dies
are causally sequential (die 1 cannot start on a sample before die 0 has
finished producing it), so what makes them concurrent is the run as a whole,
not any single instant. See ``examples/fft128_2p2s/README.md``.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fft128_2p2s_example.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "fft128_2p2s"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EXAMPLE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fft128_2p2s as EX  # noqa: E402

CHIP_YAML = Path(EX.CHIP_YAML)
BOARD_KDB = Path(EX.BOARD_KDB)
pytestmark = pytest.mark.skipif(not CHIP_YAML.exists(),
                                reason="chip yaml absent")

#: The sample count the on-chip gate drives. The transform's latency is 127,
#: so a shorter run only exercises the zero-fill transient. 200 carries 73
#: non-zero outputs.
N_SAMPLES = 200
MIN_NONZERO = 73


def _simkyt_has_multichip():
    import simkyt
    return hasattr(simkyt.MultiChipSimulation.new("probe", 5.0),
                   "set_port_input_routed")


needs_mc = pytest.mark.skipif(not _simkyt_has_multichip(),
                              reason="simkyt .so predates the multichip work")


@pytest.fixture(scope="module")
def built():
    """The real 4-die board design: placed on chain A, routed, links wired."""
    return EX.build_2p2s()


@pytest.fixture(scope="module")
def driven(built):
    """Drive N_SAMPLES through chain A ONCE; every on-chip gate reads this."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres)
    words = EX.stimulus(N_SAMPLES)
    infos = []
    got = EX.drive(eng, landing, words,
                   on_sample=lambda k, out, info: infos.append((len(out), info)))
    return words, got, infos


# =============================================================================
# 1. The design is real — placed on the BOARD, routed, built
# =============================================================================
def test_the_design_places_routes_and_builds_on_the_board(built):
    """All FOUR board dies exist, the transform occupies chain A, every net
    routes, and the build succeeds on the whole board.

    Four chips — not two. The point of the retarget is that this is the board
    the owner HAS, not an ad-hoc two-chip project shaped like part of it."""
    ctrl, bres, d0, d1 = built
    assert len(ctrl.project.chips) == 4, (
        "the 2P2S board has FOUR dies; a 2-chip project is not this board")
    assert ctrl.project.block(d0).placement.chip == EX.CHIP_DIE0
    assert ctrl.project.block(d1).placement.chip == EX.CHIP_DIE1
    assert bres.ok
    for cid in sorted(EX.CHIP_LABELS):
        assert len(bres.words(cid)) > 0, f"chip {cid} built no bitstream"
    # Six nets: xi/xq/out on each of chain A's two dies, all routed.
    assert len(ctrl.project.connections) == 6
    assert all(c.is_routed for c in ctrl.project.connections)


def test_the_carrier_links_are_the_boards_own_wiring(built):
    """Both of the board's on-carrier series links are wired, and NOTHING
    else. The FPGA never sees these; they are what makes each chain a chain.

    Chain A carries the transform; chain B is wired and idle. A cross-chain
    link (0->3, say) is not a wire the carrier provides — asserting the exact
    set is what keeps the design mappable onto hardware."""
    ctrl, _bres, _d0, _d1 = built
    got = {(i.from_chip, i.from_port, i.to_chip, i.to_port)
           for i in ctrl.project.inter_chip_connections}
    assert got == {(0, "x16_out", 1, "x16_in"),
                   (2, "x16_out", 3, "x16_in")}, got


@pytest.mark.skipif(not BOARD_KDB.exists(), reason="board file absent")
def test_the_design_is_drc_clean_against_the_real_board(built):
    """DRC against ``dev2p2s.kdb`` itself — the check that the ad-hoc two-chip
    project could never run, because it had no board to be checked against.

    ``_check_inter_chip`` verifies every inter-chip link the project declares
    is a wire the board physically provides. This is the gate that says the
    design maps onto hardware that exists."""
    from engine.drc import check_project

    ctrl, _bres, _d0, _d1 = built
    board = EX.load_board()
    assert len(board.chips) == 4
    assert board.has_chip_connection(0, "x16_out", 1, "x16_in")
    assert board.has_chip_connection(2, "x16_out", 3, "x16_in")
    drc = check_project(ctrl.project, ctrl.chip_types(), board,
                        catalog=ctrl.catalog)
    assert drc.ok, [f"{getattr(e, 'category', None)}: {e}"
                    for e in drc.errors[:8]]


def test_the_board_drc_rejects_a_link_the_carrier_does_not_provide(built):
    """INV-4 teeth for the board gate: a CROSS-CHAIN link must be rejected.

    Without this, `test_the_design_is_drc_clean_against_the_real_board` could
    be passing because the check is inert rather than because the design is
    right."""
    from engine.drc import check_project
    from model.connection import InterChipConnection

    ctrl, _bres, _d0, _d1 = built
    board = EX.load_board()
    bogus = InterChipConnection(0, "x16_out", 3, "x16_in")
    ctrl.project.inter_chip_connections.append(bogus)
    try:
        drc = check_project(ctrl.project, ctrl.chip_types(), board,
                            catalog=ctrl.catalog)
        assert not drc.ok, (
            "DRC accepted chip0 -> chip3, which the 2P2S carrier does not "
            "wire — the board check is inert")
    finally:
        ctrl.project.inter_chip_connections.remove(bogus)


def test_both_dies_land_off_the_port_cell_so_the_link_must_be_routed(built):
    """Both landings are REACHED BY A CORRIDOR, not sitting on the port cell.

    This is why the design needs the routed-input path: the inter-chip relay
    must deliver a crossed word by WRITE+JUMP over the configured hop, not by
    an at-landing raw queue."""
    _ctrl, bres, _d0, _d1 = built
    for cid in (EX.CHIP_DIE0, EX.CHIP_DIE1):
        il = list(bres.chips[cid].input_landings.values())[0]
        assert tuple(il["cell"]) != (0, 0), f"chip {cid} lands ON the port"
        # A complex landing takes BOTH operands into consecutive registers.
        assert len(il["data_addrs"]) == 2, il
        assert il["data_addrs"][1] == il["data_addrs"][0] + 1, il


def _load_shipped_kyt():
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.project = load_project(str(EX.KYT_PATH))
    return ctrl


def test_the_shipped_kyt_is_the_verified_design():
    """The ``.kyt`` the owner OPENS is the design this gate verifies — four
    dies, the board reference, the transform on chain A, both links."""
    if not EX.KYT_PATH.exists():
        pytest.skip("fft128_2p2s.kyt not generated (run build_kyt.py)")
    proj = _load_shipped_kyt().project
    assert len(proj.chips) == 4
    assert proj.board is not None and proj.board.name == "dev2p2s", (
        "the shipped .kyt must name the board it targets")
    placed = {b.type: b.placement.chip for b in proj.blocks}
    assert placed == {"FFT128Die0": EX.CHIP_DIE0,
                      "FFT128Die1": EX.CHIP_DIE1}, placed
    for b in proj.blocks:
        cells = [(c.x, c.y) for c in b.placement.cells]
        anchor = (min(x for x, _ in cells), min(y for _, y in cells))
        want = (EX.DIE0_ANCHOR if b.type == "FFT128Die0"
                else EX.DIE1_ANCHOR)
        assert anchor == want, (
            f"{b.type} sits at {anchor}, not its declared anchor {want} — a "
            "spine fold's plan uses ABSOLUTE coordinates")
    assert len(proj.connections) == 6
    assert all(c.is_routed for c in proj.connections)
    assert len(proj.inter_chip_connections) == 2
    # Chain B must stay EMPTY — it is the board's second chain, not spare
    # area for this design to spill into.
    assert not [b for b in proj.blocks
                if b.placement.chip in EX.CHAIN_B], (
        "chain B must carry no blocks")


def test_the_shipped_kyt_rebuilds_to_the_same_bitstream(built):
    """The shipped ``.kyt`` must BUILD, on every die, to the SAME bitstream
    the verified design produces — word for word."""
    if not EX.KYT_PATH.exists():
        pytest.skip("fft128_2p2s.kyt not generated (run build_kyt.py)")
    _ctrl, fresh, _d0, _d1 = built
    loaded = _load_shipped_kyt().build()
    assert loaded.ok, [str(e) for e in loaded.errors]
    for cid in sorted(EX.CHIP_LABELS):
        assert list(loaded.words(cid)) == list(fresh.words(cid)), (
            f"chip {cid}: the shipped .kyt does not rebuild to the verified "
            "bitstream — regenerate it with build_kyt.py")


# =============================================================================
# 2. THE CARRIER LINK, on the real board
# =============================================================================
@needs_mc
def test_chain_a_is_bit_exact_end_to_end(driven):
    """THE GATE. 200 samples driven into chain A's head, 400 words out of its
    tail, every one equal to the whole N=128 transform's reference.

    This is not a composition of two per-die runs — the words crossed the
    board's real carrier link on a real built bitstream."""
    words, got, _infos = driven
    ref = EX.reference(words)
    assert len(got) == 2 * len(words), (
        f"chain A's tail egressed {len(got)} words, expected "
        f"{2 * len(words)} — the chain did not carry every sample through")
    bad = [k for k in range(len(words))
           if (got[2 * k], got[2 * k + 1]) != ref[k]]
    assert not bad, (
        f"{len(bad)} sample(s) differ from whole(x); first at {bad[0]}: "
        f"got {(hex(got[2*bad[0]]), hex(got[2*bad[0]+1]))} "
        f"want {(hex(ref[bad[0]][0]), hex(ref[bad[0]][1]))}")


@needs_mc
def test_the_on_chip_run_is_not_vacuous(driven):
    """The transform's latency is 127. A run that stops short of it compares
    two streams of zeros and certifies nothing."""
    words, got, _infos = driven
    ref = EX.reference(words)
    nz_ref = sum(1 for r in ref if r != (0, 0))
    assert nz_ref >= MIN_NONZERO, (
        f"only {nz_ref} non-zero reference outputs in {len(words)} samples")
    nz_got = sum(1 for k in range(len(words))
                 if (got[2 * k], got[2 * k + 1]) != (0, 0))
    assert nz_got == nz_ref, (
        f"the chip produced {nz_got} non-zero outputs against the "
        f"reference's {nz_ref}")


@needs_mc
def test_every_trigger_yields_exactly_one_complex_sample(driven):
    """A 1:1 streaming transform emits ONE sample (out_i, out_q) per trigger.

    Rate is a separate failure from value: a link that fired die 1 twice per
    sample would emit the wrong COUNT while individual words still looked
    plausible."""
    _words, _got, infos = driven
    yields = {n for (n, _info) in infos}
    assert yields == {2}, (
        f"per-trigger word counts were {sorted(yields)}, expected exactly "
        "{2} — out_i and out_q, one complex sample per trigger")


@needs_mc
def test_the_system_reaches_quiescence_on_every_trigger(driven):
    """Every trigger's settle run reports ``completed`` — the board goes idle
    between samples rather than churning."""
    _words, _got, infos = driven
    stalled = [k for k, (_n, info) in enumerate(infos)
               if not info.get("completed")]
    assert not stalled, (
        f"{len(stalled)} trigger(s) never reached quiescence; first at "
        f"{stalled[0]} with {infos[stalled[0]][1]}")


@needs_mc
def test_chain_b_stays_silent(built):
    """Chain B is wired and carries no blocks — driving chain A must not put
    a single word into it.

    The board's two chains are independent. If chain A's traffic leaked into
    chain B, the 'two parallel chains' claim the board rests on would be
    false, and a second design hosted on chain B would be corrupted."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres, trace=EX.CHAIN_B)
    EX.drive(eng, landing, EX.stimulus(24))
    for cid in EX.CHAIN_B:
        assert eng.capture(cid, "x16_out") == [], (
            f"chain B's chip {cid} egressed words while only chain A was "
            "driven — the chains are not independent")
    leaked = [e for e in eng.drain_trace() if e.get("_chip") in EX.CHAIN_B]
    assert not leaked, (
        f"{len(leaked)} trace event(s) on chain B while only chain A was "
        f"driven; first {leaked[0]}")


# =============================================================================
# 3. DIE CONCURRENCY — the gate that would have caught the animation report
# =============================================================================
@needs_mc
def test_the_dies_are_concurrent_across_the_run(driven, built):
    """BOTH dies do real work on EVERY trigger — neither is batched.

    THE REPORT THIS GATE EXISTS FOR: watching the cell animation, the two dies
    looked like they ran one after the other — chip 0 busy for a long time
    with chip 1 idle, chip 1 only starting later. A model that genuinely
    batched one die and then handed a block of work to the other would still
    be bit-exact (arrival order does not change the arithmetic) while
    misrepresenting the hardware, so bit-exactness alone cannot catch it.
    This gate can.

    WHAT WAS MEASURED. Per-die trace events, per trigger, on this design:
    every trigger has die 0 doing ~1209 events and die 1 doing ~2900. Neither
    die is ever idle for a stretch of triggers while the other works — so the
    dies ARE concurrent across the run, and the engine does NOT batch.

    THE HONEST LIMIT, pinned by the companion gate below: inside a SINGLE
    trigger the dies are causally sequential, because die 0's crossing word
    for that sample does not exist until die 0 has finished computing it."""
    _words, _got, _infos = driven
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres, trace=(EX.CHIP_DIE0, EX.CHIP_DIE1))
    sim = eng._sim
    cursor = {EX.CHIP_DIE0: 0, EX.CHIP_DIE1: 0}
    per_trigger = []

    def _new(cid):
        ev = sim.get_trace(f"chip{cid}")
        out = len(ev) - cursor[cid]
        cursor[cid] = len(ev)
        return out

    # Past the latency so BOTH dies are genuinely computing (before it, die 1
    # is fed the pipeline's zero-fill and would look busy for the wrong
    # reason — but it must still be doing work, which is also asserted).
    def watch(_k, _out, _info):
        per_trigger.append((_new(EX.CHIP_DIE0), _new(EX.CHIP_DIE1)))

    EX.drive(eng, landing, EX.stimulus(140), on_sample=watch)

    idle0 = [k for k, (a, _b) in enumerate(per_trigger) if a == 0]
    idle1 = [k for k, (_a, b) in enumerate(per_trigger) if b == 0]
    assert not idle0, f"die 0 did NO work on trigger(s) {idle0[:5]}"
    assert not idle1, (
        f"die 1 did NO work on trigger(s) {idle1[:5]} — this is the batching "
        "signature: one die idle while the other runs")
    both = sum(1 for a, b in per_trigger if a and b)
    assert both == len(per_trigger), (
        f"only {both}/{len(per_trigger)} triggers had BOTH dies working")


@needs_mc
def test_within_one_trigger_the_dies_are_causally_sequential(built):
    """The measured LIMIT on concurrency, pinned so it is not mistaken for a
    scheduler bug — and so a future change that fixes it is noticed.

    Die 0 emits the crossing word for a sample as the very LAST thing it does
    for that sample: measured here, its egress reaches the port cell at event
    ~1208 of a ~1209-event burst. So within one trigger die 1 genuinely
    CANNOT start before die 0 has finished — the sequence is causal, not an
    artifact of round-robin scheduling.

    The corollary, which is what makes this worth a gate: shrinking the
    per-round event budget does NOT create overlap (measured: 10 overlap
    rounds at budgets of 400, 200 and 60 alike — one handoff round per
    sample, never more). Anyone "fixing" the animation by tuning the round
    budget is chasing the wrong thing.

    What DOES overlap on hardware is sample k+1 in die 0 against sample k in
    die 1 — pipelining ACROSS samples, which the per-sample drive
    deliberately does not do because the three-part complex transaction must
    be pumped to quiescence (see test_the_unpaced_drive_is_what_stalls)."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres, trace=(EX.CHIP_DIE0,))
    sim = eng._sim
    hop, entry = int(landing["hop"]), int(landing["entry"])
    a0, a1 = int(landing["data_addrs"][0]), int(landing["data_addrs"][1])
    head = f"chip{EX.CHIP_DIE0}"
    width = eng.chip_width(EX.CHIP_DIE0)

    words = EX.stimulus(135)
    cursor = 0
    for k, (wi, wq) in enumerate(words):
        last = k == len(words) - 1
        sim.inject_data_physical(head, [wi], hop, a0); sim.run(*EX.PUMP)
        sim.inject_data_physical(head, [wq], hop, a1); sim.run(*EX.PUMP)
        sim.inject_jump_physical(head, hop, entry)
        if not last:
            sim.run(*EX.SETTLE)
            eng.capture(EX.CHIP_DIE1, "x16_out")
            cursor = len(sim.get_trace(head))
            continue
        cursor = len(sim.get_trace(head))
        sim.run(EX.SETTLE[0], 1)          # ONE round: die 0's whole burst
        burst = sim.get_trace(head)[cursor:]

    assert len(burst) > 500, (
        f"die 0's per-sample burst is only {len(burst)} events — the shape "
        "this gate reasons about has changed; re-measure before editing")
    # The LAST cell die 0 touches is the one at its egress port (9, 0).
    port_cell = 0 * width + 9
    tail = [i for i, e in enumerate(burst) if e.get("cell_id") == port_cell]
    assert tail, "die 0's burst never reached its egress port cell (9, 0)"
    assert tail[-1] >= 0.95 * len(burst), (
        f"die 0's egress reached the port at event {tail[-1]} of "
        f"{len(burst)} ({100*tail[-1]/len(burst):.1f}%) — it used to be the "
        "LAST thing die 0 does. If the crossing word is now produced EARLY, "
        "die 1 can start while die 0 still works and the concurrency story "
        "improves; update this gate deliberately rather than deleting it")


def test_the_animation_interleaves_the_dies_rather_than_batching_them():
    """THE RENDERING half of the concurrency report, gated without a GUI.

    Even with both dies working every trigger, the GUI animated them one
    after the other, because the multi-chip refresh built its flash-step list
    by CONCATENATING each chip's steps in chip order. The canvas replays that
    list in order, so chip 0's entire burst played before chip 1's — exactly
    what was reported.

    Sorting by ``time_ns`` does NOT fix it: each chip keeps its own sim clock
    and the clocks diverge (measured: die 1's clock 2.3x die 0's after 130
    samples), so a global time sort still emits one die's whole burst first.
    The fix interleaves on each chip's progress through its OWN burst.

    This asserts the merge directly — no Qt, no GUI, no simulator."""
    from ui.sim_controller import SimController as SC

    merged = SC._interleave_chip_steps({0: ["a0", "a1", "a2"],
                                        1: ["b0", "b1", "b2"]})
    assert merged == ["a0", "b0", "a1", "b1", "a2", "b2"], merged
    # Unequal lengths: the busier die keeps flashing after the other drains.
    uneven = SC._interleave_chip_steps({0: ["a0"], 1: ["b0", "b1", "b2"]})
    assert uneven == ["a0", "b0", "b1", "b2"], uneven
    # Degenerate cases stay sane.
    assert SC._interleave_chip_steps({}) == []
    assert SC._interleave_chip_steps({3: ["only"]}) == ["only"]
    assert SC._interleave_chip_steps({0: [], 1: ["b"]}) == ["b"]
    # TEETH: the OLD behaviour (concatenate per chip) must NOT satisfy this.
    old = ["a0", "a1", "a2"] + ["b0", "b1", "b2"]
    assert merged != old, (
        "the interleave produced the concatenated order — the animation "
        "would still play one die's whole burst before the other's")


# =============================================================================
# 4. INV-4 teeth: the gate must FAIL on the faults it claims to catch
# =============================================================================
@needs_mc
def test_the_bit_exact_gate_has_teeth(driven):
    """A gate never shown to fail certifies nothing. Corrupt the captured
    stream three plausible ways and assert the comparison rejects each."""
    words, got, _infos = driven
    ref = EX.reference(words)

    def exact(stream):
        return (len(stream) == 2 * len(words)
                and all((stream[2 * k], stream[2 * k + 1]) == ref[k]
                        for k in range(len(words))))

    assert exact(got), "the honest stream must pass, or the teeth mean nothing"
    # (a) one word wrong.
    m = list(got)
    hit = next(k for k in range(len(words)) if ref[k] != (0, 0))
    m[2 * hit] = (m[2 * hit] + 1) & 0xFFFF
    assert not exact(m), "a single corrupted word passed"
    # (b) the rails swapped (out_q emitted before out_i).
    s = list(got)
    for k in range(len(words)):
        s[2 * k], s[2 * k + 1] = s[2 * k + 1], s[2 * k]
    assert not exact(s), "swapping the complex rails passed"
    # (c) one sample dropped — the link losing a trigger.
    assert not exact(list(got)[2:]), "a dropped sample passed"


@needs_mc
def test_the_unpaced_drive_is_what_stalls(built):
    """THE ROOT CAUSE, held as a gate — preserved through the retarget.

    A complex sample is WRITE xi, WRITE xq, JUMP. Pumped to quiescence between
    the parts, the chain runs bit-exact. Queued back to back with no pump, the
    single-outstanding input handshake is overrun and the system makes NO
    forward progress. Same bitstream, same link, same everything: only the
    drive differs. This is the ``--pattern batched`` reproduction the demo
    ships, held so a future 'simplification' cannot drop the pumps."""
    _ctrl, bres, _d0, _d1 = built
    words = EX.stimulus(8)

    # PACED — the shipped drive.
    eng, landing = EX.open_engine(bres)
    paced = EX.drive(eng, landing, words)
    assert len(paced) == 2 * len(words), (
        "the paced drive should carry every sample")

    # UNPACED — all three parts queued, then one settle.
    eng2, land2 = EX.open_engine(bres)
    sim = eng2._sim
    head = f"chip{EX.CHIP_DIE0}"
    hop, entry = int(land2["hop"]), int(land2["entry"])
    a0, a1 = int(land2["data_addrs"][0]), int(land2["data_addrs"][1])
    unpaced = []
    for wi, wq in words:
        sim.inject_data_physical(head, [wi], hop, a0)
        sim.inject_data_physical(head, [wq], hop, a1)
        sim.inject_jump_physical(head, hop, entry)
        sim.run(*EX.SETTLE)
        unpaced.extend(eng2.capture(EX.CHIP_DIE1, "x16_out"))
    assert len(unpaced) < len(paced), (
        "the unpaced drive delivered as much as the paced one — if the "
        "handshake now tolerates queued operands, this gate has outlived "
        "its purpose and the pumps may be revisited")


# =============================================================================
# 5. The LIVE-PATH resolution — what the hosted server would actually demux
# =============================================================================
def test_the_cross_wire_chain_resolves_its_tail_egress_tag(built):
    """A chain that continues across the CARRIER WIRE must resolve its egress
    tag — and the complex PAIR that goes with it.

    THE DEFECT THIS PINS. ``stream_targets`` finds a chain's egress tag by
    walking block -> block WITHIN ONE CHIP. Here the stream's input net is on
    chip 0 (die 0) while the tagged egress net belongs to die 1 on chip 1,
    joined by an INTER-CHIP WIRE rather than a block-to-block net — so the
    walk never reached it and ``out_tag`` resolved to None. The tail's words
    ARE tagged on the fabric, so a None makes the host demux drop every one of
    them: data flows on chip and the flowgraph shows NOTHING. The headless
    gates above stay green throughout, which is exactly why this is separate.

    ``complex_out`` matters just as much. A complex exit cell emits I then Q
    from ONE cell on tags (out_tag, out_tag+1) — measured at this chain's
    tail: {7: 140 words, 8: 140 words}. Only the I rail is wired to a net
    (wiring a second net to the same port kills egress), so the fabric emits
    tag 8 which the project graph never mentions. A demux that keeps only
    out_tag returns the stream at HALF LENGTH with the imaginary part gone —
    a plausible-looking wrong answer rather than an obvious failure."""
    from engine.port_config import multi_chip_stream_targets

    ctrl, bres, _d0, _d1 = built
    tg = multi_chip_stream_targets(ctrl.project, ctrl.registry,
                                   ctrl.catalog, build_result=bres)
    assert EX.STREAM_ID in tg, (
        f"the '{EX.STREAM_ID}' stream did not resolve at all: {sorted(tg)}")
    e = tg[EX.STREAM_ID]
    assert e["chip_id"] == EX.CHIP_DIE0, e["chip_id"]
    assert e["out_chip"] == EX.CHIP_DIE1, (
        f"the chain tail resolved to chip {e['out_chip']}, not chain A's "
        "tail — the inter-chip walk is wrong")
    assert e["out_tag"] == EX.OUT_TAG, (
        f"out_tag resolved to {e['out_tag']!r}, not {EX.OUT_TAG} — a chain "
        "that continues across the carrier wire lost its egress tag, and the "
        "host demux would drop every word the tail emits")
    assert e["complex_out"] is True, (
        "complex_out is False, so the demux would keep only the I rail and "
        "silently halve the stream — the transform's imaginary part vanishes")
    assert e["routed"] is True, "die 0 lands off the port cell"


@needs_mc
def test_the_tail_stamps_the_complex_tag_pair(built):
    """The FABRIC's ground truth for the gate above: chain A's tail stamps
    BOTH tags of the complex pair, in equal numbers.

    Without this, `test_the_cross_wire_chain_resolves_its_tail_egress_tag`
    could be asserting a convention nothing actually follows."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres)
    sim = eng._sim
    head = f"chip{EX.CHIP_DIE0}"
    hop, entry = int(landing["hop"]), int(landing["entry"])
    a0, a1 = int(landing["data_addrs"][0]), int(landing["data_addrs"][1])
    n = 40
    # Drive WITHOUT draining, so the tail port still holds the tagged words.
    for wi, wq in EX.stimulus(n):
        sim.inject_data_physical(head, [wi], hop, a0); sim.run(*EX.PUMP)
        sim.inject_data_physical(head, [wq], hop, a1); sim.run(*EX.PUMP)
        sim.inject_jump_physical(head, hop, entry); sim.run(*EX.SETTLE)

    timed = sim.read_port_words_timed(f"chip{EX.CHIP_DIE1}", "x16_out")
    hist = {}
    for (_v, d, _t) in timed:
        hist[int(d)] = hist.get(int(d), 0) + 1
    assert set(hist) == {EX.OUT_TAG, EX.OUT_TAG + 1}, (
        f"the tail stamped tags {sorted(hist)}, expected the complex pair "
        f"{[EX.OUT_TAG, EX.OUT_TAG + 1]}")
    assert hist[EX.OUT_TAG] == hist[EX.OUT_TAG + 1] == n, (
        f"I/Q rail counts {hist} are not both {n} — the pair is not balanced")




@pytest.mark.parametrize("cls_name", ["FFT128Die0", "FFT128Die1"])
def test_orientation_set_is_declared_and_gated(cls_name):
    """INV-23 for the CHIP-SCALE class: each die DECLARES the orientations it
    ships instead of silently skipping the D4 sweep.

    Both dies are spine folds anchored to absolute coordinates — die 0 must
    reach column 1 to keep its corridors open, die 1 spans the full height —
    so identity is the whole legal set. That is asserted here rather than
    assumed, which is what lets these blocks be exempt from the shared full-D4
    gate (``test_chip_scale_blocks_are_gated_elsewhere`` requires every
    manifest-done chip-scale block to name the suite that does this)."""
    from gr_kyttar.placement.blocks import fft_large as FL

    cls = getattr(FL, cls_name)
    assert cls.CHIP_SCALE is True
    assert cls.CHIP_SCALE_ORIENTATIONS == ((),), (
        f"{cls_name} now declares more than the identity orientation — every "
        "declared orientation needs a gate proving it computes identically")
    blk = cls("probe")
    lay = blk.default_layout()
    # The declared anchor IS the plan's own minimum: place_block normalises a
    # footprint to its bounding box, so anchoring anywhere else translates the
    # fold and invalidates every absolute fact the plan rests on.
    xs = [v[0] for v in lay.values()]
    ys = [v[1] for v in lay.values()]
    want = EX.DIE0_ANCHOR if cls_name == "FFT128Die0" else EX.DIE1_ANCHOR
    assert (min(xs), min(ys)) == blk.default_anchor == want, (
        f"{cls_name}'s declared anchor moved — a rotated or translated spine "
        "fold has no legal image here; re-derive before waiving the D4 gate")
