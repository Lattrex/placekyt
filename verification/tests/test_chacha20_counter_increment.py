# SPDX-License-Identifier: GPL-3.0-or-later
"""ChaCha20KeystreamBlock ``counter_mode="increment"`` — consecutive batches
produce CONSECUTIVE RFC 8439 keystream blocks, on the real chip.

RFC 8439 §2.4 consumes the block function with a counter that advances by one
per 64-byte block. The shipped block bakes key/nonce/counter into boot-time
constants and ``reset_per_batch`` restores all sixteen state words at every
packet boundary, so every trigger recomputes block ``counter`` — correct as a
block function, useless for a >64-byte payload. ``counter_mode="increment"``
makes batch ``N`` emit ``block(key, nonce, counter + N)``: multi-block
encryption becomes a sequence of triggers.

WHERE THE INCREMENT LIVES (see the block's docstrings for the full derivation):

* the authoritative counter is ``drn``'s ``ch``/``cl`` — the block's ONE
  StateVar pair excluded from the batch reset;
* ``drn.done`` (the close of the fourth drain lap — the one schedule point
  where BOTH baked copies of state word 12 sit at a known rotation: the row is
  idle and ``add3``'s addend register has rotated 4 times = identity) advances
  it 32-bit-wide (``ADD``/park/``ADC``/park, INV-45) and pushes the new value
  into row3's slot-0 registers as authored literal ``WRITE @hop``s on its own
  resting walk (the slot is a StateVar pair, not a port — INV-63's
  derived-literal discipline);
* NO walk in the block ends at ``add3`` (every neighbour's resting face
  forwards PAST it), so the add-back copy goes in via ``tap3.bump`` — the one
  neighbour with spare words whose NORTH flip is already paid for: ``drn``
  parks the halves in ``tap3``'s idle ``h``/``l`` (hop 8, same walk) and the
  bump plays them into ``add3``'s pinned ``k0h``/``k0l`` addresses at hop 1;
* the emission ORDER in ``done`` is load-bearing: ``{jump:rel}`` leaves LAST,
  so the release trigger (which transiently flips ``tap0``) trails every
  counter word down the state line (INV-52).

Fixed mode ships BYTE-IDENTICAL programs (gated below): the shipped 38-test
suite, its on-chip value gate and its second-batch gate all bind to unchanged
cells.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest \\
        verification/tests/test_chacha20_counter_increment.py -q
"""
from __future__ import annotations

import os
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
# The fold/walk helpers and face tables are the SHIPPED gate's own — the
# increment fold must satisfy the identical checks, so it uses the identical
# machinery.
import test_chacha20_fixed_tap_ring as ring  # noqa: E402
from gr_kyttar.placement.blocks.chacha20_keystream_block import (  # noqa: E402
    ChaCha20KeystreamBlock)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

#: The block's default key/nonce — the RFC 8439 §2.3.2 vector's.
KEY = bytes(range(32))
NONCE = bytes.fromhex("000000090000004a00000000")


# ===========================================================================
# STRUCTURE — the increment fold passes every gate the shipped fold passes.
# ===========================================================================
def test_counter_mode_is_validated():
    """Anything but 'fixed'/'increment' raises — never a silent default."""
    with pytest.raises(ValueError):
        ChaCha20KeystreamBlock("bad", counter_mode="incremant")


def test_fixed_mode_carries_no_increment_machinery():
    """MODE ISOLATION at the source: the default (fixed) build has none of the
    increment artifacts — no persistent counter, no bump entry, no bump edges,
    and every one of the sixteen state words still batch-resets. This is what
    'the shipped gates bind to unchanged bytes' means structurally; the
    byte-identity itself is implied by the shipped suite staying green.
    """
    b = ChaCha20KeystreamBlock("fx")                       # default = fixed
    progs = b.build_cell_programs()
    assert b.counter_mode == "fixed"
    assert {sv.name for sv in progs["drn"].state} == {"lap"}
    assert "bump" not in {e.name for e in progs["tap3"].entries}
    assert all(sv.reset_per_batch for sv in progs["row3"].state)
    assert all(dw.reset_per_batch for dw in progs["add3"].data)
    assert ("drn", "bump", "tap3", "bump") not in b.internal_jumps()
    assert ("drn", "cbh", "tap3", "h") not in b.internal_connections()


def _inc(name="inc", counter=1):
    return ChaCha20KeystreamBlock(name, counter=counter,
                                  counter_mode="increment")


def test_increment_mode_is_still_51_cells_positionally_paired():
    """The increment lives in EXISTING cells (drn + tap3): same cell count,
    same layout order == program order (INV-33's positional pairing)."""
    b = _inc()
    assert b.cell_count == 51
    assert list(b.build_cell_programs()) == list(b.default_layout())


def test_increment_mode_every_cell_stays_inside_its_word_budget():
    """The shipped budget arithmetic, over the INCREMENT programs. The two
    cells the mode touches close with margin: measured drn 22 instructions /
    base 9 / max pin 6, tap3 24 / base 7 / max pin 5."""
    b = _inc()
    over = []
    measured = {}
    for cid, cp in b.build_cell_programs().items():
        instr = len([l for l in cp.assembly_template.splitlines()
                     if l.strip() and not l.strip().endswith(":")])
        base = 31 - instr
        pins = [p.register for p in list(cp.inputs) + list(cp.state)
                if p.register is not None]
        pins += [d.address for d in cp.data if d.address is not None]
        measured[cid] = (instr, base, max(pins) if pins else -1)
        if pins and max(pins) >= base:
            over.append((cid, instr, base, max(pins)))
    assert not over, f"cells overlapping their own instructions: {over}"
    assert measured["drn"] == (22, 9, 6), measured["drn"]
    assert measured["tap3"] == (24, 7, 5), measured["tap3"]


def test_increment_mode_every_edge_lands_on_a_real_forwarding_walk():
    """The shipped fold gate, over the INCREMENT edges — zero exemptions.
    The three new edges all ride ``drn``'s resting walk (tap3 at hop 8)."""
    b = _inc()
    lay = b.default_layout()
    ef = ring._emit_faces_per_port(b)
    edges = ([("WRITE", *e) for e in b.internal_connections()]
             + [("JUMP", *e) for e in b.internal_jumps()])
    bad = []
    for kind, s, sp, d, dp in edges:
        faces = ef.get(s, {}).get(sp)
        if not faces:
            bad.append(f"{kind} {s}.{sp} -> {d}.{dp}: port never emitted")
            continue
        for f in sorted(faces):
            if ring._walk(lay, s, f, d) is None:
                bad.append(f"{kind} {s}.{sp} -> {d}.{dp}: face "
                           f"{ring.FACE_NAME[f]} never reaches it")
    assert not bad, "edges on no real forwarding walk:\n  " + "\n  ".join(bad)
    # The counter's own corridors, pinned: drn -> tap3 on the resting walk at
    # hop 8, and tap3's existing north flip onto add3 at hop 1 (the bump's
    # literal @1). The row3 literals ride the same walk the drain spins use.
    assert ring._walk(lay, "drn", ring.FACE_OF["north"], "tap3") == 8
    assert ring._walk(lay, "drn", ring.FACE_OF["north"], "row3") == 7
    assert ring._walk(lay, "tap3", ring.FACE_OF["north"], "add3") == 1


def test_increment_mode_backward_jump_discipline_holds():
    """INV-48 rule 2 / INV-53 over the increment edge set: at most one
    backward jump per cell, and it is that cell's highest-addressed JUMP.
    All three new edges are FORWARD (tap3 follows drn in program order), so
    the set of backward-jump cells is IDENTICAL to fixed mode's."""
    b = _inc()
    progs = b.build_cell_programs()
    idx = {c: i for i, c in enumerate(progs)}
    backward = {}
    for s, sp, d, dp in b.internal_jumps():
        if idx[d] < idx[s]:
            backward.setdefault(s, []).append((sp, d, dp))
    for cid, edges in backward.items():
        assert len(edges) == 1, (cid, edges)
        (sp, _d, _dp), = edges
        code = [ln.strip() for ln in progs[cid].assembly_template.splitlines()
                if ln.strip() and not ln.strip().endswith(":")]
        jaddrs = [i for i, ln in enumerate(code) if "{jump:" in ln]
        assert code[max(jaddrs)] == f"{{jump:{sp}}}", (
            f"{cid}'s backward jump '{sp}' is not its highest-addressed JUMP")
    assert "drn" not in backward and "tap3" not in backward


def test_entry_addresses_stay_distinct_where_edges_resolve():
    """The shipped seq.step/row0.pub pin, re-checked WITH the params (INV-6/11):
    neither cell changes in increment mode, but entry addresses are global
    coincidence hazards, so the pair is re-asserted under the new params."""
    from gr_kyttar.placement.resolver import CellProgramResolver
    R = CellProgramResolver()
    ent = {c: R.compute_entry_addresses(p)
           for c, p in _inc().build_cell_programs().items()}
    assert ent["seq"]["step"] != ent["row0"]["pub"]


def test_reset_spec_excludes_exactly_the_three_counter_copies():
    """The batch reset keeps fifteen state words and releases ONE: the counter.

    Diffing the flagged-register sets, increment mode removes EXACTLY row3's
    slot-0 pair and add3's slot-0 addend pair from the reset spec — and drn's
    ``ch``/``cl`` (which exist only in increment mode) are born unflagged.
    Anything else missing from the reset is a stale-state bug the shipped
    second-batch gate exists to catch.
    """
    def flagged(blk):
        out = set()
        for cid, cp in blk.build_cell_programs().items():
            out |= {(cid, sv.name) for sv in cp.state if sv.reset_per_batch}
            out |= {(cid, dw.name) for dw in cp.data if dw.reset_per_batch}
        return out

    fx = flagged(ChaCha20KeystreamBlock("f"))
    inc_b = _inc()
    inc = flagged(inc_b)
    assert fx - inc == {("row3", "s0h"), ("row3", "s0l"),
                        ("add3", "k0h"), ("add3", "k0l")}
    assert inc - fx == set()
    drn_state = {sv.name: sv for sv in inc_b.build_cell_programs()["drn"].state}
    assert not drn_state["ch"].reset_per_batch
    assert not drn_state["cl"].reset_per_batch
    assert drn_state["lap"].reset_per_batch


def test_the_bump_literals_are_derived_from_add3s_real_program():
    """INV-63 discipline: the bump's ``WRITE @1, addr`` operands equal the
    addresses add3's program actually pins for ``k0h``/``k0l``, and the drn
    literals equal row3's pinned slot-0 registers — resolved, never typed."""
    b = _inc()
    progs = b.build_cell_programs()
    a3 = {d.name: d.address for d in progs["add3"].data}
    r3 = {s.name: s.register for s in progs["row3"].state}
    tap3 = progs["tap3"].assembly_template
    drn = progs["drn"].assembly_template
    assert f"WRITE @1, {a3['k0h']}" in tap3
    assert f"WRITE @1, {a3['k0l']}" in tap3
    assert f"WRITE @7, {r3['s0h']}" in drn
    assert f"WRITE @7, {r3['s0l']}" in drn
    # ...and the release trigger is done's LAST emission (INV-52: it must
    # trail the counter words through tap0).
    done = drn.split("done:")[1]
    lines = [l.strip() for l in done.splitlines() if l.strip()]
    assert lines[-1] == "{jump:rel}"


def test_process_reference_follows_the_mode():
    """The float/int reference models the mode: fixed repeats block
    ``counter``; increment advances per block — §2.4's consumption."""
    import numpy as np
    fx = ChaCha20KeystreamBlock("f", counter=7)
    inc = ChaCha20KeystreamBlock("i", counter=7, counter_mode="increment")
    two = np.zeros(2)
    rf = fx.process_reference(two)
    ri = inc.process_reference(two)
    b7 = g.state_to_words16(g.block_function(KEY, NONCE, 7))
    b8 = g.state_to_words16(g.block_function(KEY, NONCE, 8))
    assert rf.tolist() == b7 + b7
    assert ri.tolist() == b7 + b8


# ===========================================================================
# ON CHIP — the gate that defines done. Placed + routed + built, N real
# batches through the REAL packet-boundary path (batch_reset_writes applied
# exactly as SimServer.process_batch does), every batch's stop_reason read.
# ===========================================================================
def _host_chip(params):
    """Place, route, build and load the block; return (chip, landings, resets)."""
    simkyt = pytest.importorskip("simkyt")
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
    ctrl.new_project("chacha_ctr", key)
    blk = ctrl.place_block("ChaCha20KeystreamBlock", 0, 0, 0,
                           library="lattrex.official", params=params)
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="sample"),
                                name="in_blk")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="blk_out")
    rep = ctrl.auto_route_all({key: ct})
    assert rep.ok, [f"{r.name}:{r.reason}" for r in rep.failed]
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, bres.errors
    land = bres.chips[0].input_landings["in_blk"]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["entry"]))
    return chip, land, bres.chips[0].batch_reset_writes


def _one_batch(chip, land):
    """One trigger; returns (words, final_run_record)."""
    chip.inject_data_physical([1], target_hop_cnt=int(land["hop"]),
                              target_addr=int(land["data_addrs"][0]))
    chip.run(max_events=6000)
    chip.inject_jump_physical(target_hop_cnt=int(land["hop"]),
                              entry_addr=int(land["entry"]))
    words, final = [], None
    for _ in range(50):
        r = chip.run(max_events=200000)
        final = r
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            words.extend(int(v) & 0xFFFF for v in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        if r.get("completed"):
            break
    return words, final


def _run_batches(chip, land, resets, n):
    """n consecutive batches through the real packet-boundary path.

    INV-56: stop_reason is read for EVERY batch — a deadlock and a clean run
    are indistinguishable by word count alone when both are wrong.
    """
    out = []
    for batch in range(n):
        if batch:
            for (x, y, addr, value) in resets:
                chip.write_cell_memory(chip.cell_id_at(int(x), int(y)),
                                       int(addr), int(value) & 0xFFFF)
        words, fin = _one_batch(chip, land)
        assert fin.get("completed") and fin.get("stop_reason") == "QueueEmpty", (
            f"batch {batch}: {fin}")
        out.append(words)
    return out


def _as_state(words):
    assert len(words) == 32, f"emitted {len(words)} words, want 32"
    return [(words[2 * i] << 16) | words[2 * i + 1] for i in range(16)]


def test_seven_consecutive_batches_are_seven_consecutive_blocks_on_chip():
    """THE VALUE GATE. Seven real batches in increment mode: batch ``k``'s 32
    words are ALL SIXTEEN state words of ``block_function(key, nonce, 1+k)``,
    bit-exact, IN §2.3.2 ORDER — a one-word or one-batch check certifies
    nothing (the shipped suite's 19-realignment bug hid behind exactly that).

    Seven batches cover the §2.4.2 encryption (2 blocks), the secure_link
    payload shape (512 bytes = 8 blocks needs the counter sound through +7,
    proven by induction the moment any k fails), and every reset-boundary
    hazard three times over. Batch 0 doubles as the §2.3.2 vector itself.
    """
    chip, land, resets = _host_chip({"counter_mode": "increment"})
    assert resets, "the block declares reset_per_batch state; none resolved"
    batches = _run_batches(chip, land, resets, 7)
    for k, words in enumerate(batches):
        got = _as_state(words)
        want = list(g.block_function(KEY, NONCE, 1 + k))
        assert got == want, (
            f"batch {k} differs from block_function(counter={1 + k}):\n"
            f"  got  {[f'{v:#010x}' for v in got]}\n"
            f"  want {[f'{v:#010x}' for v in want]}")


def test_the_counter_carries_across_0xffff_on_chip():
    """The 16-bit seam. ``counter=0xFFFF``: the first increment must carry into
    the high half (0xFFFF -> 0x10000 -> 0x10001) — the case a carry-less
    increment gets wrong while every low-half counter looks perfect."""
    chip, land, resets = _host_chip({"counter_mode": "increment",
                                     "counter": 0xFFFF})
    batches = _run_batches(chip, land, resets, 3)
    for k, want_ctr in enumerate((0xFFFF, 0x10000, 0x10001)):
        got = _as_state(batches[k])
        assert got == list(g.block_function(KEY, NONCE, want_ctr)), (
            f"batch {k} is not block {want_ctr:#x}")


def test_multiblock_encryption_matches_rfc8439_242_on_chip():
    """END USE: §2.4.2's own multi-block vector, keystream FROM THE CHIP.

    The RFC's §2.4.2 encryption spans two consecutive keystream blocks with
    the counter starting at 1 — exactly two increment-mode batches. XORing the
    RFC plaintext against the chip's serialised batch outputs must reproduce
    the RFC ciphertext byte for byte. This is the multi-block use the mode
    exists for, driven end to end.
    """
    chip, land, resets = _host_chip({"counter_mode": "increment",
                                     "counter": g.RFC8439_ENCRYPT_COUNTER,
                                     "key": g.RFC8439_ENCRYPT_KEY,
                                     "nonce": g.RFC8439_ENCRYPT_NONCE})
    n_blocks = -(-len(g.RFC8439_ENCRYPT_PLAINTEXT) // 64)
    assert n_blocks == 2
    ks = bytearray()
    for words in _run_batches(chip, land, resets, n_blocks):
        ks += g.serialize(_as_state(words))
    ct = bytes(p ^ k for p, k in zip(g.RFC8439_ENCRYPT_PLAINTEXT, ks))
    assert ct == g.RFC8439_ENCRYPT_CIPHERTEXT


# ===========================================================================
# INV-4 — the gate is worthless until PROVEN TO FAIL. Three real on-chip
# mutants, each built + placed + routed + run with the fault installed.
# ===========================================================================
def _mutate_drn(monkeypatch, old: str, new: str):
    """Monkeypatch the class so the built chip carries a corrupted ``done``."""
    orig = ChaCha20KeystreamBlock._drn

    def mutant(self):
        cp = orig(self)
        if self.counter_mode == "increment":
            assert old in cp.assembly_template, "mutation target vanished"
            cp.assembly_template = cp.assembly_template.replace(old, new)
        return cp

    monkeypatch.setattr(ChaCha20KeystreamBlock, "_drn", mutant)


def test_mutation_a_frozen_counter_fails_every_batch_after_the_first(
        monkeypatch):
    """Drop the increment (``ADD cl, one`` -> ``ADD cl, zero``): the classic
    keystream-reuse catastrophe. Batch 0 must still pass — which is exactly
    why a single-batch gate certifies nothing — and every later batch must
    repeat batch 0's bytes and FAIL the consecutive-block expectation."""
    _mutate_drn(monkeypatch, "ADD R{state:cl}, R{data:one}",
                "ADD R{state:cl}, R{data:zero}")
    chip, land, resets = _host_chip({"counter_mode": "increment"})
    batches = _run_batches(chip, land, resets, 3)
    assert _as_state(batches[0]) == list(g.block_function(KEY, NONCE, 1))
    for k in (1, 2):
        got = _as_state(batches[k])
        assert got != list(g.block_function(KEY, NONCE, 1 + k)), (
            f"the frozen-counter mutant PASSED batch {k} — the gate is vacuous")
        assert got == _as_state(batches[0]), (
            "the frozen counter must repeat batch 0 (keystream reuse)")


def test_mutation_a_carry_less_increment_fails_at_the_seam(monkeypatch):
    """``ADC ch, zero`` -> ``ADD ch, zero`` (the carry dropped): every
    counter that stays under 0x10000 still comes out perfect, so the fault is
    invisible until the seam — driven explicitly with counter=0xFFFF. Batch 1
    must fail its 0x10000 expectation, and must equal block 0 (the wrapped
    low half with the high half stuck), pinning the failure's shape."""
    _mutate_drn(monkeypatch, "ADC R{state:ch}, R{data:zero}",
                "ADD R{state:ch}, R{data:zero}")
    chip, land, resets = _host_chip({"counter_mode": "increment",
                                     "counter": 0xFFFF})
    batches = _run_batches(chip, land, resets, 2)
    assert _as_state(batches[0]) == list(g.block_function(KEY, NONCE, 0xFFFF))
    got = _as_state(batches[1])
    assert got != list(g.block_function(KEY, NONCE, 0x10000)), (
        "the carry-less mutant PASSED the seam — the gate is vacuous")
    assert got == list(g.block_function(KEY, NONCE, 0)), (
        "a dropped carry wraps to counter 0; anything else is a second fault")


def test_mode_isolation_an_increment_chip_fails_the_fixed_second_batch_gate():
    """The third mutant is MODE LEAKAGE, and the shipped suite is its gate:
    ``test_a_second_batch_recomputes_the_block_bit_exact_on_chip`` asserts
    ``second == first`` on a fixed chip. Run that gate's exact procedure on an
    INCREMENT chip: the equality must fail (batch 1 is block 2, not block 1).
    So an increment that leaked into fixed mode — the programs are selected by
    the param — would trip the shipped gate, and the shipped gate passing
    (it runs in this same session's suite) proves the isolation both ways."""
    chip, land, resets = _host_chip({"counter_mode": "increment"})
    batches = _run_batches(chip, land, resets, 2)
    first, second = batches
    assert second != first, (
        "an increment-mode chip repeated its first batch — the increment "
        "is not firing, and the fixed-mode gate could never catch leakage")
    assert _as_state(second) == list(g.block_function(KEY, NONCE, 2))
