# SPDX-License-Identifier: GPL-3.0-or-later
"""XorJoinBlock — bitwise XOR of TWO INDEPENDENT producers, verified ON CHIP.

WHY THERE IS NO GNU RADIO COUNTERPART. The FUNCTION is stock (``blocks.xor_bb``
is ``a ^ b``) but the PROBLEM is not: in GR the scheduler aligns two input
streams for you, so "which stream did this word come from" is never a question.
On the clockless Kyttar array two independent producers fire at asynchronous
times and the ONLY stream identity available is the physical channel — the
arrival FACE. ``XorBlock`` (already shipped and verified bit-exact vs
``xor_bb``) computes the same function but takes both operands from ONE source
cell via the complex-burst fan-in, so it cannot serve the case this block exists
for: a stream cipher's ``plaintext XOR keystream``, where the two operands come
from two SEPARATE on-chip chains.

So the golden is a Python reference of the pinned contract
(``XorJoinBlock.process_reference_words``) compared word-for-word against the
real chip — and, because the FUNCTION is stock, cross-checked against a LIVE
``blocks.xor_bb`` so the reference itself cannot drift.

XOR MAKES MIS-PAIRING SILENT, which is what makes this block's rendezvous worth
proving rather than assuming: ``a[1] ^ a[0]`` is a perfectly plausible-looking
byte, so a desynced join produces well-formed garbage rather than an obvious
failure. Every stimulus below is therefore chosen so that a mis-pairing CHANGES
THE OUTPUT (asserted, not assumed).

WHAT IS PROVEN (all on the real placed + routed + built chip, real simulator):
  * EXACT vs the golden on random byte pairs, over 3 seeds, plus named edges.
  * ADVERSARIAL ASYNC INTERLEAVING — both relative arrival orders and random
    per-sample orders over 3 seeds produce the IDENTICAL stream.
  * SELF-INVERSE — ``(x ^ k) ^ k == x`` on chip, the property the cipher
    decrypts with.
  * STARTUP / STALL — no output until both arms have spoken; a starved arm
    stalls and recovers.
  * SATURATION (INV-19) — the whole burst driven back-to-back with no
    inter-sample quiescence equals the per-sample result.
  * ORIENTATION (INV-23) — identical output in all 8 D4 orientations.
  * MUTATIONS (INV-4) — dropped re-lock, swapped face constants, emit before
    latching the second operand, and more, each proven to FAIL.

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_xor_join.py -q
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap
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


def _ref(a_words, b_words):
    from gr_kyttar.placement.blocks import XorJoinBlock
    return XorJoinBlock.process_reference_words(a_words, b_words)


# --------------------------------------------------------------------------- #
#  The REAL two-upstream chain: two INDEPENDENT identity/decimating arms.      #
# --------------------------------------------------------------------------- #
#
# Each arm is a KeepOneInN, fed from the ONE chip input port by its OWN net, so
# each arm has its OWN input landing (hop + entry + data address). Driving one
# arm's landing advances ONLY that arm — which is what lets the harness produce
# ANY relative arrival order, including orders the auto-placer would never
# generate. That is exactly the adversarial async interleaving the LOCK
# rendezvous must survive.

_ARM_N = 2      # each arm emits on every 2nd raw sample (GR's phase n-1)

_ANCHORS = [((2, 2), (2, 6), (5, 4)), ((1, 1), (1, 5), (4, 3)),
            ((2, 1), (2, 5), (5, 3)), ((3, 2), (3, 6), (6, 4))]


class _Chain:
    """A built two-upstream XOR-join chain + a driver that fires ONE arm."""

    def __init__(self, bres, chip, la, lb, ctrl=None, blk=None):
        self.bres, self.chip, self.la, self.lb = bres, chip, la, lb
        self.ctrl, self.blk = ctrl, blk
        self.out: list[int] = []

    def raw(self, arm: str, value: int):
        """Push ONE RAW sample into the named arm ('a' or 'b'); the arm's
        KeepOneInN emits (and drives the join) on every _ARM_N-th one."""
        land = self.la if arm == "a" else self.lb
        hop = int(land["hop"]) & 0x1F
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop,
                                       target_addr=int(land["data_addrs"][0]))
        self.chip.run(max_events=6000)
        self.chip.inject_jump_physical(target_hop_cnt=hop,
                                       entry_addr=int(land["entry"]))
        self.chip.run(max_events=300000)
        self._drain()

    def emit(self, arm: str, value: int):
        """Make the named arm EMIT exactly one word equal to ``value``: feed it
        _ARM_N raw samples whose LAST is ``value`` (KeepOneInN keeps the last of
        each group of n)."""
        for _ in range(_ARM_N - 1):
            self.raw(arm, 0)
        self.raw(arm, value)

    def sample(self, av: int, bv: int, b_first: bool = False):
        """Drive one complete (a, b) pair in the given relative order."""
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
            self.chip.run(max_events=8000)


def _build_chain(orient=None):
    """Build 2 KeepOneInN arms -> XorJoin -> x16_out on ONE 10x12 chip.

    auto_pnr is a CP-SAT search and is not deterministic across runs, so try a
    few anchor sets rather than pinning one: the block's correctness must not
    depend on a lucky layout, and the anchors that DO route exercise different
    arrival-face geometries, which is itself coverage."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("xorjoin_chain", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("XorJoinBlock", 0, *j_xy, library=LIB,
                                 params={})
            if orient:
                # Rotate/mirror the join BEFORE routing (INV-23): the nets are
                # still unrouted logical connections, so OrientBlockCommand is
                # the right primitive (it preserves them for the router).
                from commands import OrientBlockCommand
                for kind in orient:
                    OrientBlockCommand(ctrl.project, j, kind).execute()
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
            # The two arms MUST have DISTINCT landings, else the harness cannot
            # drive them independently and every interleaving test is vacuous.
            sig = {(int(il[k]["hop"]), int(il[k]["entry"]),
                    int(il[k]["data_addrs"][0])) for k in ("n0", "n1")}
            if len(sig) < 2:
                continue
            # SMOKE the built layout on a THROWAWAY chip before handing the real
            # one to a gate (INV-46 Rule 4). auto_pnr is a CP-SAT search and is
            # not deterministic; a layout can route, build, and present two
            # distinct landings and still deliver an arm somewhere the
            # rendezvous cannot accept, so the block emits nothing (or XORs a
            # stale operand). Without this probe such a layout surfaces as an
            # INTERMITTENT failure of whichever gate happened to draw it — which
            # is indistinguishable from a real block bug.
            #
            # The probe MUST use its own chip instance: driving a pair advances
            # the lock rotation and latches the a operand, so smoking the chip a
            # gate is about to use would leak the probe's values into that
            # gate's FIRST result.
            #
            # The probe values are ASYMMETRIC and all four XORs DISTINCT, so a
            # mis-delivered arm cannot pass by coincidence (a symmetric probe
            # like 0xAA/0x55 XORs to the same 0xFF whichever way it pairs).
            probe_chip = simkyt.Chip.from_yaml(CHIP_YAML)
            probe_chip.load_bitstream_physical(bres.words(0))
            probe_chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            probe = _Chain(bres, probe_chip, il["n0"], il["n1"])
            pa, pb = [0x12, 0x34, 0xF0], [0x00, 0xFF, 0x0F]
            for k, (av, bv) in enumerate(zip(pa, pb)):
                probe.sample(av, bv, b_first=bool(k % 2))
            if probe.out != _ref(pa, pb):
                continue
            chip = simkyt.Chip.from_yaml(CHIP_YAML)
            chip.load_bitstream_physical(bres.words(0))
            chip.set_port_entry_address("x16_in", int(il["n0"]["entry"]))
            return _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
    pytest.skip("no anchor routed the two-upstream XOR-join chain on this run")


# --------------------------------------------------------------------------- #
#  The GOLDEN is the stock function — cross-check it against LIVE xor_bb       #
# --------------------------------------------------------------------------- #

_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")


def _live_xor_bb(a_words, b_words):
    """Run GNU Radio's ``blocks.xor_bb`` in the GR interpreter (a separate
    process, so its NumPy never clashes with the venv's) and return the byte
    stream. Returns None if GNU Radio is not importable."""
    script = textwrap.dedent("""
        import sys, json
        from gnuradio import gr, blocks
        a, b = json.loads(sys.argv[1]), json.loads(sys.argv[2])
        tb = gr.top_block()
        sa = blocks.vector_source_b(a, False)
        sb = blocks.vector_source_b(b, False)
        x = blocks.xor_bb()
        snk = blocks.vector_sink_b()
        tb.connect(sa, (x, 0)); tb.connect(sb, (x, 1)); tb.connect(x, snk)
        tb.run()
        print(json.dumps(list(snk.data())))
    """)
    import json
    try:
        r = subprocess.run(
            [_GR_PYTHON, "-c", script, json.dumps([int(v) for v in a_words]),
             json.dumps([int(v) for v in b_words])],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_golden_matches_live_gnuradio_xor_bb():
    """The FUNCTION this block computes IS stock. Even though no GR block can
    express the two-INDEPENDENT-producer rendezvous, the arithmetic must agree
    with a LIVE ``blocks.xor_bb`` over the whole byte domain — otherwise every
    comparison in this file is measuring the wrong thing. (Skipped, not failed,
    if GNU Radio is not installed: the golden is independently pinned below.)"""
    rng = random.Random(2024)
    a = [rng.randrange(256) for _ in range(512)]
    b = [rng.randrange(256) for _ in range(512)]
    live = _live_xor_bb(a, b)
    if live is None:
        pytest.skip(f"GNU Radio not importable via {_GR_PYTHON}")
    assert live == _ref(a, b), "the golden disagrees with live blocks.xor_bb"


def test_golden_is_pinned_independently():
    """Pin the golden's own contract so a silent change to the reference cannot
    slip through even without GNU Radio present."""
    assert _ref([0x00, 0xFF, 0xAA, 0x0F], [0x00, 0xFF, 0x55, 0xF0]) == [
        0x00, 0x00, 0xFF, 0xFF]
    # Truncation to the SHORTER arm is the stall semantics, in the reference.
    assert _ref([1, 2, 3], [1]) == [0]
    assert _ref([1], [1, 2, 3]) == [0]
    assert _ref([], [1, 2]) == []


def test_golden_matches_the_shipped_xor_block():
    """The block computes the SAME function as the already-verified
    ``XorBlock`` (which is itself gated bit-exact against ``xor_bb``); only the
    INPUT topology differs. If these two references ever disagree, one of them
    has drifted."""
    from gr_kyttar.placement.blocks import XorBlock
    a = [0, 1, 127, 128, 255, 0xAA, 0x0F, 0x5A]
    b = [0, 255, 128, 127, 255, 0x55, 0xF0, 0xA5]
    assert _ref(a, b) == XorBlock("x").process_reference_bytes(a, b)


# --------------------------------------------------------------------------- #
#  EXACT vs the golden — edges + random over >=3 seeds                         #
# --------------------------------------------------------------------------- #

def test_named_edge_pairs():
    """EDGE coverage: the identity (x ^ 0), the self-annihilation (x ^ x = 0),
    the complement (x ^ 0xFF), the classic bit-interleave pair, the nibble
    swap, and the 16-bit extremes — the whole word, not just the low byte."""
    ch = _build_chain()
    a = [0x00, 0xFF, 0xFF, 0xAA, 0x0F, 0x5A, 0x7FFF, 0x8000, 0xFFFF]
    b = [0x00, 0xFF, 0x00, 0x55, 0xF0, 0xA5, 0x8000, 0x7FFF, 0x0001]
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (ch.out, _ref(a, b))


@pytest.mark.parametrize("seed", [7, 43, 911])
def test_random_byte_pairs_are_exact(seed):
    """RANDOM byte pairs over >=3 seeds (the coverage bar), EXACT vs the
    golden. The stimulus is filtered so no two samples share an XOR result,
    which is what makes a mis-pairing between samples visible rather than
    silent."""
    rng = random.Random(seed)
    ch = _build_chain()
    a, b, seen = [], [], set()
    while len(a) < 8:
        av, bv = rng.randrange(256), rng.randrange(256)
        if (av ^ bv) in seen:
            continue
        seen.add(av ^ bv)
        a.append(av)
        b.append(bv)
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (ch.out, _ref(a, b))


# --------------------------------------------------------------------------- #
#  SELF-INVERSE — the property the stream cipher decrypts with                 #
# --------------------------------------------------------------------------- #

def test_self_inverse_round_trip_on_chip():
    """``(x ^ k) ^ k == x`` — ENCRYPT then DECRYPT with the same keystream and
    recover the plaintext, both halves computed BY THE BLOCK ON CHIP.

    This is the whole reason the block exists: the same block is the encrypt
    half and the decrypt half of a stream cipher. Running the ciphertext back
    through a second chain with the same keystream must return the plaintext
    exactly."""
    plain = [0x41, 0x00, 0xFF, 0x7E, 0x13, 0xA5]
    keys = [0x5C, 0xFF, 0x01, 0x00, 0xC3, 0x5A]

    enc = _build_chain()
    for pv, kv in zip(plain, keys):
        enc.sample(pv, kv)
    cipher = list(enc.out)
    assert cipher == _ref(plain, keys), cipher
    # Non-vacuity: the ciphertext is genuinely NOT the plaintext (a block that
    # forwarded `a` unchanged would round-trip trivially and pass a naive test).
    assert cipher != plain, (
        "the keystream did not change the plaintext — this round-trip would be "
        "vacuous")

    dec = _build_chain()
    for cv, kv in zip(cipher, keys):
        dec.sample(cv, kv)
    assert dec.out == plain, (
        f"decrypt did not recover the plaintext: {dec.out} != {plain}")


def test_self_inverse_holds_with_the_operands_swapped():
    """XOR is COMMUTATIVE, so the cipher works whichever arm carries the
    keystream: feeding (key, plain) must give the same ciphertext as
    (plain, key). Proven on chip, because commutativity of the FUNCTION does
    not by itself prove the two ARMS are treated symmetrically."""
    plain = [0x41, 0x7E, 0x13]
    keys = [0x5C, 0x00, 0xC3]
    fwd = _build_chain()
    for pv, kv in zip(plain, keys):
        fwd.sample(pv, kv)
    rev = _build_chain()
    for pv, kv in zip(plain, keys):
        rev.sample(kv, pv)          # arms exchanged
    assert fwd.out == rev.out == _ref(plain, keys), (fwd.out, rev.out)


# --------------------------------------------------------------------------- #
#  ADVERSARIAL ASYNC INTERLEAVING — the core rendezvous claim                  #
# --------------------------------------------------------------------------- #

def _distinguishing_pairs(n=6):
    """Pairs where BOTH the per-sample XOR values are all distinct AND any
    cross-sample mis-pairing produces a different stream. Used by the
    interleaving gates so a desync cannot hide behind a coincidence."""
    a = [0x11 + 0x10 * i for i in range(n)]
    b = [0x03 + i for i in range(n)]
    return a, b


@pytest.mark.parametrize("b_first", [False, True],
                         ids=["a-then-b", "b-then-a"])
def test_both_relative_arrival_orders_are_identical(b_first):
    """BOTH relative arrival orders must produce the IDENTICAL stream. This is
    the whole point of the LOCK rotation over a counter: the arbiter holds each
    producer's word until it is that face's turn, so the XOR does NOT depend on
    which producer happened to fire first."""
    a, b = _distinguishing_pairs()
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv, b_first=b_first)
    assert ch.out == _ref(a, b), (
        f"arrival order b_first={b_first} broke the pairing", ch.out, _ref(a, b))


@pytest.mark.parametrize("seed", [3, 17, 91])
def test_random_interleavings_preserve_the_pairs(seed):
    """RANDOM per-sample arrival order over a long run (3 seeds). Whatever order
    the two producers fire in, the emitted stream is exactly the golden."""
    rng = random.Random(seed)
    a, b = _distinguishing_pairs(8)
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv, b_first=rng.random() < 0.5)
    assert ch.out == _ref(a, b), (ch.out, _ref(a, b))


def test_back_to_back_samples_do_not_mix():
    """Back-to-back samples with easily-attributed values: no operand of sample
    k may leak into sample k+1's XOR. The re-lock to face_a is the LAST thing
    got_b does, precisely so the next sample's b word cannot barge in before its
    a word has been latched."""
    ch = _build_chain()
    a = [0x10, 0x20, 0x40, 0x80, 0x01, 0x02]
    b = [0x01, 0x02, 0x04, 0x08, 0x20, 0x40]
    # Every XOR is a DISTINCT 2-bit pattern, so ANY cross-sample mixing shows.
    exp = _ref(a, b)
    assert len(set(exp)) == len(exp), exp
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == exp, (ch.out, exp)


def test_bursty_arms_within_the_overhang_limit():
    """BURSTY arms: arm A runs 2 samples ahead before arm B says anything, then
    B catches up. Both results must come out, correctly paired — the surplus A
    words were HELD, not dropped and not XORed with a stale partner."""
    ch = _build_chain()
    a, b = [0x11, 0x22], [0x03, 0x05]
    for av in a:
        ch.emit("a", av)
    for bv in b:
        ch.emit("b", bv)
    assert ch.out == _ref(a, b), ch.out


# --------------------------------------------------------------------------- #
#  STARTUP + STALL semantics                                                   #
# --------------------------------------------------------------------------- #

def test_startup_emits_nothing_until_both_arms_have_spoken():
    """NO PARTIAL OUTPUT, ever. After arm A alone the chip has produced NOTHING
    — in particular it has NOT emitted the raw ``a`` word, nor ``a ^ 0``. The
    result appears only when B arrives."""
    ch = _build_chain()
    ch.emit("a", 0x42)
    assert ch.out == [], f"a partial/unpaired word leaked out: {ch.out}"
    ch.emit("b", 0x18)
    assert ch.out == [0x42 ^ 0x18], ch.out


def test_starved_arm_stalls_and_never_reuses_an_operand():
    """Arm A supplies TWO words, arm B only ONE. Exactly ONE result may be
    emitted; the surplus A word must be HELD, never XORed with a stale or
    duplicated B (which is how a naive counter fails)."""
    ch = _build_chain()
    ch.emit("a", 0x11)
    ch.emit("a", 0x22)          # surplus — must be held
    ch.emit("b", 0x03)
    assert ch.out == _ref([0x11, 0x22], [0x03]), (
        f"a starved arm must yield exactly one result, got {ch.out}")


def test_starved_arm_recovers_when_the_missing_word_arrives():
    """The stall is a STALL, not a loss: once the missing B word arrives, the
    held A word is XORed with its CORRECT partner and the stream resumes."""
    ch = _build_chain()
    ch.emit("a", 0x11)
    ch.emit("a", 0x22)
    ch.emit("b", 0x03)
    assert ch.out == _ref([0x11, 0x22], [0x03])
    ch.emit("b", 0x05)
    assert ch.out == _ref([0x11, 0x22], [0x03, 0x05]), ch.out


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
    sample enqueued as raw WRITE/DATA/JUMP words via ``queue_words_physical``
    (the real streaming condition — NO inter-sample quiescence ANYWHERE), then
    ONE bounded run."""
    ch = _build_chain()
    stream: list[int] = []
    for av, bv in zip(a_words, b_words):
        for land, val in ((ch.la, av), (ch.lb, bv)):
            hop = int(land["hop"]) & 0x1F
            # KeepOneInN keeps the LAST of each group of n, so feed n-1 zeros
            # then the value — all of it enqueued, none of it settled.
            for raw_v in [0] * (_ARM_N - 1) + [int(val) & 0xFFFF]:
                stream.append(_enc_write(hop, int(land["data_addrs"][0])))
                stream.append(raw_v)
                stream.append(_enc_jump(hop, int(land["entry"])))
    ch.chip.queue_words_physical("x16_in", stream)
    # BOUNDED run, never max_events=None: a livelocking block must FAIL cleanly
    # rather than spin the machine at 100% CPU (the INV-19 harness-safety rule).
    res = ch.chip.run(max_events=cap)
    completed = res.get("completed", True) if isinstance(res, dict) else True
    ch._drain()
    return completed, ch.out


def test_saturated_equals_per_sample():
    """INV-19, the REQUIRED gate. The whole burst is enqueued back-to-back with
    NO inter-sample quiescence — both producers racing at the rendezvous, the
    real streaming condition — and the result must equal the per-sample result,
    with the correct COUNT (1:1, no dropped or duplicated samples).

    THIS BLOCK IS EXPECTED TO PASS, and the reason is structural rather than
    lucky: the arbiter LOCK it already carries IS the serialization INV-19
    prescribes as the fix. A word on the barred face is simply held by the
    arbiter until its turn, so no number of queued samples can pile up inside
    the block — there is no internal datapath for them to pile into, because at
    N=2 the face budget (INV-46: N + 2 = 4) lets the whole rendezvous be ONE
    cell. The N=3 voter, needing five faces and having four, is where this
    becomes a real limit."""
    a, b = _distinguishing_pairs(8)
    exp = _ref(a, b)

    per = _build_chain()
    for av, bv in zip(a, b):
        per.sample(av, bv)
    assert per.out == exp, ("per-sample drive already wrong", per.out, exp)

    completed, out = _saturated_run(a, b)
    assert completed, f"the saturated drive wedged; partial output={out}"
    assert out == exp, (
        f"saturated != per-sample.\n saturated={out}\n per-sample={exp}")
    assert len(out) == len(a), (
        f"wrong output COUNT: {len(out)} words for {len(a)} pairs — the block "
        f"is 1:1 and must neither drop nor duplicate under load")


def test_saturated_drive_is_not_vacuous():
    """NON-VACUITY for the gate above (INV-4 applied to the harness). The two
    arms' words really are enqueued together with no run between them, so the
    producers genuinely race at the rendezvous. The stimulus is chosen so that
    a mis-pairing would be VISIBLE: every sample's XOR is distinct, and every
    CROSS-sample XOR (a[i] ^ b[i+1]) differs from every correct one — asserted
    here, so the gate cannot be satisfied by a desynced stream."""
    a = [0x01, 0x02, 0x04, 0x08]
    b = [0x10, 0x20, 0x40, 0x80]
    exp = _ref(a, b)
    assert len(set(exp)) == len(exp), exp
    shifted = [(a[i] ^ b[i + 1]) & 0xFFFF for i in range(len(a) - 1)]
    assert not (set(shifted) & set(exp)), (
        "a one-sample desync would produce a value the gate accepts — pick "
        "stimulus where cross-sample XORs are disjoint from correct ones")
    completed, out = _saturated_run(a, b)
    assert completed and out == exp, (out, exp)


# --------------------------------------------------------------------------- #
#  INV-23 — ORIENTATION INVARIANCE, all 8 D4 orientations                      #
# --------------------------------------------------------------------------- #
#
# The universal gate (test_orientation_invariance.py) drives blocks through
# harnesses that inject on ONE input port; it cannot drive a TWO-FACE
# rendezvous, which is why neither DualFloatToComplexBlock nor
# FeaturePairJoinBlock nor TMRVoterBlock appears there either. So this block
# carries its own D4 gate, on the REAL two-arm chain.

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
    the two ``is_face`` constants (``face_a``/``face_b``) D4-mapping together
    with the cold-start ``initial_lock_face``: if either failed to transform, the
    LOCK would gate the wrong faces after rotation and the chain would build,
    route, and emit NOTHING."""
    ch = _build_chain(orient=orient)
    a, b = _distinguishing_pairs(5)
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (
        f"orientation {_d4_label(orient)} changed the XOR (or produced "
        f"nothing): got {ch.out}, expected {_ref(a, b)}")


# --------------------------------------------------------------------------- #
#  MANDATORY mutation tests (INV-4) — each corruption MUST be caught           #
# --------------------------------------------------------------------------- #

def test_mutation_dropped_relock_desyncs_after_one_sample():
    """DROP THE RE-LOCK — the first mutation named in the spec. Without the
    final ``LOCK_FACE = face_a`` the cell stays locked to face_b, so from sample
    2 on it consumes TWO b words per turn and the a operand is never refreshed:
    the stream becomes ``a0 ^ b0``, then ``a0 ^ b1``, ... Model it, assert it
    DIVERGES from the golden, then show the real block does NOT diverge over the
    same stimulus."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    # A cell stuck on face_b keeps XORing the STALE latched a0.
    mutated = [(a[0] ^ b[i]) & 0xFFFF for i in range(len(b))]
    assert mutated != good, (
        "the gate cannot see a dropped re-lock — pick stimulus where a stale "
        "`a` operand changes the result")
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == good, ch.out


def test_mutation_swapped_face_constants_is_caught_by_the_ORDER_gate():
    """SWAP THE TWO FACE CONSTANTS — the second mutation named in the spec.

    Careful: because XOR is COMMUTATIVE, swapping which face is ``a`` and which
    is ``b`` does NOT change a correctly-paired result, so a value comparison
    alone can NEVER see this mutation. What it DOES change is which face the
    cell boots locked to — so the ARRIVAL-ORDER contract breaks: with the
    constants swapped the cell waits for the b producer first, and the
    a-then-b interleaving mis-pairs (a's word is barred and held, and the pairs
    shift by one).

    Model that desync, assert it diverges, and show the real block is correct
    under BOTH orders (which the interleaving gates above also assert — this
    test names WHY they are the gate that covers this mutation)."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    # Commutativity: a pure operand swap is INVISIBLE — stated, not assumed.
    assert _ref(b, a) == good, (
        "XOR must be commutative; if this fails the reference is wrong")
    # A cell booted locked to the WRONG face under a-then-b arrival pairs a[i]
    # with b[i-1] once the streams shift by one.
    shifted = [(a[i + 1] ^ b[i]) & 0xFFFF for i in range(len(a) - 1)]
    assert shifted != good[:len(shifted)], (
        "the gate cannot see a boot-face swap — pick stimulus where a one-word "
        "shift changes the result")
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv)                       # a-then-b
    assert ch.out == good, ch.out
    ch2 = _build_chain()
    for av, bv in zip(a, b):
        ch2.sample(av, bv, b_first=True)        # b-then-a
    assert ch2.out == good, ch2.out


def test_mutation_emit_before_latching_the_second_operand_fails():
    """EMIT BEFORE LATCHING THE SECOND OPERAND — the third mutation named in the
    spec. Such a block XORs a STALE ``a`` (never refreshed), so its results are
    ``b`` XORed with whatever was left in the state register.

    THE SUBSTRATE FORM OF THIS MUTATION IS ``got_a`` NOT LATCHING, and it is
    proven on chip by ``test_substrate_mutations_are_all_caught`` below
    (``no_latch_a``, measured to emit the raw ``b`` stream). Recorded here
    because the OBVIOUS form — reordering ``MOVE R0, R{in:b}`` with the ``XOR``
    — is a NO-OP that proves nothing: both input ports are declared at R0, so
    that MOVE assembles to ``MOVE R0, R0``. That was MEASURED (the reordered
    program produced the correct golden), and it is exactly the kind of
    mutation test that certifies nothing while looking rigorous.

    This test keeps the MODEL-level half of the gate: a stale-operand stream
    must diverge from the golden, and the real block must match it."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    # A cell whose `a` is never refreshed XORs every b with the stale first a.
    mutated = [(a[0] ^ b[i]) & 0xFFFF for i in range(len(b))]
    assert mutated != good, (
        "the gate cannot see a stale `a` operand — the stimulus must make a's "
        "CURRENT value matter")
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == good, ch.out


def test_mutation_forwarding_an_operand_unchanged_fails():
    """A block that FORWARDED one operand instead of XORing (a dropped or
    no-op ALU instruction) would still emit one word per pair, at the right
    rate, in the right order. Only the VALUE catches it — so assert the golden
    differs from both raw arms."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    assert good != a and good != b, (
        "the stimulus cannot distinguish an XOR from a pass-through")
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == good, ch.out


def test_mutation_wrong_logic_op_fails():
    """A WRONG LOGIC OP (AND / OR instead of XOR) on the cell ALU. The gate must
    reject those streams — this is the same substrate-level mutation class
    ``XorBlock``'s suite proves, restated for the rendezvous topology."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    for name, fn in (("AND", lambda x, y: x & y), ("OR", lambda x, y: x | y)):
        mutated = [fn(x, y) & 0xFFFF for x, y in zip(a, b)]
        assert mutated != good, f"the gate cannot see a {name}-instead-of-XOR DUT"
    ch = _build_chain()
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == good, ch.out


def test_mutation_empty_output_fails():
    """An empty stream must never satisfy the reference (green is not reachable
    by emitting nothing)."""
    assert [] != _ref([1, 2], [3, 4])


# --------------------------------------------------------------------------- #
#  SUBSTRATE-LEVEL mutations (INV-4, the strong form): corrupt the REAL block, #
#  rebuild it on the REAL chip, and prove the gate rejects the result.         #
# --------------------------------------------------------------------------- #
#
# The model-level mutations above prove the STIMULUS can distinguish a bad
# stream. These prove the GATE catches a genuinely bad BLOCK — the difference
# between "a wrong answer would look wrong" and "a wrong block is caught". Each
# corruption below was MEASURED; the recorded on-chip output is in the table.

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
            ctrl.new_project("xorjoin_mut", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": _ARM_N})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": _ARM_N})
            j = ctrl.place_block("XorJoinBlock", 0, *j_xy, library=LIB,
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
            return _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
    return None


def _sub(template_from, template_to):
    """A build_cell_programs replacement that rewrites the assembly template."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    base = XorJoinBlock.build_cell_programs

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


# (name, mutation, measured on-chip result). "unroutable" means the corrupted
# block does not survive place-and-route at all — also a rejection, and the
# reason these gates must NOT use the probing _build_chain (which would turn
# that into a silent skip).
_SUBSTRATE_MUTANTS = [
    # Drop the XOR: the block degenerates to forwarding `b`.
    ("drop_xor", _sub("    XOR R0, R{state:va}\n", ""), "forwards b"),
    # Wrong ALU op — the XorBlock suite's own mutation class.
    ("and_instead_of_xor",
     _sub("    XOR R0, R{state:va}\n", "    AND R0, R{state:va}\n"), "ANDs"),
    # Never latch `a`: the XOR reads a never-written state register.
    ("no_latch_a", _sub("    MOVE R{state:va}, R{in:a}\n", ""), "stale a"),
    # Drop the re-lock: the rotation stops and the join desyncs after one
    # sample (INV-46 Rule 3 / the spec's first named mutation).
    ("drop_relock",
     _sub("    MOVE [LOCK_FACE], R{data:face_a}\n    HALT\n", "    HALT\n"),
     "desyncs"),
]


@pytest.mark.parametrize("name,mutation,_why",
                         _SUBSTRATE_MUTANTS,
                         ids=[m[0] for m in _SUBSTRATE_MUTANTS])
def test_substrate_mutations_are_all_caught(name, mutation, _why):
    """INV-4 IN ITS STRONG FORM. Corrupt the REAL block, rebuild it on the REAL
    chip, run the REAL simulator, and assert the output does NOT match the
    golden (or that the corrupted block does not survive place-and-route).

    A gate never shown to fail certifies nothing — and a MODEL of a mutation is
    not a mutation. Measured results:

      drop_xor            -> emits the raw ``b`` stream
      and_instead_of_xor  -> emits ``a & b``
      no_latch_a          -> emits the raw ``b`` stream (stale/zero ``a``)
      drop_relock         -> emits 2 words then desyncs, and the layout probe
                             rejects every anchor
    """
    from gr_kyttar.placement.blocks import XorJoinBlock
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    orig = XorJoinBlock.build_cell_programs
    XorJoinBlock.build_cell_programs = mutation
    try:
        ch = _build_raw_chain()
        if ch is None:
            return          # the corrupted block did not even route: rejected
        for av, bv in zip(a, b):
            ch.sample(av, bv)
        got = ch.out
    finally:
        XorJoinBlock.build_cell_programs = orig
    assert got != good, (
        f"the '{name}' mutation ({_why}) produced the CORRECT stream {got} — "
        f"this gate cannot see it, so it certifies nothing")


def test_the_probing_harness_actually_routes_this_block():
    """ANTI-SKIP GUARD, and it is load-bearing.

    ``_build_chain`` SKIPS when no anchor survives its smoke probe — which is
    the right behaviour for a flaky CP-SAT run, and a DANGEROUS one for a broken
    block: a genuinely corrupted block fails the probe at EVERY anchor, so most
    of this file would turn into skips and a casual reading of "N passed" would
    miss it. (Measured: corrupting the block's XOR to an AND turned 35 of these
    tests into skips rather than failures.)

    This test FAILS — never skips — if the probing path cannot produce a working
    chain, so a wholesale collapse into skips can never be mistaken for green."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ok = False
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        cat = BlockCatalog.from_gr_kyttar()
        ctrl = AppController(catalog=cat)
        ctrl.new_project("xorjoin_guard", ctk)
        ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                              params={"n": _ARM_N})
        kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                              params={"n": _ARM_N})
        j = ctrl.place_block("XorJoinBlock", 0, *j_xy, library=LIB, params={})
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
        ch = _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
        a, b = _distinguishing_pairs(3)
        for av, bv in zip(a, b):
            ch.sample(av, bv)
        if ch.out == _ref(a, b):
            ok = True
            break
    assert ok, (
        "NO anchor produced a correctly-pairing XOR-join chain. Every gate in "
        "this file that calls _build_chain would SKIP, so the suite could show "
        "'all passed' while the block is broken. Treat this as a hard failure.")


def test_substrate_mutation_harness_is_not_vacuous():
    """NON-VACUITY for the gates above: the UNMUTATED block, built through the
    SAME probe-free path, must produce the golden. Without this, a
    ``_build_raw_chain`` that always returned None (or always produced garbage)
    would make every mutation "pass" while proving nothing."""
    a, b = _distinguishing_pairs(4)
    ch = _build_raw_chain()
    if ch is None:
        pytest.skip("the probe-free chain did not route on this run")
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    assert ch.out == _ref(a, b), (
        f"the UNMUTATED block must produce the golden through the same path "
        f"the mutation gates use; got {ch.out}")


def test_mutation_one_sample_delay_fails():
    """A +1-sample-delay DUT (the standard harness mutation) must be rejected."""
    a, b = _distinguishing_pairs(4)
    good = _ref(a, b)
    assert [0] + good[:-1] != good


# --------------------------------------------------------------------------- #
#  STRUCTURE — the load-bearing construction claims                            #
# --------------------------------------------------------------------------- #

def test_distinct_input_faces_are_declared_and_reconciled():
    """The block must declare BOTH the face-lock flag AND the (port, face-word)
    pairs the build's face-reconciliation pass needs. Without the pairs the pass
    silently falls back to the DualFloatToComplex ``i``/``q`` names, becomes a
    NO-OP, and the chain builds + routes perfectly while emitting ZERO output —
    the exact silent failure this assertion prevents (INV-46 Rule 1)."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    assert XorJoinBlock.NEEDS_DISTINCT_INPUT_FACES is True
    spec = XorJoinBlock.RENDEZVOUS_FACE_PORTS
    assert spec == (("a", "face_a"), ("b", "face_b")), spec
    b = XorJoinBlock("x")
    cp = b.build_cell_programs()[0]
    in_ports = {p.name for p in cp.inputs}
    face_words = {d.name for d in cp.data if getattr(d, "is_face", False)}
    for (pn, wn) in spec:
        assert pn in in_ports, (pn, in_ports)
        assert wn in face_words, (wn, face_words)


def test_same_face_construction_raises():
    """Two producers on ONE face cannot be told apart by the arbiter, so the
    constructor RAISES rather than silently building a block that mis-pairs
    forever (INV-0: never clamp a hardware limit silently). For an XOR that
    matters more than usual: a mis-paired XOR is a plausible-looking byte, so
    the corruption would be SILENT."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    with pytest.raises(ValueError, match="face_a and face_b must differ"):
        XorJoinBlock("x", face_a="west", face_b="west")
    with pytest.raises(ValueError, match="face_a and face_b must differ"):
        XorJoinBlock("x", face_a="north", face_b="north")


def test_boots_pre_locked_with_no_arm_entry():
    """COLD START IS BAKED. The cell must declare ``initial_lock_face`` (LOCK=1
    + LOCK_FACE=face_a in the boot CONFIG) and must NOT have an arm entry:
    arming via a JUMP is a RACE — a word arriving before the arm-JUMP is
    accepted on an UNLOCKED face and mis-pairs, the exact failure the LOCK
    exists to prevent."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    cp = XorJoinBlock("x").build_cell_programs()[0]
    assert cp.initial_lock_face is not None, (
        "the rendezvous MUST boot pre-locked (initial_lock_face)")
    entries = [e.name for e in cp.entries]
    assert entries == ["got_a", "got_b"], entries
    assert "arm" not in entries
    # Each input port must resolve its OWN entry: without this every producer
    # resolves the single default entry, got_b never runs, and the rendezvous
    # deadlocks with 0 egress.
    assert {p.name: p.entry for p in cp.inputs} == {"a": "got_a", "b": "got_b"}


def test_built_cell_boots_locked_on_chip():
    """The cold-start LOCK is not merely declared — it is in the BITSTREAM. Load
    the built chip and confirm the cell's boot CONFIG has the LOCK bit set
    before a single word is injected."""
    import simkyt
    ch = _build_chain()
    c0 = ch.ctrl.project.block(ch.blk).placement.cells[0]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(ch.bres.words(0))
    boot_cfg = chip.read_config(chip.cell_id_at(c0.x, c0.y))
    # LOCK is CONFIG bit 14 (0x4000) in the packed config word.
    assert boot_cfg & 0x4000, (
        f"the rendezvous cell must BOOT already LOCKED (no arm) — boot CONFIG "
        f"0x{boot_cfg:04X} has LOCK clear")


def test_built_cell_rotates_the_lock_and_emits_one_word():
    """STRUCTURAL proof of the construction. The built cell must WRITE LOCK_FACE
    (CONFIG 3 = dest 35) TWICE — the a -> b -> a rotation, one write per entry —
    and emit exactly ONE data WRITE + ONE JUMP (the block is 1:1, unlike the
    two-burst FeaturePairJoin or the packet-emitting TMR voter)."""
    import simkyt
    ch = _build_chain()
    c0 = ch.ctrl.project.block(ch.blk).placement.cells[0]
    mem = ch.bres.chips[0].cells[(c0.x, c0.y)]["memory"]
    dis = simkyt.Program.from_words("d", list(mem), 0).disassemble()
    assert dis.count("dest: 35") == 2, (
        f"expected TWO LOCK_FACE writes (the a->b->a rotation); "
        f"got {dis.count('dest: 35')}:\n{dis}")
    data_writes = [l for l in dis.splitlines()
                   if "Write {" in l and "config: false" in l]
    jumps = [l for l in dis.splitlines() if "Jump {" in l]
    assert len(data_writes) == 1, (
        f"the block is 1:1 — exactly ONE output WRITE; got {len(data_writes)}:"
        f"\n{dis}")
    assert len(jumps) == 1, f"expected ONE trigger JUMP; got {len(jumps)}:\n{dis}"
    # The XOR really is in the built program (not optimised away into a MOVE).
    assert "Xor" in dis or "XOR" in dis, f"no XOR in the built cell:\n{dis}"


def test_both_operands_land_in_the_same_register_by_design():
    """A MEASURED property that changes how this block must be mutation-tested.

    Both input ports are declared at R0 — correct and deliberate, because each
    operand arrives on its OWN face-gated trigger (the shipped N=2 convention:
    DualFloatToComplex and FeaturePairJoin do the same). The consequence is that
    ``MOVE R0, R{in:b}`` assembles to ``MOVE R0, R0``: it is REDUNDANT, and
    building without it was measured correct under both arrival orders AND under
    the saturated burst.

    It is KEPT deliberately — it makes the operand explicit instead of dependent
    on a register-allocation coincidence, at a cost of one word out of 32 — and
    pinned here so nobody "optimises" it away without knowing that a future
    re-pinning of ``b`` off R0 would then silently XOR the wrong word.

    The other half of this fact is a TESTING one, and it is why this assertion
    exists: reordering that MOVE with the XOR is a NO-OP, so it is USELESS as a
    mutation (measured — the reordered program emitted the correct golden). The
    real mutations are in ``_SUBSTRATE_MUTANTS``."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    cp = XorJoinBlock("x").build_cell_programs()[0]
    regs = {p.name: p.register for p in cp.inputs}
    assert regs == {"a": 0, "b": 0}, (
        f"both operands are expected at R0; got {regs}. If `b` has been "
        f"re-pinned, the `MOVE R0, R{{in:b}}` in got_b is no longer redundant "
        f"— it is now LOAD-BEARING, and the docstring saying otherwise is wrong")
    # The state operand must NOT alias the inputs (INV-33), or the XOR would
    # read its own destination.
    assert [s.register for s in cp.state] == [3], [
        (s.name, s.register) for s in cp.state]


def test_block_declares_exactly_one_output_register():
    """ONE output register: with >1 the build classifies the cell as a COMPLEX
    2-rail source and steers the emit to consecutive registers under one
    trigger."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    b = XorJoinBlock("x")
    assert len(b.interface.output_registers) == 1, b.interface.output_registers


def test_is_a_single_cell_with_no_internal_handoffs():
    """THE FACE BUDGET (INV-46 Rule 2), asserted structurally. An N-arm
    rendezvous needs N (arms) + 1 (forward) + 1 (release corridor) = N + 2
    faces, and a cell has FOUR. At N=2 that is exactly four — which is why this
    block is ONE CELL, and a single cell needs neither a forward nor a
    serialize-LOCK release: there is no internal datapath for samples to pile
    into, so no ``WRITE.CFG`` and no internal connections."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    b = XorJoinBlock("x")
    assert b.cell_count == 1, b.cell_count
    assert not (b.internal_connections() or []), b.internal_connections()
    tmpl = b.build_cell_programs()[0].assembly_template
    assert "WRITE.CFG" not in tmpl, (
        "a single-cell N=2 rendezvous needs no serialize-LOCK release corridor")


def test_every_cell_fits_its_register_budget():
    """INV-33 static gate: no data address and no state/input register may be at
    or above ``31 - instr_count``, and every StateVar must be PINNED. A cell at
    exactly 32/32 words pins state ON TOP of its own first instruction — it
    assembles, loads, runs ONCE, then zeroes the word the next trigger enters
    at (emits one sample, goes quiescent)."""
    from gr_kyttar.placement.blocks import XorJoinBlock
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    for cid, cp in XorJoinBlock("x").build_cell_programs().items():
        base = 31 - R.count_instructions(cp)
        for d in cp.data:
            assert d.address < base, (cid, d.name, d.address, base)
        for sv in cp.state:
            assert sv.register is not None, (
                f"{cid}: state '{sv.name}' is UNPINNED (INV-33: unpinned state "
                f"lands on top of R0 and the inputs)")
            assert sv.register < base, (cid, sv.name, sv.register, base)
        for p in cp.inputs:
            if p.register is not None:
                assert p.register < base, (cid, p.name, p.register, base)


def test_drc_rejects_same_face_input_landing():
    """The build DRC is the hard safety net behind the placer's best-effort
    distinct-face constraint: FORCE both input nets to arrive on the same face
    and the ``dual_input_same_face`` violation MUST fire for this block too (it
    keys on NEEDS_DISTINCT_INPUT_FACES, not on a block name)."""
    from engine.bus_drc import _check_dual_input_same_face
    from model.connection import RoutePoint
    ch = _build_chain()
    blk = ch.ctrl.project.block(ch.blk)
    dc = (blk.placement.cells[0].x, blk.placement.cells[0].y)
    w1, w2 = (dc[0] - 1, dc[1]), (dc[0] - 2, dc[1])
    for c in ch.ctrl.project.connections:
        if (getattr(c.target, "block", None) == blk.name
                and getattr(c.target, "port", None) in ("a", "b")):
            c.route = [RoutePoint(x=w2[0], y=w2[1]),
                       RoutePoint(x=w1[0], y=w1[1]),
                       RoutePoint(x=dc[0], y=dc[1])]
    viols = _check_dual_input_same_face(ch.ctrl.project, ch.ctrl.catalog)
    assert any(v.kind == "dual_input_same_face" for v in viols), (
        f"the DRC MUST fire on a same-face landing; got {[v.kind for v in viols]}")


def test_both_inputs_are_advertised_as_external_ports():
    """The block must present BOTH operands as external input ports, and the
    GRC binding must list the same two — otherwise GRC import (which reads the
    port map) cannot wire the second producer."""
    import yaml
    from engine.catalog import BlockCatalog
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    pm = BlockCatalog.from_gr_kyttar().port_map("XorJoinBlock", {}, library=LIB)
    ins = [p.name for p in pm.ports if p.direction == "in"]
    assert ins == ["a", "b"], ins
    y = yaml.safe_load(
        (Path(__file__).resolve().parents[2]
         / "gr-kyttar" / "grc" / "kyttar_xor_join.block.yml").read_text())
    assert [i["label"] for i in y["inputs"]] == ["a", "b"], y["inputs"]
    assert len(y["outputs"]) == 1, y["outputs"]


@pytest.mark.parametrize("anchor", _ANCHORS)
def test_pairs_correctly_from_every_anchor(anchor):
    """PLACEMENT ROBUSTNESS. Correctness must not depend on a lucky layout: each
    anchor gives the two arms a DIFFERENT arrival-face geometry, and the build's
    face-reconciliation pass has to patch the authored placeholder faces to
    whatever the router chose in each case. An anchor that routes must also
    PAIR — a layout that routes and then emits nothing is the
    face-reconciliation no-op signature.

    Anchors that do not route on a given CP-SAT run are skipped, not failed;
    routability of a hand-anchor is a placer property, pairing is this block's."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    (ka_xy, kb_xy, j_xy) = anchor
    built = None
    for _attempt in range(3):
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(CHIP_YAML)
        ctk = getattr(ct, "name", None) or "kyttar_10x12"
        ctrl = AppController(catalog=cat)
        ctrl.new_project("xorjoin_anchor", ctk)
        ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                              params={"n": _ARM_N})
        kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                              params={"n": _ARM_N})
        j = ctrl.place_block("XorJoinBlock", 0, *j_xy, library=LIB, params={})
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
        built = _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
        break
    if built is None:
        pytest.skip(f"anchor {anchor} did not route on this run")
    a, b = _distinguishing_pairs(3)
    for av, bv in zip(a, b):
        built.sample(av, bv)
    assert built.out == _ref(a, b), (
        f"anchor {anchor} routed but did NOT pair correctly: {built.out}")


def test_arms_with_DIFFERENT_decimation_rates_still_pair():
    """GENUINELY INDEPENDENT producers: arm A decimates by 2, arm B by 4, so the
    two arms consume raw samples at different rates and emit at different
    wall-clock spacings. The join pairs EMISSIONS, not raw samples, so the
    output must still be exactly the golden.

    This is the shape the block is actually for (a plaintext chain and a
    keystream chain running at their own rates), and it is a stronger claim
    than the equal-rate tests: nothing about the pairing may depend on the two
    producers sharing a rate."""
    import simkyt
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    n_a, n_b = 2, 4
    built = None
    for (ka_xy, kb_xy, j_xy) in _ANCHORS:
        for _attempt in range(3):
            cat = BlockCatalog.from_gr_kyttar()
            ct = load_chip_type(CHIP_YAML)
            ctk = getattr(ct, "name", None) or "kyttar_10x12"
            ctrl = AppController(catalog=cat)
            ctrl.new_project("xorjoin_rates", ctk)
            ka = ctrl.place_block("KeepOneInNBlock", 0, *ka_xy, library=LIB,
                                  params={"n": n_a})
            kb = ctrl.place_block("KeepOneInNBlock", 0, *kb_xy, library=LIB,
                                  params={"n": n_b})
            j = ctrl.place_block("XorJoinBlock", 0, *j_xy, library=LIB,
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
            built = _Chain(bres, chip, il["n0"], il["n1"], ctrl, j)
            break
        if built is not None:
            break
    if built is None:
        pytest.skip("the unequal-rate chain did not route on this run")

    a, b = _distinguishing_pairs(3)
    for av, bv in zip(a, b):
        for _ in range(n_a - 1):        # arm A: n_a raw samples per emission
            built.raw("a", 0)
        built.raw("a", av)
        for _ in range(n_b - 1):        # arm B: n_b raw samples per emission
            built.raw("b", 0)
        built.raw("b", bv)
    assert built.out == _ref(a, b), (
        f"unequal-rate producers (n_a={n_a}, n_b={n_b}) must still pair "
        f"emission-for-emission; got {built.out}")


# --------------------------------------------------------------------------- #
#  Dashboard report                                                            #
# --------------------------------------------------------------------------- #

def test_emit_report():
    """Emit the dashboard report. The metric is EXACT — this block computes a
    BITWISE function, not Q15 arithmetic, so every emitted word must equal the
    reference bit-for-bit; there is no quantization tolerance to spend and an
    amplitude metric would be the wrong claim about what was proven."""
    ch = _build_chain()
    a = [0x00, 0xFF, 0xAA, 0x0F, 0x5A, 0x12, 0x77, 0x01]
    b = [0x00, 0x00, 0x55, 0x3C, 0xA5, 0x00, 0xFF, 0x80]
    for av, bv in zip(a, b):
        ch.sample(av, bv)
    ref = _ref(a, b)
    assert ch.out == ref, (ch.out, ref)
    res = compare_against_grc(
        ch.out, [((w - 0x10000) if w >= 0x8000 else w) / 32768.0 for w in ref],
        metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("XorJoinBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 0, "mutation": True,
        "on_chip_two_producer_chain": True, "async_interleavings": 2,
        "saturated": True, "orientations": 8, "self_inverse": True})
