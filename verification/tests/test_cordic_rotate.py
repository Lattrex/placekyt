# SPDX-License-Identifier: GPL-3.0-or-later
"""CordicRotateBlock — general vector rotation by a STREAMED angle, ON CHIP.

WHY THERE IS NO STOCK GNU RADIO COUNTERPART. GR rotates with a host-side
``blocks.rotator_cc`` whose phase is a HOST-CLOCKED increment, or with float
trig — there is no GR block that rotates one stream BY another stream's angle
sample-for-sample. On this array the rotation is the FOC Park / inverse Park
transform (and any polar/mixer rotation): three independent Q15 streams
(x, y, theta) rendezvous on three DISTINCT faces and one complex packet
(x', y') leaves per matched triple. The golden is therefore a BIT-EXACT
integer model of the exact cell chain (``cordic_rotate_word``, in the block's
own module), itself pinned against FLOAT ROTATION saturated to Q15 by a
measured tolerance bound (max 24.75 LSB / mean 6.24 over a 48k-case sweep —
the gate here re-measures its own sweep and holds max <= 25.0 LSB).

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * FULL-CIRCLE SWEEP — all 8 octants, the axes, the exact 45-degree
    boundaries, theta = 0 and theta = 0x8000 (the wrap seam: -pi == +pi),
    BIT-EXACT against the golden and inside the float bound.
  * BOTH SIGNS — sign=-1 (Park) equals its own golden and provably differs
    from sign=+1 (the Park/inverse-Park distinction).
  * SATURATION CORNERS — x = y = 0x7FFF rotated onto the rails clamps
    exactly like the float reference saturated to Q15 (rails proven HIT).
  * ADVERSARIAL ARRIVAL — all 6 relative arm orders + random interleavings
    produce the identical stream (the N=3 LOCK rotation can never mis-pair).
  * INV-19 — per-triple SATURATED drive (the three arm words enqueued
    back-to-back, racing at the rendezvous) equals per-sample; the
    whole-burst depth-2 wall is MEASURED and guarded (the TMRVoter
    face-budget limit, INV-46 rule 3).
  * INV-67 — the healthy mid-group arbiter hold reports "Deadlock"; the
    group-completing run and every drain report "QueueEmpty".
  * INV-23 — identical output in all 8 D4 orientations.
  * INV-4 — mutation gates that corrupt the REAL block and rebuild ON CHIP:
    one CORDIC iteration dropped, K compensation removed, sign flipped,
    quadrant/wrap arithmetic broken — each proven to FIRE.

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_cordic_rotate.py -q
"""
from __future__ import annotations

import dataclasses
import itertools
import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_PLACEKYT = _ROOT / "placekyt"
for p in (str(_PLACEKYT), str(_ROOT / "runtime" / "python"),
          str(Path(__file__).parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import compare_against_grc, write_report, Metric  # noqa: E402
from gr_kyttar.placement.blocks.cordic_rotate_block import (  # noqa: E402
    CordicRotateBlock, cordic_rotate_word, rotate_stream, _s16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"
ARM_PORTS = ("x", "y", "theta")

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

# The MEASURED accuracy bound (see the block module docstring): max |error|
# vs float rotation saturated to Q15 was 24.75 LSB over 48192 cases. The
# sweep below re-measures its own subset and must stay under this.
FLOAT_BOUND_LSB = 25.0


def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    return (BlockCatalog, load_chip_type, AppController,
            ChipPortEndpoint, BlockEndpoint)


def _float_ref(xi, yi, theta, sign=1):
    """Float rotation saturated to Q15 — the physics the golden must track."""
    ang = _s16(int(theta)) / 32768.0 * np.pi * (1 if int(sign) > 0 else -1)
    c, s = np.cos(ang), np.sin(ang)
    xf = _s16(int(xi)) * c - _s16(int(yi)) * s
    yf = _s16(int(xi)) * s + _s16(int(yi)) * c
    return (float(np.clip(xf, -32768.0, 32767.0)),
            float(np.clip(yf, -32768.0, 32767.0)))


# --------------------------------------------------------------------------- #
#  The REAL three-upstream chain: three INDEPENDENT identity relays.           #
# --------------------------------------------------------------------------- #
#
# Each arm is a StreamSplitterBlock (exact, memoryless identity relay), fed
# from the ONE chip input port by its OWN net, so each arm has its own input
# landing and the harness can produce ANY relative arrival order — the
# adversarial async interleaving the LOCK rendezvous must survive (the
# TMRVoter harness topology, at the CORDIC datapath's scale).
#
# GEOMETRY: the 20-cell block folds 5x5 with the rendezvous a LEAF at the
# bbox corner (its three free faces take the arms). A candidate layout must
# leave the arm corridors AND the egress routable on the 10x12 chip; auto
# routing is not guaranteed for every anchor, so candidates are tried in
# order and every one is SMOKED on a throwaway chip before a gate gets it
# (INV-46 rule 4: ~4% of routed+built layouts still mis-deliver an arm; a
# distinct-value triple probe catches a mis-delivered or swapped arm).

# identity-orientation anchors: ([splitter_x, splitter_y, splitter_t], block)
_ANCHORS = [
    ([(3, 6), (4, 4), (3, 11)], (4, 6)),
    ([(3, 5), (4, 3), (5, 10)], (4, 5)),
]

_DELTA = {"south": (0, 1), "east": (1, 0), "west": (-1, 0), "north": (0, -1)}
_NET_ORDERS = [(0, 1, 2), (2, 0, 1), (1, 2, 0), (2, 1, 0)]

# Discovered per-orientation configs (rendezvous target, arm distance, net
# creation order) — found by sweep, verified by the probe; the generic sweep
# below remains the fallback if CP-SAT/routing drift invalidates one.
_ORIENT_CONFIGS: dict[str, list] = {}


def _arm_dirs_d4(blk):
    """The D4-MAPPED authored arm directions, in ARM_PORTS order (x, y,
    theta). The identity block has face_x=west, face_y=north, face_t=south;
    `pre`'s merged unlock word (release direction == LOCK_FACE value, the
    TMR reclaim) is only correct when arm x really arrives on the D4 image
    of west — so the harness lands each arm on its MAPPED face, exactly the
    distinct-face reservation the real placer makes at identity. MEASURED:
    with free-face-order (unmapped) arm placement a rotated chain routes,
    builds, emits ONE correct packet per stale-coincidence and then wedges —
    the release re-points LOCK_FACE at the wrong arm's face. The map M is
    derived from the PLACED cells: M(1,0) = pre-rdv, M(0,1) =
    (it4-rdv) - (pre-rdv) (identity offsets (1,0) and (1,1))."""
    cells = blk.placement.cells
    rx, ry = cells[0].x, cells[0].y            # rdv
    ex, ey = cells[1].x - rx, cells[1].y - ry  # M(1,0) = pre - rdv
    sx, sy = cells[8].x - rx, cells[8].y - ry  # it4 = program index 8: M(1,1)
    ux, uy = sx - ex, sy - ey                  # M(0,1)

    def m(v):
        return (v[0] * ex + v[1] * ux, v[0] * ey + v[1] * uy)
    return (rx, ry), [m((-1, 0)), m((0, -1)), m((0, 1))]   # x, y, theta


_rdv_offset_cache: dict[tuple, tuple] = {}


def _rdv_offset(orient):
    """The rendezvous cell's offset inside the placed bbox for ``orient``
    (the anchor is the bbox top-left in every orientation)."""
    key = tuple(orient or ())
    if key in _rdv_offset_cache:
        return _rdv_offset_cache[key]
    BlockCatalog, load_chip_type, AppController, _CPE, _BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.new_project("probe_off", "kyttar_10x12")
    v = ctrl.place_block("CordicRotateBlock", 0, 2, 2, library=LIB,
                         params={"sign": 1})
    if orient:
        from commands import OrientBlockCommand
        for kind in orient:
            OrientBlockCommand(ctrl.project, v, kind).execute()
    blk = ctrl.project.block(v)
    xs = [c.x for c in blk.placement.cells]
    ys = [c.y for c in blk.placement.cells]
    rx, ry = blk.placement.cells[0].x, blk.placement.cells[0].y
    _rdv_offset_cache[key] = (rx - min(xs), ry - min(ys))
    return _rdv_offset_cache[key]


def _candidate_configs(orient):
    """(block_anchor, explicit_splitters_or_None, arm_dist, net_order)."""
    label = "identity" if not orient else "+".join(orient)
    ox, oy = _rdv_offset(orient) if orient else (0, 0)
    for (wx, wy), dist, order in _ORIENT_CONFIGS.get(label, ()):
        yield (wx - ox, wy - oy), None, dist, order
    if not orient:
        for arm_xy, v_xy in _ANCHORS:
            yield v_xy, arm_xy, None, (0, 1, 2)
        return
    for (wx, wy) in [(4, 6), (4, 5), (5, 6), (3, 6), (5, 5), (4, 7),
                     (4, 2), (5, 2), (4, 3), (5, 3), (3, 2), (4, 1),
                     (8, 6), (8, 5), (7, 6), (7, 5), (6, 6), (6, 5),
                     (4, 9), (5, 9), (4, 10), (5, 8)]:
        ax, ay = wx - ox, wy - oy
        if not (0 <= ax and ax + 4 <= 9 and 0 <= ay and ay + 4 <= 11):
            continue
        for dist in (1, 2):
            for order in _NET_ORDERS:
                yield (ax, ay), None, dist, order


class _Chain:
    """A built three-upstream rotate chain + a driver that fires ONE arm."""

    def __init__(self, bres, chip, landings, ctrl=None, blk=None):
        self.bres, self.chip, self.landings = bres, chip, landings
        self.ctrl, self.blk = ctrl, blk
        self.out: list[int] = []
        self.last_run: dict = {}

    def fire(self, arm: int, value: int):
        land = self.landings[f"i{arm}"]
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self.chip.run(max_events=6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        res = self.chip.run(max_events=600_000)
        self.last_run = res if isinstance(res, dict) else {}
        self._drain()

    def sample(self, x, y, t, order=(0, 1, 2)):
        vals = {0: x, 1: y, 2: t}
        for arm in order:
            self.fire(arm, vals[arm])

    def _drain(self):
        while self.chip.output_available("x16_out"):
            w = self.chip.read_port_i16("x16_out").view("uint16").tolist()
            self.out.extend(int(v) & 0xFFFF for v in w)
            self.chip.release_output_ack("x16_out")
            self.chip.run(max_events=8000)


# probe triples: distinct roles — a swapped or mis-delivered arm cannot pass
# (x=0.5,y=0,theta=0 -> identity; theta=+90deg separates x/y; a random-ish
# triple separates theta from data).
_PROBE = [(16384, 0, 0), (16384, 0, 0x4000), (1000, 2000, 0x1234)]


def _attempt(sign, orient, v_xy, arm_xy, dist, net_order):
    """One placement/routing/build/probe attempt -> _Chain or None."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("rot_chain", ctk)
    try:
        v = ctrl.place_block("CordicRotateBlock", 0, *v_xy, library=LIB,
                             params={"sign": sign})
    except Exception:  # noqa: BLE001
        return None
    if orient:
        from commands import OrientBlockCommand
        try:
            for kind in orient:
                OrientBlockCommand(ctrl.project, v, kind).execute()
        except Exception:  # noqa: BLE001
            return None
    if arm_xy is None:
        blk = ctrl.project.block(v)
        (rx, ry), mdirs = _arm_dirs_d4(blk)
        arm_xy = [(rx + dx * dist, ry + dy * dist) for (dx, dy) in mdirs]
        if any(not (0 <= x <= 9 and 0 <= y <= 11) for (x, y) in arm_xy):
            return None
    try:
        ks = [ctrl.place_block("StreamSplitterBlock", 0, *arm_xy[i],
                               library=LIB, params={}) for i in range(3)]
    except Exception:  # noqa: BLE001
        return None
    # net CREATION order = the router's routing order; the strict
    # shortest-path router has no rip-up, so which arm routes first decides
    # whether the last one still has a corridor (INV-32).
    for i in net_order:
        ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                    BE(block=ks[i], port="sample"),
                                    name=f"i{i}")
        ctrl.add_logical_connection(BE(block=ks[i], port="out"),
                                    BE(block=v, port=ARM_PORTS[i]),
                                    name=f"w{i}")
    ctrl.add_logical_connection(BE(block=v, port="yi"),
                                CPE(chip=0, port="x16_out"), name="o")
    try:
        if not ctrl.auto_route_all({ctk: ct}).ok:
            return None
    except Exception:  # noqa: BLE001
        return None
    bres = ctrl.build()
    if not bres.ok:
        return None
    il = bres.chips[0].input_landings
    if not all(f"i{i}" in il for i in range(3)):
        return None
    sig = {(int(il[f"i{i}"]["hop"]), int(il[f"i{i}"]["entry"]),
            int(il[f"i{i}"]["data_addrs"][0])) for i in range(3)}
    if len(sig) < 3:
        return None
    # SMOKE on a THROWAWAY chip (driving triples advances the lock rotation
    # and latches arm state — never probe the chip a gate will use).
    probe = simkyt.Chip.from_yaml(CHIP_YAML)
    probe.load_bitstream_physical(bres.words(0))
    probe.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
    pch = _Chain(bres, probe, il)
    for (a, b, c) in _PROBE:
        pch.sample(a, b, c)
    if pch.out != rotate_stream([t[0] for t in _PROBE],
                                [t[1] for t in _PROBE],
                                [t[2] for t in _PROBE], sign):
        return None
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
    return _Chain(bres, chip, il, ctrl, v)


def _build_chain(sign=1, orient=None):
    for v_xy, arm_xy, dist, order in _candidate_configs(orient):
        ch = _attempt(sign, orient, v_xy, arm_xy, dist, order)
        if ch is not None:
            return ch
    pytest.skip("no anchor routed the three-arm rotate chain on this run")


# --------------------------------------------------------------------------- #
#  THE GOLDEN — float-rotation tolerance bound + the conventions              #
# --------------------------------------------------------------------------- #

def _sweep_cases():
    """Edge + boundary + random (x, y, theta) cases for the model-vs-float
    bound: full circle at several magnitudes incl. the Q15 corners, all 8
    octant interiors, the axes, the exact 45-degree boundaries, theta = 0
    and the 0x8000 wrap, plus uniform randoms over 3 seeds."""
    thetas = list(range(0, 65536, 1024))
    thetas += [0x1FFF, 0x2000, 0x2001, 0x3FFF, 0x4000, 0x4001, 0x5FFF,
               0x6000, 0x7FFF, 0x8000, 0x8001, 0x9FFF, 0xA000, 0xBFFF,
               0xC000, 0xDFFF, 0xE000, 0xFFFF, 1]
    vecs = [(0x7FFF, 0x7FFF), (0x8000, 0x8000), (0x8000, 0x7FFF),
            (0x7FFF, 0x8000), (0x7FFF, 0), (0, 0x7FFF), (0x8000, 0),
            (0, 0x8000), (0, 0), (1, 0), (0, 1), (23170, 23170),
            (0x4000, 0xC000)]
    cases = [(vx, vy, th) for th in thetas for (vx, vy) in vecs]
    for seed in (7, 41, 907):
        rng = np.random.default_rng(seed)
        for _ in range(1500):
            cases.append((int(rng.integers(0, 65536)),
                          int(rng.integers(0, 65536)),
                          int(rng.integers(0, 65536))))
    return cases


def test_golden_tracks_float_rotation_within_the_measured_bound():
    """THE ACCURACY BOUND. The bit-exact golden must track float rotation
    (saturated to Q15) within the measured bound on BOTH rails, both signs,
    over the full sweep. The bound is MEASURED (24.75 LSB max / 6.2 mean
    over 48k cases at authoring), not tuned: a dropped iteration moves the
    max to ~hundreds of LSB and a missing K compensation to ~thousands."""
    maxe, n = 0.0, 0
    for sign in (1, -1):
        for (vx, vy, th) in _sweep_cases():
            gx, gy = cordic_rotate_word(vx, vy, th, sign)
            fx, fy = _float_ref(vx, vy, th, sign)
            maxe = max(maxe, abs(_s16(gx) - fx), abs(_s16(gy) - fy))
            n += 1
    assert n > 10000
    assert maxe <= FLOAT_BOUND_LSB, (
        f"golden drifted from float rotation: max |err| {maxe:.2f} LSB over "
        f"{n} cases exceeds the measured bound {FLOAT_BOUND_LSB}")
    # Non-vacuity: the bound is tight enough to see a real corruption — one
    # dropped iteration (modelled as skipping i=7's x/y update) must exceed it.
    from gr_kyttar.placement.blocks.cordic_blocks import NITER, ATAN_Q15

    def _dropped(xi, yi, theta, sign=1):
        from gr_kyttar.placement.blocks.cordic_rotate_block import _mulq
        x = _mulq(int(xi), 1 << 13)
        y = _mulq(int(yi), 1 << 13)
        z = int(theta) & 0xFFFF
        if sign < 0:
            z = (~z + 1) & 0xFFFF
        q = z >> 14
        if q == 1:
            z = (z - 0x4000) & 0xFFFF
            x, y = (~y + 1) & 0xFFFF, x
        elif q == 2:
            z = (z + 0x4000) & 0xFFFF
            x, y = y, (~x + 1) & 0xFFFF
        for i in range(NITER):
            if i == 7:
                continue
            sgn = z >> 15
            msk = (0 - sgn) & 0xFFFF
            if i < NITER - 1:
                z = (z - (((ATAN_Q15[i] ^ msk) + sgn) & 0xFFFF)) & 0xFFFF
            ax = (x if i == 0 else (_s16(x) >> i)) & 0xFFFF
            ay = (y if i == 0 else (_s16(y) >> i)) & 0xFFFF
            x, y = ((x - (((ay ^ msk) + sgn) & 0xFFFF)) & 0xFFFF,
                    (y + (((ax ^ msk) + sgn) & 0xFFFF)) & 0xFFFF)
        def _comp(v):
            from gr_kyttar.placement.blocks.cordic_blocks import KINV_Q15
            p = (_s16(KINV_Q15) * _s16(v)) >> 15
            acc = p
            for _ in range(2):
                a2 = acc + acc
                if a2 > 32767 or a2 < -32768:
                    return (0x7FFF + ((p >> 15) & 1)) & 0xFFFF
                acc = a2
            return acc & 0xFFFF
        return _comp(x), _comp(y)
    worst = 0.0
    for (vx, vy, th) in [(0x7FFF, 0, 0x2000), (0, 0x7FFF, 0x6000),
                         (23170, 23170, 0x1234)]:
        mx, my = _dropped(vx, vy, th)
        fx, fy = _float_ref(vx, vy, th)
        worst = max(worst, abs(_s16(mx) - fx), abs(_s16(my) - fy))
    assert worst > FLOAT_BOUND_LSB, (
        f"a dropped iteration stays inside the bound ({worst:.1f} LSB) — the "
        f"bound is too loose to certify the iteration count")


def test_golden_wrap_seam_and_sign_convention():
    """theta = 0x8000 IS -pi == +pi (16-bit wrap = mod 2*pi): rotating by it
    must equal rotating by it with the OPPOSITE sign, and must negate both
    components of an axis vector (within the bound). And the sign param is
    the Park/inverse-Park distinction: for any non-seam theta the two signs
    differ."""
    for (vx, vy) in [(0x7FFF, 0), (0, 0x7FFF), (12345, 54321)]:
        a = cordic_rotate_word(vx, vy, 0x8000, 1)
        b = cordic_rotate_word(vx, vy, 0x8000, -1)
        assert a == b, "the +-pi seam must be sign-invariant (wrap negate)"
        fx, fy = _float_ref(vx, vy, 0x8000, 1)
        assert abs(_s16(a[0]) - fx) <= FLOAT_BOUND_LSB
        assert abs(_s16(a[1]) - fy) <= FLOAT_BOUND_LSB
    # sign=-1 == rotation by -theta: equals sign=+1 driven with negated theta.
    for th in (0x1234, 0x4000, 0x7FFF, 0xC000):
        neg = (-th) & 0xFFFF
        assert cordic_rotate_word(1000, 2000, th, -1) == \
            cordic_rotate_word(1000, 2000, neg, 1)
        assert cordic_rotate_word(1000, 2000, th, -1) != \
            cordic_rotate_word(1000, 2000, th, 1), (
            "sign=-1 must differ from sign=+1 at a non-axis theta")


def test_golden_unity_gain():
    """K is compensated internally: the rotated magnitude equals the input
    magnitude within the bound (no residual 1.6468 gain, no 1/K droop)."""
    for (vx, vy) in [(16384, 0), (0, 20000), (12000, 9000)]:
        m_in = np.hypot(_s16(vx), _s16(vy))
        for th in range(0, 65536, 4096):
            gx, gy = cordic_rotate_word(vx, vy, th, 1)
            m_out = np.hypot(_s16(gx), _s16(gy))
            assert abs(m_out - m_in) <= 2 * FLOAT_BOUND_LSB, (
                f"unity gain violated at theta={th:#06x}: |in|={m_in:.0f} "
                f"|out|={m_out:.0f}")


# --------------------------------------------------------------------------- #
#  ON-CHIP VALUE GATES                                                        #
# --------------------------------------------------------------------------- #

def _full_circle_triples():
    """The spec's full-circle sweep: all 8 octant interiors, the four axes,
    the four exact 45-degree boundaries, theta=0 and the 0x8000 wrap, at a
    mid-scale vector plus per-case corner vectors."""
    thetas = [0x0000, 0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000,
              0x7000, 0x7FFF, 0x8000, 0x8001, 0x9000, 0xA000, 0xB000,
              0xC000, 0xD000, 0xE000, 0xF000, 0xFFFF]
    return [(23170, 11585, th) for th in thetas]


def test_full_circle_sweep_is_bit_exact_on_chip():
    """The placed + routed + built chain equals the golden WORD FOR WORD over
    the full-circle sweep, and the emitted words stay inside the float
    bound. This is the accuracy gate the dropped-iteration mutant fails."""
    ch = _build_chain()
    tr = _full_circle_triples()
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == exp, (ch.out, exp)
    for k, (x, y, t) in enumerate(tr):
        fx, fy = _float_ref(x, y, t, 1)
        assert abs(_s16(ch.out[2 * k]) - fx) <= FLOAT_BOUND_LSB
        assert abs(_s16(ch.out[2 * k + 1]) - fy) <= FLOAT_BOUND_LSB


@pytest.mark.parametrize("seed", [5, 41, 907])
def test_random_triples_are_bit_exact_on_chip(seed):
    """Random full-range (x, y, theta) triples (>=3 seeds, the coverage
    bar): the chip equals the golden word for word."""
    rng = random.Random(seed)
    ch = _build_chain()
    tr = [(rng.randrange(0, 65536), rng.randrange(0, 65536),
           rng.randrange(0, 65536)) for _ in range(10)]
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == exp, (ch.out, exp)


def test_sign_negative_park_on_chip():
    """sign=-1 (the Park transform) built on chip equals ITS OWN golden and
    provably differs from the +1 stream on the same stimulus."""
    ch = _build_chain(sign=-1)
    tr = [(23170, 11585, 0x1000), (16384, 0, 0x4000), (1000, 2000, 0x9000),
          (30000, 40000, 0xC000)]
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], -1)
    pos = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert exp != pos, "stimulus cannot distinguish the signs — pick asym thetas"
    assert ch.out == exp, (ch.out, exp)


def test_saturation_corners_clamp_on_chip():
    """x = y = 0x7FFF (|v| = 1.414) rotated onto an axis exceeds Q15: the
    block must CLAMP to the rails exactly as the golden (and the float
    reference saturated to Q15) does. Non-vacuity: the rails are proven HIT."""
    ch = _build_chain()
    tr = [(0x7FFF, 0x7FFF, 0xF000), (0x7FFF, 0x7FFF, 0x2000),
          (0x8000, 0x8000, 0x2000), (0x7FFF, 0x8000, 0x0000),
          (0x8000, 0x7FFF, 0x8000), (0x7FFF, 0x7FFF, 0x6000)]
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == exp, (ch.out, exp)
    assert 0x7FFF in ch.out and 0x8000 in ch.out, (
        f"the saturation corners never hit the rails — the gate is vacuous: "
        f"{ch.out}")


@pytest.mark.parametrize("order", list(itertools.permutations((0, 1, 2))))
def test_every_relative_arrival_order_rotates_identically(order):
    """All 6 relative arm arrival orders produce the IDENTICAL stream — the
    LOCK rotation holds each arm's word until its turn, so the pairing never
    depends on which producer fired first."""
    ch = _build_chain()
    tr = [(16384, 0, 0x2000), (1000, 2000, 0x1234), (30000, 50000, 0xC000)]
    for (x, y, t) in tr:
        ch.sample(x, y, t, order=order)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == exp, (f"arrival order {order} broke the pairing",
                           ch.out, exp)


@pytest.mark.parametrize("seed", [3, 17, 91])
def test_random_interleavings_preserve_the_triples(seed):
    """Random per-sample arrival order over a longer run (3 seeds): whatever
    order the three arms fire in, the stream equals the golden."""
    rng = random.Random(seed)
    ch = _build_chain()
    xs, ys, ts = [], [], []
    for _ in range(8):
        xs.append(rng.randrange(0, 65536))
        ys.append(rng.randrange(0, 65536))
        ts.append(rng.randrange(0, 65536))
        order = [0, 1, 2]
        rng.shuffle(order)
        ch.sample(xs[-1], ys[-1], ts[-1], order=tuple(order))
    assert ch.out == rotate_stream(xs, ys, ts, 1), ch.out


def test_startup_and_starved_arm():
    """NO PARTIAL PACKET, ever: nothing is emitted until all three arms have
    spoken, a starved arm stalls the rendezvous, and it recovers exactly
    when the missing word arrives."""
    ch = _build_chain()
    ch.fire(0, 16384)
    assert ch.out == [], f"partial packet after ONE arm: {ch.out}"
    ch.fire(1, 0)
    assert ch.out == [], f"partial packet after TWO arms: {ch.out}"
    ch.fire(2, 0x4000)
    assert ch.out == rotate_stream([16384], [0], [0x4000], 1)
    n0 = len(ch.out)
    ch.fire(0, 1000)          # x runs ahead
    ch.fire(1, 2000)
    assert len(ch.out) == n0, f"emitted without theta: {ch.out}"
    ch.fire(2, 0x1234)        # theta catches up
    assert ch.out[n0:] == rotate_stream([1000], [2000], [0x1234], 1)


def test_stop_reason_signature_of_a_healthy_rendezvous():
    """INV-67 + INV-56: an arbiter-HELD word reports "Deadlock" MID-GROUP on
    a perfectly healthy rendezvous (pin it, so nobody later 'fixes' the
    harness by treating any Deadlock as fatal); the run that COMPLETES the
    group, and every drain after it, must report "QueueEmpty" — a Deadlock
    THERE is real (the INV-46 rule-3 re-lock bug presents exactly so)."""
    ch = _build_chain()
    # out-of-lock-order drive: theta first (the barred face) -> held.
    ch.fire(2, 0x4000)
    held = ch.last_run.get("stop_reason")
    ch.fire(0, 16384)
    ch.fire(1, 0)             # completes the group
    done = ch.last_run.get("stop_reason")
    assert ch.out == rotate_stream([16384], [0], [0x4000], 1), ch.out
    assert done == "QueueEmpty", (
        f"the group-completing run must settle clean; got {done!r}")
    assert held == "Deadlock", (
        f"the mid-group hold reported {held!r} — INV-67 documents 'Deadlock' "
        f"as the healthy hold signature; if the simulator's report changed, "
        f"update INV-67, do not delete this pin")
    # a second, in-lock-order group settles clean at every step.
    for arm, val in ((0, 1000), (1, 2000), (2, 0x1234)):
        ch.fire(arm, val)
        if arm == 2:
            assert ch.last_run.get("stop_reason") == "QueueEmpty"


# --------------------------------------------------------------------------- #
#  INV-19 — SATURATED drive == per-sample (at the depth the block supports)   #
# --------------------------------------------------------------------------- #

def _enc_write(hop: int, addr: int) -> int:
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def _queue_triple(ch, x, y, t):
    stream: list[int] = []
    for arm, val in ((0, x), (1, y), (2, t)):
        land = ch.landings[f"i{arm}"]
        hop = int(land["hop"]) & 0x1F
        stream += [_enc_write(hop, int(land["data_addrs"][0])),
                   int(val) & 0xFFFF, _enc_jump(hop, int(land["entry"]))]
    ch.chip.queue_words_physical("x16_in", stream)


def test_saturated_equals_per_sample():
    """INV-19 at the depth the block supports: each triple's THREE ARM WORDS
    are enqueued back-to-back with no quiescence between them — the three
    producers race at the rendezvous, the hazard the LOCK rotation exists to
    survive — and the stream must equal the per-sample result. (The bounded
    run + completed check is the INV-19 harness-safety rule.)"""
    tr = [(1000, 2000, 0x1234), (30000, 40000, 0x9000), (500, 600, 0x4000),
          (0x7FFF, 0x7FFF, 0x2000), (7, 8, 0x8000), (100, 200, 0xC000)]
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    per = _build_chain()
    for (x, y, t) in tr:
        per.sample(x, y, t)
    assert per.out == exp, ("per-sample drive already wrong", per.out, exp)
    sat = _build_chain()
    for (x, y, t) in tr:
        _queue_triple(sat, x, y, t)
        res = sat.chip.run(max_events=800_000)
        assert not isinstance(res, dict) or res.get("completed", True), (
            f"a per-triple saturated burst wedged: {res}")
        sat._drain()
    assert sat.out == exp, (
        f"saturated != per-sample.\n saturated={sat.out}\n per-sample={exp}")
    # NON-VACUITY: every triple above has three DISTINCT values, so any
    # cross-arm mix-up under the racing drive changes the output.
    assert len(sat.out) == 2 * len(tr)


def test_known_limit_whole_burst_depth_is_one():
    """EXPLICIT GUARD for a real, MEASURED wall (AGENTS.md §6) — the same
    face-budget limit TMRVoterBlock documents (INV-46 rules 2/3): the N=3
    rendezvous release must ride the ONE abutting cell (`pre`), so the block
    sustains ONE TRIPLE IN FLIGHT. Any number of triples driven one at a
    time is fine (the gate above); TWO OR MORE whole triples queued into the
    port FIFO before running deadlock. Measured here: depth-2 whole-burst ->
    stop_reason "Deadlock" after ~1.4k events, zero packets. If this guard
    ever FAILS the wall has moved — re-measure, then let the saturated gate
    drive whole bursts."""
    ch = _build_chain()
    for (x, y, t) in [(1000, 2000, 0x1234), (500, 600, 0x4000)]:
        _queue_triple(ch, x, y, t)
    res = ch.chip.run(max_events=4_000_000)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    ch._drain()
    assert not completed, (
        f"the depth-2 whole-burst boundary MOVED (it now settles: {res}, "
        f"out={ch.out}). GOOD NEWS if real — re-measure the limit, delete "
        f"this guard, and drive the saturated gate with whole bursts.")


# --------------------------------------------------------------------------- #
#  INV-23 — ORIENTATION INVARIANCE, all 8 D4 orientations                     #
# --------------------------------------------------------------------------- #
#
# The universal gate (test_orientation_invariance.py) injects on ONE input
# port and cannot drive a THREE-FACE rendezvous, so — like TMRVoterBlock —
# this block carries its own D4 gate on the real three-arm chain.

_D4 = [
    [],
    ["cw"],
    ["cw", "cw"],
    ["cw", "cw", "cw"],
    ["mirror_v"],
    ["mirror_v", "cw"],
    ["mirror_v", "cw", "cw"],
    ["mirror_v", "cw", "cw", "cw"],
]


def _d4_label(orient):
    return "identity" if not orient else "+".join(orient)


@pytest.mark.parametrize("orient", _D4, ids=[_d4_label(o) for o in _D4])
def test_orientation_invariant(orient):
    """INV-23: identical output in all 8 D4 orientations. For this block
    that exercises the D4 transform of SIX face words (the three arm faces,
    face_fwd, and pre's unlock_face/face_tap pair), the rotated serialize-
    LOCK release corridor, and the whole 20-cell serpentine's forwarding
    faces. A failure of any of them builds + routes and emits nothing."""
    ch = _build_chain(orient=orient)
    tr = [(16384, 0, 0x4000), (1000, 2000, 0x1234), (30000, 50000, 0xC000)]
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    exp = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == exp, (
        f"orientation {_d4_label(orient)} changed the rotation (or produced "
        f"nothing): got {ch.out}, expected {exp}")


def test_the_probing_harness_actually_routes_this_block():
    """INV-46 rule 4a: the probing/skip pattern above hides a genuinely
    broken block as a wall of SKIPS. This gate FAILS (not skips) when the
    identity chain cannot be built and probed clean."""
    for v_xy, arm_xy, dist, order in _candidate_configs(None):
        if _attempt(1, None, v_xy, arm_xy, dist, order) is not None:
            return
    pytest.fail(
        "NO identity anchor produced a routed, built, probe-clean three-arm "
        "chain — either the block is broken or every layout failed; this "
        "must never be reported as a skip")


# --------------------------------------------------------------------------- #
#  MANDATORY mutation gates (INV-4) — corrupt the REAL block, rebuild ON CHIP #
# --------------------------------------------------------------------------- #
#
# All these mutants are GEOMETRY-PRESERVING (same cells, ports, faces), so
# they MUST place, route and build — a None chain is a HARD FAILURE of the
# gate, never a rejection (INV-67 corollary: the first cut of a sibling
# block's mutant assigned to a frozen DataWord, silently failed to BUILD,
# and a 'None = rejected' reading passed a gate that never ran the mutant).

def _mutate_programs(mutator):
    """Return a build_cell_programs override applying ``mutator(cells)``."""
    orig = CordicRotateBlock.build_cell_programs

    def patched(self):
        cells = orig(self)
        mutator(cells)
        return cells
    return patched


def _replace_data(cp, name, value):
    cp.data[:] = [dataclasses.replace(d, value=value) if d.name == name else d
                  for d in cp.data]


def _run_mutant(mutator, triples, sign=1):
    """Corrupt the REAL block class, rebuild the REAL chain on chip, drive
    ``triples``, restore, and return the emitted stream."""
    orig = CordicRotateBlock.build_cell_programs
    CordicRotateBlock.build_cell_programs = _mutate_programs(mutator)
    try:
        ch = None
        for v_xy, arm_xy, dist, order in _candidate_configs(None):
            ch = _attempt_mutant(sign, v_xy, arm_xy, dist, order)
            if ch is not None:
                break
        assert ch is not None, (
            "the mutant did not place+route+build — it is geometry-"
            "preserving, so the MUTATION ITSELF is broken (or a P&R flake "
            "across all anchors); the gate has not observed the mutant and "
            "certifies nothing")
        for (x, y, t) in triples:
            ch.sample(x, y, t)
        return ch.out
    finally:
        CordicRotateBlock.build_cell_programs = orig


def _attempt_mutant(sign, v_xy, arm_xy, dist, order):
    """_attempt WITHOUT the golden probe (a mutant must fail it!) — only the
    structural landing checks."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("rot_mut", ctk)
    try:
        v = ctrl.place_block("CordicRotateBlock", 0, *v_xy, library=LIB,
                             params={"sign": sign})
    except Exception:  # noqa: BLE001
        return None
    if arm_xy is None:
        blk = ctrl.project.block(v)
        (rx, ry), mdirs = _arm_dirs_d4(blk)
        arm_xy = [(rx + dx * dist, ry + dy * dist) for (dx, dy) in mdirs]
    try:
        ks = [ctrl.place_block("StreamSplitterBlock", 0, *arm_xy[i],
                               library=LIB, params={}) for i in range(3)]
    except Exception:  # noqa: BLE001
        return None
    for i in order:
        ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                    BE(block=ks[i], port="sample"),
                                    name=f"i{i}")
        ctrl.add_logical_connection(BE(block=ks[i], port="out"),
                                    BE(block=v, port=ARM_PORTS[i]),
                                    name=f"w{i}")
    ctrl.add_logical_connection(BE(block=v, port="yi"),
                                CPE(chip=0, port="x16_out"), name="o")
    try:
        if not ctrl.auto_route_all({ctk: ct}).ok:
            return None
    except Exception:  # noqa: BLE001
        return None
    bres = ctrl.build()
    if not bres.ok:
        return None
    il = bres.chips[0].input_landings
    if not all(f"i{i}" in il for i in range(3)):
        return None
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(il["i0"]["entry"]))
    return _Chain(bres, chip, il, ctrl, v)


_MUT_TRIPLES = [(23170, 11585, 0x1000), (0x7FFF, 0, 0x3000),
                (16384, 0, 0x8000), (1000, 2000, 0x9000)]


_PASSTHROUGH_IT = (
    "start:\n"
    "    MOVE R0, R{in:z}\n"
    "    {write:z}\n"
    "    MOVE R0, R{in:x}\n"
    "    {write:x}\n"
    "    MOVE R0, R{in:y}\n"
    "    {write:y}\n"
    "    {jump:trig}\n"
)


def _drop_iteration(cid):
    def mut(cells):
        cells[cid] = dataclasses.replace(
            cells[cid], assembly_template=_PASSTHROUGH_IT, state=[])
    return mut


def test_mutation_dropped_iteration_fails_the_accuracy_gate():
    """DROP THE FIRST CORDIC ITERATION (it0 becomes a pure x/y/z
    pass-through). Iterations 1..13 can only rotate Sigma atan(2^-i) ~ 32
    degrees, so any |z| beyond that after the quadrant reduction — e.g.
    theta = 0x3000, 67.5 degrees — is left ~35 degrees short: the stream
    must fail the bit-exact gate AND land far outside the float bound."""
    got = _run_mutant(_drop_iteration("it0"), _MUT_TRIPLES)
    good = rotate_stream([t[0] for t in _MUT_TRIPLES],
                         [t[1] for t in _MUT_TRIPLES],
                         [t[2] for t in _MUT_TRIPLES], 1)
    assert got != good, (
        "a dropped it0 produced the CORRECT stream — the accuracy gate "
        "cannot see the iteration count")
    errs = []
    for k, (x, y, t) in enumerate(_MUT_TRIPLES):
        if 2 * k + 1 < len(got):
            fx, fy = _float_ref(x, y, t, 1)
            errs.append(max(abs(_s16(got[2 * k]) - fx),
                            abs(_s16(got[2 * k + 1]) - fy)))
    assert errs and max(errs) > FLOAT_BOUND_LSB, (
        f"dropping it0 stayed inside the float bound: {errs}")


def test_mutation_dropped_mid_iteration_is_caught_by_the_exact_gate():
    """THE MEASURED REDUNDANCY FINDING, pinned. Dropping a MID-sequence
    iteration (it7) is nearly INVISIBLE to a tolerance gate: the CORDIC
    angle sequence is barely complete (Sigma_{i>n} atan(2^-i) ~ atan(2^-n)),
    so iterations 8..13 absorb almost all of the un-executed rotation —
    measured on chip: max ~13 LSB from float, INSIDE the 25-LSB bound. Only
    the BIT-EXACT compare sees it, which is exactly why the on-chip value
    gates hold word-for-word equality with the golden rather than a
    tolerance."""
    got = _run_mutant(_drop_iteration("it7"), _MUT_TRIPLES)
    good = rotate_stream([t[0] for t in _MUT_TRIPLES],
                         [t[1] for t in _MUT_TRIPLES],
                         [t[2] for t in _MUT_TRIPLES], 1)
    assert got != good, (
        "a dropped it7 produced the golden stream bit-for-bit — even the "
        "exact gate cannot see a mid-sequence iteration")
    # ...and demonstrate WHY the exact compare is load-bearing: the same
    # mutant stays INSIDE the float tolerance (the redundancy property).
    errs = []
    for k, (x, y, t) in enumerate(_MUT_TRIPLES):
        if 2 * k + 1 < len(got):
            fx, fy = _float_ref(x, y, t, 1)
            errs.append(max(abs(_s16(got[2 * k]) - fx),
                            abs(_s16(got[2 * k + 1]) - fy)))
    assert errs and max(errs) <= FLOAT_BOUND_LSB, (
        f"expected the it7 mutant INSIDE the float bound (the redundancy "
        f"property this gate documents); measured {errs} — if the property "
        f"moved, update this pin and the lessons entry")


def test_mutation_k_compensation_removed_fails_unity_gain():
    """REMOVE THE K COMPENSATION (kinv -> 0x7FFF ~ 1.0 in postx AND posty):
    every output scales by K = 1.6468, so mid-scale vectors overshoot and
    the stream diverges from the golden."""
    def mut(cells):
        _replace_data(cells["postx"], "kinv", 0x7FFF)
        _replace_data(cells["posty"], "kinv", 0x7FFF)
    got = _run_mutant(mut, _MUT_TRIPLES)
    good = rotate_stream([t[0] for t in _MUT_TRIPLES],
                         [t[1] for t in _MUT_TRIPLES],
                         [t[2] for t in _MUT_TRIPLES], 1)
    assert got != good, (
        "removing the 1/K compensation produced the CORRECT stream — the "
        "unity-gain claim is not being checked")


def test_mutation_sign_flipped_fails():
    """FLIP THE SIGN (inject the sign=-1 negation into a sign=+1 build):
    the Park/inverse-Park distinction. Every non-axis theta rotates the
    wrong way and the stream diverges."""
    def mut(cells):
        cp = cells["prep1"]
        cells["prep1"] = dataclasses.replace(
            cp, assembly_template=cp.assembly_template.replace(
                "start:\n",
                "start:\n"
                "    NOT R{in:z}\n"
                "    ADD R0, R{data:one}\n"
                "    MOVE R{in:z}, R0\n", 1))
    got = _run_mutant(mut, _MUT_TRIPLES)
    good = rotate_stream([t[0] for t in _MUT_TRIPLES],
                         [t[1] for t in _MUT_TRIPLES],
                         [t[2] for t in _MUT_TRIPLES], 1)
    neg = rotate_stream([t[0] for t in _MUT_TRIPLES],
                        [t[1] for t in _MUT_TRIPLES],
                        [t[2] for t in _MUT_TRIPLES], -1)
    assert neg != good, "stimulus cannot distinguish the signs"
    assert got != good, (
        "a flipped sign produced the CORRECT +1 stream — the gate cannot "
        "tell Park from inverse Park")


def test_mutation_broken_quadrant_wrap_fails_the_wrap_case():
    """BREAK THE WRAP/QUADRANT ARITHMETIC (prep1's half word 0x4000 -> 0):
    the +-90-degree pre-rotations still swap the components but never adjust
    z, so every second/third-quadrant theta — including the 0x8000 wrap case
    the spec names — rotates ~90 degrees wrong. First-quadrant thetas are
    UNAFFECTED (the mutant is stimulus-selective, which is why the sweep
    must cover the whole circle)."""
    def mut(cells):
        _replace_data(cells["prep1"], "half", 0)
    got = _run_mutant(mut, _MUT_TRIPLES)
    good = rotate_stream([t[0] for t in _MUT_TRIPLES],
                         [t[1] for t in _MUT_TRIPLES],
                         [t[2] for t in _MUT_TRIPLES], 1)
    assert got != good, (
        "breaking the z-wrap/quadrant adjustment produced the CORRECT "
        "stream — the wrap case (theta=0x8000) is not really covered")
    # the wrap-case triple specifically must be among the wrong ones
    k = next(i for i, t in enumerate(_MUT_TRIPLES) if t[2] == 0x8000)
    if 2 * k + 1 < len(got):
        assert got[2 * k:2 * k + 2] != good[2 * k:2 * k + 2], (
            "theta=0x8000 survived the broken wrap — the wrap gate is "
            "vacuous")


def test_mutation_empty_output_fails():
    """Green is not reachable by emitting nothing."""
    assert [] != rotate_stream([1], [2], [3], 1)


def test_mutation_single_rail_fails():
    """A rotate that emitted ONLY x' (dropping y') halves the packet; the
    reference must reject that."""
    good = rotate_stream([100, 200], [300, 400], [0x1000, 0x2000], 1)
    assert good[0::2] != good, "the x rail alone must not equal the packet"


# --------------------------------------------------------------------------- #
#  STRUCTURE — the load-bearing construction claims                           #
# --------------------------------------------------------------------------- #

def test_register_budget_and_pinned_state_both_signs():
    """INV-33 static gate: no data address, pinned state register, or input
    register at or above 31 - instr_count, for EVERY cell, BOTH signs; and
    every StateVar is explicitly pinned."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    for sign in (1, -1):
        for cid, cp in CordicRotateBlock("t", sign=sign) \
                .build_cell_programs().items():
            base = 31 - R.count_instructions(cp)
            for d in cp.data:
                assert d.address < base, (sign, cid, d.name, d.address, base)
            for sv in cp.state:
                assert sv.register is not None, (
                    f"{cid}: state '{sv.name}' is UNPINNED (INV-33)")
                assert sv.register < base, (sign, cid, sv.name)
            for p in cp.inputs:
                if p.register is not None:
                    assert p.register < base, (sign, cid, p.name)


def test_positional_pairing_and_no_head_on_faces():
    """INV-51: program-dict order == layout order (the ids hide a mismatch
    and whole cells load EMPTY). INV-56: no two cells rest facing each
    other (the two-cell conveyor deadlock)."""
    b = CordicRotateBlock("t")
    progs = list(b.build_cell_programs().keys())
    lay = b.default_layout()
    assert list(lay.keys()) == progs
    at = {(x, y): cid for cid, (x, y, _f) in lay.items()}
    for cid, (x, y, face) in lay.items():
        dx, dy = _DELTA[face]
        nbr = at.get((x + dx, y + dy))
        if nbr is None:
            continue
        nx, ny, nface = lay[nbr]
        ndx, ndy = _DELTA[nface]
        assert (nx + ndx, ny + ndy) != (x, y), (
            f"{cid} and {nbr} rest facing each other (INV-56)")


def test_release_is_a_backward_jump_reading_the_reconciled_face():
    """INV-69: the serialize-LOCK release must re-point LOCK_FACE from the
    RENDEZVOUS's OWN ``face_x`` word — the one copy the build's
    face-reconciliation pass (``_apply_rendezvous_input_faces``) patches to
    the ROUTED arm geometry.

    THE DEFECT THIS PINS (measured on examples/foc_motor): the release used
    to be a value-carrying ``WRITE.CFG @1, 3`` whose value came from an
    AUTHORED ``unlock_face`` DataWord living in ``pre``. That copy is never
    reconciled, so on any layout where the router lands arm x somewhere other
    than the authored face the release aimed the lock at the wrong face and
    the chain wedged from the SECOND triple onward. In the FOC chain the
    router lands arm x NORTH while the constant said WEST.

    So: NO value-carrying WRITE.CFG anywhere in the block, exactly ONE
    backward edge (a JUMP into `relock`, in internal_jumps so portmap never
    classifies an arm as a feedback RETURN), and `relock` reads `face_x`."""
    b = CordicRotateBlock("t")
    cps = b.build_cell_programs()
    order = list(cps.keys())
    idx = {c: i for i, c in enumerate(order)}

    # Exactly ONE backward edge, and it is a JUMP into the rendezvous's
    # `relock` entry — not a data/CONFIG connection.
    back_jumps = [(s, sp, d, dp) for (s, sp, d, dp) in b.internal_jumps()
                  if d != "__terminate__" and idx[d] < idx[s]]
    assert back_jumps == [("pre", "unlock", "rdv", "relock")], back_jumps
    back_conns = [(s, sp, d, dp) for (s, sp, d, dp) in
                  b.internal_connections() if idx.get(d, 99) < idx[s]]
    assert back_conns == [], back_conns

    # The release carries NO face value of its own: a value-carrying
    # WRITE.CFG is exactly the unreconciled-constant defect.
    for cid, cp in cps.items():
        assert "WRITE.CFG" not in cp.assembly_template, (
            f"{cid} carries a WRITE.CFG — the release must be a backward "
            f"JUMP into `relock` so the lock value comes from the "
            f"RECONCILED face_x word (INV-69)")

    # `relock` re-points the lock from face_x, the reconciled word.
    rdv = cps["rdv"].assembly_template
    relock = rdv.split("relock:\n", 1)[1]
    assert "MOVE [LOCK_FACE], R{data:face_x}" in relock, relock
    # And the block declares no UNLOCK_CFG_ADDR: there is no CONFIG write to
    # patch any more.
    assert not hasattr(CordicRotateBlock, "UNLOCK_CFG_ADDR")


def test_relock_reads_the_same_word_the_build_reconciles():
    """The INV-69 property stated as an identity: the DataWord `relock`
    writes into LOCK_FACE must be the SAME word named by
    RENDEZVOUS_FACE_PORTS for the FIRST-accepted arm (the one the build
    patches and boots the LOCK to). If these ever drift apart the release
    re-admits the wrong arm and the chain mis-pairs from triple two."""
    b = CordicRotateBlock("t")
    first_port, first_word = CordicRotateBlock.RENDEZVOUS_FACE_PORTS[0]
    assert first_port == "x"
    cps = b.build_cell_programs()
    relock = cps["rdv"].assembly_template.split("relock:\n", 1)[1]
    assert f"MOVE [LOCK_FACE], R{{data:{first_word}}}" in relock, (
        f"relock must write the FIRST-accepted arm's reconciled face word "
        f"({first_word}); got:\n{relock}")
    # And that word really is a reconciled is_face DataWord on the rdv cell.
    fw = {d.name: d for d in cps["rdv"].data if getattr(d, "is_face", False)}
    assert first_word in fw, sorted(fw)


def test_every_declared_entry_is_jumped():
    """INV-39: an entry nothing jumps at is dead code that runs the wrong
    path forever. prep2's three quadrant entries and the rendezvous's three
    arm entries make this load-bearing here."""
    b = CordicRotateBlock("t")
    cps = b.build_cell_programs()
    jumped: dict = {}
    for (_s, _p, d, e) in b.internal_jumps():
        jumped.setdefault(d, set()).add(e)
    jumped.setdefault("rdv", set()).update(
        {p.entry for p in cps["rdv"].inputs if p.entry})
    for cid, cp in cps.items():
        for e in cp.entries:
            name = e.name if e.name != "default" else "default"
            ok = (name in jumped.get(cid, set())
                  or ("default" in jumped.get(cid, set())))
            assert ok, (f"cell '{cid}' declares entry '{e.name}' that "
                        f"NOTHING jumps at (INV-39)")


def test_no_goto_anywhere():
    """INV-43 (measured on THIS block): the assembler compiles a GOTO near a
    write/jump placeholder into an EXTERNAL output jump — a GOTO variant of
    the restore cells emitted NOTHING. The saturating restore uses the
    ComplexGain conditional-branch idiom instead; keep GOTO out."""
    for sign in (1, -1):
        for cid, cp in CordicRotateBlock("t", sign=sign) \
                .build_cell_programs().items():
            assert "GOTO" not in cp.assembly_template, (cid, sign)


def test_rendezvous_is_a_leaf_with_reserved_column():
    """The N=3 face budget (INV-46 rule 2): the rendezvous needs its three
    free faces, so it must be a LEAF of the fold (exactly ONE in-block
    neighbour — `pre`), and column 0 of the layout holds ONLY the
    rendezvous."""
    lay = CordicRotateBlock("t").default_layout()
    pos = {cid: (x, y) for cid, (x, y, _f) in lay.items()}
    rx, ry = pos["rdv"]
    occ = set(pos.values())
    nbrs = [p for p in [(rx + 1, ry), (rx - 1, ry), (rx, ry + 1),
                        (rx, ry - 1)] if p in occ]
    assert nbrs == [pos["pre"]], nbrs
    col0 = [cid for cid, (x, _y) in pos.items() if x == rx]
    assert col0 == ["rdv"], (
        f"column 0 must hold ONLY the rendezvous (its south face is an arm "
        f"face); got {col0}")
    w = max(x for x, _ in occ) - min(x for x, _ in occ)
    h = max(y for _, y in occ) - min(y for _, y in occ)
    assert (w, h) == (4, 4), (w, h)


def test_declares_distinct_faces_and_rendezvous_ports():
    """The face-lock declarations the build's reconciliation pass needs —
    without RENDEZVOUS_FACE_PORTS the pass silently no-ops (TMR measured:
    builds + routes perfectly, emits ZERO)."""
    assert CordicRotateBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = CordicRotateBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("x", "face_x"), ("y", "face_y"), ("theta", "face_t"))
    cp = CordicRotateBlock("t").build_cell_programs()["rdv"]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports and wn in face_words, (pn, wn)
    assert "face_fwd" in face_words


def test_bad_construction_raises():
    """Same-face arms cannot be told apart (the face IS the arm identity)
    and sign must be +-1: both raise instead of building a block that
    mis-pairs or silently rounds."""
    with pytest.raises(ValueError, match="DISTINCT"):
        CordicRotateBlock("t", face_x="west", face_y="west")
    with pytest.raises(ValueError, match="sign"):
        CordicRotateBlock("t", sign=0)
    with pytest.raises(ValueError, match="sign"):
        CordicRotateBlock("t", sign=2)


def test_rendezvous_boots_pre_locked_with_no_arm_entry():
    """Cold start is BAKED (initial_lock_face): arming via a JUMP is a race
    — a word arriving before the arm-JUMP is accepted on an unlocked face
    and mis-pairs (the exact failure the LOCK prevents)."""
    cp = CordicRotateBlock("t").build_cell_programs()["rdv"]
    assert cp.initial_lock_face is not None
    # The three ARM entries, plus `relock` — the serialize-LOCK release target
    # (INV-69). `relock` is NOT an arm: nothing external ever jumps it; the
    # abutting `pre` cell does, on the internal forward face.
    assert [e.name for e in cp.entries] == ["got_x", "got_y", "got_t",
                                            "relock"]


def test_block_declares_two_output_registers():
    """The (yi, yq) complex packet: >1 output registers is THE build
    discriminator that steers the two rails to consecutive downstream
    registers (INV-23's brokered 2-rail guard); with one register the
    packet collapses."""
    b = CordicRotateBlock("t")
    assert len(b.interface.output_registers) == 2
    posty = b.build_cell_programs()["posty"].assembly_template
    # yi (x') is emitted FIRST: the emit order IS the packet rail order.
    assert posty.index("{write:yi}") < posty.index("{write:yq}")


def test_grc_binding_lists_the_three_arms_and_the_complex_out():
    """The GRC yml must present exactly the block's ports: three float
    arms in (x, y, theta) and one complex out — and expose `sign`."""
    import yaml
    y = yaml.safe_load(
        (_ROOT / "gr-kyttar" / "grc" / "kyttar_cordic_rotate.block.yml")
        .read_text())
    assert [i["label"] for i in y["inputs"]] == ["x", "y", "theta"]
    assert [o["dtype"] for o in y["outputs"]] == ["complex"]
    assert any(p["id"] == "sign" for p in y["parameters"])


def test_iteration_count_matches_the_shipped_cordic():
    """The spec pins the iteration count to the shipped CORDIC's NITER
    unless a different count is MEASURED necessary — it was not: 14
    iterations meet the 25-LSB bound (the accuracy gate) and the dropped-
    iteration mutant proves the gate sees the count."""
    from gr_kyttar.placement.blocks.cordic_blocks import NITER
    assert NITER == 14
    b = CordicRotateBlock("t")
    its = [c for c in b.build_cell_programs() if c.startswith("it")]
    assert len(its) == NITER
    assert b.cell_count == NITER + 6 == 20


# --------------------------------------------------------------------------- #
#  Dashboard report                                                           #
# --------------------------------------------------------------------------- #

def test_zz_emit_report():
    """Emit the dashboard report (INV-38: through the sanctioned session
    writer — the file appears only if this suite's own tests all passed).
    Metric EXACT: the on-chip words must equal the bit-exact golden word
    for word; the float-rotation accuracy is the golden's own separate,
    measured bound (24.75 LSB max, gated above at 25.0)."""
    ch = _build_chain()
    tr = _full_circle_triples() + [(0x7FFF, 0x7FFF, 0x2000),
                                   (1000, 2000, 0x9000)]
    for (x, y, t) in tr:
        ch.sample(x, y, t)
    ref = rotate_stream([t[0] for t in tr], [t[1] for t in tr],
                        [t[2] for t in tr], 1)
    assert ch.out == ref, (ch.out, ref)
    res = compare_against_grc(
        ch.out, [_s16(w) / 32768.0 for w in ref],
        metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("CordicRotateBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 2, "mutation": True,
        "on_chip_three_arm_chain": True, "full_circle_octants": 8,
        "async_interleavings": 6, "saturated": True, "orientations": 8,
        "float_bound_lsb_max": 24.75})
