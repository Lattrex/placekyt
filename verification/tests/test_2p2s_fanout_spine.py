# SPDX-License-Identifier: GPL-3.0-or-later
"""The 2P2S 1->3 FAN-OUT SPINE — the transport de-risk gate for a four-die
"one stream feeds three consumers" example (a secure-link / AEAD shape:
one produced stream must reach a same-chain tail block, a far-chain head
block, and a far-chain tail block).

WHAT IT PROVES, on the real 4-die board wiring (both carrier links), driven
on ``simkyt.MultiChipSimulation`` with the SAME stream resolution the hosted
GRC server uses (``multi_chip_stream_targets``):

  * the achievable 1->3 fan-out REALIZATION: the producing chain egresses ONE
    tagged stream to its chain tail; the FPGA/GRC side (the harness here, the
    flowgraph in an example) re-injects those words as THREE independent
    ingress streams, each with its own per-stream landing:
      - a cross-chip ingress that TRANSITS the head into a TAIL block
        (gain_2p2s stream-B shape) — the "same-chain tail consumer" leg,
      - a head-block stream whose tagged egress TRANSITS the link to the far
        tail port (gain_2p2s stream-A shape),
      - a second cross-chip ingress into the far chain's tail block.
    All four legs are WORD-EXACT and every settle reaches quiescence.

  * the LIMITATION that forces the FPGA-mediated form, pinned as a known-limit
    guard: a single source cell on a head chip CANNOT both feed a far-chip
    block and emit a tagged transit stream from the same output port. The
    inter-chip hop patch pairs the exit with the far landing and the tagged
    arm is silently lost (measured: the far-block leg word-exact, the tagged
    arm ZERO words, in BOTH declaration orders; a relay-splitter variant
    degrades further to untagged zeros). If an engine change ever fixes this,
    the guard test FAILS — that is the signal to upgrade examples to the
    on-chip splitter fan-out.

Every settled run's ``completed`` is read (INV-56's multi-chip idiom — the
multichip run dict carries no per-chip stop_reason; ``completed`` is the
quiescence signal the fft128_2p2s gates use).

Run:
    QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_2p2s_fanout_spine.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = _ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"
pytestmark = pytest.mark.skipif(not CHIP_YAML.exists(),
                                reason="chip yaml absent")


def _simkyt_has_multichip():
    import simkyt
    return hasattr(simkyt.MultiChipSimulation.new("probe", 5.0),
                   "set_port_input_routed")


needs_mc = pytest.mark.skipif(not _simkyt_has_multichip(),
                              reason="simkyt .so predates the multichip work")

#: One byte per 16-bit word (the data_link convention). Values chosen so every
#: leg's expected output differs from the payload AND from every other leg's
#: (0.5x vs 0.25x vs identity are pairwise distinct on these).
PAYLOAD = [0x11, 0x22, 0x33, 0x44, 0x00, 0xFF, 0x7F, 0x80]

TAG_CT, TAG_TXB, TAG_RXA, TAG_RXB = 5, 6, 7, 8


def _gain_golden(words, gain):
    """The on-chip Q15 multiply ``(word * q15(gain)) >> 15`` (floor)."""
    q = int(round(float(gain) * 32768.0))
    return [((int(w) & 0xFFFF) * q) >> 15 for w in words]


def _build(shape):
    """Place + route + build one of the two measured shapes on the 4-die board.

    ``shape="spine"``  — the WORKING realization (four proven per-stream legs).
    ``shape="dual"``   — the LIMIT probe: one splitter cell on the head with
                         two arms, one to a far-chip block, one a tagged
                         egress that should transit to the tail port.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint, RoutePoint
    from model.project import BoardRef
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    ctrl = AppController(catalog=cat)
    ctrl.new_project(f"fanout_{shape}", "kyttar_10x12")
    for _ in range(3):
        ctrl.add_chip()
    ctrl.project.board = BoardRef(name="dev2p2s",
                                  config="resources/boards/dev2p2s.kdb")
    C = ctrl.add_logical_connection
    BE, CPE = BlockEndpoint, ChipPortEndpoint

    if shape == "spine":
        g_txa = ctrl.place_block("GainBlock", 0, 2, 0,
                                 library="lattrex.official",
                                 params={"gain": 0.5})
        g_txb = ctrl.place_block("GainBlock", 1, 1, 0,
                                 library="lattrex.official",
                                 params={"gain": 0.25})
        C(CPE(chip=0, port="x16_in"), BE(block=g_txa, port="sample"),
          name="in_src")
        C(BE(block=g_txa, port="out"), CPE(chip=0, port="x16_out"),
          name="out_ct")
        C(CPE(chip=0, port="x16_in"), BE(block=g_txb, port="sample"),
          name="in_mac_tx")
        C(BE(block=g_txb, port="out"), CPE(chip=1, port="x16_out"),
          name="out_txb")
    else:
        fan = ctrl.place_block("StreamSplitterBlock", 0, 2, 0,
                               library="lattrex.official")
        g_txb = ctrl.place_block("GainBlock", 1, 1, 0,
                                 library="lattrex.official",
                                 params={"gain": 0.5})
        C(CPE(chip=0, port="x16_in"), BE(block=fan, port="x"), name="in_src")
        C(BE(block=fan, port="out"), CPE(chip=0, port="x16_out"),
          name="arm_far_exit")
        C(CPE(chip=1, port="x16_in"), BE(block=g_txb, port="sample"),
          name="arm_far_land")
        C(BE(block=fan, port="out"), CPE(chip=0, port="x16_out"),
          name="out_ct")
        C(BE(block=g_txb, port="out"), CPE(chip=1, port="x16_out"),
          name="out_txb")

    g_rxa = ctrl.place_block("GainBlock", 2, 1, 0, library="lattrex.official",
                             params={"gain": 0.5})
    g_rxb = ctrl.place_block("GainBlock", 3, 1, 0, library="lattrex.official",
                             params={"gain": 0.25})
    C(CPE(chip=2, port="x16_in"), BE(block=g_rxa, port="sample"), name="in_rx")
    C(BE(block=g_rxa, port="out"), CPE(chip=2, port="x16_out"), name="out_rxa")
    C(CPE(chip=2, port="x16_in"), BE(block=g_rxb, port="sample"), name="in_mac")
    C(BE(block=g_rxb, port="out"), CPE(chip=3, port="x16_out"), name="out_rxb")

    rep = ctrl.auto_route_all({"kyttar_10x12": ct})
    # Cross-chip ingress nets (head port -> far-chip block) are refused by the
    # auto-router by design ("cross-chip auto-route not supported yet"); the
    # SHIPPED encoding is a stub route on the head-port cell — exactly what
    # examples/gain_2p2s/gain_2p2s.kyt carries for its streams B and D. The
    # injection hop is composed by the server/build, not the route.
    for r in rep.results:
        if not r.ok:
            assert r.reason == "cross-chip auto-route not supported yet", (
                f"net {r.name} failed for an unexpected reason: {r.reason}")
            conn = next(c for c in ctrl.project.connections if c.name == r.name)
            conn.route = [RoutePoint(0, 0)]
    for c in ctrl.project.connections:
        tag = {"out_ct": TAG_CT, "out_txb": TAG_TXB,
               "out_rxa": TAG_RXA, "out_rxb": TAG_RXB}.get(c.name)
        sid = {"in_src": "src", "in_mac_tx": "mac_tx",
               "in_rx": "rx", "in_mac": "mac"}.get(c.name)
        if tag is not None:
            c.out_tag = tag
        if sid is not None:
            c.stream_id = sid
    ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
    ctrl.add_inter_chip(2, "x16_out", 3, "x16_in")
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors[:5])
    return ctrl, bres, cat


class _Board:
    """The loaded board + the hosted-server stream resolution, driveable."""

    def __init__(self, ctrl, bres, cat):
        from engine.port_config import multi_chip_stream_targets
        from engine.simulator import MultiChipSimEngine

        self.targets = multi_chip_stream_targets(
            ctrl.project, ctrl.registry, cat, build_result=bres)
        eng = MultiChipSimEngine({cid: str(CHIP_YAML) for cid in range(4)})
        eng.connect(0, "x16_out", 1, "x16_in")
        eng.connect(2, "x16_out", 3, "x16_in")
        for cid in range(4):
            eng.load(cid, bres.words(cid))
            landings = list(bres.chips[cid].input_landings.values())
            if landings:
                il = landings[0]
                eng.configure_input_port(
                    cid, "x16_in", entry_addr=il["entry"], hop_count=il["hop"],
                    data_addr=il["data_addrs"][0], routed=True)
        self.eng = eng
        self.completed_all = True

    def _target(self, sid):
        for t in self.targets.values():
            if t.get("stream_id") == sid:
                return t
        raise KeyError(f"stream {sid!r} not resolved")

    def drive(self, sid, words):
        """Per-word WRITE -> pump -> JUMP -> settle at the stream's own landing
        (the transaction the hosted multichip server injects per sample)."""
        t = self._target(sid)
        sim = self.eng._sim
        name = f"chip{int(t['chip_id'])}"
        hop, entry = int(t["hop_count"]), int(t["entry_addr"])
        a0 = int((t.get("data_addrs") or [0])[0])
        for w in words:
            sim.inject_data_physical(name, [int(w) & 0xFFFF], hop, a0)
            self.eng.run(30_000, 20)
            sim.inject_jump_physical(name, hop, entry)
            info = self.eng.run(120_000, 80)
            self.completed_all &= bool(info.get("completed"))

    def drain_tags(self, chip):
        out = {}
        for (v, d, _t) in self.eng._sim.read_port_words_timed(
                f"chip{chip}", "x16_out"):
            out.setdefault(int(d), []).append(int(v) & 0xFFFF)
        return out


@pytest.fixture(scope="module")
def spine():
    return _build("spine")


@pytest.fixture(scope="module")
def driven(spine):
    """ONE clean end-to-end run of the whole spine; every gate reads this."""
    board = _Board(*spine)
    board.drive("src", PAYLOAD)
    tags_a1 = board.drain_tags(1)
    ct = list(tags_a1.get(TAG_CT, []))
    # THE 1->3 FAN-OUT: the recovered ciphertext words feed all three
    # consumers as three independently-landed streams (the FPGA/GRC role).
    board.drive("mac_tx", ct)
    board.drive("rx", ct)
    board.drive("mac", ct)
    tags_a2 = board.drain_tags(1)
    tags_b = board.drain_tags(3)
    return board, ct, tags_a1, tags_a2, tags_b


# =============================================================================
# 1. The spine builds on the whole board
# =============================================================================
@needs_mc
def test_the_spine_places_routes_and_builds_on_the_board(spine):
    ctrl, bres, _cat = spine
    assert len(ctrl.project.chips) == 4
    links = {(ic.from_chip, ic.to_chip)
             for ic in ctrl.project.inter_chip_connections}
    assert links == {(0, 1), (2, 3)}, links
    assert all(bres.chips[cid].cell_count > 0 for cid in range(4)), (
        "every die carries program words (blocks or their delivery corridors)")


# =============================================================================
# 2. All four legs, word-exact, quiescent
# =============================================================================
@needs_mc
def test_the_ct_leg_is_word_exact(driven):
    """Head-processed stream, tagged egress TRANSITING the link to the tail."""
    _b, ct, tags_a1, _a2, _tb = driven
    assert ct == _gain_golden(PAYLOAD, 0.5), (tags_a1, ct)


@needs_mc
def test_the_same_chain_tail_consumer_leg_is_word_exact(driven):
    """Ciphertext re-injected as a cross-chip ingress TRANSITING the head into
    the chain's TAIL block — the same-chain tail consumer (TX-B shape)."""
    _b, ct, _a1, tags_a2, _tb = driven
    assert tags_a2.get(TAG_TXB, []) == _gain_golden(ct, 0.25), tags_a2


@needs_mc
def test_the_far_chain_head_and_tail_consumer_legs_are_word_exact(driven):
    """Ciphertext into the far chain: one stream consumed on the HEAD (egress
    transits to the tail), one transiting the head into the TAIL block."""
    _b, ct, _a1, _a2, tags_b = driven
    assert tags_b.get(TAG_RXA, []) == _gain_golden(ct, 0.5), tags_b
    assert tags_b.get(TAG_RXB, []) == _gain_golden(ct, 0.25), tags_b


@needs_mc
def test_every_settle_reaches_quiescence(driven):
    board, _ct, _a1, _a2, _tb = driven
    assert board.completed_all, (
        "at least one per-word settle did not report completed")


@needs_mc
def test_all_three_consumers_saw_the_same_ciphertext(driven):
    """The fan-out property itself: tags TXB/RXA/RXB are all exact functions
    of the SAME captured tag-CT words (never of the payload directly — 0.5x
    of the payload differs from the payload on every non-zero word)."""
    _b, ct, _a1, tags_a2, tags_b = driven
    assert ct and ct != PAYLOAD
    assert tags_a2.get(TAG_TXB) == _gain_golden(ct, 0.25)
    assert tags_b.get(TAG_RXA) == _gain_golden(ct, 0.5)
    assert tags_b.get(TAG_RXB) == _gain_golden(ct, 0.25)


# =============================================================================
# 3. Teeth: a tampered channel word propagates, and only it
# =============================================================================
@needs_mc
def test_a_tampered_channel_word_reaches_the_consumer(spine):
    """Flip one bit of one word between capture and re-injection (the channel
    role a secure-link example gives the flowgraph): the consumer's output
    differs from the clean golden in EXACTLY that word. This is the INV-4
    tooth for the leg gates — the same drive with corrupted input fails them."""
    board = _Board(*spine)
    board.drive("src", PAYLOAD)
    ct = board.drain_tags(1).get(TAG_CT, [])
    assert ct == _gain_golden(PAYLOAD, 0.5)
    tampered = list(ct)
    tampered[3] ^= 0x10
    board.drive("rx", tampered)
    got = board.drain_tags(3).get(TAG_RXA, [])
    clean = _gain_golden(ct, 0.5)
    assert got != clean, "the tampered word was silently healed"
    diff = [k for k, (a, b) in enumerate(zip(got, clean)) if a != b]
    assert diff == [3], f"tamper at word 3 surfaced at words {diff}"
    assert got == _gain_golden(tampered, 0.5)
    assert board.completed_all


# =============================================================================
# 4. The KNOWN LIMIT that forces the FPGA-mediated realization
# =============================================================================
@needs_mc
def test_known_limit_a_source_cell_cannot_feed_far_block_and_tagged_transit():
    """MEASURED LIMITATION, pinned: one head-chip source cell with two arms —
    one to a far-chip block (exit net + far landing net), one a tagged egress
    meant to transit to the tail port — delivers ONLY the far-block arm. The
    tagged arm produces ZERO words, silently (build ok, routes ok, settles
    completed). Measured identically in both net-declaration orders.

    If this test FAILS on the tagged-arm assertion, the engine has learned to
    honour both arms — upgrade the fan-out examples to the on-chip splitter
    form and retire the FPGA-mediated workaround."""
    board = _Board(*_build("dual"))
    board.drive("src", PAYLOAD)
    tags = board.drain_tags(1)
    assert tags.get(TAG_TXB, []) == _gain_golden(PAYLOAD, 0.5), (
        f"the far-block arm itself broke: {tags}")
    assert tags.get(TAG_CT, []) == [], (
        "the tagged transit arm DELIVERED alongside a far-block arm — the "
        "measured engine limitation this guard pins has been lifted; upgrade "
        f"the fan-out realization (got {tags})")
    assert board.completed_all
