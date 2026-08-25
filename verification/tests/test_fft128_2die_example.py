# SPDX-License-Identifier: GPL-3.0-or-later
"""FFT128 across TWO DIES — THE CROSSING, on the real two-chip system.

``test_fft128_split.py`` gates the arithmetic: the composition identity
``whole(x) == die1(die0(x))``, both dies' cell contracts, both folds. It
deliberately stops at the die boundary — "the crossing needs the multi-chip
engine and is gated separately". **This file is that separate gate.**

WHAT IT PROVES, and how that differs from the split gates: the design is
PLACED on two chips, ROUTED, BUILT, and DRIVEN on
``simkyt.MultiChipSimulation``, and the words that come out of chip 1's
``x16_out`` are compared to the whole transform's reference. Nothing here is
composed in Python — a run that never crossed the boundary cannot pass.

MEASURED (2026-08-24, the shipped example):
    200 samples driven -> 400 words egress chip 1 -> 200/200 BIT-EXACT
    (73 non-zero outputs past the latency-127 transient, so not vacuous)

THE FAULT THIS GATE PINS. The two-die design was previously reported as
livelocked — "0 of 520 words, from trigger 1". It was not. The dies, the
routes, the build and the crossing were all correct; the DRIVE was not. A
complex sample is a three-part transaction (``WRITE xi``, ``WRITE xq``, one
``JUMP``) and on the multi-chip path each part must be pumped to quiescence
before the next is injected. Queue them back to back and the single-
outstanding input handshake is overrun and the system makes no forward
progress. ``test_the_unpaced_drive_is_what_stalls`` holds that distinction so
it cannot be re-learned the hard way, and
``test_the_crossing_carries_the_complex_pair_and_one_trigger`` pins the
boundary packet shape that makes the paced drive work.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fft128_2die_example.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "fft128_2die"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EXAMPLE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fft128_2die as EX  # noqa: E402

CHIP_YAML = Path(EX.CHIP_YAML)
pytestmark = pytest.mark.skipif(not CHIP_YAML.exists(),
                                reason="chip yaml absent")

#: The sample count the on-chip gate drives. The transform's latency is 127,
#: so a shorter run only exercises the zero-fill transient — the REACH
#: discipline FFT64 paid for. 200 carries 73 non-zero outputs.
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
    """The real two-chip design: placed, routed, crossing wired, built."""
    return EX.build_two_die()


@pytest.fixture(scope="module")
def driven(built):
    """Drive N_SAMPLES through the pair ONCE; every on-chip gate reads this."""
    _ctrl, bres, _d0, _d1 = built
    eng, landing = EX.open_engine(bres)
    words = EX.stimulus(N_SAMPLES)
    infos = []
    got = EX.drive(eng, landing, words,
                   on_sample=lambda k, out, info: infos.append((len(out), info)))
    return words, got, infos


# =============================================================================
# 1. The design is real — placed, routed, built as TWO chips
# =============================================================================
def test_the_design_places_routes_and_builds_on_two_chips(built):
    """Both dies place at their declared anchors, every net routes, and the
    two-chip build succeeds. ``build_two_die`` asserts route/build success
    internally, so reaching here IS the proof; this pins the shape."""
    ctrl, bres, d0, d1 = built
    assert len(ctrl.project.chips) == 2
    assert ctrl.project.block(d0).placement.chip == 0
    assert ctrl.project.block(d1).placement.chip == 1
    assert bres.ok
    assert len(bres.words(0)) > 0 and len(bres.words(1)) > 0
    # Six nets: xi/xq/out on each chip, all routed.
    assert len(ctrl.project.connections) == 6
    assert all(c.is_routed for c in ctrl.project.connections)


def test_the_crossing_is_wired_chip0_out_to_chip1_in(built):
    """ONE inter-chip wire, in ONE direction. A stage-boundary cut of a
    feed-forward pipeline needs exactly one crossing — if this grows, the cut
    is no longer at a stage boundary."""
    ctrl, _bres, _d0, _d1 = built
    ics = ctrl.project.inter_chip_connections
    assert len(ics) == 1
    ic = ics[0]
    assert (ic.from_chip, ic.from_port) == (0, "x16_out")
    assert (ic.to_chip, ic.to_port) == (1, "x16_in")


def test_both_dies_land_off_the_port_cell_so_the_crossing_must_be_routed(built):
    """Both landings are REACHED BY A CORRIDOR, not sitting on the port cell.

    This is why the design needs the routed-input path: the inter-chip relay
    must deliver a crossed word by WRITE+JUMP over the configured hop, not by
    an at-landing raw queue. A design whose head sat ON the port would never
    exercise it — which is how this path stayed untested."""
    _ctrl, bres, _d0, _d1 = built
    for cid in (0, 1):
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


def test_the_shipped_kyt_is_the_verified_design(built):
    """The ``.kyt`` the owner OPENS is the design this gate verifies.

    A shipped example whose file has drifted from the verified design is worse
    than no example: it looks inspectable and is not. So assert the STRUCTURE
    the design's correctness rests on — the two dies, their chips, their
    anchors, all six nets routed, and the one crossing.

    Deliberately NOT a bitstream-identity check. The auto-router is not
    deterministic across repeated builds in one process: die 1's egress corner
    has THREE equal-length (17-cell) shortest paths and the tie-break varies,
    so the same design legitimately builds to different — equally correct —
    bitstreams. Measured: every observed variant runs bit-exact
    (``test_the_pair_is_bit_exact_end_to_end`` drives whichever one this
    process produced). Pinning the bytes would make this gate fail on a
    coin-flip while proving nothing extra; what matters is that the file
    reloads into the same design AND that it builds AND that it runs, which
    is what this and the on-chip gates assert between them."""
    if not EX.KYT_PATH.exists():
        pytest.skip("fft128_2die.kyt not generated (run build_kyt.py)")
    ctrl = _load_shipped_kyt()
    proj = ctrl.project
    assert len(proj.chips) == 2
    placed = {b.type: b.placement.chip for b in proj.blocks}
    assert placed == {"FFT128Die0": 0, "FFT128Die1": 1}, placed
    for b in proj.blocks:
        cells = [(c.x, c.y) for c in b.placement.cells]
        anchor = (min(x for x, _ in cells), min(y for _, y in cells))
        want = EX.DIE0_ANCHOR if b.type == "FFT128Die0" else EX.DIE1_ANCHOR
        assert anchor == want, (
            f"{b.type} sits at {anchor}, not its declared anchor {want} — a "
            "spine fold's plan uses ABSOLUTE coordinates")
    assert len(proj.connections) == 6
    assert all(c.is_routed for c in proj.connections)
    ics = proj.inter_chip_connections
    assert len(ics) == 1
    assert (ics[0].from_chip, ics[0].from_port,
            ics[0].to_chip, ics[0].to_port) == (0, "x16_out", 1, "x16_in")


def test_the_shipped_kyt_rebuilds_to_the_same_bitstream(built):
    """The shipped ``.kyt`` must BUILD, on both chips, to the SAME bitstream
    the verified design produces — word for word.

    This gate is only meaningful because the build is deterministic, and it
    was not always: CP-SAT ran an 8-worker portfolio with no fixed seed, so a
    design with TIED optimal routings returned whichever equally-optimal one
    finished first. The 2-die FFT128 has exactly one such net (die 1's egress
    corner, three tied 17-cell paths), and this gate failed on a coin-flip
    while the design was perfectly correct. Fixed at the source — the solver
    is seeded and its search interleaved — so bitstream identity is now a
    real, checkable property rather than a race."""
    if not EX.KYT_PATH.exists():
        pytest.skip("fft128_2die.kyt not generated (run build_kyt.py)")
    _ctrl, fresh, _d0, _d1 = built
    loaded = _load_shipped_kyt().build()
    assert loaded.ok, [str(e) for e in loaded.errors]
    for cid in (0, 1):
        assert list(loaded.words(cid)) == list(fresh.words(cid)), (
            f"chip {cid}: the shipped .kyt does not rebuild to the verified "
            "bitstream — regenerate it with build_kyt.py")


def test_the_build_is_deterministic(built):
    """The SAME design built twice in one process yields the SAME bitstream.

    Guards the CP-SAT determinism fix directly. Without a fixed seed the
    solver's parallel portfolio returns an arbitrary member of the tied
    optimum set, which makes builds irreproducible and silently defeats every
    bitstream comparison in the repo. This design is the sensitive case (it
    HAS a tied net), so it is the right place to hold the property."""
    _ctrl, first, _d0, _d1 = built
    _ctrl2, second, _e0, _e1 = EX.build_two_die()
    for cid in (0, 1):
        assert list(second.words(cid)) == list(first.words(cid)), (
            f"chip {cid}: two builds of the identical design differ — the "
            "router has become non-deterministic again (check that the "
            "CP-SAT solver still sets random_seed + interleave_search)")


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


def test_die1_egress_is_a_shortest_path(built):
    """Die 1's egress corridor is the pinned-length shortest path to x16_out.

    The net that carried the tie. Length is what the composed hop depends on,
    so pin it: a future change that returns a LONGER member of the solution
    set changes the hop and is not benign, however 'equivalent' it looks."""
    ctrl, _bres, _d0, _d1 = built
    out = next(c for c in ctrl.project.connections if c.name == "c1_out")
    route = [(p.x, p.y) for p in (out.route or [])]
    assert route, "die 1's egress net has no route"
    assert route[-1] == (9, 0), route[-1]
    assert len(route) == 17, (
        f"die 1's egress route is {len(route)} cells, not the pinned 17 — a "
        "route that changes LENGTH changes the hop")
    for (ax, ay), (bx, by) in zip(route, route[1:]):
        assert abs(ax - bx) + abs(ay - by) == 1, "route is not cell-adjacent"


# =============================================================================
# 2. THE CROSSING, on the real two-chip system
# =============================================================================
@needs_mc
def test_the_pair_is_bit_exact_end_to_end(driven):
    """THE GATE. 200 samples driven into chip 0, 400 words out of chip 1,
    every one equal to the whole N=128 transform's reference.

    This is not a composition of two per-die runs — the words crossed a real
    inter-chip boundary on a real built bitstream. A broken crossing cannot
    pass it."""
    words, got, _infos = driven
    ref = EX.reference(words)
    assert len(got) == 2 * len(words), (
        f"chip 1 egressed {len(got)} words, expected {2 * len(words)} — "
        "the pair did not carry every sample through")
    bad = [k for k in range(len(words))
           if (got[2 * k], got[2 * k + 1]) != ref[k]]
    assert not bad, (
        f"{len(bad)} sample(s) differ from whole(x); first at {bad[0]}: "
        f"got {(hex(got[2*bad[0]]), hex(got[2*bad[0]+1]))} "
        f"want {(hex(ref[bad[0]][0]), hex(ref[bad[0]][1]))}")


@needs_mc
def test_the_on_chip_run_is_not_vacuous(driven):
    """The transform's latency is 127. A run that stops short of it compares
    two streams of zeros and certifies nothing — the exact trap an 80-sample
    FFT64 run fell into. Pin the non-zero count actually carried."""
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

    Rate is a separate failure from value: a crossing that fired die 1 twice
    per sample (once per operand) would emit the wrong COUNT while individual
    words still looked plausible. Assert the count per trigger, not just the
    total."""
    _words, _got, infos = driven
    yields = {n for (n, _info) in infos}
    assert yields == {2}, (
        f"per-trigger word counts were {sorted(yields)}, expected exactly "
        "{2} — out_i and out_q, one complex sample per trigger")


@needs_mc
def test_the_system_reaches_quiescence_on_every_trigger(driven):
    """Every trigger's settle run reports ``completed`` — the two-chip system
    goes idle between samples rather than churning.

    This is the direct negation of the livelock that was reported: a design
    that never reaches quiescence hits the round cap with work still pending,
    and ``completed`` is False."""
    _words, _got, infos = driven
    stalled = [k for k, (_n, info) in enumerate(infos)
               if not info.get("completed")]
    assert not stalled, (
        f"{len(stalled)} trigger(s) never reached quiescence; first at "
        f"{stalled[0]} with {infos[stalled[0]][1]}")


# =============================================================================
# 3. What the crossing actually carries — the boundary packet
# =============================================================================
@needs_mc
def test_the_crossing_carries_the_complex_pair_and_one_trigger(built):
    """The die boundary delivers a COMPLEX PAIR followed by ONE trigger.

    This is the shape the whole design rests on. Die 0's exit cell emits
    out_i then out_q and then its JUMP, all carrying the hop composed past the
    boundary; the wire is transparent, so they arrive at die 1's landing in
    that order — WRITE reg 1, WRITE reg 2, JUMP entry. Delivered as
    WRITE+JUMP *per word* instead, die 1 would fire twice per sample on a
    half-primed operand pair (the on-chip 'matched filter gets xi but never
    xq' data loss), so this is worth asserting rather than assuming.

    Watched at the trigger where die 0 first emits real data past its
    delay-64 latency — where a fault would actually show."""
    _ctrl, bres, _d0, _d1 = built
    # Tracing must be on AT LOAD — re-loading chip 1 later to enable it would
    # reset the very state we want to watch.
    eng, landing = EX.open_engine(bres, trace=(1,))
    il1 = list(bres.chips[1].input_landings.values())[0]
    land_id = il1["cell"][1] * eng.chip_width(1) + il1["cell"][0]

    n = 70                       # past die 0's delay-64 latency
    words = EX.stimulus(n)
    mid = EX.crossing_reference(words)
    seen = {}

    def watch(k, _out, _info):
        evs = [e for e in eng.drain_trace()
               if e.get("_chip") == 1 and e.get("cell_id") == land_id
               and e.get("kind") in ("instr_arrival", "data_arrival")]
        seen[k] = evs

    EX.drive(eng, landing, words, on_sample=watch)

    k = 66                       # die 0 is emitting real data by here
    assert mid[k] != (0, 0), "pick a trigger where the crossing carries data"
    evs = seen.get(k) or []
    # The packet: WRITE <i>, WRITE <q>, JUMP — in that order, at the head.
    kinds = [e["kind"] for e in evs][:5]
    assert kinds[:5] == ["instr_arrival", "data_arrival", "instr_arrival",
                         "data_arrival", "instr_arrival"], kinds
    data = [int(e["data"], 16) for e in evs if e["kind"] == "data_arrival"]
    assert (data[0], data[1]) == mid[k], (
        f"the crossing delivered {(hex(data[0]), hex(data[1]))} at trigger "
        f"{k}; die 0's output stream carries "
        f"{(hex(mid[k][0]), hex(mid[k][1]))}")
    # ONE trigger for the pair — not one per operand.
    jumps = [e for e in evs[:5]
             if e["kind"] == "instr_arrival"
             and int(e["word"], 16) >> 12 == 0x7]
    assert len(jumps) == 1, (
        f"{len(jumps)} JUMPs arrived for one complex sample — the boundary "
        "must trigger die 1 ONCE per pair, not once per operand")


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
    # (c) one sample dropped — the crossing losing a trigger.
    assert not exact(list(got)[2:]), "a dropped sample passed"


@needs_mc
def test_the_unpaced_drive_is_what_stalls(built):
    """THE ROOT CAUSE, held as a gate.

    A complex sample is WRITE xi, WRITE xq, JUMP. Pumped to quiescence between
    the parts, the pair runs bit-exact. Queued back to back with no pump, the
    single-outstanding input handshake is overrun and the system makes NO
    forward progress — which is the "livelock" the two-die design was
    quarantined for. Same bitstream, same crossing, same everything: only the
    drive differs.

    This is deliberately a SMALL run (the point is the contrast, not reach),
    and it guards against a future 'simplification' that drops the pumps."""
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
    hop, entry = int(land2["hop"]), int(land2["entry"])
    a0, a1 = int(land2["data_addrs"][0]), int(land2["data_addrs"][1])
    unpaced = []
    for wi, wq in words:
        sim.inject_data_physical("chip0", [wi], hop, a0)
        sim.inject_data_physical("chip0", [wq], hop, a1)
        sim.inject_jump_physical("chip0", hop, entry)
        sim.run(*EX.SETTLE)
        unpaced.extend(eng2.capture(1, "x16_out"))
    assert len(unpaced) < len(paced), (
        "the unpaced drive delivered as much as the paced one — if the "
        "handshake now tolerates queued operands, this gate has outlived "
        "its purpose and the pumps may be revisited")


# =============================================================================
# 5. The measured record (written ONLY by a clean session)
# =============================================================================
@needs_mc
def test_zz_write_reports(built, driven):
    """Write each die's report from THIS session's MEASURED numbers.

    ``write_session_report`` refuses to write if the session had any failure,
    so a report on disk means the whole file was green — the counts below are
    taken from the run above, never hardcoded."""
    from kyttar_verify.session_report import write_session_report

    _ctrl, bres, _d0, _d1 = built
    words, got, infos = driven
    ref = EX.reference(words)
    n_ok = sum(1 for k in range(len(words))
               if (got[2 * k], got[2 * k + 1]) == ref[k])
    nz = sum(1 for r in ref if r != (0, 0))
    # NOTE: no "passed" key. INV-38 — the verdict is the SESSION's, supplied by
    # write_session_report; a literal here would be the hardcoded-pass
    # anti-pattern that module exists to forbid.
    common = {
        "metric": "exact",
        "n_compared": len(got), "max_abs_err": 0.0, "tolerance": 0.0,
        "bit_errors": 0, "delay_used": 0,
        "coverage": {
            "edge": False, "random": 1, "mutation": True,
            "n_fft": EX.N, "latency": EX.LATENCY,
            "output_order": "bit_reversed", "scale": "fft_over_128",
            "chip_scale": True, "two_die": True,
            "samples_driven": len(words),
            "samples_bit_exact": n_ok,
            "nonzero_outputs": nz,
            "words_egressed": len(got),
            "quiescent_triggers": sum(1 for _n, i in infos
                                      if i.get("completed")),
            "words_per_trigger": sorted({n for n, _i in infos}),
        },
    }
    assert n_ok == len(words) and len(got) == 2 * len(words)
    for name, cid, stages in (("FFT128Die0", 0, "0"),
                              ("FFT128Die1", 1, "1..6")):
        payload = dict(common)
        payload["kyttar_block"] = name
        payload["coverage"] = dict(common["coverage"],
                                   stages=stages,
                                   cells=bres.chips[cid].cell_count,
                                   bitstream_words=len(bres.words(cid)))
        write_session_report(name, payload)
