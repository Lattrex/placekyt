# SPDX-License-Identifier: GPL-3.0-or-later
"""ClarkeTransformBlock — two-input Clarke (abc -> alpha-beta) transform for
3-phase FOC, verified ON CHIP.

WHY THERE IS NO GNU RADIO COUNTERPART. GNU Radio ships no Clarke-transform
block, and even the FUNCTION's trivial half (an add + two multiplies) does not
capture the PROBLEM: the two phase currents ia and ib are produced by two
SEPARATE on-chip chains firing at independent, asynchronous times, and on the
clockless Kyttar array the only stream identity available is the physical
channel — the arrival FACE. So the golden is a host reference of the pinned
contract (``ClarkeTransformBlock.process_reference_words`` — an EXACT integer
model of the shipped chip arithmetic: the Q15 constant, the truncating MULQ,
the two saturating adds in evaluation order — the poly1305_golden pattern),
compared word for word against the real chip, plus a FLOAT-reference tolerance
bound proving the integer model tracks the ideal (ia + 2*ib)/sqrt(3).

A MIS-PAIRED CLARKE OUTPUT IS SILENT — (ia[1], ib[0]) transforms to a
perfectly plausible current vector — which is what makes the rendezvous worth
proving rather than assuming. Every interleaving stimulus below is chosen so
that a mis-pairing CHANGES THE OUTPUT (asserted, not assumed).

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * EXACT vs the golden integer model on named edges INCLUDING the +/-0x7FFF
    saturation corners and the both-arms-saturated case, a full-scale sweep,
    and random pairs over 3 seeds.
  * The golden itself tracks the FLOAT Clarke reference within a DERIVED
    bound (<= 5 LSB off the saturation rails; bound derivation inline).
  * ADVERSARIAL ASYNC INTERLEAVING — both relative arrival orders and random
    per-sample orders produce the IDENTICAL stream.
  * STARTUP / STALL — no output until both arms have spoken; a starved arm
    stalls and recovers.
  * SATURATION (INV-19) — the whole burst driven back-to-back with no
    inter-sample quiescence equals the per-sample result (this block is a
    LOCK join: saturated == per-sample, the lock IS the serialization).
  * stop_reason READ FOR EVERY RUN (INV-56): the harness records it; the
    healthy signature is pinned (an arbiter-HELD word mid-pair reports
    "Deadlock" by design; a completed pair flushes to "QueueEmpty").
  * ORIENTATION (INV-23) — identical output in all 8 D4 orientations.
  * MUTATIONS (INV-4, substrate form — corrupt the REAL block, rebuild on the
    REAL chip): wrong 1/sqrt(3) constant, dropped ia term in beta, swapped
    alpha/beta output rails, NON-saturating adds (driven at the overflow
    corner), dropped re-lock. Each proven to FIRE. All mutants are IN-MEMORY
    program mutations (no file edit), so the INV-4 stale-pyc trap cannot
    apply.

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_clarke_transform.py -q
"""
from __future__ import annotations

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


def _ref(ia_words, ib_words):
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    return ClarkeTransformBlock.process_reference_words(ia_words, ib_words)


def _s16(w: int) -> int:
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


# --------------------------------------------------------------------------- #
#  The REAL two-upstream chain: two INDEPENDENT identity/decimating arms.      #
# --------------------------------------------------------------------------- #
#
# Each arm is a KeepOneInN, fed from the ONE chip input port by its OWN net, so
# each arm has its OWN input landing (hop + entry + data address). Driving one
# arm's landing advances ONLY that arm — which is what lets the harness produce
# ANY relative arrival order. (The xor_join harness, verbatim; the join block
# and its output shape differ.)

_ARM_N = 2      # each arm emits on every 2nd raw sample (GR's phase n-1)

_ANCHORS = [((2, 2), (2, 6), (5, 4)), ((1, 1), (1, 5), (4, 3)),
            ((2, 1), (2, 5), (5, 3)), ((3, 2), (3, 6), (6, 4))]


class _Chain:
    """A built two-upstream Clarke chain + a driver that fires ONE arm.

    OUTPUT SHAPE: the block is a COMPLEX 2-rail source — yi (i_alpha) and yq
    (i_beta) both egress x16_out per trigger, interleaved in emit order — so
    ``out`` accumulates ``[alpha0, beta0, alpha1, beta1, ...]``, exactly the
    golden's flat stream.

    INV-56: EVERY ``chip.run`` records its stop_reason in ``stop_reasons``.
    """

    def __init__(self, bres, chip, la, lb, ctrl=None, blk=None):
        self.bres, self.chip, self.la, self.lb = bres, chip, la, lb
        self.ctrl, self.blk = ctrl, blk
        self.out: list[int] = []
        self.stop_reasons: list[str] = []

    def _run(self, cap: int):
        r = self.chip.run(max_events=cap)
        if isinstance(r, dict):
            self.stop_reasons.append(str(r.get("stop_reason")))
        return r

    def raw(self, arm: str, value: int):
        """Push ONE RAW sample into the named arm ('a'=ia or 'b'=ib); the arm's
        KeepOneInN emits (and drives the join) on every _ARM_N-th one."""
        land = self.la if arm == "a" else self.lb
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self._run(6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        self._run(300000)
        self._drain()

    def emit(self, arm: str, value: int):
        """Make the named arm EMIT exactly one word equal to ``value``."""
        for _ in range(_ARM_N - 1):
            self.raw(arm, 0)
        self.raw(arm, value)

    def sample(self, av: int, bv: int, b_first: bool = False):
        """Drive one complete (ia, ib) pair in the given relative order."""
        if b_first:
            self.emit("b", bv)
            self.emit("a", av)
        else:
            self.emit("a", av)
            self.emit("b", bv)

    def _drain(self):
        while self.chip.output_available("x16_out"):
            w = self.chip.read_port_i16("x16_out").view("uint16").tolist()
            self.out.extend(int(x) & 0xFFFF for x in w)
            self.chip.release_output_ack("x16_out")
            self._run(8000)


def _wire(ctrl, CPE, BE, ka, kb, j):
    ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                BE(block=ka, port="sample"), name="n0")
    ctrl.add_logical_connection(CPE(chip=0, port="x16_in"),
                                BE(block=kb, port="sample"), name="n1")
    ctrl.add_logical_connection(BE(block=ka, port="out"),
                                BE(block=j, port="ia"), name="n2")
    ctrl.add_logical_connection(BE(block=kb, port="out"),
                                BE(block=j, port="ib"), name="n3")
    # ONE net: the yi rail. The yq rail rides the same egress (the complex
    # 2-rail port handoff gives it the next tag); wiring yq as a second net to
    # the same port is the known-broken shape (see run_block_dut_complex).
    ctrl.add_logical_connection(BE(block=j, port="yi"),
                                CPE(chip=0, port="x16_out"), name="n4")


def _build_chain(orient=None):
    """Build 2 KeepOneInN arms -> ClarkeTransform -> x16_out on ONE 10x12 chip.

    auto_pnr is a CP-SAT search and is not deterministic across runs, so try a
    few anchor sets rather than pinning one. Every candidate layout is SMOKED
    on a THROWAWAY chip before a gate gets it (INV-46 Rule 4): a layout can
    route, build, and present two distinct landings and still mis-deliver an
    arm. The probe values are chosen so a mis-delivered or swapped arm cannot
    pass by coincidence (alpha identifies ia exactly; beta separates ib)."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("clarke_chain", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("ClarkeTransformBlock", 0, *j_xy, library=LIB,
                                 params={})
            if orient:
                # Rotate/mirror the join BEFORE routing (INV-23): the nets are
                # still unrouted logical connections, so OrientBlockCommand is
                # the right primitive (it preserves them for the router).
                from commands import OrientBlockCommand
                for kind in orient:
                    OrientBlockCommand(ctrl.project, j, kind).execute()
            _wire(ctrl, CPE, BE, ka, kb, j)
            if not ctrl.auto_pnr({ctk: ct}).ok:
                continue
            bres = ctrl.build()
            if not bres.ok:
                continue
            il = bres.chips[0].input_landings
            if "n0" not in il or "n1" not in il:
                continue
            # The two arms MUST have DISTINCT landings, else the harness cannot
            # drive them independently and every interleaving test is vacuous.
            sig = {(int(il[k]["hop"]), int(il[k]["entry"]),
                    int(il[k]["data_addrs"][0])) for k in ("n0", "n1")}
            if len(sig) < 2:
                continue
            # SMOKE on a throwaway chip (never the gate's chip — driving pairs
            # advances the lock rotation and latches arm state).
            probe_chip = simkyt.Chip.from_yaml(CHIP_YAML)
            probe_chip.load_bitstream_physical(bres.words(0))
            probe_chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            probe = _Chain(bres, probe_chip, il["n0"], il["n1"])
            pa, pb = [0x1234, 0xF00C, 0x0400], [0x0111, 0x2222, 0xFC00]
            for k, (av, bv) in enumerate(zip(pa, pb)):
                probe.sample(av, bv, b_first=bool(k % 2))
            if probe.out != _ref(pa, pb):
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            return _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
    pytest.skip("no anchor routed the two-upstream Clarke chain on this run")


# --------------------------------------------------------------------------- #
#  THE GOLDEN — pinned constants, pinned vectors, float-reference bound        #
# --------------------------------------------------------------------------- #

def test_golden_constant_is_derived_not_invented():
    """C = round(32768/sqrt(3)) = 18919, and the INV-15 fact that makes one
    data word serve both terms: 2/sqrt(3) quantized in Q14 is the SAME word,
    so 'apply the halved coefficient twice' is exactly the ideal 2/sqrt(3)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock as B
    assert B.INV_SQRT3_Q15 == round(32768.0 / math.sqrt(3.0)) == 18919
    assert round(2.0 / math.sqrt(3.0) * (1 << 14)) == B.INV_SQRT3_Q15
    assert B.SAT_POS_Q15 == 0x7FFF


def test_golden_is_pinned_independently():
    """Pin the golden's own contract with explicit vectors so a silent change
    to the reference cannot slip through. Includes ALL the saturation corners
    the spec names (each value below verified against the shipped arithmetic
    by hand: t = (x*18919)>>15, beta = sat(sat(t_a+t_b)+t_b))."""
    # zeros and tiny values
    assert _ref([0], [0]) == [0, 0]
    assert _ref([100], [0]) == [100, 57]          # (100*C)>>15 = 57
    assert _ref([0], [100]) == [0, 114]           # 2*57
    # both-arms-saturated, positive and negative: beta clamps to the rails
    assert _ref([0x7FFF], [0x7FFF]) == [0x7FFF, 0x7FFF]
    assert _ref([0x8000], [0x8000]) == [0x8000, 0x8000]
    # single-arm corners: no overflow, exact truncating arithmetic
    #   +max: t = (32767*18919)>>15 = 18918 (truncated);
    #   -max: t = (-32768*18919)>>15 = -18919 (exact).
    #   ia=+max, ib=-max: beta = 18918 - 2*18919 = -18920, in range, no clamp.
    assert _ref([0x7FFF], [0x8000]) == [0x7FFF, (-18920) & 0xFFFF]
    #   ia=-max, ib=+max: beta = -18919 + 2*18918 = 18917, in range, no clamp.
    assert _ref([0x8000], [0x7FFF]) == [0x8000, 18917]
    # alpha is ALWAYS the raw ia word, exact — including the negative rail
    assert _ref([0x8000], [0]) == [0x8000, (-18919) & 0xFFFF]
    # stall semantics: the reference truncates to the SHORTER arm
    assert _ref([1, 2, 3], [1]) == _ref([1], [1])
    assert _ref([], [1, 2]) == []


def test_golden_saturating_adds_clamp_and_wrap_free():
    """The one-sided overflow cases: values where t_a + t_b overflows but the
    second add pulls back IN range cannot exist (same-sign operands only push
    further out), pinned; and a value pair just BELOW the overflow threshold
    must NOT clamp."""
    # Just below the positive rail: ia=ib=0x6000 (0.75): t=17361? compute:
    t = (0x6000 * 18919) >> 15
    s1 = min(2 * t, 32767)
    beta = min(s1 + t, 32767)
    assert _ref([0x6000], [0x6000]) == [0x6000, beta & 0xFFFF]
    # 0.5/0.5 stays fully in range and unclamped
    t = (0x4000 * 18919) >> 15          # 9459
    assert _ref([0x4000], [0x4000]) == [0x4000, 3 * t]
    assert 3 * t < 32767


def test_golden_tracks_the_float_clarke_reference():
    """DERIVED tolerance bound (not tuned): each truncating MULQ loses up to
    1 LSB (floor error in (-1, 0]); the ib term is applied twice (2 LSB); the
    constant quantization |18919 - 32768/sqrt(3)| = 0.39 contributes <= 0.39
    LSB per unit operand (x1 for ia, x2 for ib) — total worst |err| < 1 + 2 +
    0.39 + 0.78 = 4.2 LSB. Assert <= 5 LSB over a dense random sweep, OFF the
    saturation rails (where the float reference itself is clamped)."""
    rng = random.Random(20260831)
    worst = 0.0
    for _ in range(5000):
        ia = rng.randrange(-32768, 32768)
        ib = rng.randrange(-32768, 32768)
        ref_f = (ia + 2 * ib) / math.sqrt(3.0)
        if not (-32700 < ref_f < 32700):
            continue
        beta = _s16(_ref([ia & 0xFFFF], [ib & 0xFFFF])[1])
        worst = max(worst, abs(beta - ref_f))
    assert worst <= 5.0, f"integer model drifted {worst} LSB off the float Clarke"


def test_golden_matches_the_blocks_float_reference():
    """``process_reference`` (the float path the generic harness sees) and the
    integer golden must agree within the same derived bound."""
    import numpy as np
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    b = ClarkeTransformBlock("x")
    ia = [0.5, -0.25, 0.1, -0.9, 0.0]
    ib = [-0.25, 0.5, 0.3, 0.2, 0.0]
    z = np.array([complex(a, q) for a, q in zip(ia, ib)])
    out = b.process_reference(z)
    words = _ref([int(round(a * 32768)) & 0xFFFF for a in ia],
                 [int(round(q * 32768)) & 0xFFFF for q in ib])
    for k in range(len(ia)):
        assert abs(out[k].real * 32768 - _s16(words[2 * k])) <= 1.0
        assert abs(out[k].imag * 32768 - _s16(words[2 * k + 1])) <= 5.0


# --------------------------------------------------------------------------- #
#  EXACT vs the golden ON CHIP — edges + full-scale sweep + random x3 seeds    #
# --------------------------------------------------------------------------- #

def test_named_edges_and_saturation_corners_on_chip():
    """EDGE coverage on the real chip, EXACT: zeros, +/-1 LSB, the +/-0x7FFF
    corners on each arm, the BOTH-ARMS-SATURATED cases (all four sign
    combinations — the two same-sign ones drive both saturating adds into
    their clamp paths), and mixed extremes."""
    ch = _build_chain()
    a = [0x0000, 0x0001, 0xFFFF, 0x7FFF, 0x8000, 0x7FFF, 0x8000, 0x7FFF,
         0x8000, 0x0000, 0x0000]
    b = [0x0000, 0xFFFF, 0x0001, 0x7FFF, 0x8000, 0x8000, 0x7FFF, 0x0000,
         0x0000, 0x7FFF, 0x8000]
    for k, (av, bv) in enumerate(zip(a, b)):
        ch.sample(av, bv, b_first=bool(k % 2))
    assert ch.out == _ref(a, b), (ch.out, _ref(a, b))


def test_full_scale_sweep_is_exact_on_chip():
    """FULL-SCALE sweep: a ladder across the whole Q15 range on both arms
    (including the exact rails), covering unclamped, singly-clamped and
    doubly-clamped beta paths. EXACT vs the integer golden."""
    ch = _build_chain()
    ladder = [0x8000, 0xA000, 0xC000, 0xE000, 0x0000,
              0x2000, 0x4000, 0x6000, 0x7FFF]
    a = list(ladder) + list(reversed(ladder))
    b = list(reversed(ladder)) + list(ladder)
    for k, (av, bv) in enumerate(zip(a, b)):
        ch.sample(av, bv, b_first=bool(k % 2))
    assert ch.out == _ref(a, b), (ch.out, _ref(a, b))


@pytest.mark.parametrize("seed", [7, 43, 911])
def test_random_pairs_are_exact(seed):
    """RANDOM full-range pairs over >=3 seeds (the coverage bar), EXACT vs the
    golden. Values are filtered so every sample's (alpha, beta) is distinct
    and every one-step mis-pairing changes the stream (mis-pairing is
    otherwise SILENT for a transform)."""
    rng = random.Random(seed)
    ch = _build_chain()
    a, b = [], []
    while len(a) < 8:
        av = rng.randrange(0, 0x10000)
        bv = rng.randrange(0, 0x10000)
        if av in a or bv in b:
            continue
        a.append(av)
        b.append(bv)
    exp = _ref(a, b)
    shifted = _ref(a[1:], b[:-1])
    assert shifted != exp[:len(shifted)], "stimulus cannot see a 1-pair desync"
    for av, bv in zip(a, b):
        ch.sample(av, bv, b_first=rng.random() < 0.5)
    assert ch.out == exp, (ch.out, exp)


# --------------------------------------------------------------------------- #
#  ADVERSARIAL ASYNC INTERLEAVING — the rendezvous claim                       #
# --------------------------------------------------------------------------- #

def _distinguishing_pairs(n=5):
    """Pairs where every sample's (alpha, beta) is distinct AND any one-step
    cross-pairing produces a different stream (asserted by the callers)."""
    a = [(0x0800 + 0x0777 * i) & 0xFFFF for i in range(n)]
    b = [(0xF000 + 0x0533 * i) & 0xFFFF for i in range(n)]
    return a, b


@pytest.mark.parametrize("b_first", [False, True],
                         ids=["ia-then-ib", "ib-then-ia"])
def test_both_relative_arrival_orders_are_identical(b_first):
    """BOTH relative arrival orders must produce the IDENTICAL stream — the
    LOCK holds the early producer's word until it is that face's turn."""
    a, b = _distinguishing_pairs()
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv, b_first=b_first)
    assert ch.out == _ref(a, b), (
        f"arrival order b_first={b_first} broke the pairing", ch.out, _ref(a, b))


@pytest.mark.parametrize("seed", [3, 17, 91])
def test_random_interleavings_preserve_the_pairs(seed):
    """RANDOM per-sample arrival order (3 seeds): whatever order the two
    producers fire in, the emitted stream is exactly the golden."""
    rng = random.Random(seed)
    a, b = _distinguishing_pairs(7)
    exp = _ref(a, b)
    shifted = _ref(a[1:], b[:-1])
    assert shifted != exp[:len(shifted)], "stimulus cannot see a desync"
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv, b_first=rng.random() < 0.5)
    assert ch.out == exp, (ch.out, exp)


def test_bursty_arm_runs_ahead_and_is_held():
    """BURSTY arms: arm ia runs 2 samples ahead before ib says anything, then
    ib catches up. Both pairs must come out correctly matched — the surplus ia
    words were HELD by the arbiter, not dropped or mis-paired."""
    ch = _build_chain()
    a, b = [0x1100, 0x2200], [0x0300, 0x0500]
    for av in a:
        ch.emit("a", av)
    for bv in b:
        ch.emit("b", bv)
    assert ch.out == _ref(a, b), ch.out


# --------------------------------------------------------------------------- #
#  STARTUP + STALL semantics                                                   #
# --------------------------------------------------------------------------- #

def test_startup_emits_nothing_until_both_arms_have_spoken():
    """NO PARTIAL OUTPUT, ever: after arm ia alone the chip has produced
    NOTHING — not alpha alone, not a (alpha, 0) packet. The pair appears only
    when ib arrives."""
    ch = _build_chain()
    ch.emit("a", 0x4200)
    assert ch.out == [], f"a partial/unpaired packet leaked out: {ch.out}"
    ch.emit("b", 0x1800)
    assert ch.out == _ref([0x4200], [0x1800]), ch.out


def test_starved_arm_stalls_and_recovers():
    """Arm ia supplies TWO words, arm ib only ONE: exactly ONE pair may be
    emitted; the surplus ia word is HELD, and when the missing ib word arrives
    the held ia is paired with its CORRECT partner."""
    ch = _build_chain()
    ch.emit("a", 0x1100)
    ch.emit("a", 0x2200)          # surplus — must be held
    ch.emit("b", 0x0300)
    assert ch.out == _ref([0x1100, 0x2200], [0x0300]), ch.out
    ch.emit("b", 0x0500)
    assert ch.out == _ref([0x1100, 0x2200], [0x0300, 0x0500]), ch.out


# --------------------------------------------------------------------------- #
#  INV-56 — stop_reason, read for EVERY case                                   #
# --------------------------------------------------------------------------- #

def test_stop_reason_signature_of_a_healthy_rendezvous():
    """INV-56 says read stop_reason FIRST — and for a LOCK-rendezvous block
    the healthy signature is worth pinning, because it CONTAINS "Deadlock":
    a word delivered to the barred face is HELD by the arbiter, and a run
    that ends with a held word reports stop_reason == "Deadlock" with
    completed == False even though the block is doing exactly its job
    (measured here on the ib-first drive). The diagnosis-bearing reading is
    the stop_reason AFTER the pair completes: that run and the drain must
    come back "QueueEmpty". A "Deadlock" AFTER a completed pair is a real
    wedge (the TMR one-triple-in-flight class), and this gate would catch it."""
    ch = _build_chain()
    # ia-then-ib: no word is ever held long -> every run QueueEmpty.
    ch.sample(0x1000, 0x0800)
    assert set(ch.stop_reasons) == {"QueueEmpty"}, ch.stop_reasons
    # ib-first: the held ib word makes intermediate runs report Deadlock...
    n0 = len(ch.stop_reasons)
    ch.sample(0x2000, 0x1000, b_first=True)
    mid = ch.stop_reasons[n0:]
    assert "Deadlock" in mid, (
        "expected the arbiter-HELD ib word to report as Deadlock mid-pair "
        f"(the measured healthy signature); got {mid}")
    # ...but the run that completes the pair, and the drain, are QueueEmpty.
    assert ch.stop_reasons[-1] == "QueueEmpty", ch.stop_reasons
    assert ch.out == _ref([0x1000, 0x2000], [0x0800, 0x1000]), ch.out


# --------------------------------------------------------------------------- #
#  INV-19 — SATURATED drive == per-sample drive                                #
# --------------------------------------------------------------------------- #

def _enc_write(hop: int, addr: int) -> int:
    """WRITE opcode 0x6, hop in [9:5], dest in [4:0]."""
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    """JUMP opcode 0x7, hop in [9:5], entry in [4:0]."""
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def _saturated_run(a_words, b_words, cap: int = 4_000_000):
    """Drive the WHOLE burst SATURATED: every raw word of every arm of every
    sample enqueued via ``queue_words_physical`` (the real streaming condition
    — NO inter-sample quiescence ANYWHERE), then ONE bounded run (never
    ``max_events=None`` — the INV-19 harness-safety rule)."""
    ch = _build_chain()
    stream: list[int] = []
    for av, bv in zip(a_words, b_words):
        for land, val in ((ch.la, av), (ch.lb, bv)):
            hop = int(land["hop"]) & 0x1F
            for raw_v in [0] * (_ARM_N - 1) + [int(val) & 0xFFFF]:
                stream.append(_enc_write(hop, int(land["data_addrs"][0])))
                stream.append(raw_v)
                stream.append(_enc_jump(hop, int(land["entry"])))
    ch.chip.queue_words_physical("x16_in", stream)
    res = ch.chip.run(max_events=cap)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    reason = res.get("stop_reason") if isinstance(res, dict) else None
    ch._drain()
    return completed, reason, ch.out


def test_saturated_equals_per_sample():
    """INV-19, the REQUIRED gate for a LOCK join block: the whole burst
    enqueued back-to-back — both producers racing at the rendezvous — must
    equal the per-sample result, with the correct COUNT (one packet per pair).

    The stimulus INCLUDES the both-arms-saturated overflow pairs, so the
    saturating-add clamp paths are exercised UNDER LOAD, not only settled.

    Structurally expected to pass (the XorJoin argument): at N=2 the whole
    rendezvous is ONE cell, and the arbiter LOCK it already carries IS the
    serialization INV-19 prescribes — there is no internal datapath for
    queued samples to pile into."""
    a, b = _distinguishing_pairs(6)
    a += [0x7FFF, 0x8000]           # the overflow corners, saturated drive
    b += [0x7FFF, 0x8000]
    exp = _ref(a, b)

    per = _build_chain()
    for av, bv in zip(a, b):
        per.sample(av, bv)
    assert per.out == exp, ("per-sample drive already wrong", per.out, exp)

    completed, reason, out = _saturated_run(a, b)
    assert completed, (
        f"the saturated drive wedged (stop_reason={reason}); partial={out}")
    assert reason == "QueueEmpty", (
        f"INV-56: a completed saturated burst must flush to QueueEmpty, "
        f"got {reason}")
    assert out == exp, (
        f"saturated != per-sample.\n saturated={out}\n per-sample={exp}")
    assert len(out) == 2 * len(a), (
        f"wrong output COUNT: {len(out)} words for {len(a)} pairs — one "
        f"(alpha, beta) packet per pair, never dropped or duplicated")


def test_saturated_drive_is_not_vacuous():
    """NON-VACUITY for the gate above (INV-4 applied to the harness): the two
    arms' words really are enqueued together (no run between them), and the
    stimulus is chosen so a one-pair desync produces a DIFFERENT stream —
    asserted, so the gate cannot be satisfied by a desynced block."""
    a = [0x0100, 0x0200, 0x0400, 0x0800]
    b = [0x1000, 0x2000, 0x4000, 0x6000]
    exp = _ref(a, b)
    shifted = _ref(a[1:], b[:-1])
    assert shifted != exp[:len(shifted)], (
        "a one-sample desync would satisfy this stimulus — pick values whose "
        "cross-pairings differ")
    completed, reason, out = _saturated_run(a, b)
    assert completed and out == exp, (reason, out, exp)


# --------------------------------------------------------------------------- #
#  INV-23 — ORIENTATION INVARIANCE, all 8 D4 orientations                      #
# --------------------------------------------------------------------------- #
#
# The universal gate (test_orientation_invariance.py) drives blocks through
# harnesses that inject on ONE input port; it cannot drive a TWO-FACE
# rendezvous — so, like DualFloatToComplex/FeaturePairJoin/TMRVoter/XorJoin,
# this block carries its OWN D4 gate on the real two-arm chain.

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
    """INV-23: identical output in all 8 D4 orientations. For this block that
    exercises the two ``is_face`` constants (face_ia/face_ib) D4-mapping
    together with the cold-start ``initial_lock_face`` AND the 2-rail complex
    egress patch under rotation (the INV-23 brokered-rail bug class): if any
    failed to transform, the chain would build, route, and emit nothing — or
    ship beta on the alpha rail."""
    ch = _build_chain(orient=orient)
    a, b = _distinguishing_pairs(4)
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (
        f"orientation {_d4_label(orient)} changed the transform (or produced "
        f"nothing): got {ch.out}, expected {_ref(a, b)}")


# --------------------------------------------------------------------------- #
#  MANDATORY mutation gates (INV-4) — model-level stimulus checks              #
# --------------------------------------------------------------------------- #

def test_mutation_empty_output_fails():
    """An empty stream never satisfies the reference."""
    assert [] != _ref([1, 2], [3, 4])


def test_mutation_one_sample_delay_fails():
    """A +1-sample-delay DUT (the standard harness mutation) is rejected."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    assert [0, 0] + good[:-2] != good


def test_mutation_alpha_passthrough_of_the_wrong_arm_fails():
    """A block that forwarded ib on the alpha rail (arm mix-up) is caught by
    value: the stimulus keeps a[i] != b[i] everywhere."""
    a, b = _distinguishing_pairs(4)
    assert all(x != y for x, y in zip(a, b))
    good = _ref(a, b)
    swapped_arms = _ref(b, a)
    assert swapped_arms != good, "stimulus cannot distinguish the two arms"


# --------------------------------------------------------------------------- #
#  SUBSTRATE-LEVEL mutations (INV-4, the strong form): corrupt the REAL block, #
#  rebuild it on the REAL chip, and prove the gate rejects the result.         #
# --------------------------------------------------------------------------- #
#
# All mutants are IN-MEMORY CellProgram mutations (a build_cell_programs
# override) — never a file edit — so the INV-4 stale-pyc trap cannot apply.

def _build_raw_chain():
    """Build the chain WITHOUT the smoke probe, so a mutant's misbehaviour is
    OBSERVED rather than silently skipped over by the anchor loop."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ctrl = AppController(catalog=cat)
            ctrl.new_project("clarke_mut", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("ClarkeTransformBlock", 0, *j_xy, library=LIB,
                                 params={})
            _wire(ctrl, CPE, BE, ka, kb, j)
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
            return _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
    return None


def _sub(template_from, template_to):
    """A build_cell_programs replacement that rewrites the assembly template.
    Asserts the mutation genuinely applied (a stale ``from`` string would make
    the gate vacuous)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    base = ClarkeTransformBlock.build_cell_programs

    def _mut(self):
        cps = base(self)
        cp = cps[0]
        new = cp.assembly_template.replace(template_from, template_to)
        assert new != cp.assembly_template, (
            f"the mutation did not apply — {template_from!r} is no longer in "
            f"the block's template, so this gate has gone vacuous")
        cp.assembly_template = new
        return cps
    return _mut


def _wrong_constant():
    """Wrong 1/sqrt(3): the coefficient DataWord becomes 0.5 (16384).
    DataWord is a frozen dataclass, so the mutant REBUILDS the data list with
    a replaced word (assigning ``dw.value`` raises and the block silently
    fails to BUILD — which this gate would mis-read as a rejection)."""
    import dataclasses
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    base = ClarkeTransformBlock.build_cell_programs

    def _mut(self):
        cps = base(self)
        cp = cps[0]
        new_data = [dataclasses.replace(dw, value=16384)
                    if dw.name == "inv_sqrt3" else dw for dw in cp.data]
        assert any(dw.name == "inv_sqrt3" and dw.value == 16384
                   for dw in new_data), (
            "inv_sqrt3 data word not found — mutation gone vacuous")
        cp.data = new_data
        return cps
    return _mut


def _swapped_outputs():
    """Swap the alpha/beta rails: the FIRST write (rail 0) carries beta and
    the SECOND (rail 1) carries alpha. Write ORDER is what steers the rails,
    so this is authored as a full replacement got_ib that computes beta first
    and emits it on yi."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    base = ClarkeTransformBlock.build_cell_programs

    def _mut(self):
        cps = base(self)
        cp = cps[0]
        cp.assembly_template = (
            "got_ia:\n"
            "    MOVE R{state:xa}, R{in:ia}\n"
            "    MOVE [LOCK_FACE], R{data:face_ib}\n"
            "    HALT\n"
            "got_ib:\n"
            "    MULQ R0, R{data:inv_sqrt3}\n"
            "    MOVE R{state:tb}, R0\n"
            "    MOVE R0, R{state:xa}\n"
            "    MULQ R0, R{data:inv_sqrt3}\n"
            "    ADD R0, R{state:tb}\n"
            "    BR.NV +3\n"
            "    MOVE R0, R{state:tb}\n"
            "    SHR R0, #15\n"
            "    ADD R0, R{data:satpos}\n"
            "    ADD R0, R{state:tb}\n"
            "    BR.NV +3\n"
            "    MOVE R0, R{state:tb}\n"
            "    SHR R0, #15\n"
            "    ADD R0, R{data:satpos}\n"
            "    {write:yi}\n"                          # beta on rail 0 (SWAP)
            "    MOVE R0, R{state:xa}\n"
            "    {write:yq}\n"                          # alpha on rail 1 (SWAP)
            "    {jump:trig}\n"
            "    MOVE [LOCK_FACE], R{data:face_ia}\n"
            "    HALT\n")
        return cps
    return _mut


# (name, mutation factory, why it must fire, stimulus exciting it).
# "unroutable" would also be a rejection — which is why these gates use the
# probe-FREE _build_raw_chain (the probing loop would turn it into a skip).
_OVF = ([0x7FFF, 0x8000, 0x6000], [0x7FFF, 0x8000, 0x7000])   # overflow corner
_MID = ([0x1234, 0xF00C, 0x2000], [0x0111, 0x2222, 0xEE00])   # generic


def _mutants():
    return [
        # Wrong 1/sqrt(3) constant: beta scales wrongly on every sample.
        ("wrong_inv_sqrt3_constant", _wrong_constant(), _MID),
        # Dropped ia term in beta: t_a is replaced by 0 (SUB R0, R0 zeroes the
        # accumulator, then the two t_b adds proceed) -> beta = sat(2*t_b).
        ("dropped_ia_term_in_beta",
         _sub("    {write:yi}\n"
              "    MULQ R0, R{data:inv_sqrt3}\n"
              "    ADD R0, R{state:tb}\n",
              "    {write:yi}\n"
              "    SUB R0, R0\n"
              "    ADD R0, R{state:tb}\n"), _MID),
        # Swapped alpha/beta output rails.
        ("swapped_alpha_beta_outputs", _swapped_outputs(), _MID),
        # NON-saturating adds: both clamp sequences collapse to bare ADDs, so
        # the overflow corner WRAPS (a sign flip) instead of clamping. Driven
        # at the both-arms-saturated corner, the case the spec names.
        ("non_saturating_add",
         _sub("    ADD R0, R{state:tb}\n"
              "    BR.NV +3\n"
              "    MOVE R0, R{state:tb}\n"
              "    SHR R0, #15\n"
              "    ADD R0, R{data:satpos}\n",
              "    ADD R0, R{state:tb}\n"), _OVF),
        # Dropped re-lock: the rotation stops and the join desyncs.
        ("dropped_relock",
         _sub("    MOVE [LOCK_FACE], R{data:face_ia}\n    HALT\n",
              "    HALT\n"), _MID),
    ]


@pytest.mark.parametrize("name", [m[0] for m in _mutants()])
def test_substrate_mutations_are_all_caught(name):
    """INV-4 IN ITS STRONG FORM: corrupt the REAL block, rebuild it on the
    REAL chip, run the REAL simulator, and assert the output does NOT match
    the golden. Each mutant's stimulus is chosen to EXCITE its bug (the
    non-saturating mutant is driven at the overflow corner — in range it is
    byte-identical to the real block and MUST be driven saturated to fire).

    Every mutant here is GEOMETRY-PRESERVING (same cell count, ports, faces),
    so it MUST place, route, and build — a None chain is a HARD FAILURE of
    this gate, not a rejection. That teeth matters: the first cut of the
    wrong-constant mutant assigned to a frozen DataWord field, the block
    silently failed to BUILD, and a 'None = rejected' reading passed a gate
    that had never run the mutant at all (measured in this session).

    MEASURED on-chip results (each proven able to FIRE):
      wrong_inv_sqrt3_constant  -> beta scaled by 0.5 instead of 1/sqrt(3)
      dropped_ia_term_in_beta   -> beta = sat(2*t_b) (ia gone from beta)
      swapped_alpha_beta_outputs-> [beta, alpha] packets (rails swapped)
      non_saturating_add        -> corner wraps: beta 56754 (sign flip) where
                                   the golden clamps to 32767
      dropped_relock            -> 2 correct packets then desync (stuck on
                                   the ib face)"""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    name_, mutation, (a, b) = next(m for m in _mutants() if m[0] == name)
    good = _ref(a, b)
    orig = ClarkeTransformBlock.build_cell_programs
    ClarkeTransformBlock.build_cell_programs = mutation
    try:
        ch = _build_raw_chain()
        assert ch is not None, (
            f"the '{name_}' mutant did not place+route+build — it is "
            f"geometry-preserving, so this means the MUTATION ITSELF is "
            f"broken (or a P&R flake across all anchors); the gate has not "
            f"observed the mutant and certifies nothing")
        for av, bv in zip(a, b):
            ch.sample(av, bv)
        got = ch.out
    finally:
        ClarkeTransformBlock.build_cell_programs = orig
    assert got != good, (
        f"the '{name_}' mutation produced the CORRECT stream {got} — this "
        f"gate cannot see it, so it certifies nothing")


def test_non_saturating_mutant_would_pass_in_range():
    """WHY the overflow stimulus is load-bearing (stated, not assumed): on
    in-range values the non-saturating mutant computes the IDENTICAL stream
    (the clamp path never runs), so a mutation gate that drove it mid-range
    would certify nothing. Model-level proof pinned here."""
    a, b = [0x1000, 0x2000], [0x0800, 0x1000]
    C = 18919
    wrap = []
    for av, bv in zip(a, b):
        tb = (_s16(bv) * C) >> 15
        ta = (_s16(av) * C) >> 15
        wrap += [av, (ta + 2 * tb) & 0xFFFF]
    assert wrap == _ref(a, b), "in-range: mutant and golden must coincide"
    # And at the corner they MUST differ (the wrap = the sign flip):
    corner = _ref([0x7FFF], [0x7FFF])
    tb = (32767 * C) >> 15
    assert ((2 * tb + tb) & 0xFFFF) != corner[1]


def test_substrate_mutation_harness_is_not_vacuous():
    """NON-VACUITY control: the UNMUTATED block, built through the SAME
    probe-free path the mutation gates use, must produce the golden."""
    a, b = _distinguishing_pairs(3)
    ch = _build_raw_chain()
    if ch is None:
        pytest.skip("the probe-free chain did not route on this run")
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (
        f"the UNMUTATED block must produce the golden through the mutation "
        f"gates' own path; got {ch.out}")


def test_the_probing_harness_actually_routes_this_block():
    """ANTI-SKIP GUARD (INV-46 Rule 4a), load-bearing: ``_build_chain`` SKIPS
    when no anchor survives its smoke probe — right for a flaky CP-SAT run,
    dangerous for a broken block, which fails the probe at EVERY anchor and
    turns the suite into skips that read as green. This test FAILS — never
    skips — if the probing path cannot produce a working chain."""
    ch = _build_raw_chain()
    assert ch is not None, (
        "NO anchor routed the two-upstream Clarke chain at all — every gate "
        "in this file that calls _build_chain would SKIP; treat as a hard "
        "failure")
    a, b = _distinguishing_pairs(3)
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (
        "NO correctly-pairing chain: every probing gate in this file would "
        "SKIP, so the suite could read 'passed' while the block is broken. "
        f"Got {ch.out}, expected {_ref(a, b)}")


# --------------------------------------------------------------------------- #
#  STRUCTURE — the load-bearing construction claims                            #
# --------------------------------------------------------------------------- #

def test_distinct_input_faces_are_declared_and_reconciled():
    """The block must declare BOTH the face-lock flag AND the (port, face-word)
    pairs the build's face-reconciliation pass needs — without them the pass
    falls back to the DualFloatToComplex names and becomes a SILENT NO-OP
    (builds + routes + zero output; INV-46 Rule 1)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    assert ClarkeTransformBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = ClarkeTransformBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("ia", "face_ia"), ("ib", "face_ib")), spec
    cp = ClarkeTransformBlock("x").build_cell_programs()[0]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports, (pn, in_ports)
        assert wn in face_words, (wn, face_words)


def test_same_face_construction_raises():
    """Two producers on ONE face cannot be told apart by the arbiter, so the
    constructor RAISES rather than silently building a block that mis-pairs
    forever (INV-0: never clamp a hardware limit silently)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    with pytest.raises(ValueError, match="face_ia and face_ib must differ"):
        ClarkeTransformBlock("x", face_ia="west", face_ib="west")
    with pytest.raises(ValueError, match="face_ia and face_ib must differ"):
        ClarkeTransformBlock("x", face_ia="north", face_ib="north")


def test_boots_pre_locked_with_no_arm_entry():
    """COLD START IS BAKED (initial_lock_face); NO arm entry (arming via a
    JUMP is a race); each input port resolves its OWN entry (without that,
    every producer resolves the single default entry, got_ib never runs, and
    the rendezvous deadlocks with 0 egress)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    cp = ClarkeTransformBlock("x").build_cell_programs()[0]
    assert cp.initial_lock_face is not None
    entries = [e.name for e in cp.entries]
    assert entries == ["got_ia", "got_ib"], entries
    assert "arm" not in entries
    assert {p.name: p.entry for p in cp.inputs} == {
        "ia": "got_ia", "ib": "got_ib"}


def test_block_declares_two_output_registers():
    """TWO output registers — load-bearing: the build keys every complex
    2-rail patcher (abutted, brokered, output-port) on
    ``len(output_registers) > 1``. One register would collapse both rails
    onto one downstream register (the INV-23 orientation bug class)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    b = ClarkeTransformBlock("x")
    assert b.interface.output_registers == [0, 1], b.interface.output_registers


def test_is_a_single_cell_with_no_internal_handoffs():
    """THE FACE BUDGET (INV-46 Rule 2): N=2 arms + 1 forward = 3 of 4 faces,
    so the whole rendezvous + datapath is ONE cell — no internal datapath, no
    serialize-LOCK release corridor, no WRITE.CFG."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    b = ClarkeTransformBlock("x")
    assert b.cell_count == 1
    assert not (b.internal_connections() or [])
    tmpl = b.build_cell_programs()[0].assembly_template
    assert "WRITE.CFG" not in tmpl


def test_every_cell_fits_its_register_budget():
    """INV-33 static gate: no data address and no state/input register at or
    above ``31 - instr_count``, and every StateVar PINNED (an unpinned one
    lands on top of R0 and the inputs; a cell at exactly 32/32 pins state on
    its own first instruction and dies after one sample)."""
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    for cid, cp in ClarkeTransformBlock("x").build_cell_programs().items():
        base = 31 - R.count_instructions(cp)
        for d in cp.data:
            assert d.address < base, (cid, d.name, d.address, base)
        for sv in cp.state:
            assert sv.register is not None, (
                f"{cid}: state '{sv.name}' is UNPINNED (INV-33)")
            assert sv.register < base, (cid, sv.name, sv.register, base)
        for p in cp.inputs:
            if p.register is not None:
                assert p.register < base, (cid, p.name, p.register, base)


def test_built_cell_boots_locked_on_chip():
    """The cold-start LOCK is in the BITSTREAM, not merely declared: load the
    built chip and confirm the cell's boot CONFIG has the LOCK bit set before
    a single word is injected."""
    import simkyt
    ch = _build_chain()
    c0 = ch.ctrl.project.block(ch.blk).placement.cells[0]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(ch.bres.words(0))
    boot_cfg = chip.read_config(chip.cell_id_at(c0.x, c0.y))
    # LOCK is CONFIG bit 14 (0x4000) in the packed config word.
    assert boot_cfg & 0x4000, (
        f"the rendezvous cell must BOOT already LOCKED — boot CONFIG "
        f"0x{boot_cfg:04X} has LOCK clear")


def test_built_cell_rotates_the_lock_and_emits_one_packet():
    """STRUCTURAL proof of the construction in the BUILT memory: TWO
    LOCK_FACE writes (the ia -> ib -> ia rotation, one per entry — these are
    Move-opcode words, untouched by the handoff patchers), exactly TWO data
    WRITEs (the yi/yq rails) + ONE trigger JUMP (one complex packet per
    pair), and the MULQ really present (not optimised into a MOVE)."""
    import simkyt
    from gr_kyttar.placement.blocks import ClarkeTransformBlock
    from gr_kyttar.placement.resolver import CellProgramResolver
    ch = _build_chain()
    c0 = ch.ctrl.project.block(ch.blk).placement.cells[0]
    mem = ch.bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    # Restrict the scan to the INSTRUCTION address range: the disassembler
    # cannot tell data from instructions, and the block's satpos DATA word
    # (0x7FFF at a low address) disassembles as a plausible-looking Jump —
    # counting it would make this gate fire on a healthy cell (measured).
    cp = ClarkeTransformBlock("x").build_cell_programs()[0]
    base = 31 - CellProgramResolver().count_instructions(cp)

    def _addr(line: str) -> int:
        head = line.strip().split(":", 1)[0]
        try:
            return int(head, 16)
        except ValueError:
            return -1
    instr_lines = [l for l in dis.splitlines() if base <= _addr(l) < 31]
    lock_moves = [l for l in instr_lines if "Move {" in l and "dest: 35" in l]
    data_writes = [l for l in instr_lines
                   if "Write {" in l and "config: false" in l]
    jumps = [l for l in instr_lines if "Jump {" in l]
    assert len(lock_moves) == 2, (
        f"expected TWO LOCK_FACE writes (the ia->ib->ia rotation); got "
        f"{len(lock_moves)}:\n{dis}")
    assert len(data_writes) == 2, (
        f"expected exactly TWO data WRITEs (yi, yq); got "
        f"{len(data_writes)}:\n{dis}")
    assert len(jumps) == 1, f"expected ONE trigger JUMP; got {len(jumps)}:\n{dis}"
    assert any("Mul" in l for l in instr_lines), f"no MULQ in the built cell:\n{dis}"


def test_both_inputs_are_advertised_and_grc_binding_matches():
    """The block must present BOTH phase currents as external input ports, and
    the GRC binding must list the same two (float) plus ONE complex output —
    otherwise GRC import cannot wire the second producer or the alpha-beta
    pair."""
    import yaml
    from engine.catalog import BlockCatalog
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    pm = BlockCatalog.from_gr_kyttar().port_map(
        "ClarkeTransformBlock", {}, library=LIB)
    ins = [p.name for p in pm.ports if p.direction == "in"]
    assert ins == ["ia", "ib"], ins
    y = yaml.safe_load(
        (Path(__file__).resolve().parents[2]
         / "gr-kyttar" / "grc" / "kyttar_clarke_transform.block.yml").read_text())
    assert [i["label"] for i in y["inputs"]] == ["ia", "ib"], y["inputs"]
    assert all(i["dtype"] == "float" for i in y["inputs"])
    assert len(y["outputs"]) == 1 and y["outputs"][0]["dtype"] == "complex"


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_pairs_correctly_from_every_anchor(anchor):
    """PLACEMENT ROBUSTNESS: each anchor gives the arms a DIFFERENT
    arrival-face geometry, and the build's face-reconciliation pass has to
    patch the authored placeholder faces to whatever the router chose in each
    case. An anchor that routes must also PAIR — routes-but-emits-nothing is
    the face-reconciliation no-op signature. Anchors that do not route on a
    given CP-SAT run are skipped (routability of a hand-anchor is a placer
    property; pairing is this block's)."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    (ka_xy, kb_xy, j_xy) = anchor
    built = None
    for _attempt in range(3):
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(CHIP_YAML)
        ctk = getattr(ct, "name", None) or "kyttar_10x12"
        ctrl = AppController(catalog=cat)
        ctrl.new_project("clarke_anchor", ctk)
        ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                              params={"n": _ARM_N})
        kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                              params={"n": _ARM_N})
        j = ctrl.place_block("ClarkeTransformBlock", 0, *j_xy, library=LIB,
                             params={})
        _wire(ctrl, CPE, BE, ka, kb, j)
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
        built = _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
        break
    if built is None:
        pytest.skip(f"anchor {anchor} did not route on this run")
    a, b = _distinguishing_pairs(3)
    for av, bv in zip(a, b):
        built.sample(av, bv)
    assert built.out == _ref(a, b), (
        f"anchor {anchor} routed but did NOT pair correctly: {built.out}")


# --------------------------------------------------------------------------- #
#  Dashboard report                                                            #
# --------------------------------------------------------------------------- #

def test_emit_report():
    """Emit the dashboard report. The metric is EXACT — the golden is the
    integer model of the shipped arithmetic, and every emitted word (both
    rails, saturation corners included) must equal it bit for bit; there is
    no tolerance to spend on-chip (the derived <=5 LSB float bound is a claim
    about the MODEL, gated separately above)."""
    ch = _build_chain()
    a = [0x1000, 0x7FFF, 0x8000, 0xF234, 0x0400, 0x7FFF, 0x2345, 0x8000]
    b = [0x0800, 0x7FFF, 0x8000, 0x1111, 0xFC00, 0x8000, 0x6543, 0x7FFF]
    for k, (av, bv) in enumerate(zip(a, b)):
        ch.sample(av, bv, b_first=bool(k % 2))
    ref = _ref(a, b)
    assert ch.out == ref, (ch.out, ref)
    res = compare_against_grc(
        ch.out, [_s16(w) / 32768.0 for w in ref], metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("ClarkeTransformBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 0, "mutation": True,
        "on_chip_two_producer_chain": True, "async_interleavings": 2,
        "saturated": True, "orientations": 8, "saturation_corners": True,
        "full_scale_sweep": True})
