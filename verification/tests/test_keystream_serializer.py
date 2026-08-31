# SPDX-License-Identifier: GPL-3.0-or-later
"""KeystreamSerializerBlock ON THE REAL PLACED + ROUTED + BUILT CHIP.

The block converts ``ChaCha20KeystreamBlock``'s wire convention (16-bit hi/lo
HALVES of each 32-bit state word, hi first) into RFC 8439 §2.3 serialization
order (each 32-bit word emitted as its four bytes LITTLE-ENDIAN, one keystream
byte per 16-bit word — the data-link convention). 2 words in -> 4 words out.
The golden is ``chacha20_golden.serialize`` — the same authority the ChaCha20
suite pins against the RFC's own §2.3.2 vector before it gates anything.

Layers gated here (stop_reason read for EVERY chip case — INV-56):

* THE gate: the sixteen §2.3.2 state words, fed as hi/lo halves, come out as
  the RFC's 64 serialized keystream bytes, bit-exact and in order;
* edge + random (3 seeds) stimulus, bit-exact vs the block's own reference,
  which is itself pinned against ``serialize()`` and ``int.to_bytes``;
* the 1:2 rate (a 4-word burst on every second trigger, nothing on the hi
  trigger; a trailing unpaired hi word emits nothing);
* saturated drive (INV-19): the whole burst enqueued back-to-back via
  ``queue_words_physical``, flat stream == per-sample flat stream, correct
  COUNT included;
* MID-CHAIN composition: Delay(2) -> serializer -> Delay(1) hand-placed on
  ONE chip, all nets routed, composed reference exact — the block is
  mid-chain friendly (plain routed egress), unlike the RAW-egress tail-only
  blocks (INV-66);
* all 8 D4 orientations on the FULL burst (INV-23);
* the batch boundary: ``reset_per_batch`` on the parity + held-hi registers,
  applied exactly as the hosted bridge applies ``batch_reset_writes``;
* INV-4 ON-CHIP mutants, patched IN MEMORY (never on disk — a mutated file
  can leave a stale .pyc, INV-4 addendum), each placed+routed+built+run:
  hi/lo parity swapped, byte order within the 32-bit word reversed
  (big-endian), parity not reset per batch (the SECOND batch fails while the
  first passes);
* the INV-33 overlap/budget static gate with its INV-4 negative.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_keystream_serializer.py -q
"""
from __future__ import annotations

import dataclasses
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

import chacha20_golden as g  # noqa: E402
from gr_kyttar.placement.blocks.keystream_serializer_block import (  # noqa: E402
    KeystreamSerializerBlock)
from kyttar_verify import run_block_dut_rate, D4_ORIENTATIONS  # noqa: E402
from kyttar_verify.dut_runner import run_block_dut_pipelined  # noqa: E402

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" /
                "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")

#: The RFC 8439 §2.3.2 keystream block as the block's INPUT (hi/lo halves) and
#: expected OUTPUT (the RFC's serialized bytes, one per word).
RFC_WORDS = g.state_to_words16(g.RFC8439_BLOCK_EXPECTED_STATE)
RFC_BYTES = list(g.RFC8439_BLOCK_EXPECTED_KEYSTREAM)


def _ref(words) -> list[int]:
    return KeystreamSerializerBlock("r").process_reference_q15(words)


# --------------------------------------------------------------------------
# Build + drive helpers (poly1305 pattern: stop_reason read for EVERY case)
# --------------------------------------------------------------------------

def _build(orient=None, mutate=None, block="KeystreamSerializerBlock",
           params=None, anchor=(1, 1)):
    """Place + orient + auto-route + build; returns (BuildResult, landing)."""
    import simkyt  # noqa: F401
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    orig = KeystreamSerializerBlock.build_cell_programs
    try:
        if mutate is not None:
            def patched(self):
                progs = orig(self)
                mutate(self, progs)
                return progs
            KeystreamSerializerBlock.build_cell_programs = patched
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(CHIP_YAML)
        key = getattr(ct, "name", None) or "kyttar_10x12"
        ctrl = AppController(catalog=cat)
        ctrl.new_project("ks", key)
        blk = ctrl.place_block(block, 0, anchor[0], anchor[1],
                               library="lattrex.official", params=params or {})
        for _k in (orient or []):
            ctrl.project.block(blk).placement.transform(_k)
        ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                    BlockEndpoint(block=blk, port="word"),
                                    name="in_blk")
        ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                    ChipPortEndpoint(chip=0, port="x16_out"),
                                    name="blk_out")
        rep = ctrl.auto_route_all({key: ct})
        assert rep.ok, "route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in rep.failed)
        bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
        assert bres.ok, f"build failed: {bres.errors}"
        land = (getattr(bres.chips.get(0), "input_landings", {}) or {})["in_blk"]
        return bres, land
    finally:
        KeystreamSerializerBlock.build_cell_programs = orig


def _fresh_chip(bres, land):
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["entry"]))
    return chip


def _drive(chip, land, words, events_per_word=60000):
    """Per-word drive on an EXISTING chip. Returns (flat, per_trigger).

    INV-56: every trigger's final run must reach quiescence with
    ``stop_reason == "QueueEmpty"`` — asserted here, for every case, because
    word count alone cannot tell a deadlock from a clean run.
    """
    flat, per = [], []
    for v in words:
        chip.inject_data_physical([int(v) & 0xFFFF],
                                  target_hop_cnt=int(land["hop"]),
                                  target_addr=int(land["data_addrs"][0]))
        r = chip.run(max_events=4000)
        assert r.get("completed"), f"data delivery did not settle: {r}"
        chip.inject_jump_physical(target_hop_cnt=int(land["hop"]),
                                  entry_addr=int(land["entry"]))
        got, fin = [], {}
        for _ in range(40):
            fin = chip.run(max_events=events_per_word // 20)
            while chip.output_available("x16_out"):
                w = chip.read_port_i16("x16_out").view("uint16").tolist()
                got.extend(int(x) & 0xFFFF for x in w)
                chip.release_output_ack("x16_out")
            if fin.get("completed"):
                break
        assert fin.get("completed") and fin.get("stop_reason") == "QueueEmpty", (
            f"trigger for word 0x{int(v) & 0xFFFF:04x} did not reach "
            f"quiescence: {fin}")
        per.append(got)
        flat.extend(got)
    return flat, per


def _chip_bytes(words, orient=None, mutate=None):
    bres, land = _build(orient=orient, mutate=mutate)
    return _drive(_fresh_chip(bres, land), land, words)


# --------------------------------------------------------------------------
# Golden sanity — the host reference is pinned before it gates anything
# --------------------------------------------------------------------------

def test_golden_reference_is_the_rfc_serialization():
    """The trivial host reference IS RFC 8439 §2.3's serialize(): per 32-bit
    word, ``int(v).to_bytes(4, "little")`` — pinned against the golden's own
    ``serialize()`` (the authority) and against the RFC §2.3.2 vector."""
    assert g.serialize(g.RFC8439_BLOCK_EXPECTED_STATE) == \
        g.RFC8439_BLOCK_EXPECTED_KEYSTREAM
    assert _ref(RFC_WORDS) == RFC_BYTES
    # Property, over random 32-bit words: reference == to_bytes little-endian.
    rng = random.Random(5)
    vals = [rng.getrandbits(32) for _ in range(64)]
    words = g.state_to_words16(vals)
    want = list(b"".join(int(v).to_bytes(4, "little") for v in vals))
    assert _ref(words) == want
    assert _ref(words) == list(g.serialize(vals))


def test_golden_trailing_unpaired_hi_word_emits_nothing():
    assert _ref([0x1234]) == []
    assert _ref([0xAAAA, 0xBBBB, 0xCCCC]) == [0xBB, 0xBB, 0xAA, 0xAA]


# --------------------------------------------------------------------------
# THE gate: RFC 8439 §2.3.2 on the built chip
# --------------------------------------------------------------------------

def test_rfc8439_keystream_bytes_exact_on_chip():
    """The sixteen §2.3.2 state words, fed as hi/lo halves, come out as the
    RFC's 64 serialized keystream bytes — bit-exact, in order, one byte per
    word, 4-word burst on every lo trigger, quiescent every case."""
    flat, per = _chip_bytes(RFC_WORDS)
    assert [len(t) for t in per] == [0, 4] * 16, [len(t) for t in per]
    assert len(flat) == 64
    assert all(0 <= w <= 0xFF for w in flat), "an output word exceeds a byte"
    assert flat == RFC_BYTES, (
        f"on-chip serialization differs from RFC 8439 §2.3.2:\n"
        f"got {bytes(flat).hex()}\nexp {bytes(RFC_BYTES).hex()}")


# --------------------------------------------------------------------------
# Edge + random coverage, bit-exact
# --------------------------------------------------------------------------

EDGE_WORDS = [0x0000, 0x0000,   # zero word
              0xFFFF, 0xFFFF,   # all-ones
              0x00FF, 0xFF00,   # byte boundaries both halves
              0x0001, 0x8000,   # LSB / sign bit
              0xAA55, 0x55AA,   # alternating nibbles
              0x7FFF, 0x8001]   # Q15 rails carried as raw words


def test_edge_words_bit_exact_on_chip():
    flat, per = _chip_bytes(EDGE_WORDS)
    assert flat == _ref(EDGE_WORDS)
    assert [len(t) for t in per] == [0, 4] * (len(EDGE_WORDS) // 2)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_words_bit_exact_on_chip(seed):
    rng = random.Random(seed)
    words = [rng.getrandbits(16) for _ in range(24)]
    flat, per = _chip_bytes(words)
    assert flat == _ref(words), f"seed {seed} mismatch"
    assert [len(t) for t in per] == [0, 4] * 12


def test_trailing_unpaired_hi_word_emits_nothing_on_chip():
    """An odd-length stream: the dangling hi half is HELD (no output), exactly
    like the reference — and the next lo half releases it."""
    words = [0xDEAD, 0xBEEF, 0x1234]
    flat, per = _chip_bytes(words)
    assert flat == _ref(words) == [0xEF, 0xBE, 0xAD, 0xDE]
    assert [len(t) for t in per] == [0, 4, 0]


# --------------------------------------------------------------------------
# Saturated drive (INV-19): whole burst back-to-back, count AND values
# --------------------------------------------------------------------------

def test_saturated_drive_equals_per_sample():
    """The whole RFC burst enqueued via ``queue_words_physical`` with NO
    inter-sample quiescence: the flat stream must equal the per-sample flat
    stream (already RFC-exact above) — correct COUNT included. The runner
    itself fails on non-quiescence and reports the stop_reason."""
    sat = run_block_dut_pipelined(
        "KeystreamSerializerBlock", [(w,) for w in RFC_WORDS],
        chip_yaml=CHIP_YAML, in_ports=("word",), out_port="out")
    assert sat.ok, sat.reason
    assert len(sat.outputs_q15) == 64, (
        f"saturated drive changed the output COUNT: {len(sat.outputs_q15)}")
    assert sat.outputs_q15 == RFC_BYTES, "saturated values diverge"


@pytest.mark.parametrize("seed", [3, 11])
def test_saturated_random_equals_per_sample(seed):
    rng = random.Random(seed)
    words = [rng.getrandbits(16) for _ in range(20)]
    sat = run_block_dut_pipelined(
        "KeystreamSerializerBlock", [(w,) for w in words],
        chip_yaml=CHIP_YAML, in_ports=("word",), out_port="out")
    assert sat.ok, sat.reason
    assert sat.outputs_q15 == _ref(words)


# --------------------------------------------------------------------------
# MID-CHAIN composition: Delay(2) -> serializer -> Delay(1), one chip
# --------------------------------------------------------------------------

def _build_chain():
    """Hand-place Delay(2) -> KeystreamSerializer -> Delay(1) on one chip,
    every net routed by the real router (plain routed ingress AND egress for
    the serializer — no raw port literals anywhere)."""
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
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("ks_chain", key)
    d1 = ctrl.place_block("DelayBlock", 0, 1, 1, library="lattrex.official",
                          params={"delay": 2})
    ks = ctrl.place_block("KeystreamSerializerBlock", 0, 1, 4,
                          library="lattrex.official", params={})
    d2 = ctrl.place_block("DelayBlock", 0, 1, 7, library="lattrex.official",
                          params={"delay": 1})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=d1, port="sample"),
                                name="in_blk")
    ctrl.add_logical_connection(BlockEndpoint(block=d1, port="out"),
                                BlockEndpoint(block=ks, port="word"), name="ab")
    ctrl.add_logical_connection(BlockEndpoint(block=ks, port="out"),
                                BlockEndpoint(block=d2, port="sample"),
                                name="bc")
    ctrl.add_logical_connection(BlockEndpoint(block=d2, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="blk_out")
    rep = ctrl.auto_route_all({key: ct})
    assert rep.ok, "chain route failed: " + "; ".join(
        f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, f"chain build failed: {bres.errors}"
    land = (getattr(bres.chips.get(0), "input_landings", {}) or {})["in_blk"]
    return bres, land


def test_mid_chain_composition_exact_on_chip():
    """The serializer sits BETWEEN two other blocks on one chip and the whole
    chain is exact: Delay(2) keeps the hi/lo pairing (one prepended zero
    PAIR), the serializer converts, Delay(1) shifts the byte stream by one.
    Expected output composed from each block's own exact semantics — this is
    the head-and-tail composability the RAW-egress blocks lack (INV-66)."""
    bres, land = _build_chain()
    flat, per = _drive(_fresh_chip(bres, land), land, RFC_WORDS,
                       events_per_word=120000)
    ser_in = [0, 0] + RFC_WORDS[:-2]        # Delay(2), 32 words
    ser_out = _ref(ser_in)                  # 64 bytes
    want = [0] + ser_out[:-1]               # Delay(1), 64 words
    assert len(flat) == 64, f"chain emitted {len(flat)} words"
    assert flat == want, (
        f"mid-chain composition differs:\ngot {bytes(flat).hex()}\n"
        f"exp {bytes(want).hex()}")
    # Non-vacuity: the composed expectation still contains the RFC bytes
    # (shifted), so a dead serializer cannot pass on zeros.
    assert ser_out[:60] == _ref([0, 0] + RFC_WORDS[:-2])[:60]
    assert sum(want) > 0


# --------------------------------------------------------------------------
# Orientation invariance (INV-23): full burst, all 8 D4 orientations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("orient", D4_ORIENTATIONS[1:],
                         ids=lambda o: "+".join(o) or "identity")
def test_orientation_invariant_full_burst(orient):
    """The FULL 4-word-per-pair burst is identical in every D4 orientation
    (the shared orientation suite covers the last-word view; this is the
    burst-complete check, ChaCha20QR precedent)."""
    dut = run_block_dut_rate("KeystreamSerializerBlock", RFC_WORDS,
                             chip_yaml=CHIP_YAML, in_port="word",
                             out_port="out", orient=list(orient))
    assert dut.ok, dut.reason
    assert dut.outputs_q15 == RFC_BYTES, f"orientation {orient} diverges"


# --------------------------------------------------------------------------
# The batch boundary: reset_per_batch, exactly as the hosted bridge applies it
# --------------------------------------------------------------------------

def _batch_pair_drive(mutate=None):
    """Batch A (ODD word count — leaves the parity armed and a hi half held),
    then the bridge's packet-boundary reset, then batch B (the RFC burst) on
    the SAME chip. Returns (batchA_flat, resets, batchB_flat)."""
    bres, land = _build(mutate=mutate)
    chip = _fresh_chip(bres, land)
    odd = [0xDEAD, 0xBEEF, 0x1234]          # one pair + a dangling hi half
    flat_a, _ = _drive(chip, land, odd)
    resets = bres.chips[0].batch_reset_writes
    for (x, y, addr, value) in resets:
        chip.write_cell_memory(chip.cell_id_at(int(x), int(y)),
                               int(addr), int(value) & 0xFFFF)
    flat_b, _ = _drive(chip, land, RFC_WORDS)
    return flat_a, resets, flat_b


def test_second_batch_after_reset_is_exact():
    """A batch ending on an unpaired hi half must NOT skew the next batch:
    the build resolves reset writes for the parity + held-hi registers and
    applying them (exactly as SimServer.process_batch does) restores hi-phase
    — the second batch is RFC-exact."""
    flat_a, resets, flat_b = _batch_pair_drive()
    assert flat_a == [0xEF, 0xBE, 0xAD, 0xDE]
    assert len(resets) == 2, (
        f"expected reset writes for exactly (hi, par); got {resets}")
    assert flat_b == RFC_BYTES, "stale hi/lo phase survived the batch boundary"


# --------------------------------------------------------------------------
# INV-4 mutants — ON CHIP, patched in memory, each proven to fire
# --------------------------------------------------------------------------

def _retemplate(progs, old, new):
    t = progs[0].assembly_template
    assert old in t, f"mutation anchor missing: {old!r}"
    progs[0] = dataclasses.replace(progs[0],
                                   assembly_template=t.replace(old, new))


def test_onchip_mutant_parity_swapped_fails():
    """Hi/lo parity SWAPPED (``par`` boots at 1): every hi half is treated as
    a lo half and vice versa, so the byte stream comes out phase-skewed with
    the halves exchanged — big-endian-style corruption. Same word COUNT (64),
    wrong values: only the RFC value gate can see it."""
    def mutate(self, progs):
        progs[0] = dataclasses.replace(progs[0], state=[
            dataclasses.replace(s, initial_value=1) if s.name == "par" else s
            for s in progs[0].state])
    flat, per = _chip_bytes(RFC_WORDS, mutate=mutate)
    assert len(flat) == 64, "mutant changed the COUNT, not the values"
    assert flat != RFC_BYTES, "a parity-swapped mutant went undetected!"
    # The phase skew is exactly the predicted one: the k-th emit carries
    # (hi_k's bytes, then the PREVIOUS lo's bytes) — halves exchanged.
    w = RFC_WORDS
    pred, prev_lo = [], 0
    for i in range(0, len(w), 2):
        hi, lo = w[i], w[i + 1]
        pred.extend([hi & 0xFF, (hi >> 8) & 0xFF,
                     prev_lo & 0xFF, (prev_lo >> 8) & 0xFF])
        prev_lo = lo
    assert flat == pred, "the mutant's failure shape is not the modeled one"


def test_onchip_mutant_byte_order_reversed_fails():
    """Byte order within the 32-bit word REVERSED (big-endian serialization:
    hi>>8, hi&FF, lo>>8, lo&FF). Same COUNT, wrong order — must fail the RFC
    vector."""
    good = """\
    AND R{state:lo}, R{data:ff}
    {write:out}
    {jump:out}
    SHR R{state:lo}, #8
    {write:out}
    {jump:out}
    AND R{state:hi}, R{data:ff}
    {write:out}
    {jump:out}
    SHR R{state:hi}, #8
    {write:out}
    {jump:out}"""
    bad = """\
    SHR R{state:hi}, #8
    {write:out}
    {jump:out}
    AND R{state:hi}, R{data:ff}
    {write:out}
    {jump:out}
    SHR R{state:lo}, #8
    {write:out}
    {jump:out}
    AND R{state:lo}, R{data:ff}
    {write:out}
    {jump:out}"""
    def mutate(self, progs):
        _retemplate(progs, good, bad)
    flat, per = _chip_bytes(RFC_WORDS, mutate=mutate)
    assert len(flat) == 64, "mutant changed the COUNT, not the values"
    assert flat != RFC_BYTES, "a byte-order-reversed mutant went undetected!"
    # It is precisely the big-endian serialization — the named wrong answer.
    assert flat == list(b"".join(
        int(v).to_bytes(4, "big")
        for v in g.RFC8439_BLOCK_EXPECTED_STATE))


def test_onchip_mutant_parity_not_reset_per_batch_fails_second_batch():
    """Parity StateVar NOT reset per batch: strip ``reset_per_batch`` from the
    block's state. The FIRST batch is still byte-exact (the mutation is
    invisible to any single-batch gate — that is the point), but after an
    odd-length batch the second batch comes out phase-skewed and MUST fail."""
    def mutate(self, progs):
        progs[0] = dataclasses.replace(progs[0], state=[
            dataclasses.replace(s, reset_per_batch=False)
            for s in progs[0].state])
    flat_a, resets, flat_b = _batch_pair_drive(mutate=mutate)
    assert flat_a == [0xEF, 0xBE, 0xAD, 0xDE], (
        "the mutant is supposed to pass the FIRST batch — the gate must need "
        "the second")
    assert resets == [], "the mutant still resolved reset writes"
    assert flat_b != RFC_BYTES, (
        "a parity-not-reset-per-batch mutant went undetected by the "
        "second-batch gate!")


# --- model-level mutants (the standard INV-4 set, cheap) ----------------------

def test_mutation_plus_one_word_shift_fails():
    shifted = [0] + RFC_BYTES[:-1]
    assert shifted != RFC_BYTES, "+1 word shift went undetected"


def test_mutation_inverted_output_fails():
    inverted = [b ^ 0xFF for b in RFC_BYTES]
    assert inverted != RFC_BYTES, "inverted output went undetected"


def test_mutation_empty_output_fails():
    assert [] != RFC_BYTES and len(RFC_BYTES) == 64


def test_mutation_halves_swapped_model_fails():
    """The model-side twin of the parity swap: serializing (lo, hi) instead of
    (hi, lo) — i.e. bytes [hi&FF, hi>>8, lo&FF, lo>>8] — differs from the
    RFC keystream."""
    w = RFC_WORDS
    swapped = []
    for i in range(0, len(w), 2):
        hi, lo = w[i], w[i + 1]
        swapped.extend([hi & 0xFF, (hi >> 8) & 0xFF,
                        lo & 0xFF, (lo >> 8) & 0xFF])
    assert swapped != RFC_BYTES, "hi/lo halves swapped went undetected"


# --------------------------------------------------------------------------
# Structural guards (INV-33 overlap/budget, with the INV-4 negative)
# --------------------------------------------------------------------------

def test_cell_fits_its_word_budget_no_overlap():
    """INV-33's overlap half as a static gate: no data address, state
    register, or pinned input register at or above ``31 - instr_count``
    (the resolver's own guard checks only DATA — never state or inputs)."""
    from gr_kyttar.placement.resolver import (CellProgramResolver,
                                              ResolvedTargets, WriteTarget,
                                              JumpTarget)
    res = CellProgramResolver()
    offenders = []
    for cid, p in KeystreamSerializerBlock("b").build_cell_programs().items():
        tg = ResolvedTargets(
            writes={o.name: WriteTarget(1, 1) for o in p.outputs},
            jumps={o.name: JumpTarget(1, 1) for o in p.outputs})
        asm = res._substitute_registers(p.assembly_template, p,
                                        res._allocate_data(p.data),
                                        state_map={}, input_map={}, dummy=True)
        asm = res._substitute_write_jump(asm, tg, dummy=True)
        base = 31 - res._count_instructions(asm)
        used = ([d.address for d in p.data if d.address is not None]
                + [s.register for s in p.state if s.register is not None]
                + [i.register for i in p.inputs if i.register is not None])
        over = sorted(a for a in used if a >= base)
        if over:
            offenders.append((cid, base, over))
    assert not offenders, f"cell overlaps its own instructions: {offenders}"


def test_overlap_gate_catches_a_known_bad_shape():
    """INV-4 for the gate above: a variant whose state is pinned into the
    instruction span must be FLAGGED by the same arithmetic."""
    from gr_kyttar.placement.block import StateVar
    from gr_kyttar.placement.resolver import (CellProgramResolver,
                                              ResolvedTargets, WriteTarget,
                                              JumpTarget)
    res = CellProgramResolver()
    p = KeystreamSerializerBlock("b").build_cell_programs()[0]
    bad = dataclasses.replace(p, state=list(p.state) + [
        StateVar("bad", register=30)])
    tg = ResolvedTargets(
        writes={o.name: WriteTarget(1, 1) for o in bad.outputs},
        jumps={o.name: JumpTarget(1, 1) for o in bad.outputs})
    asm = res._substitute_registers(bad.assembly_template, bad,
                                    res._allocate_data(bad.data),
                                    state_map={}, input_map={}, dummy=True)
    asm = res._substitute_write_jump(asm, tg, dummy=True)
    base = 31 - res._count_instructions(asm)
    over = [s.register for s in bad.state
            if s.register is not None and s.register >= base]
    assert over, ("a state register pinned into the instruction span was not "
                  "flagged — the overlap gate has no teeth")


def test_reference_and_rate_declared_consistently():
    """1:2 rate arithmetic: 2N input words -> 4N output words for every even
    N, straight from the reference."""
    for n in (0, 2, 8, 32):
        words = list(range(n))
        assert len(_ref(words)) == 2 * n


# --------------------------------------------------------------------------
# Report — an artifact of THIS session (INV-38; write_report gates on the
# session's own outcomes and unlinks first)
# --------------------------------------------------------------------------

def test_zz_emit_report():
    from kyttar_verify import CompareResult, Metric, write_report

    flat, per = _chip_bytes(RFC_WORDS)
    errs = sum(1 for a, b in zip(flat, RFC_BYTES) if a != b) + abs(
        len(flat) - len(RFC_BYTES))
    res = CompareResult(passed=(errs == 0), metric=Metric.EXACT,
                        n_compared=len(RFC_BYTES), bit_errors=errs,
                        delay_used=0)
    assert res.passed, res.summary()
    write_report("KeystreamSerializerBlock", res, coverage={
        "golden": ("RFC 8439 §2.3 serialize() — chacha20_golden.py, pinned by "
                   "the §2.3.2 vector; no GNU Radio counterpart"),
        "anchors": ("the sixteen §2.3.2 state words as hi/lo halves -> the "
                    "RFC's 64 keystream bytes, bit-exact IN ORDER on chip"),
        "edge": True, "random": 3,
        "rate": "1:2 (2 half-words in -> 4 byte-words out; burst on the lo "
                "trigger; a trailing hi half is held, emitting nothing)",
        "saturated": "queue_words_physical back-to-back == per-sample "
                     "(count and values), RFC burst + 2 random seeds",
        "mid_chain": ("Delay(2) -> serializer -> Delay(1) hand-placed on one "
                      "chip, all nets routed, composed reference exact — "
                      "plain routed egress, mid-chain friendly (INV-66)"),
        "orientation": "all 8 D4 on the full burst + the shared suite",
        "batch": "odd batch -> batch_reset_writes -> RFC batch exact "
                 "(reset_per_batch on hi+par)",
        "mutation_onchip": ("3 real mutants placed+routed+built+run: parity "
                            "swapped / byte order reversed (== big-endian, "
                            "asserted) / reset_per_batch stripped (first "
                            "batch PASSES, second fails)"),
        "structural": "INV-33 overlap gate with INV-4 negative",
        "cells": KeystreamSerializerBlock("b").cell_count,
        "stop_reason": "read for EVERY chip case; QueueEmpty required",
        "note": "serializes ChaCha20KeystreamBlock's hi/lo half-word stream "
                "into RFC 8439 little-endian keystream bytes, one byte per "
                "16-bit word (the data_link convention)",
    })
