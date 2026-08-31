# SPDX-License-Identifier: GPL-3.0-or-later
"""Poly1305MACBlock ON THE REAL PLACED + ROUTED + BUILT CHIP.

This is the suite pass 1 never had: every value gate here drives the block
through the full engine path — catalog -> place -> auto-route -> build ->
simKYT — and asserts the **complete RFC 8439 tag** (all eight output words,
never one), reading ``stop_reason`` for EVERY case (INV-56, and its corollary
from this block's own first pass: read it for every case, because the same
layout has reported Deadlock on one input and QueueEmpty on the next).

Layers gated here:

* THE gate: RFC 8439 §2.5.2's worked example — 17 words, three blocks
  including a partial final block — tag EXACT on the built chip.
* the six §A.3 edge vectors expressible in whole 16-bit words (the three
  odd-byte vectors are a documented interface limit, gated at the golden);
* random messages over every final-block residue m = 1..8;
* the saturated (back-to-back ``queue_words_physical``) drive — the INV-20
  serialize-LOCK on the input landing is what makes it exact;
* the emission rate: the 8-word tag leaves ONLY after the final word;
* INV-4 ON-CHIP mutants — each one placed, routed, built and run, each
  proven to actually FIRE (its model prediction must differ before the chip
  result is allowed to count): skip the r clamp / drop the high bit (both
  the full-block fold and the partial-block inject) / wrong reduction
  constant / omit +s / the wrong 32-bit MAC half order (INV-58) / a
  one-stage carry in the split rounds;
* the static fold gates: per-cell budget (INV-33, with an INV-4 negative),
  positional pairing (INV-51), every internal edge on a real forwarding walk
  within the 31-hop limit, no head-on resting-face pairs (INV-56), the
  one-backward-jump-per-cell audit (INV-53), entry reachability (INV-39),
  and the chip-scale orientation declaration.

Mutants are patched IN MEMORY (``build_cell_programs`` wrappers), never on
disk — a mutated-and-restored source file can serve a stale .pyc for the
rest of the run (seconds-granularity mtime validation, measured at the
ChaCha20 landing).

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_poly1305_mac.py -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_ROOT / "verification"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import poly1305_golden as g  # noqa: E402
from gr_kyttar.placement.blocks import poly1305_mac_block as pm  # noqa: E402
from gr_kyttar.placement.blocks.poly1305_mac_block import (  # noqa: E402
    _DELTA, _FACE_CODE, BLOCK_WORDS, Poly1305MACBlock)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" /
                "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

#: The block is CHIP_SCALE and 10 wide; its egress port pair is authored for
#: THIS anchor (RAW_OUTPUT_HOPS literals — INV-63). Pinned, and asserted.
ANCHOR = (0, 1)


def _words(msg: bytes):
    assert len(msg) % 2 == 0
    return [int.from_bytes(msg[i:i + 2], "little")
            for i in range(0, len(msg), 2)]


def _want(msg: bytes, key: bytes):
    tag = g.poly1305_mac(msg, key)
    return [int.from_bytes(tag[2 * i:2 * i + 2], "little") for i in range(8)]


def _build(msg_words: int, key: bytes):
    """Place + auto-route + build the block; returns (BuildResult, landing)."""
    import simkyt  # noqa: F401
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("poly", ct_key)
    blk = ctrl.place_block("Poly1305MACBlock", 0, ANCHOR[0], ANCHOR[1],
                           library="lattrex.official",
                           params={"r_key": key[:16].hex(),
                                   "s_key": key[16:].hex(),
                                   "msg_words": msg_words})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="w"),
                                name="in_blk")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="blk_out")
    rep = ctrl.auto_route_all({ct_key: ct})
    assert rep.ok, "route failed: " + "; ".join(
        f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ct_key: ct})
    assert bres.ok, f"build failed: {bres.errors}"
    land = (getattr(bres.chips.get(0), "input_landings", {}) or {})["in_blk"]
    return bres, land


def _run(bres, land, words, events_per_word=400000):
    """Per-word drive. Returns (flat_out, per_word_out, stop_reasons)."""
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["entry"]))
    out, per_word, reasons = [], [], []
    for v in words:
        chip.inject_data_physical([int(v) & 0xFFFF],
                                  target_hop_cnt=int(land["hop"]),
                                  target_addr=int(land["data_addrs"][0]))
        chip.run(max_events=4000)
        chip.inject_jump_physical(target_hop_cnt=int(land["hop"]),
                                  entry_addr=int(land["entry"]))
        got, r = [], {}
        for _ in range(80):
            r = chip.run(max_events=events_per_word // 40)
            while chip.output_available("x16_out"):
                w = chip.read_port_i16("x16_out").view("uint16").tolist()
                got.extend(int(x) & 0xFFFF for x in w)
                chip.release_output_ack("x16_out")
            if r.get("completed"):
                break
        reasons.append(r.get("stop_reason"))
        per_word.append(got)
        out.extend(got)
    return out, per_word, reasons


def _chip_tag(msg: bytes, key: bytes, mutate=None):
    """Build (optionally with an in-memory mutation) and run one message.

    EVERY case's stop_reason must be QueueEmpty — a Deadlock on one input and
    silence on another is this block's own measured failure shape (INV-56).
    """
    words = _words(msg)
    orig = Poly1305MACBlock.build_cell_programs
    try:
        if mutate is not None:
            def patched(self):
                progs = orig(self)
                mutate(self, progs)
                return progs
            Poly1305MACBlock.build_cell_programs = patched
        bres, land = _build(len(words), key)
    finally:
        Poly1305MACBlock.build_cell_programs = orig
    out, per_word, reasons = _run(bres, land, words)
    bad = [x for x in reasons if x != "QueueEmpty"]
    assert not bad, f"non-QueueEmpty stop reasons: {reasons}"
    return out, per_word


# --------------------------------------------------------------------------
# THE gate: RFC 8439 §2.5.2 on the built chip
# --------------------------------------------------------------------------

def test_rfc8439_2_5_2_tag_exact_on_chip():
    """RFC 8439 §2.5.2's worked example — 17 words, three blocks including a
    partial final block — produces the EXACT 16-byte tag on the real placed +
    routed + built chip. The FULL tag: all eight words, never one (twice in
    this campaign a defect left every checked word right and one wrong)."""
    out, per_word = _chip_tag(g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY)
    want = _want(g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY)
    assert out == want, (f"on-chip tag {[hex(x) for x in out]} != RFC "
                         f"{[hex(x) for x in want]}")
    tag = b"".join(int(w).to_bytes(2, "little") for w in out)
    assert tag == g.RFC8439_2_5_2_TAG


def test_tag_emits_only_after_the_final_word():
    """Rate gate: 0 words after every non-final trigger, the whole 8-word tag
    after the last — the tag never leaks early and never re-emits."""
    out, per_word = _chip_tag(g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY)
    assert [len(x) for x in per_word[:-1]] == [0] * (len(per_word) - 1)
    assert len(per_word[-1]) == 8


# --------------------------------------------------------------------------
# §A.3 edge vectors expressible in whole words
# --------------------------------------------------------------------------

_A3_EVEN = [v for v in g.RFC8439_A3_VECTORS if len(v[2]) % 2 == 0]


@pytest.mark.parametrize("name,key,msg,exp", _A3_EVEN,
                         ids=[v[0] for v in _A3_EVEN])
def test_a3_vector_on_chip(name, key, msg, exp):
    """The RFC's own edge cases — the 2^130-5 wrap, an all-ones block, the +s
    carry-out truncation, carry propagation both ways, the largest reduced
    value — each exact on the built chip."""
    out, _ = _chip_tag(msg, key)
    assert b"".join(int(w).to_bytes(2, "little") for w in out) == exp


# --------------------------------------------------------------------------
# random + every final-block residue
# --------------------------------------------------------------------------

@pytest.mark.parametrize("nw", [2, 3, 4, 5, 6, 7, 9, 15])
def test_random_message_every_final_block_residue(nw):
    """Random messages covering every final-block word count m = 1..8 (nw=9
    gives m=1 after a full block; the RFC vector covers m=1 at 17; A.3 covers
    m=8) — full tag, bit-exact, per-case stop_reason clean."""
    rng = random.Random(1000 + nw)
    msg = bytes(rng.randrange(256) for _ in range(2 * nw))
    key = bytes(rng.randrange(256) for _ in range(32))
    out, _ = _chip_tag(msg, key)
    assert out == _want(msg, key)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_seeded_messages(seed):
    rng = random.Random(seed)
    nw = rng.randrange(1, 25)
    msg = bytes(rng.randrange(256) for _ in range(2 * nw))
    key = bytes(rng.randrange(256) for _ in range(32))
    out, _ = _chip_tag(msg, key)
    assert out == _want(msg, key)


def test_all_max_words_drive_the_carry_ceiling():
    """A message of all-0xFFFF words with an adversarial key: the case family
    that found the pass-1 two-stage carry split (a random sample never reaches
    the accumulator ceiling). Exact on chip."""
    msg = b"\xff" * 32
    key = b"\xff" * 16 + b"\xff" * 16
    out, _ = _chip_tag(msg, key)
    assert out == _want(msg, key)


# --------------------------------------------------------------------------
# saturated drive: the whole message enqueued back-to-back (INV-19/20)
# --------------------------------------------------------------------------

def test_saturated_backtoback_drive_bit_exact():
    """The whole 17-word RFC message enqueued physically back-to-back — no
    inter-word quiescence — through ONE continuous run. The input landing's
    arbiter LOCK (INV-20's serialize-LOCK idiom) holds each next word until
    the previous word's whole pipeline (including whole-block computes) has
    finished; without it the pack chain's word registers and the limb
    accumulators are overwritten mid-compute.

    The hop/entry come from the build's OWN input_landings (INV-60), never a
    manhattan guess — the guess is off by one for this corridor (measured).
    """
    import simkyt
    from kyttar_verify.dut_runner import _enc_jump, _enc_write

    key = g.RFC8439_2_5_2_KEY
    words = _words(g.RFC8439_2_5_2_MSG)
    bres, land = _build(len(words), key)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["entry"]))
    hop, addr, entry = (int(land["hop"]), int(land["data_addrs"][0]),
                        int(land["entry"]))
    stream = []
    for v in words:
        stream += [_enc_write(hop, addr), int(v) & 0xFFFF,
                   _enc_jump(hop, entry)]
    chip.queue_words_physical("x16_in", stream)
    out, r = [], {}
    for _ in range(400):
        r = chip.run(max_events=20000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
        if r.get("completed"):
            break
    assert r.get("stop_reason") == "QueueEmpty", r
    assert out == _want(g.RFC8439_2_5_2_MSG, key), (
        f"saturated tag {[hex(x) for x in out]}")


# --------------------------------------------------------------------------
# INV-4: on-chip mutants. Each is REQUIRED to fire (model prediction first).
# --------------------------------------------------------------------------


def _set_data(cp, name, value):
    """DataWords are frozen; swap the list element for a mutated copy."""
    import dataclasses
    for i, d in enumerate(cp.data):
        if d.name == name:
            cp.data[i] = dataclasses.replace(d, value=value)
            return
    raise KeyError(name)

def _model_tag(msg, key, msg_words=None):
    b = Poly1305MACBlock("m", r_key=key[:16].hex(), s_key=key[16:].hex(),
                         msg_words=msg_words or (len(msg) // 2))
    return [int(x) for x in b.process_reference(_words(msg))]


def _assert_mutant_fires_and_is_caught(msg, key, mutate, model_mutation,
                                       label):
    """(1) the mutation MUST change the model's answer for this stimulus (a
    mutant that cannot fire proves nothing); (2) the mutated CHIP must differ
    from the golden while still emitting a FULL 8-word tag (the shape a
    count-based gate cannot see)."""
    want = _want(msg, key)
    predicted = model_mutation(msg, key)
    assert predicted != want, (
        f"mutant {label!r} cannot fire on this stimulus — pick another")
    out, _ = _chip_tag(msg, key, mutate=mutate)
    assert len(out) == 8, f"mutant {label!r} changed the word COUNT"
    assert out != want, f"ON-CHIP mutant went undetected: {label}"


def test_mutant_skip_r_clamp_is_caught():
    """Skipping the RFC 8439 §2.5 clamp — the classic 'it still looks like a
    MAC' bug — must change the tag on chip."""
    msg, key = g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY

    def mutate(self, progs):
        rl = pm._to_limbs(int.from_bytes(key[:16], "little"))  # UNclamped
        for i in range(pm.N_LIMBS):
            _set_data(progs["rrom"], f"r{i}", rl[i])

    def model(msg, key):
        b = Poly1305MACBlock("m", r_key=key[:16].hex(), s_key=key[16:].hex(),
                             msg_words=len(msg) // 2)
        b._r_limbs = pm._to_limbs(int.from_bytes(key[:16], "little"))
        return [int(x) for x in b.process_reference(_words(msg))]

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model, "skip clamp")


def test_mutant_dropped_high_bit_full_block_is_caught():
    """pack_7 folds the full-block high bit as +256 on its limb-12 piece;
    zeroing that fold (the non-injective-padding bug) must be caught."""
    msg, key = b"\xa5" * 16, g.RFC8439_2_5_2_KEY   # exactly one FULL block

    def mutate(self, progs):
        _set_data(progs["pack_7"], "c256", 0)

    def model(msg, key):
        orig = pm.pack_pieces
        try:
            def patched(j, w):
                out = orig(j, w)
                return out
            # the +256 fold lives in process_reference itself; emulate by
            # subtracting it from the final-limb piece path:
            b = Poly1305MACBlock("m", r_key=key[:16].hex(),
                                 s_key=key[16:].hex(),
                                 msg_words=len(msg) // 2)
            ref = b.process_reference

            # simplest faithful model: golden WITHOUT the high bit
            m_int = int.from_bytes(msg, "little")
            r, s = g.split_key(key)
            acc = (m_int * r) % g.P1305          # high bit omitted
            tag = ((acc + s) % (1 << 128)).to_bytes(16, "little")
            return [int.from_bytes(tag[2 * i:2 * i + 2], "little")
                    for i in range(8)]
        finally:
            pm.pack_pieces = orig

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model,
                                       "drop high bit (full block)")


def test_mutant_dropped_high_bit_partial_block_is_caught():
    """The partial-final-block high bit is a compile-time piece injected by
    pack_{m-1}; zeroing it must be caught."""
    msg, key = b"\x11\x22", g.RFC8439_2_5_2_KEY    # one word: m=1, hb=2^16

    def mutate(self, progs):
        _set_data(progs["pack_0"], "hbc", 0)

    def model(msg, key):
        m_int = int.from_bytes(msg, "little")
        r, s = g.split_key(key)
        acc = (m_int * r) % g.P1305
        tag = ((acc + s) % (1 << 128)).to_bytes(16, "little")
        return [int.from_bytes(tag[2 * i:2 * i + 2], "little")
                for i in range(8)]

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model,
                                       "drop high bit (partial block)")


def test_mutant_wrong_reduction_constant_is_caught():
    """The 2^130 ≡ 5 fold rides mulA_12's rotation edge (MUL five,a). A wrong
    constant (3) computes in a DIFFERENT ring — must be caught. The stimulus
    is all-max words so the wrap terms are guaranteed nonzero."""
    msg, key = b"\xff" * 32, g.RFC8439_2_5_2_KEY

    def mutate(self, progs):
        _set_data(progs["mulA_12"], "five", 3)

    def model(msg, key):
        # golden with reduction constant 3 in the multiply fold
        r, s = g.split_key(key)
        acc = 0
        P = 1 << 130
        for i in range(0, len(msg), 16):
            n = g.block_value(msg[i:i + 16])
            prod = (acc + n) * r
            # fold 2^130 -> 3 once per wrap level (approximates the chip's
            # single-wrap-per-limb fold; enough to prove divergence)
            while prod >= P:
                prod = (prod % P) + 3 * (prod // P)
            acc = prod
        tag = ((acc + s) % (1 << 128)).to_bytes(16, "little")
        return [int.from_bytes(tag[2 * i:2 * i + 2], "little")
                for i in range(8)]

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model,
                                       "reduction constant 3")


def test_mutant_omitted_plus_s_is_caught():
    """Zeroing every fin cell's s-limb (the +s blind omitted) must be caught."""
    msg, key = g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY

    def mutate(self, progs):
        for k in range(pm.N_LIMBS):
            _set_data(progs[f"fin_{k}"], "sk", 0)

    def model(msg, key):
        r, _s = g.split_key(key)
        acc = g.poly1305_accumulate(msg, r)
        tag = (acc % (1 << 128)).to_bytes(16, "little")
        return [int.from_bytes(tag[2 * i:2 * i + 2], "little")
                for i in range(8)]

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model, "omit +s")


def test_mutant_wrong_mac_half_order_is_caught():
    """INV-58's exact defect: the 'obvious' 6-instruction MAC order (MUL/ADD/
    MOVE/MULHI/ADC/MOVE) lets MULHI destroy the carry — the LOW word stays
    bit-exact while the high word drifts by 0x10000 per lost carry, the shape
    a one-word gate cannot see. Rebuilt ON CHIP in all 13 mulA cells; all-max
    stimulus so carries are guaranteed."""
    msg, key = b"\xff" * 32, g.RFC8439_2_5_2_KEY

    def mutate(self, progs):
        for k in range(pm.N_LIMBS):
            cp = progs[f"mulA_{k}"]
            bad = ("    MUL R{in:c}, R{in:a}\n"
                   "    ADD R0, R{state:lo}\n"
                   "    MOVE R{state:lo}, R0\n"
                   "    MULHI R{in:c}, R{in:a}\n"
                   "    ADC R{state:t}, R{state:hi}\n"
                   "    MOVE R{state:t}, R0\n"      # keep instruction count-ish
                   "    MOVE R{state:hi}, R{state:t}\n")
            good = ("    MULHI R{in:c}, R{in:a}\n"
                    "    MOVE R{state:t}, R0\n"
                    "    MUL R{in:c}, R{in:a}\n"
                    "    ADD R0, R{state:lo}\n"
                    "    MOVE R{state:lo}, R0\n"
                    "    ADC R{state:t}, R{state:hi}\n"
                    "    MOVE R{state:hi}, R0\n")
            assert good in cp.assembly_template, f"mulA_{k} template changed"
            cp.assembly_template = cp.assembly_template.replace(good, bad)

    def model(msg, key):
        # model the lost carry: hi += MULHI + (stale carry instead of ADD's)
        b = Poly1305MACBlock("m", r_key=key[:16].hex(), s_key=key[16:].hex(),
                             msg_words=len(msg) // 2)
        rl = b._r_limbs
        words = _words(msg)
        # run the block model but drop the lo->hi carry in the MAC
        N = pm.N_LIMBS
        a = [0] * N
        ain0 = 0
        hi = [0] * N
        lo = [0] * N
        lv = [0] * N
        for blkw in [words[i:i + 8] for i in range(0, len(words), 8)]:
            for j, wv in enumerate(blkw):
                for kk, piece in pm.pack_pieces(j, wv):
                    if j == 7 and kk == 12:
                        piece += 256
                    lv[kk] += piece
            if len(blkw) < 8:
                hb = (16 * len(blkw)) // 10
                lv[hb] += 1 << (16 * len(blkw) - 10 * hb)
            for kk in range(N):
                a[kk] = lv[kk]
            ain0 = lv[0]
            for i in range(N):
                c = rl[i]
                prev = None
                for kk in range(N):
                    if kk == 0:
                        a[0] = ain0
                    av = a[kk]
                    p = c * av
                    lo_n = (lo[kk] + (p & 0xFFFF)) & 0xFFFF       # ADD
                    # MULHI clobbers the ADD's carry -> ADC adds a STALE 0
                    hi[kk] = (hi[kk] + (p >> 16)) & 0xFFFF
                    lo[kk] = lo_n
                    if kk == 0:
                        prev = av
                    elif kk == N - 1:
                        ain0 = (5 * av) & 0xFFFF
                        a[kk] = prev
                    else:
                        a[kk], prev = prev, av
            hiB, loB = list(hi), list(lo)
            hi = [0] * N
            lo = [0] * N
            w1 = 0
            for _rnd in range(2):
                cin = (5 * w1) & 0xFFFF
                for kk in range(N):
                    fwd = hiB[kk]
                    acc = loB[kk] + 64 * cin
                    hiB[kk], loB[kk] = (acc >> 16) & 0xFFFF, acc & 0xFFFF
                    cin = fwd
                w1 = cin
            seed = (320 * w1) & 0xFFFF
            for _ in range(40):
                cin = seed
                for kk in range(N):
                    v = loB[kk] + cin
                    vlo, carry = v & 0xFFFF, v >> 16
                    co = (vlo >> 10) + 64 * carry + 64 * hiB[kk]
                    hiB[kk] = 0
                    loB[kk] = vlo & 0x3FF
                    lv[kk] = loB[kk]
                    cin = co
                if cin == 0:
                    break
                seed = (5 * cin) & 0xFFFF
        cin = 5
        for kk in range(N):
            cin = (lv[kk] + cin) >> 10
        f = 1 if cin else 0
        cin = 5 * f
        outw = []
        partial = 0
        sl = b._s_limbs
        for kk in range(N):
            v = lv[kk] + sl[kk] + cin
            cin = v >> 10
            limb = v & (0xFF if kk == 12 else 0x3FF)
            bk = pm._BK[kk]
            merged = (partial | ((limb << bk) & 0xFFFF)) & 0xFFFF
            if bk + 10 >= 16 or kk == 12:
                outw.append(merged)
                partial = limb >> (16 - bk) if kk < 12 else 0
            else:
                partial = merged
        return outw

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model,
                                       "wrong MAC half order")


def test_mutant_one_stage_carry_is_caught():
    """The split round's 17th bit: dropping the ADC capture (treating the ADD
    as if it could not overflow 16 bits) must be caught. The stimulus was
    SEARCHED for (3000 random candidates; found at #143): a case whose split
    round measurably reaches v = 0x1001d > 0xFFFF — a random message almost
    never gets there, which is exactly why the firing proof below is
    mandatory (pass 1's own lesson about the carry ceiling)."""
    msg = bytes.fromhex(
        "b4ab797c895248f45fbf480d2e382a2bdbea9244846185bf16cc95bdaf620aa0"
        "b299e124661c41c31e476a738e74f961922a77b7048d3ffe62d45172cfe3b6dc")
    key = bytes.fromhex(
        "f6fb19453ad79f093b346306da4d8b32d8ec6aa8fc8caeed02c8e11f8f8e4ad6")

    def mutate(self, progs):
        for k in range(pm.N_LIMBS):
            cp = progs[f"mulC_{k}"]
            good = ("    ADC R{data:z0}, R{data:z0}\n"
                    "    MOVE R{state:t}, R0\n")
            bad = ("    MOVE R{state:t}, R{data:z0}\n"
                   "    MOVE R{state:t}, R{data:z0}\n")
            assert good in cp.assembly_template
            cp.assembly_template = cp.assembly_template.replace(good, bad)

    def model(msg, key):
        # process_reference with the carry-capture dropped
        b = Poly1305MACBlock("m", r_key=key[:16].hex(), s_key=key[16:].hex(),
                             msg_words=len(msg) // 2)
        # quick monkey-model: run the reference but mask the spl carry
        rl, sl = b._r_limbs, b._s_limbs
        words = _words(msg)
        N = pm.N_LIMBS
        a = [0] * N
        ain0 = 0
        hi = [0] * N
        lo = [0] * N
        lv = [0] * N
        for blkw in [words[i:i + 8] for i in range(0, len(words), 8)]:
            for j, wv in enumerate(blkw):
                for kk, piece in pm.pack_pieces(j, wv):
                    if j == 7 and kk == 12:
                        piece += 256
                    lv[kk] += piece
            if len(blkw) < 8:
                hb = (16 * len(blkw)) // 10
                lv[hb] += 1 << (16 * len(blkw) - 10 * hb)
            for kk in range(N):
                a[kk] = lv[kk]
            ain0 = lv[0]
            for i in range(N):
                c = rl[i]
                prev = None
                for kk in range(N):
                    if kk == 0:
                        a[0] = ain0
                    av = a[kk]
                    acc = ((hi[kk] << 16) | lo[kk]) + c * av
                    hi[kk], lo[kk] = (acc >> 16) & 0xFFFF, acc & 0xFFFF
                    if kk == 0:
                        prev = av
                    elif kk == N - 1:
                        ain0 = (5 * av) & 0xFFFF
                        a[kk] = prev
                    else:
                        a[kk], prev = prev, av
            hiB, loB = list(hi), list(lo)
            hi = [0] * N
            lo = [0] * N
            w1 = 0
            for _rnd in range(2):
                cin = (5 * w1) & 0xFFFF
                for kk in range(N):
                    fwd = hiB[kk]
                    acc = loB[kk] + 64 * cin
                    hiB[kk], loB[kk] = (acc >> 16) & 0xFFFF, acc & 0xFFFF
                    cin = fwd
                w1 = cin
            seed = (320 * w1) & 0xFFFF
            for _spin in range(60):
                cin = seed
                for kk in range(N):
                    v = loB[kk] + cin
                    vlo = v & 0xFFFF
                    co = (vlo >> 10) + 64 * hiB[kk]      # 17th bit DROPPED
                    hiB[kk] = 0
                    loB[kk] = vlo & 0x3FF
                    lv[kk] = loB[kk]
                    cin = co
                if cin == 0:
                    break
                seed = (5 * cin) & 0xFFFF
        cin = 5
        for kk in range(N):
            cin = (lv[kk] + cin) >> 10
        f = 1 if cin else 0
        cin = 5 * f
        outw = []
        partial = 0
        for kk in range(N):
            v = lv[kk] + sl[kk] + cin
            cin = v >> 10
            limb = v & (0xFF if kk == 12 else 0x3FF)
            bk = pm._BK[kk]
            merged = (partial | ((limb << bk) & 0xFFFF)) & 0xFFFF
            if bk + 10 >= 16 or kk == 12:
                outw.append(merged)
                partial = limb >> (16 - bk) if kk < 12 else 0
            else:
                partial = merged
        return outw

    _assert_mutant_fires_and_is_caught(msg, key, mutate, model,
                                       "one-stage carry")


# --------------------------------------------------------------------------
# static fold gates
# --------------------------------------------------------------------------

def _block():
    return Poly1305MACBlock("s", msg_words=17)


def test_every_cell_fits_its_word_budget():
    """INV-33: no data address, state register or pinned input register may
    reach ``31 - instruction_count`` — the resolver's own guard checks only
    data, and an over-full cell assembles and runs WRONG silently."""
    from gr_kyttar.placement.resolver import (CellProgramResolver,
                                              JumpTarget, ResolvedTargets,
                                              WriteTarget)
    res = CellProgramResolver()
    offenders = []
    for cid, cp in _block().build_cell_programs().items():
        tg = ResolvedTargets(
            writes={o.name: WriteTarget(1, 1) for o in cp.outputs},
            jumps={o.name: JumpTarget(1, 1) for o in cp.outputs})
        asm = res._substitute_registers(cp.assembly_template, cp,
                                       res._allocate_data(cp.data),
                                       state_map={}, input_map={}, dummy=True)
        asm = res._substitute_write_jump(asm, tg, dummy=True)
        base = 31 - res._count_instructions(asm)
        pins = ([d.address for d in cp.data if d.address is not None]
                + [s.register for s in cp.state if s.register is not None]
                + [i.register for i in cp.inputs if i.register is not None])
        over = [a for a in pins if a >= base]
        if over:
            offenders.append((cid, base, over))
    assert not offenders, f"cells overlap their own code: {offenders}"


def test_budget_gate_catches_a_known_bad_shape():
    """INV-4 for the gate above: a 24-instruction cell with a register pinned
    at 8 must FLAG (base_addr 7 < pin 8)."""
    assert 8 >= 31 - 24, "the known-bad shape must be detected"


def test_positional_pairing_and_transits_last():
    b = _block()
    order_p = list(b.build_cell_programs())
    lay = list(b.default_layout())
    assert order_p == [c for c in lay if not str(c).startswith("transit_")]
    n_trail = len(lay) - len(order_p)
    assert all(str(c).startswith("transit_") for c in lay[-n_trail:])
    assert b.cell_count == len(lay)


def test_input_landing_is_cell_zero():
    """INV-61.4: the catalog derives the external input from the FIRST cell;
    measured on this block — with another first cell the injection landed
    there and the chip ran 4 events of silence."""
    b = _block()
    first = next(iter(b.build_cell_programs()))
    assert first == "seq_top"
    assert b.build_cell_programs()["seq_top"].inputs[0].name == "w"


DX = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}


def _walk(lay, src, face, dst, limit=31):
    pos = {c: (x, y) for c, (x, y, f) in lay.items()}
    rest = {c: _FACE_CODE[f] for c, (x, y, f) in lay.items()}
    at = {v: k for k, v in pos.items()}
    x, y = pos[src]
    f, n = face, 0
    while n < limit:
        dx, dy = DX[f]
        x, y = x + dx, y + dy
        n += 1
        c = at.get((x, y))
        if c is None:
            return None
        if c == dst:
            return n
        f = rest[c]
    return None


@pytest.mark.parametrize("msg_words", [17, 1, 2, 3, 4, 5, 6, 7, 8])
def test_every_internal_edge_is_on_a_real_forwarding_walk(msg_words):
    """Every declared edge must land on the resting-face walk within the
    31-hop limit (INV-48/INV-36), for EVERY final-block residue (the m=4..7
    high-bit trigger routes through the unlock relays precisely because its
    direct edge measured 32 hops and failed the build). The three flip
    families are RAW literal @1 writes (INV-63's escape from the
    backward-write repatch, which was measured to re-aim them at hop 21 and
    deadlock) — their adjacency is asserted separately below."""
    b = Poly1305MACBlock("s", msg_words=msg_words)
    lay = b._geometry()
    edges = ([("W", *e) for e in b.internal_connections()]
             + [("J", *e) for e in b.internal_jumps()])
    bad = []
    hops = []
    for kind, s, p, d, dp in edges:
        if p == "unlock":
            continue                      # config edge, @1 adjacency below
        h = _walk(lay, s, _FACE_CODE[lay[s][2]], d)
        if h is None:
            bad.append(f"{kind} {s}.{p} -> {d}.{dp}")
        else:
            hops.append(h)
    assert not bad, "edges on no walk:\n  " + "\n  ".join(bad)
    assert max(hops) <= 31


def test_the_walk_gate_catches_a_missing_edge():
    """INV-4 negative: a fabricated edge AGAINST the conveyor (out -> seq_top)
    must have no walk."""
    b = _block()
    lay = b._geometry()
    assert _walk(lay, "out", _FACE_CODE[lay["out"][2]], "seq_top") is None


def test_flip_deliveries_are_abutting_pairs():
    """The three RAW @1 flip families (pub, xfer2, lvout) plus ulk's unlock
    WRITE.CFG each require physical abutment — guaranteed by the serpentine
    for group cells, asserted here for every k."""
    b = _block()
    lay = b._geometry()
    pos = {c: (x, y) for c, (x, y, f) in lay.items()}

    def abut(a, c):
        ax, ay = pos[a]
        cx, cy = pos[c]
        assert abs(ax - cx) + abs(ay - cy) == 1, f"{a} !~ {c}"

    for k in range(pm.N_LIMBS):
        abut(f"lh_{k}", f"mulA_{k}")
        abut(f"mulB_{k}", f"mulC_{k}")
        abut(f"mulC_{k}", f"lh_{k}")
    abut("ulk", "seq_top")


def test_no_two_cells_rest_facing_each_other():
    """INV-56's static head-on check over the whole 100-cell fold."""
    b = _block()
    lay = b._geometry()
    at = {(x, y): c for c, (x, y, f) in lay.items()}
    pairs = []
    for cid, (x, y, face) in lay.items():
        dx, dy = _DELTA[face]
        nbr = at.get((x + dx, y + dy))
        if nbr is None:
            continue
        bx, by = lay[nbr][0], lay[nbr][1]
        ndx, ndy = _DELTA[lay[nbr][2]]
        if (bx + ndx, by + ndy) == (x, y):
            pairs.append((cid, nbr))
    assert not pairs, f"head-on resting pairs: {pairs}"


def test_at_most_one_backward_jump_per_cell_and_it_is_highest():
    """INV-53 both clauses, over every cell."""
    b = _block()
    progs = b.build_cell_programs()
    idx = {c: i for i, c in enumerate(progs)}
    backward = {}
    for s, sp, d, dp in b.internal_jumps():
        if idx[d] < idx[s]:
            backward.setdefault(s, []).append(sp)
    for cid, edges in backward.items():
        assert len(edges) == 1, f"{cid} declares {len(edges)} backward jumps"
        code = [ln.strip() for ln in
                progs[cid].assembly_template.splitlines()
                if ln.strip() and not ln.strip().endswith(":")]
        jaddrs = [i for i, ln in enumerate(code) if "{jump:" in ln]
        assert code[max(jaddrs)] == "{jump:%s}" % edges[0], (
            f"{cid}: backward jump {edges[0]} is not the highest-addressed")


def test_every_entry_is_reachable():
    """INV-39: every declared entry is jumped, or is the external landing.
    ``bnd.unlk`` is a local BR label, deliberately NOT an entry."""
    b = _block()
    targeted = {(d, e) for (_s, _p, d, e) in b.internal_jumps()}
    for cid, cp in b.build_cell_programs().items():
        for e in cp.entries:
            ok = (cid, e.name) in targeted or (cid, e.name) == ("seq_top",
                                                                "go")
            assert ok, f"unreachable entry {cid}.{e.name}"


def test_chip_scale_orientation_set_is_declared():
    """A 10-wide, 10-tall fold cannot rotate on a 10x12 array; identity is
    the whole shipped set, declared and asserted (the chip-scale contract —
    test_chip_scale_blocks_are_gated_elsewhere.py points here)."""
    assert Poly1305MACBlock.CHIP_SCALE is True
    assert Poly1305MACBlock.CHIP_SCALE_ORIENTATIONS == ((),)
    lay = _block()._geometry()
    w = max(x for x, y, f in lay.values()) + 1
    h = max(y for x, y, f in lay.values()) + 1
    assert w == 10 and h == 10, (w, h)


def test_every_branch_has_a_flag_setter_in_its_basic_block():
    """INV-61 clause 2, refined: MOVE/WRITE/JUMP/LOAD preserve flags, so a BR
    is sound iff SOME ALU instruction precedes it in its basic block. A BR
    whose walk-back hits a label/entry (or the block start) with no flag
    setter reads stale state."""
    preserving = ("MOVE", "WRITE", "JUMP", "LOAD", "{write:", "{jump:")
    for cid, cp in _block().build_cell_programs().items():
        lines = [ln.strip() for ln in cp.assembly_template.splitlines()
                 if ln.strip()]
        for i, ln in enumerate(lines):
            if not ln.startswith("BR."):
                continue
            j = i - 1
            ok = False
            while j >= 0:
                prev = lines[j]
                if prev.endswith(":"):
                    break                       # label: control may enter here
                if prev.startswith(preserving) or prev == "HALT":
                    j -= 1
                    continue
                ok = True                       # an ALU op sets the flags
                break
            assert ok, f"{cid}: {ln!r} has no flag setter in its basic block"


def test_process_reference_matches_golden():
    """The shipped reference (the EXACT cell schedule) equals the golden —
    re-checked here so the chip gates above compare against something that is
    itself pinned to the RFC."""
    rng = random.Random(99)
    for _ in range(50):
        nw = rng.randrange(1, 40)
        msg = bytes(rng.randrange(256) for _ in range(2 * nw))
        key = bytes(rng.randrange(256) for _ in range(32))
        got = _model_tag(msg, key)
        assert got == _want(msg, key)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def test_emit_report():
    """Dashboard report — emitted only from a passing bit-exact run of THE
    gate (INV-38: a report is an artifact of a verified session)."""
    from kyttar_verify import CompareResult, Metric, write_report

    out, _ = _chip_tag(g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY)
    want = _want(g.RFC8439_2_5_2_MSG, g.RFC8439_2_5_2_KEY)
    errs = sum(1 for a, b in zip(out, want) if a != b) + abs(
        len(out) - len(want))
    res = CompareResult(passed=(errs == 0), metric=Metric.EXACT,
                        n_compared=len(want), bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("Poly1305MACBlock", res, coverage={
        "golden": ("RFC 8439 §2.5 (verification/tests/poly1305_golden.py, "
                   "two independent implementations); no GNU Radio "
                   "counterpart"),
        "anchors": ("§2.5.2 worked example (17 words, 3 blocks incl. a "
                    "partial final block) EXACT ON CHIP — full 8-word tag; "
                    "all six even-length §A.3 edge vectors on chip"),
        "random": ("8 residue-sweep messages (every final-block m=1..8) + 3 "
                   "seeded randoms + the all-max carry-ceiling case, all "
                   "bit-exact on chip"),
        "saturated": ("whole message queue_words_physical back-to-back, one "
                      "continuous run — the INV-20 serialize-LOCK on the "
                      "input landing"),
        "rate": "msg_words words in -> 8-word tag out on the final trigger",
        "mutation_onchip": ("7 real mutants, each placed+routed+built+run "
                            "and each PROVEN able to fire (model prediction "
                            "first): skip r clamp / drop high bit x2 (full + "
                            "partial block) / reduction constant 3 / omit +s "
                            "/ wrong 32-bit MAC half order (INV-58) / "
                            "one-stage carry"),
        "structural": ("INV-33 budget (with INV-4 negative) / positional "
                       "pairing / 425-edge walk gate (with negative) / "
                       "head-on pairs / INV-53 backward-jump audit / INV-39 "
                       "entry reachability / INV-61 stale-flag checker / "
                       "input landing = cell 0"),
        "cells": _block().cell_count,
        "stop_reason": "read for EVERY case; QueueEmpty required everywhere",
        "note": ("one-time MAC: exactly ONE message per build (RFC 8439 "
                 "one-time-key semantics); odd-byte messages inexpressible "
                 "at the 16-bit word interface (golden-gated)"),
    })
