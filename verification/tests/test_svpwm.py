# SPDX-License-Identifier: GPL-3.0-or-later
"""SVPWMBlock — space-vector PWM by min-max injection, verified ON CHIP.

WHY THERE IS NO GNU RADIO COUNTERPART. SVPWM is a motor-drive modulator (the
last stage of a field-oriented controller), not a communications block; GR has
no three-phase inverter model. The golden is therefore a standalone host
reference written directly from the specification
(``verification/tests/svpwm_golden.py`` — a float textbook model AND an exact
integer model of the shipped arithmetic, cross-checked against the block's own
``SVPWMBlock.duties``), compared word for word against the real chip.

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * SIX-SECTOR SWEEP — a full 0..2pi rotation of a unit vector whose stimulus
    provably visits ALL SIX sectors AND all six 60-degree sector boundaries
    exactly (four of them with EXACT pairwise phase ties); every duty word
    equals the integer model bit-for-bit and stays within a stated bound of
    the float reference.
  * CENTERING INVARIANT — after injection, max(duties) + min(duties) equals
    the parity sum of the pre-injection extremes (in {0, 1, 2} LSB) for every
    linear-range sample. Plain sine PWM (injection dropped) violates this by
    up to half of full scale, which is what makes it a real gate.
  * OVERMODULATION — |v| beyond the linear range engages the saturating
    clamps predictably; pinned word-for-word against the integer model.
  * RENDEZVOUS — both relative arrival orders and random interleavings give
    the identical packet stream; no partial packet at startup; a starved arm
    stalls and recovers.
  * SATURATION (INV-19) — pair-saturated drive (both arm words enqueued
    back-to-back, no quiescence within a sample) equals per-sample over a
    long run; the whole-burst depth boundary is measured and guarded.
  * ORIENTATION (INV-23) — identical output in all 8 D4 orientations (its own
    gate: no shared harness can drive two independent producers on two
    distinct faces).
  * MUTATIONS (INV-4, strong form) — the REAL block corrupted, REBUILT on
    chip, and each corruption caught: inverted sector compare, injection
    dropped (sine PWM), phase sign flip, non-saturating add, dropped
    serialize-LOCK release, wrong sqrt(3)/2 constant.

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_svpwm.py -q
"""
from __future__ import annotations

import dataclasses
import math
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
from svpwm_golden import (  # noqa: E402
    HALF_Q15, SQRT3_2_Q15, svpwm_duties, svpwm_duties_float, svpwm_phases,
    svpwm_sector, svpwm_stream, _s16, _sat16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


def _q15(x: float) -> int:
    return int(round(max(-1.0, min(0.9999695, x)) * 32768)) & 0xFFFF


def _rot_pairs(n: int, amp: float = 1.0) -> list:
    """n equally spaced samples of a rotating vector of amplitude ``amp`` —
    the SVPWM reference stimulus. With n a multiple of 6, every 60-degree
    sector boundary is hit EXACTLY (theta = k*(360/n) passes through 0, 60,
    120, 180, 240, 300 when n % 6 == 0)."""
    out = []
    for k in range(n):
        th = 2 * math.pi * k / n
        out.append((_q15(amp * math.cos(th)), _q15(amp * math.sin(th))))
    return out


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
#  The REAL two-upstream chain: two INDEPENDENT identity relays.               #
# --------------------------------------------------------------------------- #
#
# Each arm is a StreamSplitterBlock — an exact, memoryless identity relay — fed
# from the ONE chip input port by its OWN net, so each arm has its own landing
# (hop + entry + data address) and the harness can produce ANY relative arrival
# order (the adversarial async interleaving the LOCK rendezvous must survive).
#
# GEOMETRY (load-bearing): the block's `rendezvous` cell is a LEAF of its 7x1
# fold — its only in-block neighbour is `scale`, to the EAST — leaving free
# faces for the two arms. The alpha arm is anchored WEST of it (the authored
# `unlock_face` the release re-points the lock to), the beta arm north/south.

_ANCHORS = [
    ([(0, 5), (2, 3)], (2, 5)),
    ([(0, 5), (2, 7)], (2, 5)),
    ([(0, 4), (2, 2)], (2, 4)),
    ([(0, 6), (2, 8)], (2, 6)),
    ([(0, 3), (2, 1)], (2, 3)),
    ([(0, 7), (2, 9)], (2, 7)),
]

PORTS = ("v_alpha", "v_beta")


def _pnr(ctrl, ctk, ct) -> bool:
    """ROUTE-ONLY (auto_route_all), keeping the authored anchors — and the
    choice is MEASURED, not stylistic. ``auto_pnr`` re-PACKS the placement
    compactly, and its packs herd both arm blocks into the port corner where
    the two arm corridors SHARE cells: a beta word arriving FIRST is then held
    at the face-locked rendezvous, its in-flight WRITE/DATA/JUMP words back up
    the shared segment, and the alpha word can no longer reach its own arm —
    the pair never completes and the stream stays EMPTY (measured: 10/12
    packed layouts wedge on a beta-first sample; the same layouts are perfect
    alpha-first, which is why the probe drives BOTH orders). NOTE the INV-67
    reading discipline this diagnosis needed: the held beta word's own run
    reporting ``Deadlock`` MID-GROUP is the HEALTHY hold signature and proves
    nothing by itself — the wedge is proven by the POST-group state (the
    completing alpha word blocked in transit, zero packets after the full
    pair). With the authored spread-out anchors and route-only, 12/12 layouts
    pass both orders. The head-of-line hazard is a property of two
    independent streams sharing corridor cells — keep the arms' corridors
    disjoint in any real design."""
    try:
        if bool(ctrl.auto_route_all({ctk: ct}).ok):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(ctrl.auto_pnr({ctk: ct}).ok)
    except Exception:  # noqa: BLE001 — a failed pack is just another anchor
        return False


class _Chain:
    """A built two-upstream SVPWM chain + a driver that fires ONE arm."""

    def __init__(self, bres, chip, landings, ctrl=None, blk=None):
        self.bres, self.chip, self.landings = bres, chip, landings
        self.ctrl, self.blk = ctrl, blk
        self.out: list[int] = []

    def fire(self, arm: int, value: int):
        """Push ONE word into arm ``arm`` (0=v_alpha, 1=v_beta) and settle."""
        land = self.landings[f"i{arm}"]
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self.chip.run(max_events=6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        res = self.chip.run(max_events=400000)
        self._drain()
        return res

    def sample(self, a: int, b: int, order=(0, 1)):
        """Drive one complete (v_alpha, v_beta) pair in the given order."""
        vals = {0: a, 1: b}
        for arm in order:
            self.fire(arm, vals[arm])

    def _drain(self):
        while self.chip.output_available("x16_out"):
            w = self.chip.read_port_i16("x16_out").view("uint16").tolist()
            self.out.extend(int(x) & 0xFFFF for x in w)
            self.chip.release_output_ack("x16_out")
            self.chip.run(max_events=8000)


def _layout_candidates(orient=None):
    """Yield routed + built chain candidates (distinct landings), sweeping the
    anchors with two CP-SAT attempts each. Every candidate is a REAL placed +
    routed + built chip; whether its layout DELIVERS correctly is what the
    value probe below decides."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for arm_xy, v_xy in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("svpwm_chain", ctk)
            ks = [ctrl.place_block("StreamSplitterBlock", 0, *arm_xy[i],
                                   library=LIB, params={}) for i in range(2)]
            v = ctrl.place_block("SVPWMBlock", 0, *v_xy, library=LIB, params={})
            if orient:
                # Rotate/mirror BEFORE routing (INV-23): the nets are still
                # unrouted logical connections, so OrientBlockCommand preserves
                # them for the router.
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
            if not all(f"i{i}" in il for i in range(2)):
                continue
            # The two arms MUST have DISTINCT landings, else the harness cannot
            # drive them independently and every interleaving test is vacuous.
            sig = {(int(il[f"i{i}"]["hop"]), int(il[f"i{i}"]["entry"]),
                    int(il[f"i{i}"]["data_addrs"][0])) for i in range(2)}
            if len(sig) < 2:
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
            yield _Chain(bres, chip, il, ctrl, v)


def _build_chain_raw(orient=None):
    """The FIRST routed layout, WITHOUT the value probe. The MUTATION gates
    use this — the value-probing builder rejects a corrupted block at the
    probe, which would collapse every mutation test into a skip (INV-46 Rule
    4a's exact hazard, measured on the XorJoin: 35 skips for a broken
    block)."""
    return next(_layout_candidates(orient=orient), None)


# The probe pairs are ASYMMETRIC (duties(a,b) != duties(b,a)) and cover TWO
# consecutive samples driven in OPPOSITE arrival orders, so the probe sees
# (a) a swapped-arm delivery, (b) a release that fails to re-admit the second
# pair, and (c) the shared-corridor head-of-line wedge a beta-first arrival
# exposes (see _pnr). Values stay in the linear range so a saturation defect
# cannot mask an arm-routing defect here.
_PROBE_PAIRS = [(0x1111, 0x2222), (0x0333, 0x7000)]
_PROBE_ORDERS = [(0, 1), (1, 0)]


def _try_build_probed(orient=None):
    """The value-probing build: every candidate layout is SMOKED (INV-46 Rule
    4) before a gate may use it, on its own THROWAWAY chip — driving a pair
    advances the lock rotation and latches arm state, so smoking the chip a
    gate is about to use would leak the probe's values into that gate's first
    packet. Returns the first candidate whose probe is exact, else None.

    WHY the probe is TWO consecutive asymmetric samples: auto_pnr is a CP-SAT
    search and not deterministic; a layout can route, build, and present two
    distinct landings yet still (a) deliver the arms SWAPPED — caught because
    duties(a,b) != duties(b,a) for the probe pairs — or (b) land arm alpha
    off the authored `unlock_face`, so the serialize-LOCK release re-points
    the lock at a face nothing drives and the chain wedges after ONE packet —
    caught by the SECOND sample. Either surfaces as an intermittent failure
    of whichever gate drew the layout, indistinguishable from a block bug."""
    import simkyt
    exp = svpwm_stream([p[0] for p in _PROBE_PAIRS],
                       [p[1] for p in _PROBE_PAIRS])
    for ch in _layout_candidates(orient=orient):
        probe = simkyt.Chip.from_yaml(CHIP_YAML)
        probe.load_bitstream_physical(ch.bres.words(0))
        probe.set_port_entry_address("x16_in", int(ch.landings["i0"]["entry"]))
        pch = _Chain(ch.bres, probe, ch.landings)
        for (a, b), order in zip(_PROBE_PAIRS, _PROBE_ORDERS):
            pch.sample(a, b, order=order)
        if pch.out == exp:
            return ch
    return None


def _build_chain(orient=None):
    ch = _try_build_probed(orient=orient)
    if ch is None:
        pytest.skip("no anchor routed the two-upstream SVPWM chain on this run")
    return ch


# --------------------------------------------------------------------------- #
#  The GOLDEN agrees with the block's own reference                            #
# --------------------------------------------------------------------------- #

def test_golden_matches_the_block_reference():
    """The standalone golden and the block class's ``duties`` must agree over
    a dense sweep + the Q15 corners — if they drift, every comparison below
    measures the wrong thing."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    corners = [0, 1, 0x7FFF, 0x8000, 0xFFFF, 0x4000, 0xC000, 28378, 16384]
    for a in corners:
        for b in corners:
            assert svpwm_duties(a, b) == SVPWMBlock.duties(a, b), (a, b)
    for k in range(720):
        th = 2 * math.pi * k / 720
        for amp in (0.3, 0.9, 1.0):
            a, b = _q15(amp * math.cos(th)), _q15(amp * math.sin(th))
            assert svpwm_duties(a, b) == SVPWMBlock.duties(a, b), (a, b)
    assert (SVPWMBlock.SQRT3_2_Q15, SVPWMBlock.HALF_Q15) == (
        SQRT3_2_Q15, HALF_Q15) == (28378, 16384)


def test_integer_model_stays_within_the_float_bound():
    """THE STATED BOUND: every integer-model duty is within 4 Q15 LSB of the
    float reference over the rotation sweeps. Derived, not tuned: two floor
    halvings + one floor product truncate < 1 LSB each, the sqrt(3)/2 constant
    rounds within 0.5 LSB, and the input quantization contributes ~1.37 LSB
    through the Clarke row — measured worst case 2.84 LSB, bounded at 4."""
    worst = 0.0
    for k in range(1440):
        th = 2 * math.pi * k / 1440
        for amp in (0.25, 0.5, 0.9, 1.0):
            a, b = _q15(amp * math.cos(th)), _q15(amp * math.sin(th))
            d = svpwm_duties(a, b)
            f = svpwm_duties_float(_s16(a) / 32768.0, _s16(b) / 32768.0)
            for di, fi in zip(d, f):
                worst = max(worst, abs(di / 32768.0 - fi) * 32768.0)
    assert worst <= 4.0, f"integer model drifted {worst:.2f} LSB from float"


def test_model_centering_invariant_and_its_teeth():
    """The exact centering claim, on the model: for every linear-range sample
    max(duties) + min(duties) == (max(p) & 1) + (min(p) & 1)  (in {0, 1, 2}).
    TEETH: the same property computed WITHOUT the injection (m = 0 — plain
    sine PWM) must violate the bound massively, else the gate could not see
    the injection-dropped mutant."""
    rng = random.Random(11)
    worst_sine = 0
    for _ in range(4000):
        amp = rng.uniform(0.05, 1.0)
        th = rng.uniform(0, 2 * math.pi)
        a, b = _q15(amp * math.cos(th)), _q15(amp * math.sin(th))
        pa, pb, pc = svpwm_phases(a, b)
        mx, mn = max(pa, pb, pc), min(pa, pb, pc)
        m = ((mx * HALF_Q15) >> 15) + ((mn * HALF_Q15) >> 15)
        if any(not (-32768 <= p - m <= 32767) for p in (pa, pb, pc)):
            continue                       # saturated sample: invariant not claimed
        d = svpwm_duties(a, b)
        assert max(d) + min(d) == (mx & 1) + (mn & 1), (a, b, d)
        assert 0 <= max(d) + min(d) <= 2
        worst_sine = max(worst_sine, abs(mx + mn))     # sine PWM's residual
    assert worst_sine > 2000, (
        "the stimulus never produced a mid-sector sample where sine PWM is "
        "off-center — the centering gate would be toothless")


def test_model_tie_flip_is_value_invariant_so_the_fired_mutant_is_inversion():
    """WHY the boundary-compare mutant is an INVERSION and not a bare </<=
    flip, recorded executably. In min-max injection a tie holds two EQUAL
    values, so flipping the tie-break picks the same VALUE and the duties are
    bit-identical — a '<= instead of <' mutation can NEVER fire and a gate
    built on it would certify nothing (INV-4). Assert that over a dense sweep
    including all exact-tie boundaries, then assert the INVERTED compare (the
    mutant the suite actually builds on chip) diverges on the same sweep."""
    def duties_tieflip(a, b):
        pa, pb, pc = svpwm_phases(a, b)
        mx = pa
        if pb >= mx:
            mx = pb
        if pc >= mx:
            mx = pc
        mn = pa
        if pb <= mn:
            mn = pb
        if pc <= mn:
            mn = pc
        m = ((mx * HALF_Q15) >> 15) + ((mn * HALF_Q15) >> 15)
        return tuple(_sat16(v - m) for v in (pa, pb, pc))

    def duties_inverted(a, b):
        pa, pb, pc = svpwm_phases(a, b)
        mx = min(pa, pb, pc)               # the inverted-compare selection
        mn = min(pa, pb, pc)
        m = ((mx * HALF_Q15) >> 15) + ((mn * HALF_Q15) >> 15)
        return tuple(_sat16(v - m) for v in (pa, pb, pc))

    ties_seen = 0
    inverted_diverges = 0
    for a, b in _rot_pairs(96, 1.0):
        pa, pb, pc = svpwm_phases(a, b)
        if pa == pb or pa == pc or pb == pc:
            ties_seen += 1
        assert svpwm_duties(a, b) == duties_tieflip(a, b), (a, b)
        if svpwm_duties(a, b) != duties_inverted(a, b):
            inverted_diverges += 1
    assert ties_seen >= 4, (
        f"only {ties_seen} exact-tie boundary samples — the sweep is not "
        f"hitting the boundaries exactly")
    assert inverted_diverges > 80, inverted_diverges


# --------------------------------------------------------------------------- #
#  GATE 1 — the six-sector rotation sweep, on chip                             #
# --------------------------------------------------------------------------- #

def test_full_rotation_sweep_all_six_sectors_and_boundaries_exact():
    """A full 0..2pi rotation of a UNIT vector, 48 equally spaced samples —
    every duty word EXACT against the integer model, and within the stated
    4-LSB bound of the float reference.

    NON-VACUITY of the sector claim, asserted on the stimulus itself: the 48
    samples visit all SIX (argmax, argmin) sectors, hit every 60-degree
    boundary angle exactly (48 % 6 == 0), and four of those boundaries carry
    EXACT pairwise phase ties — the samples where a broken tie/compare path
    would sit. The chip agreeing with the model on every one of these IS the
    six-sector + boundary proof."""
    pairs = _rot_pairs(48, 1.0)
    sectors = {svpwm_sector(a, b) for a, b in pairs}
    assert len(sectors) == 6, f"stimulus covers only {sectors}"
    boundary = [pairs[k] for k in range(0, 48, 8)]     # 0,60,...,300 degrees
    assert len(boundary) == 6
    ties = sum(1 for a, b in boundary
               if len(set(svpwm_phases(a, b))) < 3)
    assert ties >= 4, f"only {ties} boundary samples carry an exact tie"

    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == exp, (
        f"sweep mismatch: first diff at word "
        f"{next(i for i, (g, e) in enumerate(zip(ch.out, exp)) if g != e) if ch.out != exp and len(ch.out) == len(exp) else 'len'} "
        f"(got {len(ch.out)} words, expected {len(exp)})")
    # ...and every chip word is within the stated float bound.
    for i, w in enumerate(ch.out):
        a, b = pairs[i // 3]
        f = svpwm_duties_float(_s16(a) / 32768.0, _s16(b) / 32768.0)[i % 3]
        assert abs(_s16(w) / 32768.0 - f) * 32768.0 <= 4.0, (i, w, f)


def test_centering_invariant_on_chip():
    """GATE 2 — the sum property, on the REAL chip words: for every
    linear-range sweep sample, max(duty) + min(duty) equals the parity sum of
    the pre-injection extremes (0, 1 or 2 LSB). This is the gate that fails
    for plain sine PWM (injection dropped): its residual is |max+min| of the
    RAW phases — up to half of full scale mid-sector (proven above)."""
    pairs = _rot_pairs(24, 0.9)
    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    assert len(ch.out) == 3 * len(pairs), (len(ch.out), len(pairs))
    checked = 0
    for i, (a, b) in enumerate(pairs):
        pa, pb, pc = svpwm_phases(a, b)
        mx, mn = max(pa, pb, pc), min(pa, pb, pc)
        m = ((mx * HALF_Q15) >> 15) + ((mn * HALF_Q15) >> 15)
        if any(not (-32768 <= p - m <= 32767) for p in (pa, pb, pc)):
            continue
        d = [_s16(w) for w in ch.out[3 * i:3 * i + 3]]
        assert max(d) + min(d) == (mx & 1) + (mn & 1), (a, b, d)
        checked += 1
    assert checked >= 20, f"only {checked} linear-range samples — vacuous"


def test_overmodulation_saturates_predictably():
    """GATE 3 — |v| beyond the linear range drives the saturating clamps;
    the output is pinned word-for-word against the integer model, and the
    stimulus provably ENGAGES the clamps (both rails), so this cannot pass
    vacuously on unsaturated arithmetic."""
    pairs = [
        (0x8000, 0x7FFF),     # -1 alpha, +1 beta: nh + t = +44761 -> 0x7FFF
        (0x8000, 0x8000),     # nh - t = +44762 -> 0x7FFF
        (0x7FFF, 0x8000),     # t - nh = -44760 -> 0x8000 on the b phase
        (0x7FFF, 0x7FFF),
        (0x8000, 0x0000),     # duty subtract saturation territory
        (0xA000, 0x7FFF),
        (0x6000, 0x8000),
    ]
    # Non-vacuity: the model must clamp at BOTH rails somewhere in this set.
    seen = set()
    for a, b in pairs:
        pa, pb, pc = svpwm_phases(a, b)
        nh = -((_s16(a) * HALF_Q15) >> 15)
        t = (_s16(b) * SQRT3_2_Q15) >> 15
        if nh + t > 32767 or nh - t > 32767:
            seen.add("hi")
        if nh + t < -32768 or nh - t < -32768:
            seen.add("lo")
    assert seen == {"hi", "lo"}, (
        f"the overmodulation stimulus never engages both clamp rails: {seen}")

    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == exp, (ch.out, exp)


def test_packet_is_three_words_in_fixed_a_b_c_order():
    """THE PACKET CONVENTION, pinned: 3 words per sample on one stream, fixed
    order duty_a, duty_b, duty_c. The stimulus makes all three duties
    pairwise DISTINCT, so ANY permutation or dropped word would differ."""
    a, b = 0x2000, 0x6000
    da, db, dc = svpwm_duties(a, b)
    assert len({da, db, dc}) == 3, "stimulus cannot distinguish the order"
    ch = _build_chain()
    ch.sample(a, b)
    assert ch.out == [da & 0xFFFF, db & 0xFFFF, dc & 0xFFFF], (
        f"packet order broken: got {ch.out}, expected [a, b, c] = "
        f"{[da & 0xFFFF, db & 0xFFFF, dc & 0xFFFF]}")


@pytest.mark.parametrize("seed", [5, 41, 907])
def test_random_vectors_exact(seed):
    """RANDOM coverage (>=3 seeds): arbitrary (v_alpha, v_beta) pairs across
    the full Q15 plane — not just rotation samples — exact vs the model."""
    rng = random.Random(seed)
    pairs = [(rng.randrange(0, 0x10000), rng.randrange(0, 0x10000))
             for _ in range(10)]
    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == exp, (ch.out, exp)


def test_edge_values_exact():
    """EDGE coverage: zero vector, +-full scale on each axis, the 1-LSB
    vector, and the Q15 extremes together."""
    pairs = [(0, 0), (0x7FFF, 0), (0x8000, 0), (0, 0x7FFF), (0, 0x8000),
             (1, 1), (0xFFFF, 0xFFFF), (0x7FFF, 0x8000), (0x8000, 0x7FFF)]
    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == exp, (ch.out, exp)


# --------------------------------------------------------------------------- #
#  RENDEZVOUS — arrival order, startup, starvation                             #
# --------------------------------------------------------------------------- #

def test_reversed_arrival_order_is_identical():
    """BETA-FIRST arrival must give the identical stream: the arbiter holds
    the beta word until the alpha word has been latched. Asymmetric values so
    a swap would be visible (duties(a,b) != duties(b,a) for these)."""
    pairs = [(0x1111, 0x2222), (0x0333, 0x7000), (0x8000, 0x7FFF)]
    for a, b in pairs:
        assert svpwm_duties(a, b) != svpwm_duties(b, a), (a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b, order=(1, 0))
    assert ch.out == exp, (ch.out, exp)


@pytest.mark.parametrize("seed", [3, 17])
def test_random_interleavings_preserve_the_pairs(seed):
    """Random per-sample arrival order over a longer run — whatever order the
    two arms fire in, the emitted stream is exactly the golden."""
    rng = random.Random(seed)
    ch = _build_chain()
    a_w, b_w = [], []
    for _ in range(8):
        a = rng.randrange(0, 0x10000)
        b = rng.randrange(0, 0x10000)
        a_w.append(a)
        b_w.append(b)
        ch.sample(a, b, order=(0, 1) if rng.random() < 0.5 else (1, 0))
    assert ch.out == svpwm_stream(a_w, b_w), (ch.out,)


def test_startup_emits_nothing_until_both_arms_have_spoken():
    """NO PARTIAL PACKET, ever: after the alpha word alone the chip has
    produced NOTHING; the 3-word packet appears only when beta lands."""
    ch = _build_chain()
    ch.fire(0, 0x1234)
    assert ch.out == [], f"a partial packet leaked after ONE arm: {ch.out}"
    ch.fire(1, 0x2345)
    assert ch.out == svpwm_stream([0x1234], [0x2345]), ch.out


def test_stop_reason_signature_of_a_healthy_rendezvous():
    """INV-67, pinned for THIS block: a face-locking chip reports
    ``stop_reason == "Deadlock"`` for an arbiter-HELD word MID-GROUP while
    perfectly healthy — the held word's handshake stays open, which is the
    same signal a genuine wedge shows. Only a Deadlock AFTER the group
    boundary is real (that is exactly how the whole-burst depth wall below
    presents). Pin both halves so nobody later 'fixes' the harness by
    treating any mid-group Deadlock as fatal:

      * the run holding an out-of-order beta word MAY report Deadlock — and
        the stream stays EMPTY (no partial packet);
      * the run that COMPLETES the pair reports QueueEmpty, and the packet
        is exact.
    """
    ch = _build_chain()
    res_b = ch.fire(1, 0x2222)                 # beta FIRST: held by the LOCK
    assert ch.out == [], f"a partial packet leaked from a held word: {ch.out}"
    stop_b = res_b.get("stop_reason") if isinstance(res_b, dict) else None
    res_a = ch.fire(0, 0x1111)                 # alpha completes the group
    stop_a = res_a.get("stop_reason") if isinstance(res_a, dict) else None
    assert ch.out == svpwm_stream([0x1111], [0x2222]), (ch.out, stop_b)
    # The post-group run must be clean — a Deadlock HERE is a real wedge.
    assert stop_a == "QueueEmpty", (
        f"post-group run reported {stop_a!r} — for a face-locking block only "
        f"a post-group Deadlock is a genuine wedge (INV-67), and this is one")
    # The mid-group hold legitimately reports Deadlock (INV-67's measured
    # healthy signature). Record rather than require — some layouts settle
    # the held word's transit differently — but it must be one of the two.
    assert stop_b in ("Deadlock", "QueueEmpty"), stop_b


def test_starved_arm_stalls_and_recovers():
    """A starved beta arm STALLS the modulator (no stale or duplicated packet)
    and RECOVERS exactly when the missing word arrives."""
    ch = _build_chain()
    ch.sample(0x1000, 0x2000)
    first = svpwm_stream([0x1000], [0x2000])
    assert ch.out == first
    ch.fire(0, 0x3000)              # alpha runs ahead
    assert ch.out == first, f"emitted without the beta word: {ch.out}"
    ch.fire(1, 0x4000)              # beta catches up
    assert ch.out == svpwm_stream([0x1000, 0x3000], [0x2000, 0x4000]), ch.out


# --------------------------------------------------------------------------- #
#  INV-19 — SATURATED drive == per-sample drive                                #
# --------------------------------------------------------------------------- #

def _enc_write(hop: int, addr: int) -> int:
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def _pair_words(ch, a, b):
    stream = []
    for arm, val in ((0, a), (1, b)):
        land = ch.landings[f"i{arm}"]
        hop = int(land["hop"]) & 0x1F
        stream += [_enc_write(hop, int(land["data_addrs"][0])),
                   int(val) & 0xFFFF,
                   _enc_jump(hop, int(land["entry"]))]
    return stream


def _chunked_run(pairs, cap: int = 500_000):
    """PAIR-SATURATED drive: each sample's TWO ARM WORDS are enqueued
    back-to-back as raw WRITE/DATA/JUMP words (no quiescence WITHIN a sample —
    the two producers race at the rendezvous, the hazard the LOCK exists to
    survive), one bounded run per sample, drain, next. This is the pacing the
    block supports; see the depth guard below."""
    ch = _build_chain()
    completed_all, stops = True, []
    for a, b in pairs:
        ch.chip.queue_words_physical("x16_in", _pair_words(ch, a, b))
        res = ch.chip.run(max_events=cap)
        if isinstance(res, dict):
            stops.append(res.get("stop_reason"))
            if not res.get("completed", True):
                completed_all = False
        ch._drain()
    return completed_all, stops, ch.out


def test_saturated_equals_per_sample():
    """INV-19 at the depth the block supports: pair-saturated == per-sample
    over a long mixed run (linear + saturating + asymmetric samples)."""
    pairs = [(0x1111, 0x2222), (0x0333, 0x7000), (0x8000, 0x7FFF),
             (0, 0), (0x4000, 0xC000), (0x7FFF, 0x8000), (0x2000, 0x6000),
             (0xF00D, 0x0BAD)]
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])

    per = _build_chain()
    for a, b in pairs:
        per.sample(a, b)
    assert per.out == exp, ("per-sample drive already wrong", per.out, exp)

    completed, stops, out = _chunked_run(pairs)
    assert completed, (
        f"the pair-saturated drive wedged (stop_reasons={stops}); "
        f"partial output={out}")
    assert out == exp, (
        f"saturated != per-sample.\n saturated={out}\n per-sample={exp}")


def test_saturated_drive_is_not_vacuous():
    """INV-4 applied to the harness: the raw-word drive really races the two
    producers, and the stimulus would SHOW a mis-pairing — every pair is
    asymmetric (duties(a,b) != duties(b,a)) and every cross-sample pairing
    differs from every correct packet."""
    pairs = [(0x1000, 0x5000), (0x2000, 0x6000), (0x3000, 0x7000)]
    correct = {svpwm_duties(a, b) for a, b in pairs}
    for a, b in pairs:
        assert svpwm_duties(a, b) != svpwm_duties(b, a), (a, b)
    for i, (a, _) in enumerate(pairs):
        for j, (_, b) in enumerate(pairs):
            if i != j:
                assert svpwm_duties(a, b) not in correct, (
                    "a cross-sample mix-up would produce a correct-looking "
                    "packet — pick different stimulus")
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    completed, stops, out = _chunked_run(pairs)
    assert completed and out == exp, (stops, out, exp)
    assert len(out) == 3 * len(pairs), (
        f"dropped or duplicated packets: {len(out)} words for {len(pairs)} "
        f"pairs")


def test_known_limit_saturated_burst_depth_is_one():
    """EXPLICIT GUARD for a real, MEASURED substrate limit (AGENTS.md §6).

    THE LIMIT: the block sustains ONE PAIR IN FLIGHT. A pair's two arm words
    back-to-back is fine and unbounded in NUMBER of pairs (the gates above).
    TWO OR MORE complete pairs queued into the port FIFO before running wedge
    after the first packet.

    WHY: the serialize-LOCK release rides `scale` — the ONE cell abutting the
    rendezvous (the TMRVoter geometry; a release from deeper in the chain
    needs a corridor the emit cell cannot carry, because the build's full-cell
    port patch must cover all three egress bursts and an inline WRITE.CFG
    would break it, INV-63). So the next pair is admitted once `scale` has
    dispatched, while the previous sample still occupies the clarke..emit
    conveyor; a second whole pair then collides with the release corridor on
    the same row (INV-56's two-waves shape) and the chip wedges.

    MEASURED (INV-56: read stop_reason for EVERY case): depth 2 and 3 report
    `Deadlock`; depth 5 reports `EventLimit` (the port FIFO variant of the
    same wedge) — one case's stop_reason is a sample, not a diagnosis. All
    emit EXACTLY ONE packet first.

    NOT A PROBLEM FOR THE INTENDED USE: an FOC loop is host-paced one sample
    set per control iteration. This guard exists so that if the boundary ever
    MOVES, a test says so instead of a chain silently deadlocking."""
    pairs = [(0x1111, 0x2222), (0x0333, 0x7000)]
    ch = _build_chain()
    stream = []
    for a, b in pairs:
        stream += _pair_words(ch, a, b)
    ch.chip.queue_words_physical("x16_in", stream)
    res = ch.chip.run(max_events=3_000_000)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    stop = res.get("stop_reason") if isinstance(res, dict) else None
    ch._drain()
    assert not completed, (
        f"the depth-2 saturated boundary MOVED (the burst now settles, "
        f"stop={stop}, out={ch.out}). If the substrate or engine changed this "
        f"is GOOD NEWS — re-measure, delete this guard, and make "
        f"test_saturated_equals_per_sample drive the whole burst at once.")
    assert ch.out == svpwm_stream([pairs[0][0]], [pairs[0][1]]), (
        f"expected exactly ONE packet before the wall (stop={stop}); "
        f"got {ch.out}")


# --------------------------------------------------------------------------- #
#  INV-23 — ORIENTATION INVARIANCE, all 8 D4 orientations                      #
# --------------------------------------------------------------------------- #
#
# The universal gate (test_orientation_invariance.py) injects one stream
# through one port landing; it cannot drive a TWO-FACE rendezvous — the same
# reason DualFloatToComplex / FeaturePairJoin / TMRVoter carry their own D4
# gates. So this block carries its own, on the REAL two-arm chain.

_D4 = [
    [], ["cw"], ["cw", "cw"], ["cw", "cw", "cw"],
    ["mirror_v"], ["mirror_v", "cw"], ["mirror_v", "cw", "cw"],
    ["mirror_v", "cw", "cw", "cw"],
]


def _d4_label(orient):
    return "identity" if not orient else "+".join(orient)


@pytest.mark.parametrize("orient", _D4, ids=[_d4_label(o) for o in _D4])
def test_orientation_invariant(orient):
    """INV-23: identical duties in all 8 D4 orientations. For THIS block that
    exercises the D4 transform of FIVE face words together: the two arm
    constants, `face_fwd` (the arm-barring stop), and the `unlock_face` /
    `face_tap` pair steering the serialize-LOCK release. If any failed to
    map, the LOCK would gate the wrong faces after rotation and the chain
    would build, route, and emit NOTHING."""
    ch = _build_chain(orient=orient)
    pairs = [(0x1111, 0x2222), (0x8000, 0x7FFF), (0x2000, 0x6000)]
    for a, b in pairs:
        ch.sample(a, b)
    exp = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == exp, (
        f"orientation {_d4_label(orient)} changed the duties (or produced "
        f"nothing): got {ch.out}, expected {exp}")


# --------------------------------------------------------------------------- #
#  MANDATORY mutation tests (INV-4, STRONG form — the REAL block corrupted,    #
#  REBUILT on the REAL chip, and each corruption caught)                       #
# --------------------------------------------------------------------------- #

def _tmpl_sub(cell, t_from, t_to):
    """A build_cell_programs replacement rewriting one cell's template."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    base = SVPWMBlock.build_cell_programs

    def _mut(self):
        cps = base(self)
        cp = cps[cell]
        new = cp.assembly_template.replace(t_from, t_to)
        assert new != cp.assembly_template, (
            f"the mutation did not apply — {t_from!r} is no longer in the "
            f"'{cell}' template, so this gate has gone vacuous")
        cp.assembly_template = new
        return cps
    return _mut


def _data_sub(cell, name, value):
    """A build_cell_programs replacement rewriting one DataWord's VALUE.
    DataWord is a frozen dataclass — replace the object, never mutate it."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    base = SVPWMBlock.build_cell_programs

    def _mut(self):
        cps = base(self)
        cp = cps[cell]
        assert any(d.name == name for d in cp.data), (cell, name)
        cp.data = [dataclasses.replace(d, value=value) if d.name == name
                   else d for d in cp.data]
        return cps
    return _mut


# The mutant stimulus mixes linear-range asymmetric pairs (arm/sector/sign
# corruption visible) with hard-saturating pairs (clamp corruption visible).
_MUT_PAIRS = [(0x1111, 0x2222), (0x7FFF, 0x0333), (0x8000, 0x7FFF),
              (0x4000, 0xC000), (0x2000, 0x2000)]

# (name, mutation, why it must be caught). Measured on chip: every one FIRES.
_SUBSTRATE_MUTANTS = [
    # The spec's "wrong sector at a boundary" corruption, in the form that CAN
    # fire: the max-selection compare INVERTED (BR.GE -> BR.LT), so the wrong
    # phase is chosen as the sector max everywhere INCLUDING the boundaries.
    # (A bare < -> <= tie-flip is proven VALUE-INVARIANT above — equal values
    # tie — so a gate built on it would certify nothing.)
    ("inverted_sector_compare",
     _tmpl_sub("maxsel", "    BR.GE mx_b\n", "    BR.LT mx_b\n"),
     "selects the wrong sector max"),
    # Midpoint injection dropped -> plain sine PWM: `half` zeroed in minsel
    # makes m = 0, exactly the duties == raw-phases degeneration the
    # linear-range centering gate exists to catch.
    ("injection_dropped_sine_pwm", _data_sub("minsel", "half", 0),
     "becomes plain sine PWM"),
    # One phase sign flipped: vb's ADD becomes SUB, so vb == vc.
    ("phase_b_sign_flipped",
     _tmpl_sub("clarke", "    ADD R{in:nh}, R{in:t}\n",
               "    SUB R{in:nh}, R{in:t}\n"),
     "phase b collapses onto phase c"),
    # Non-saturating add: the clamp branch made unconditional-skip, so vb
    # WRAPS on overmodulation instead of clamping.
    ("non_saturating_add",
     _tmpl_sub("clarke", "    BR.NV vb_ok\n", "    GOTO vb_ok\n"),
     "wraps instead of saturating"),
    # The serialize-LOCK release neutered: `relock` no longer re-points the
    # lock, so the rendezvous stays barred on face_fwd forever — one packet,
    # then the chain is dead. (Deleting the jump itself is also caught, but
    # as a BUILD failure — the declared release edge cannot resolve; this
    # form keeps the corrupted block buildable so the wedge is OBSERVED.)
    ("dropped_release",
     _tmpl_sub("rendezvous",
               "relock:\n"
               "    MOVE [LOCK_FACE], R{data:face_alpha}\n",
               "relock:\n"),
     "one packet then wedged"),
    # Wrong sqrt(3)/2 constant (0.5 instead): the Clarke geometry is wrong in
    # every sector.
    ("wrong_sqrt3_constant", _data_sub("scale", "k", HALF_Q15),
     "wrong Clarke geometry"),
]


@pytest.mark.parametrize("name,mutation,_why", _SUBSTRATE_MUTANTS,
                         ids=[m[0] for m in _SUBSTRATE_MUTANTS])
def test_substrate_mutations_are_all_caught(name, mutation, _why):
    """Corrupt the REAL block, rebuild on the REAL chip, run the REAL
    simulator, and assert the output does NOT match the golden.

    Every mutant here is GEOMETRY-PRESERVING (same cells, ports, faces —
    template arithmetic edits and data-word value changes), so it MUST place,
    route and build: a None chain is a VACUOUS gate, not a rejection — the
    INV-67 corollary, measured on the Clarke sibling, where a frozen-DataWord
    assignment killed every build and the gate read 'unroutable = rejected =
    pass' without ever running the mutant. (This suite's _data_sub uses
    dataclasses.replace for exactly that reason.) Uses the RAW builder: the
    value-probing builder rejects a corrupted block at its probe, which would
    collapse this gate into a silent skip (INV-46 Rule 4a)."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    good = svpwm_stream([p[0] for p in _MUT_PAIRS],
                        [p[1] for p in _MUT_PAIRS])
    orig = SVPWMBlock.build_cell_programs
    SVPWMBlock.build_cell_programs = mutation
    try:
        ch = _build_chain_raw()
        assert ch is not None, (
            f"the GEOMETRY-PRESERVING '{name}' mutant failed to place/route/"
            f"build at every anchor — the gate never ran the mutant and is "
            f"vacuous (INV-67 corollary). If the mutation genuinely changes "
            f"geometry, reclassify it; otherwise the mutation helper broke "
            f"the build.")
        for a, b in _MUT_PAIRS:
            ch.sample(a, b)
        got = ch.out
    finally:
        SVPWMBlock.build_cell_programs = orig
    assert got != good, (
        f"the '{name}' mutation ({_why}) produced the CORRECT stream {got} — "
        f"this gate cannot see it, so it certifies nothing")


def test_mutation_injection_dropped_fails_the_centering_gate_specifically():
    """The spec's named requirement: with the injection dropped the block IS
    plain sine PWM and the LINEAR-RANGE (centering) gate must fail — not just
    'some word differs'. Model the mutant exactly (m = 0) and assert the
    centering property is violated on the same stimulus the on-chip centering
    gate uses; then assert the real chip words SATISFY it (they do — that is
    test_centering_invariant_on_chip; here we pin the contrast)."""
    pairs = _rot_pairs(24, 0.9)
    violations = 0
    for a, b in pairs:
        pa, pb, pc = svpwm_phases(a, b)         # sine PWM: duties == phases
        mx, mn = max(pa, pb, pc), min(pa, pb, pc)
        if not (0 <= mx + mn <= 2):
            violations += 1
    assert violations >= 12, (
        f"only {violations}/24 sweep samples expose sine PWM — the centering "
        f"gate would be near-toothless on this stimulus")


def test_mutation_empty_output_fails():
    """Green must not be reachable by emitting nothing."""
    assert [] != svpwm_stream([1], [1])


def test_mutation_swapped_arms_would_fail():
    """A build that delivered the beta stream to the alpha port (a swapped-arm
    layout — the ~4% CP-SAT hazard the probe screens for) must be visible to
    every value gate: assert the probe stimulus distinguishes the arms."""
    for a, b in _PROBE_PAIRS:
        assert svpwm_duties(a, b) != svpwm_duties(b, a), (
            f"probe pair {a, b} is arm-symmetric — the layout probe could "
            f"pass a swapped-arm chain")


def test_the_probing_harness_actually_routes_this_block():
    """ANTI-SKIP GUARD (INV-46 Rule 4a), load-bearing: a genuinely broken
    block fails the layout probe at EVERY anchor, and the probing builder then
    SKIPS — a suite full of skips reads 'passed' at a glance (measured on the
    XorJoin: 35 skips for a corrupted block). This gate FAILS instead: the
    real block must route, build, and probe clean on at least one anchor."""
    ch = _try_build_probed()
    assert ch is not None, (
        "the REAL SVPWMBlock chain did not route/build/probe on ANY anchor — "
        "every probing gate above would silently skip. Either the block is "
        "broken (most likely) or every CP-SAT draw failed (re-run once).")


# --------------------------------------------------------------------------- #
#  STRUCTURE — the load-bearing construction claims                            #
# --------------------------------------------------------------------------- #

def test_declares_distinct_input_faces_and_reconciliation_pairs():
    """The face-lock flag AND the (port, face-word) pairs the build's
    reconciliation pass needs. Without the pairs the pass falls back to the
    DualFloatToComplex names and becomes a SILENT NO-OP: the chain builds and
    routes perfectly while emitting ZERO output."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    assert SVPWMBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = SVPWMBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("v_alpha", "face_alpha"), ("v_beta", "face_beta")), spec
    cp = SVPWMBlock("s").build_cell_programs()["rendezvous"]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports, (pn, in_ports)
        assert wn in face_words, (wn, face_words)


def test_same_face_construction_raises():
    """Two streams on ONE face cannot be told apart by the arbiter — the
    constructor RAISES rather than silently building a block that mis-pairs
    forever (INV-0: never clamp a hardware limit silently)."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    with pytest.raises(ValueError, match="face_alpha and face_beta"):
        SVPWMBlock("s", face_alpha="west", face_beta="west")


def test_rendezvous_boots_pre_locked_with_no_arm_entry():
    """COLD START IS BAKED (initial_lock_face) and there is NO arm entry:
    arming via a JUMP is a race — a word arriving before the arm-JUMP is
    accepted on an unlocked face and mis-pairs."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    cp = SVPWMBlock("s").build_cell_programs()["rendezvous"]
    assert cp.initial_lock_face is not None
    # got_alpha/got_beta for the arms, relock for the serialize-LOCK release —
    # and no `arm` entry anywhere.
    assert [e.name for e in cp.entries] == ["got_alpha", "got_beta", "relock"]


def test_rotation_has_three_stops_and_release_reads_the_reconciled_face():
    """INV-46 Rule 3 at N=2, with the RECONCILED-face release this block
    pins. Three structural facts, each measured to matter:

    1. got_beta locks to the INTERNAL forward face (bars both arms) — the
       straight re-lock to face_alpha is the measured saturated deadlock.
    2. The release is a BACKWARD JUMP into the rendezvous's `relock` entry,
       which re-points LOCK_FACE from the rendezvous's OWN face_alpha word —
       the only copy the build's face-reconciliation pass patches to the
       ROUTED geometry. A TMRVoter-style WRITE.CFG carrying an AUTHORED face
       value mis-aimed the lock on ~70% of routed layouts of this chain
       (auto_pnr relocates freely): the next beta word barged in ahead of its
       alpha and the packet carried a STALE alpha (measured, decoded as
       duties(previous_alpha, beta)).
    3. The release lives in `scale` (the one cell abutting the rendezvous)
       and NOT in `emit` — the emit cell's three egress bursts need the
       build's full-cell patch, which an inline release would break (INV-63).
    """
    from gr_kyttar.placement.blocks import SVPWMBlock
    b = SVPWMBlock("s")
    cps = b.build_cell_programs()
    rz = cps["rendezvous"].assembly_template
    got_beta = rz.split("got_beta:")[1].split("relock:")[0]
    assert "face_fwd" in got_beta, (
        "got_beta must lock to the internal forward face, not face_alpha")
    relock = rz.split("relock:")[1]
    assert "face_alpha" in relock and "LOCK_FACE" in relock, (
        "relock must re-point LOCK_FACE from the RECONCILED face_alpha word")
    sc = cps["scale"].assembly_template
    assert "{jump:unlock}" in sc, "the serialize-LOCK release is MISSING"
    assert "WRITE.CFG" not in sc, (
        "the release must NOT be a WRITE.CFG carrying an authored face value "
        "— that value is not reconciled to the routed arm geometry (the "
        "measured stale-alpha failure)")
    back = [e for e in b.internal_jumps()
            if e[0] == "scale" and e[1] == "unlock"
            and e[2] == "rendezvous" and e[3] == "relock"]
    assert back, "no scale->rendezvous.relock release edge declared"
    # ...and no data edge may target the rendezvous (an internal_connections
    # edge aimed at a real input port makes portmap classify it as a feedback
    # RETURN and DROP that arm from the external inputs — the TMRVoter trap).
    assert not [e for e in b.internal_connections() if e[2] == "rendezvous"]


def test_both_arms_are_advertised_as_external_inputs():
    """The block must present BOTH arms to GRC import (see above — a backward
    edge aimed at a real input port silently drops that arm from the port
    map; a hand-wired chain survives it, GRC import does not)."""
    from engine.catalog import BlockCatalog
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    pm = BlockCatalog.from_gr_kyttar().port_map("SVPWMBlock", {}, library=LIB)
    ins = [p.name for p in pm.ports if p.direction == "in"]
    assert ins == ["v_alpha", "v_beta"], ins
    import yaml
    y = yaml.safe_load(
        (Path(__file__).resolve().parents[2]
         / "gr-kyttar" / "grc" / "kyttar_svpwm.block.yml").read_text())
    assert [i["label"] for i in y["inputs"]] == ["v_alpha", "v_beta"]
    assert [o["label"] for o in y["outputs"]] == ["out"]


def test_block_declares_exactly_one_output_register():
    """The 3-word packet is THREE SEQUENTIAL BURSTS on ONE stream. With more
    than one output register the build classifies the emit cell as a COMPLEX
    rail source and collapses the packet (FeaturePairJoin condition (a))."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    b = SVPWMBlock("s")
    assert len(b.interface.output_registers) == 1, b.interface.output_registers


def test_emit_cell_carries_no_handoff_and_no_writecfg():
    """FeaturePairJoin condition (b) + INV-63: the emit cell must source NO
    internal connection and hold NO WRITE.CFG, so `_output_cell_carries_
    handoffs` stays False and the build's FULL-CELL patch covers all THREE
    bursts. An inline WRITE.CFG there would leave two bursts unpatched — which
    is also why the serialize-LOCK release cannot ride `emit`."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    b = SVPWMBlock("s")
    assert "WRITE.CFG" not in b.build_cell_programs()["emit"].assembly_template
    assert not [e for e in b.internal_connections() if e[0] == "emit"], (
        "the emit cell sources an internal connection — the port patch will "
        "cover only its last WRITE/JUMP and the packet loses two words")
    assert b.output_cell_ids() == ["emit"]


def test_rendezvous_cell_is_a_leaf_of_the_fold():
    """The face budget (INV-46 Rule 2): 2 arms + 1 forward + 1 release = 4 =
    all faces of a cell, so the rendezvous must be a LEAF — exactly ONE
    in-block neighbour (`scale`, which carries both the forward and the
    release). The fold stays a chain, <= 8 across (INV-9)."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    layout = SVPWMBlock("s").default_layout()
    pos = {cid: (dx, dy) for cid, (dx, dy, _f) in layout.items()}
    rx, ry = pos["rendezvous"]
    occupied = set(pos.values())
    in_block = [n for n in
                [(rx + 1, ry), (rx - 1, ry), (rx, ry + 1), (rx, ry - 1)]
                if n in occupied]
    assert in_block == [pos["scale"]], (in_block, pos)
    w = max(x for x, _ in pos.values()) - min(x for x, _ in pos.values())
    h = max(y for _, y in pos.values()) - min(y for _, y in pos.values())
    assert w <= 8 and h <= 8, (w, h)
    assert not [c for c in layout if c.startswith("transit_")], (
        "no transit unlock lane — the release rides `scale` (see INV-46)")


def test_every_cell_fits_its_register_budget():
    """INV-33 static gate: no data address and no state/input register at or
    above ``31 - instr_count`` (a cell at exactly 32/32 pins state on top of
    its own first instruction: assembles, runs ONCE, then zeroes the entry
    word — emits one sample and goes quiescent). Every StateVar pinned."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    for cid, cp in SVPWMBlock("s").build_cell_programs().items():
        n = R.count_instructions(cp)
        base = 31 - n
        for d in cp.data:
            assert d.address is not None and d.address < base, (
                f"{cid}: data '{d.name}' @{d.address} vs base {base}")
        for sv in cp.state:
            assert sv.register is not None, (
                f"{cid}: state '{sv.name}' UNPINNED (INV-33)")
            assert sv.register < base, (
                f"{cid}: state '{sv.name}' @{sv.register} vs base {base}")
        for p in cp.inputs:
            if p.register is not None:
                assert p.register < base, (
                    f"{cid}: input '{p.name}' @{p.register} vs base {base}")


def test_every_declared_entry_is_jumped():
    """INV-39: an entry nothing jumps at is unreachable dead code. Every
    datapath entry must be the target of an internal_jumps edge; the
    rendezvous entries are targeted by the EXTERNAL producers (declared on
    the input Ports)."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    b = SVPWMBlock("s")
    cps = b.build_cell_programs()
    jumped: dict = {}
    for (_s, _j, dst_cell, dst_entry) in b.internal_jumps():
        jumped.setdefault(dst_cell, set()).add(dst_entry)
    jumped.setdefault("rendezvous", set()).update(
        {p.entry for p in cps["rendezvous"].inputs if p.entry})
    for cid, cp in cps.items():
        for e in cp.entries:
            assert e.name in jumped.get(cid, set()), (
                f"cell '{cid}' declares entry '{e.name}' that NOTHING jumps "
                f"at — dead code (INV-39)")


def test_layout_order_matches_program_order():
    """INV-33 positional pairing: build_cell_programs() dict order MUST equal
    default_layout() order — a mismatch assigns program A to cell B with no
    error and the block runs garbage."""
    from gr_kyttar.placement.blocks import SVPWMBlock
    b = SVPWMBlock("s")
    assert list(b.build_cell_programs().keys()) == list(b.default_layout().keys())


def test_built_rendezvous_cell_boots_locked_on_chip():
    """The cold-start LOCK is in the BITSTREAM, not merely declared: the
    rendezvous cell's boot CONFIG has the LOCK bit set before any word."""
    import simkyt
    ch = _build_chain()
    blk = ch.ctrl.project.block(ch.blk)
    c0 = blk.placement.cells[0]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(ch.bres.words(0))
    boot_cfg = chip.read_config(chip.cell_id_at(c0.x, c0.y))
    assert boot_cfg & 0x4000, (
        f"the rendezvous cell must BOOT already LOCKED — boot CONFIG "
        f"0x{boot_cfg:04X} has LOCK clear")


# --------------------------------------------------------------------------- #
#  Dashboard report                                                            #
# --------------------------------------------------------------------------- #

def test_emit_report():
    """Emit the dashboard report (INV-38: through the sanctioned writer only —
    the file appears only if this suite's own session earned it). The metric
    is EXACT: the on-chip words must equal the integer golden bit-for-bit;
    the quantization budget lives between the integer model and the FLOAT
    reference (the stated 4-LSB bound), not between chip and model."""
    pairs = _rot_pairs(12, 0.9) + [(0x8000, 0x7FFF), (0x2000, 0x6000)]
    ch = _build_chain()
    for a, b in pairs:
        ch.sample(a, b)
    ref = svpwm_stream([p[0] for p in pairs], [p[1] for p in pairs])
    assert ch.out == ref, (ch.out, ref)
    res = compare_against_grc(
        ch.out, [_s16(w) / 32768.0 for w in ref], metric=Metric.EXACT,
        delay=0)
    assert res.passed, res.summary()
    write_report("SVPWMBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 1, "mutation": True,
        "on_chip_two_arm_chain": True, "six_sector_sweep": 48,
        "sector_boundaries_exact": 6, "overmodulation": True,
        "saturated": True, "orientations": 8, "substrate_mutants": 6})
