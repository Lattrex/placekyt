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
import subprocess
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

# ---------------------------------------------------------------- the DISPLAY
GRC_PATH = _EXAMPLE / "fft128_2p2s.grc"
_RUNNER = _ROOT / "verification" / "grc_userpath_run.py"
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
#: The GUI's default bind, which the shipped ``.grc`` bakes into server_port.
#: Override for local iteration when the owner is holding 58950; the shipped
#: value is what the final confirmation run must use.
_PORT = int(os.environ.get("KYTTAR_USERPATH_PORT", "58950"))

#: The two ON-BIN stimulus tones' NATURAL bins, the SLOTS they leave the chip
#: on, and their frequencies. Pinned as LITERALS — never recomputed from the
#: map under test, so a corrupted map cannot silently agree with itself:
#: bit_reverse_7(9) = 72 and bit_reverse_7(37) = 82, and at fs = 32000 with
#: N = 128 each bin is 250 Hz so bins 9/37 are 2250/9250 Hz.
TONE_SLOT_A = 72
TONE_SLOT_B = 82
BIN_HZ = 250.0
TONE_A_HZ = 2250.0
TONE_B_HZ = 9250.0
#: Positions on the CENTRED (-fs/2 .. +fs/2) axis: bin b -> (b + N/2) % N.
TONE_INDEX_A = 73
TONE_INDEX_B = 101

#: The coherent-bin POWER each ON-BIN tone must reach (amplitude squared) and
#: the floor everything else must stay under. An exactly-on-bin tone leaks
#: nowhere, so the honest floor is 0 — the bar is set well under the weaker
#: tone rather than at zero so Q15 rounding is not read as a defect.
COHERENT_A = EX.AMP_A ** 2              # 0.2025
COHERENT_B = EX.AMP_B ** 2              # 0.1225
COHERENT_TOL = 0.01
LEAKAGE_MAX = 0.01


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




# =============================================================================
# 6. THE DISPLAY — what the GRC waveform window actually DRAWS
# =============================================================================
# THE REPORT THIS SECTION EXISTS FOR, in the owner's words: "the scale for the
# FFT output in the GRC waveform viewer still doesn't look right. It doesn't
# show the actual frequency where the spikes are ... that still has time as the
# x axis and those spikes just flow across the screen."
#
# He was right, and the chip was not the problem — the chain was already
# bit-exact through the hosted server (section 5 above). The `.grc` plotted the
# kyttar sink's RAW stream on a qtgui TIME sink titled "FFT128 output words
# (I, Q interleaved)". That stream is not a spectrum and cannot be made into
# one by an axis relabel; FOUR transformations separate them, and all four are
# gated here:
#
#   1. DE-INTERLEAVE. The chain tail is a COMPLEX exit cell — out_i then out_q
#      from one cell — so the sink stream carries TWO float words per frequency
#      bin. A time sink draws them as two adjacent samples of one time series.
#   2. STRIP THE 127-SAMPLE LATENCY. The first 127 complex outputs of a burst
#      are the zero-initialised pipeline's startup values, not a frame.
#   3. UN-REVERSE. The transform emits DIF order with deliberately no reorder
#      buffer: slot k carries bin bit_reverse_7(k). Plotting slots is a
#      SCRAMBLED spectrum that still looks plausible.
#   4. FFTSHIFT. Natural order runs 0 -> +fs/2 then JUMPS to -fs/2 -> 0, which
#      no single linear axis can label; rolling by N/2 makes it monotonic so
#      the sink's set_x_axis(-samp_rate/2, bin_hz) labels every point.
#
# The gates below assert the DRAWN trace (tapped at the display blocks' own
# output through the live user path), not merely the sink stream — a gate that
# stops at the sink passes while the plot is unusable, which is exactly what
# happened.

def _grc_doc():
    import yaml
    with open(GRC_PATH) as fh:
        return yaml.safe_load(fh)


def _grc_block(doc, name):
    for b in doc.get("blocks", []):
        if b.get("name") == name:
            return b
    raise AssertionError(f"no block named {name!r} in the shipped .grc")


def _spectrum_gate(centred):
    """The example's own DISPLAY assertion, reusable by the mutants.

    A correct plot is TWO clean lines at the tones' centred positions, each at
    its coherent power, with every other point on the floor."""
    n = EX.N
    if len(centred) != n:
        return False
    top = sorted(range(n), key=lambda i: centred[i], reverse=True)[:2]
    if set(top) != {TONE_INDEX_A, TONE_INDEX_B}:
        return False
    if abs(centred[TONE_INDEX_A] - COHERENT_A) > COHERENT_TOL:
        return False
    if abs(centred[TONE_INDEX_B] - COHERENT_B) > COHERENT_TOL:
        return False
    rest = max(v for i, v in enumerate(centred)
               if i not in (TONE_INDEX_A, TONE_INDEX_B))
    return rest < LEAKAGE_MAX


# ------------------------------------------------- the shipped .grc's config
def test_the_shipped_grc_plots_a_frequency_axis():
    """The display sink is a VECTOR (frequency) sink on a real Hz axis — not a
    time sink. This is the reported defect, asserted structurally.

    ``qtgui_time_sink_x`` has no frequency axis to configure: its x axis is
    time, and a stream of raw interleaved words plotted on it is exactly the
    "spikes flow across the screen" the owner saw. The fix is a different sink
    fed by a different stream, so the gate pins BOTH."""
    doc = _grc_doc()
    sink = _grc_block(doc, "spectrum_sink")
    assert sink["id"] == "qtgui_vector_sink_f", (
        f"the spectrum display is a {sink['id']!r} — a time sink cannot show "
        "frequency on its x axis no matter how it is labelled")
    p = sink["parameters"]
    assert p["x_units"] == '"Hz"', (
        f"the spectrum x axis is in {p['x_units']!r}, not Hz — the user still "
        "cannot tell what frequency a spike is on")
    assert p["x_start"] == "-samp_rate / 2", f"x_start is {p['x_start']!r}"
    assert p["x_step"] == "bin_hz", f"x_step is {p['x_step']!r}"
    assert p["vlen"] == "n_fft", f"vlen is {p['vlen']!r}"
    assert "Hz" in p["x_axis_label"] and "samp_rate" in p["x_axis_label"], (
        f"the x label {p['x_axis_label']!r} does not state the bin -> Hz map")

    variables = {b["name"]: b["parameters"].get("value")
                 for b in doc["blocks"] if b.get("id") == "variable"}
    assert variables.get("samp_rate") == "32000", (
        f"samp_rate is {variables.get('samp_rate')!r}, expected 32000 — the "
        "documented Hz mapping is stated at that rate")
    assert variables.get("bin_hz") == "samp_rate / n_fft", (
        f"bin_hz is {variables.get('bin_hz')!r} — the bin width must be "
        "DERIVED from samp_rate and n_fft, never a hard-coded number")
    assert variables.get("n_fft") == str(EX.N)
    assert variables.get("latency") == str(EX.LATENCY)
    assert float(variables["samp_rate"]) / EX.N == BIN_HZ

    # And the OLD display must be gone: no time sink may be fed the raw
    # recovered word stream any more.
    conns = [list(c) for c in doc["connections"]]
    assert ["kyt_sink", "0", "spectrum", "0"] in conns, (
        "the kyttar sink no longer feeds the spectrum display block")
    time_sinks = {b["name"] for b in doc["blocks"]
                  if b.get("id") == "qtgui_time_sink_x"}
    fed_by_chip = {c[2] for c in conns if c[0] == "kyt_sink"}
    assert not (time_sinks & fed_by_chip), (
        f"a TIME sink {sorted(time_sinks & fed_by_chip)} is still plotting the "
        "chip's raw recovered words — that is the reported defect")
    # The input scope is fine and stays: it is a time-domain signal.
    assert "in_scope" in time_sinks


def test_the_shipped_grc_display_block_does_all_four_transformations():
    """The display block de-interleaves, strips the latency, un-reverses AND
    fftshifts. Any one of the four missing puts the spikes somewhere wrong
    while the plot still looks like a plausible spectrum."""
    src = _grc_block(_grc_doc(), "spectrum")["parameters"]["_source_code"]
    assert "frame[0::2]" in src and "frame[1::2]" in src, (
        "the display block does not DE-INTERLEAVE the complex pair — it is "
        "plotting raw words, two per bin")
    assert "np.abs(bins) ** 2" in src, "no per-bin power is computed"
    assert "self.latency" in src and "off < self.latency" in src, (
        "the 127-sample startup transient is not stripped")
    assert "nat[self.rev] = power" in src, (
        "the bit-reversed DIF slot order is not un-reversed — the spectrum "
        "would be scrambled while still looking plausible")
    assert "(np.arange(self.n) + self.n // 2) % self.n" in src, (
        "the shift is not the fftshift permutation")
    assert "centred[self.shift] = nat" in src, (
        "the fftshift is computed but never APPLIED — a linear Hz axis would "
        "mislabel every negative-frequency bin")
    assert "off + self.n > self.burst" in src, (
        "the display block does not drop the burst's RAGGED TAIL — see "
        "test_the_display_drops_the_bursts_ragged_tail for what that paints")
    # the permutations the .grc computes match this module's published ones
    n = EX.N
    assert [(k + n // 2) % n for k in range(n)] == EX.fftshift_order(n)


def test_the_display_drops_the_bursts_ragged_tail():
    """A LIVE-PATH defect this gate caught, and the reason the drawn-trace gate
    taps the display rather than the sink.

    ``burst_len`` is 384 while ``latency + 2*n_fft`` is 383, so every burst
    ends with ONE sample left over. A frame reader that keeps consuming across
    the boundary builds its next "frame" from that 1 real sample plus 127 of
    the NEXT burst's zero-fill transient — an ALL-ZERO spectrum. With
    ``server_repeat`` looping the batch, the plot then blanks on EVERY THIRD
    frame: measured, 4728 frames of a live run in a perfectly regular
    good / good / blank cycle. Bit-exactness cannot see this at all; it is
    purely a framing fault in the display glue.

    Asserted on the arithmetic directly (no server needed): the shipped
    burst/latency/size really do leave a ragged tail, and the display block's
    own rule drops exactly it.
    """
    assert EX.BURST == 384
    assert EX.LATENCY + 2 * EX.N == 383 < EX.BURST, (
        "the burst is now a whole number of frames past the latency, so the "
        "ragged tail this gate describes no longer exists — re-derive before "
        "relaxing the display block's boundary rule")

    # Walk the shipped block's own frame rule over several looped bursts and
    # demand every frame start INSIDE one burst.
    starts, pos = [], 0
    for _ in range(6 * 4):
        off = pos % EX.BURST
        if off < EX.LATENCY:
            pos += EX.LATENCY - off
            continue
        if off + EX.N > EX.BURST:
            pos += EX.BURST - off          # THE RULE UNDER TEST
            continue
        starts.append(pos)
        pos += EX.N
    assert starts, "the frame rule emitted no frames at all"
    assert len(starts) == len({s // EX.BURST for s in starts}) * 2, (
        f"the frame rule does not emit exactly 2 frames per burst: {starts}")
    for s in starts:
        burst_i, off = divmod(s, EX.BURST)
        assert off >= EX.LATENCY, (
            f"frame at {s} starts inside burst {burst_i}'s startup transient")
        assert off + EX.N <= EX.BURST, (
            f"frame at {s} STRADDLES burst {burst_i}'s boundary — it would be "
            f"{EX.BURST - off} real samples plus {off + EX.N - EX.BURST} of "
            "the next burst's zero-fill, i.e. a blank spectrum")

    # TEETH: without the rule, the straddling frame reappears.
    starts_bad, pos = [], 0
    for _ in range(6 * 4):
        off = pos % EX.BURST
        if off < EX.LATENCY:
            pos += EX.LATENCY - off
            continue
        starts_bad.append(pos)
        pos += EX.N
    straddling = [s for s in starts_bad
                  if (s % EX.BURST) + EX.N > EX.BURST]
    assert straddling, (
        "dropping the boundary rule produced no straddling frame — this gate "
        "would then be inert")


def test_the_shipped_grc_states_the_tone_frequencies():
    """Opened cold, without the README, the flowgraph says in Hz where the
    spikes are — bin width and both tones."""
    doc = _grc_doc()
    desc = doc["options"]["parameters"]["description"]
    title = doc["options"]["parameters"]["title"]
    for text, where in ((desc, "description"), (title, "title")):
        assert "2250" in text and "9250" in text, (
            f"the .grc {where} never states the tone frequencies: {text!r}")
        assert "250" in text and "Hz" in text, (
            f"the .grc {where} does not state the bin width in Hz")
    plot = _grc_block(doc, "spectrum_sink")["parameters"]["name"]
    assert "2250" in plot and "9250" in plot and "250 Hz/bin" in plot, (
        f"the plot's own title {plot!r} does not state the bin width and the "
        "tone frequencies")


# ---------------------------------------------------- the bin -> Hz mapping
def test_bin_to_hz_mapping_pinned():
    """``bin_hz = fs/N`` and ``f(k) = k*fs/N`` — against literals."""
    assert EX.SAMP_RATE == 32000.0
    assert EX.bin_hz() == BIN_HZ == 250.0
    assert EX.bin_to_hz(EX.TONE_A) == TONE_A_HZ == 2250.0
    assert EX.bin_to_hz(EX.TONE_B) == TONE_B_HZ == 9250.0
    assert EX.bin_to_hz(0) == 0.0
    # the positive half runs up to (but not including) N/2 ...
    assert EX.bin_to_hz(EX.N // 2 - 1) == (EX.N // 2 - 1) * BIN_HZ
    # ... and bins at or above N/2 are NEGATIVE frequencies.
    assert EX.bin_to_hz(EX.N // 2) == -(EX.N / 2) * BIN_HZ == -16000.0
    assert EX.bin_to_hz(EX.N - 1) == -BIN_HZ
    with pytest.raises(ValueError):
        EX.bin_to_hz(EX.N)


def test_the_centred_axis_is_monotonic_and_spans_the_band():
    """The display axis is ``-fs/2 + i*bin_hz`` — monotonic (the whole reason
    for the fftshift) and covering exactly one band, with the shift map and
    ``bin_to_hz`` agreeing on every bin."""
    axis = EX.axis_hz()
    assert len(axis) == EX.N
    assert axis[0] == -EX.SAMP_RATE / 2 == -16000.0
    assert axis[-1] == EX.SAMP_RATE / 2 - BIN_HZ
    assert all(axis[i + 1] - axis[i] == BIN_HZ for i in range(EX.N - 1))
    shift = EX.fftshift_order()
    for k in range(EX.N):
        assert axis[shift[k]] == EX.bin_to_hz(k), (
            f"bin {k}: shifted to axis point {shift[k]} = {axis[shift[k]]} Hz, "
            f"but bin_to_hz says {EX.bin_to_hz(k)}")
    # and the pinned tone indices really are where the tones land
    assert shift[EX.TONE_A] == TONE_INDEX_A
    assert shift[EX.TONE_B] == TONE_INDEX_B
    assert axis[TONE_INDEX_A] == TONE_A_HZ
    assert axis[TONE_INDEX_B] == TONE_B_HZ


def test_the_bit_reversal_map_is_pinned():
    """The slot -> bin map is a permutation, an involution, and spot-pinned
    against LITERALS — never against the code that produces it."""
    rev = EX.unreverse()
    assert len(rev) == EX.N
    assert rev[:8] == [0, 64, 32, 96, 16, 80, 48, 112]
    assert rev[TONE_SLOT_A] == EX.TONE_A and rev[EX.TONE_A] == TONE_SLOT_A
    assert rev[TONE_SLOT_B] == EX.TONE_B and rev[EX.TONE_B] == TONE_SLOT_B
    assert sorted(rev) == list(range(EX.N))
    assert all(rev[rev[k]] == k for k in range(EX.N)), "not an involution"


# ------------------------------------------- the spectrum on the real chip
@needs_mc
def test_the_tones_land_at_their_frequencies_on_the_real_chip(built):
    """THE answer to "where are the spikes, in Hz": drive the ``.grc``'s OWN
    two-tone stimulus through the real 4-die board and read the peaks off the
    SAME centred Hz axis the flowgraph plots.

    Measured: +2250.0 Hz at power 0.2025 and +9250.0 Hz at power 0.1225, with
    every other one of the 128 points at exactly 0.0."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres)
    stim = EX.grc_stimulus()
    got = EX.drive(eng, landing, stim)
    assert len(got) == 2 * len(stim), (
        f"chain A's tail egressed {len(got)} words for {len(stim)} samples")
    pairs = [(got[2 * k], got[2 * k + 1]) for k in range(len(stim))]

    frames = EX.frames_of(pairs)
    assert len(frames) == 2, (
        f"expected 2 whole 128-bin frames past the latency, got {len(frames)}")
    axis = EX.axis_hz()
    for f, frame in enumerate(frames):
        # the tones leave the chip on their BIT-REVERSED slots
        power_by_slot = [(EX.s16(i) / 32768.0) ** 2 + (EX.s16(q) / 32768.0) ** 2
                         for (i, q) in frame]
        top_slots = sorted(range(EX.N), key=lambda s: power_by_slot[s],
                           reverse=True)[:2]
        assert set(top_slots) == {TONE_SLOT_A, TONE_SLOT_B}, (
            f"frame {f}: the chip's strongest SLOTS are {sorted(top_slots)}, "
            f"expected the bit-reversed {sorted([TONE_SLOT_A, TONE_SLOT_B])}")

        centred = EX.centred_power_spectrum(frame)
        assert _spectrum_gate(centred), f"frame {f} is not the two-line spectrum"
        i_a = int(max(range(EX.N), key=lambda i: centred[i]))
        assert axis[i_a] == TONE_A_HZ, (
            f"frame {f}: the strongest line is at {axis[i_a]:.0f} Hz, "
            f"expected {TONE_A_HZ:.0f} Hz")
        assert axis[TONE_INDEX_B] == TONE_B_HZ
        assert abs(centred[TONE_INDEX_A] - COHERENT_A) < COHERENT_TOL
        assert abs(centred[TONE_INDEX_B] - COHERENT_B) < COHERENT_TOL
        # an exactly-ON-BIN pair leaks NOWHERE: every other point is zero.
        assert all(centred[i] == 0.0 for i in range(EX.N)
                   if i not in (TONE_INDEX_A, TONE_INDEX_B)), (
            f"frame {f}: an on-bin tone pair leaked into other bins")


# ------------------------------------------------ THE DRAWN TRACE, live
def _serve(kyt, *, wait_s: float = 240.0):
    """Host ``kyt`` on the user-path port, waiting for an EXCLUSIVE bind.

    The wait is correctness, not politeness. Every user-path suite binds the
    same port, so a concurrent suite (or the owner's own GUI) holds it, and the
    dangerous failure is NOT the loud one: if the bind quietly returns
    something else the flowgraph happily talks to SOMEBODY ELSE'S server on
    that port and this gate reads somebody else's chip output as a spectrum
    defect. So this retries until it holds the port itself.
    """
    import time

    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.set_project(load_project(str(kyt)))
    sim = SimController(ctrl)
    deadline = time.monotonic() + wait_s
    bound = None
    while time.monotonic() < deadline:
        try:
            bound = sim.start_gnuradio_server(port=_PORT)
        except OSError:
            bound = None
        if bound == _PORT:
            return ctrl, sim
        try:
            sim.stop_gnuradio_server()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
    raise AssertionError(
        f"never obtained an EXCLUSIVE bind of port {_PORT} within {wait_s}s "
        f"(last bind result {bound!r}) — another user-path suite or a stale "
        "server is holding it; run this suite STANDALONE")


def _run_flowgraph(grc, secs=120, taps=""):
    r = subprocess.run(
        [_GR_PYTHON, str(_RUNNER), str(grc), str(secs), taps],
        capture_output=True, text=True, timeout=secs + 300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    sinks = {}
    for line in r.stdout.splitlines():
        if line.startswith("SINK "):
            parts = line.split()
            sinks[parts[1]] = [float(x) for x in parts[2:]]
    assert r.returncode == 0 and sinks, (
        f"generated flowgraph failed (rc={r.returncode}):\n"
        f"{r.stdout[-1500:]}\n{r.stderr[-2000:]}")
    return sinks


@pytest.mark.skipif(not os.path.exists(_GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_the_drawn_spectrum_trace_through_the_shipped_user_path():
    """THE DISPLAY GATE: host the SHIPPED ``.kyt``, run the SHIPPED ``.grc``'s
    generated top block under the real GNU Radio interpreter, and assert the
    trace the VECTOR SINK IS ACTUALLY DRAWN WITH.

    The tap is on ``to_db`` and ``spectrum`` — the display glue itself, not the
    kyttar sink — because a gate that stops at the sink is testing the wrong
    thing: the sink stream was already bit-exact while the plot was a scrolling
    time series of raw words. ``grc_userpath_run.py``'s third argument names
    these extra ``block.port`` taps and matches the tap's vlen to the port, so
    what is asserted here is the 128-point vector the sink paints.

    ⚠️ RUN THIS SUITE STANDALONE — see ``_serve``.

    Asserted:
      * every frame the display emits is the two-line spectrum, at the tones'
        CENTRED positions (73 -> +2250 Hz, 101 -> +9250 Hz);
      * each line is at its coherent power (0.45^2 and 0.35^2), everything else
        on the floor;
      * the dB trace agrees (-6.9 and -9.1 dBFS) and sits inside the sink's
        -95..5 dBFS y axis, so nothing paints off-scale;
      * every frame across the whole run is identical — ``server_repeat``
        replays the genuine batch rather than sliding the frame grid.
    """
    kyt = _EXAMPLE / "fft128_2p2s.kyt"
    if not kyt.exists() or not GRC_PATH.exists():
        pytest.skip("fft128_2p2s example not generated (run build_kyt.py)")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    _ctrl, sim = _serve(kyt)
    try:
        sinks = _run_flowgraph(GRC_PATH, taps="spectrum.0,to_db.0")
    finally:
        sim.stop_gnuradio_server()

    n = EX.N
    lin = sinks.get("spectrum.0")
    db = sinks.get("to_db.0")
    assert lin and db, (
        "the display blocks were not tapped — the gate cannot see what the "
        f"vector sink draws (got {sorted(sinks)})")
    assert len(lin) % n == 0 and len(db) % n == 0, (
        f"the display emitted {len(lin)} / {len(db)} floats, not a whole "
        f"number of {n}-point vectors")
    assert len(lin) >= n, "the display never emitted one whole spectrum frame"

    lin_frames = [lin[i * n:(i + 1) * n] for i in range(len(lin) // n)]
    db_frames = [db[i * n:(i + 1) * n] for i in range(len(db) // n)]
    axis = EX.axis_hz()

    for f, frame in enumerate(lin_frames):
        assert _spectrum_gate(frame), (
            f"drawn frame {f} is not the two-line spectrum: strongest points "
            f"{sorted(range(n), key=lambda i: frame[i], reverse=True)[:4]}")
        # THE MEASUREMENT: the spikes are AT these frequencies.
        peak = int(max(range(n), key=lambda i: frame[i]))
        assert axis[peak] == TONE_A_HZ, (
            f"drawn frame {f}: the strongest line is at {axis[peak]:.0f} Hz, "
            f"expected {TONE_A_HZ:.0f} Hz")
        second = int(max((i for i in range(n) if i != peak),
                         key=lambda i: frame[i]))
        assert axis[second] == TONE_B_HZ, (
            f"drawn frame {f}: the second line is at {axis[second]:.0f} Hz, "
            f"expected {TONE_B_HZ:.0f} Hz")
        rest = max(v for i, v in enumerate(frame)
                   if i not in (TONE_INDEX_A, TONE_INDEX_B))
        assert rest < LEAKAGE_MAX, (
            f"drawn frame {f}: {rest} leaked into another bin — two ON-BIN "
            "tones must be two clean lines")

    # The dB trace the sink is actually configured for, on its own axis.
    import math
    want_a_db = 10.0 * math.log10(COHERENT_A)      # -6.93
    want_b_db = 10.0 * math.log10(COHERENT_B)      # -9.12
    for f, frame in enumerate(db_frames):
        assert abs(frame[TONE_INDEX_A] - want_a_db) < 0.2, (
            f"drawn dB frame {f}: +{TONE_A_HZ:.0f} Hz reads "
            f"{frame[TONE_INDEX_A]:.2f} dBFS, expected ~{want_a_db:.2f}")
        assert abs(frame[TONE_INDEX_B] - want_b_db) < 0.2, (
            f"drawn dB frame {f}: +{TONE_B_HZ:.0f} Hz reads "
            f"{frame[TONE_INDEX_B]:.2f} dBFS, expected ~{want_b_db:.2f}")
        # every point must land INSIDE the sink's ymin/ymax or it paints
        # off-scale even though the chip is right (the class of defect that
        # produced the raw +-30000 flat line before).
        assert all(-95.0 <= v <= 5.0 for v in frame), (
            f"drawn dB frame {f} has points outside the sink's -95..5 dBFS "
            f"axis: {[v for v in frame if not -95.0 <= v <= 5.0][:4]}")

    # server_repeat LOOPS the genuine one-batch result: every later frame must
    # be a clean replay of the first, or the frame grid is sliding.
    for f in range(1, len(lin_frames)):
        assert lin_frames[f] == lin_frames[0], (
            f"drawn frame {f} differs from frame 0 — the looped display is "
            "not a clean replay of the real batch")


# ---------------------------------------------- INV-4 teeth for the DISPLAY
@needs_mc
def test_the_display_mutations_fail(built):
    """A display gate never shown to fail certifies nothing. Each mutation
    below is a way the plot could look plausible and be WRONG; every one must
    be rejected by the same ``_spectrum_gate`` the live gate uses.

    These are not hypothetical: the "no un-reversal" mutant IS the plot the
    example shipped with before the fft_spectrum-style display chain, and the
    "raw words as a time series" mutant is literally the reported defect."""
    import numpy as np

    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres)
    stim = EX.grc_stimulus()
    got = EX.drive(eng, landing, stim)
    pairs = [(got[2 * k], got[2 * k + 1]) for k in range(len(stim))]
    frame = EX.frames_of(pairs)[0]

    honest = EX.centred_power_spectrum(frame)
    assert _spectrum_gate(honest), (
        "the honest spectrum must PASS, or the teeth mean nothing")

    def power_slots():
        return [(EX.s16(i) / 32768.0) ** 2 + (EX.s16(q) / 32768.0) ** 2
                for (i, q) in frame]

    shift = EX.fftshift_order()

    # (a) NO UN-REVERSAL — plot the chip's raw DIF slots, fftshifted. A clean
    #     two-line spectrum, with both lines in the WRONG place (slots 72/82
    #     shift to 8/18, i.e. -14000 and -11500 Hz).
    raw = power_slots()
    bad = [0.0] * EX.N
    for k, i in enumerate(shift):
        bad[i] = raw[k]
    assert not _spectrum_gate(bad), (
        "the raw bit-reversed slots passed the display gate — the un-reversal "
        "is not load-bearing, so the gate certifies nothing")

    # (b) WRONG N in the un-reverse map: a 6-bit (FFT64) reversal applied to
    #     128 slots — a subtly wrong permutation.
    rev6 = []
    for k in range(EX.N):
        r, v = 0, k
        for _ in range(6):
            r = (r << 1) | (v & 1)
            v >>= 1
        rev6.append(r % EX.N)
    nat = [0.0] * EX.N
    for slot, b in enumerate(rev6):
        nat[b] = raw[slot]
    bad = [0.0] * EX.N
    for k, i in enumerate(shift):
        bad[i] = nat[k]
    assert not _spectrum_gate(bad), "a 6-bit reversal map passed the gate"

    # (c) NO FFTSHIFT — correct natural-order bins read against the centred
    #     axis. The lines land at indices 9 and 37, i.e. -13750 and -6750 Hz.
    rev = EX.unreverse()
    nat = [0.0] * EX.N
    for slot, b in enumerate(rev):
        nat[b] = raw[slot]
    assert not _spectrum_gate(nat), (
        "the unshifted natural-order vector passed the gate — a linear Hz "
        "axis would mislabel every negative-frequency bin")

    # (d) RAW WORDS AS A TIME SERIES — THE REPORTED DEFECT. The interleaved
    #     I/Q float stream, one window of N points straight off the sink, is
    #     what the old time sink drew. It is not a spectrum at all.
    words = [EX.s16(w) / 32768.0
             for k in range(EX.LATENCY, EX.LATENCY + EX.N // 2)
             for w in pairs[k]]
    assert len(words) == EX.N
    assert not _spectrum_gate(words), (
        "a raw interleaved-word time series passed the display gate — the "
        "gate cannot tell a spectrum from the scrolling word stream it "
        "replaces")

    # (e) NO DE-INTERLEAVE — treat the I and Q rails as separate bins (twice
    #     as many "bins", half a frame's worth of real ones).
    half = [abs(EX.s16(w) / 32768.0) ** 2
            for k in range(EX.N // 2) for w in frame[k]]
    assert not _spectrum_gate(half), (
        "an un-de-interleaved half-frame passed the display gate")

    # (f) the LATENCY not stripped: read a frame at offset 0 rather than 127.
    early = EX.centred_power_spectrum(pairs[:EX.N])
    assert not _spectrum_gate(early), (
        "the startup transient already looks like the answer — the latency "
        "strip would be untestable")

    # (g) degenerate streams a broken chain would produce.
    assert not _spectrum_gate([0.0] * EX.N), "an all-zero spectrum passed"
    assert not _spectrum_gate([1.0] * EX.N), "a flat full-scale spectrum passed"
    assert not _spectrum_gate(list(np.zeros(EX.N // 2))), "a short vector passed"


def test_mutation_wrong_sample_rate_moves_the_frequency():
    """INV-4 for the Hz claim: it is a claim about fs, not a constant. Halving
    the declared rate must halve every frequency — a mapping that ignored fs
    would keep reading 2250 Hz."""
    assert EX.bin_to_hz(EX.TONE_A, samp_rate=16000.0) == 1125.0
    assert EX.bin_to_hz(EX.TONE_A, samp_rate=16000.0) != TONE_A_HZ
    assert EX.bin_hz(samp_rate=16000.0) == 125.0


def test_mutation_unshifted_axis_mislabels_the_tones():
    """INV-4: reading the NATURAL-order vector against the centred axis (i.e.
    forgetting the fftshift) puts both tones at the wrong frequency — the
    wrong-but-plausible plot the shift exists to prevent."""
    axis = EX.axis_hz()
    # natural bin 9 sits at index 9 of an UNSHIFTED vector; on the centred
    # axis index 9 is -16000 + 9*250 = -13750 Hz, not +2250.
    assert axis[EX.TONE_A] == -13750.0
    assert axis[EX.TONE_B] == -6750.0
    assert axis[EX.TONE_A] != EX.bin_to_hz(EX.TONE_A)
    assert axis[EX.TONE_B] != EX.bin_to_hz(EX.TONE_B)


def test_the_grc_stimulus_helper_matches_the_shipped_flowgraph():
    """The gate drives ``grc_stimulus()`` while the flowgraph computes its
    vector inline. If they ever diverge, every "the tones are at 2250/9250 Hz"
    claim above is about a different stimulus than the user runs."""
    doc = _grc_doc()
    vec = _grc_block(doc, "stim")["parameters"]["vector"]
    variables = {b["name"]: b["parameters"].get("value")
                 for b in doc["blocks"] if b.get("id") == "variable"}
    assert variables.get("tone_a") == str(EX.TONE_A)
    assert variables.get("tone_b") == str(EX.TONE_B)
    assert variables.get("burst_len") == str(EX.BURST)
    assert str(EX.AMP_A) in vec and str(EX.AMP_B) in vec, (
        f"the .grc's stimulus amplitudes are not {EX.AMP_A}/{EX.AMP_B}: {vec}")
    assert "tone_a" in vec and "tone_b" in vec and "n_fft" in vec, (
        "the .grc's stimulus does not use its own tone/size variables, so the "
        f"published frequencies can drift from what it drives: {vec}")

    # Evaluate the .grc's OWN expression in the .grc's OWN variable scope and
    # demand it equal the helper the gates drive, sample for sample.
    scope = {"burst_len": EX.BURST, "n_fft": EX.N,
             "tone_a": EX.TONE_A, "tone_b": EX.TONE_B}
    theirs = [(EX.q15(c.real), EX.q15(c.imag)) for c in eval(vec, {}, scope)]
    assert theirs == EX.grc_stimulus(), (
        "the .grc's inline stimulus and grc_stimulus() have drifted apart")


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
