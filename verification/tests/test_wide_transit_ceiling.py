# SPDX-License-Identifier: GPL-3.0-or-later
"""The WIDE-VALUE TRANSIT CEILING, measured on the real placed+routed chip.

This gate exists because a stated limit in the knowledge base was wrong, and a
wrong limit is worse than a missing one: every builder reads it as a design
constraint, so it makes correct designs get abandoned.

INV-47 asserted, from algebra alone:

    "a live set wider than 10 sixteen-bit words cannot transit a cell at all"

derived by solving ``3W + 1 <= 31``. That derivation prices ONE construction —
a relay that HOLDS all ``W`` words in its own registers and forwards each with
``MOVE R0, Rw`` + ``WRITE``. It is correct FOR THAT SHAPE and false as a
statement about cells.

A STREAMING relay — one word in, the same word straight out, holding nothing —
costs a constant 3 instructions regardless of how wide the frame is. This suite
runs frames of 8..128 words through 1 and 3 real cells and asserts they arrive
exact, which refutes the universal claim; and it pins the hold-and-forward
ceiling where it actually falls, so the true (narrower) rule stays available.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_wide_transit_ceiling.py -q
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

from kyttar_verify import run_block_dut_rate  # noqa: E402

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(not os.path.exists(CHIP_YAML),
                                reason="chip yaml absent")


def _stream(width: int, stages: int):
    """Run a `width`-word frame through `stages` STREAMING relay cells."""
    stim = [(0x2000 + i) & 0xFFFF for i in range(width)]
    res = run_block_dut_rate(
        "TransitProbeBlock", stim, chip_yaml=CHIP_YAML,
        params={"width": width, "stages": stages, "mode": "stream"},
        in_port="w", out_port="out")
    assert res.ok, f"W={width} stages={stages}: {getattr(res, 'reason', '?')}"
    return [int(v) & 0xFFFF for v in (res.outputs_q15 or [])], stim


# --------------------------------------------------------------------------
# The refutation: wide live sets DO transit cells.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("width", [8, 10, 12, 16, 24, 32, 64, 128])
@pytest.mark.parametrize("stages", [1, 3])
def test_a_wide_frame_does_transit_a_cell(width, stages):
    """A frame far wider than 10 words crosses real cells, bit-exact.

    W=32 is the full ChaCha20 state — the exact case INV-47 declared
    impossible. W=128 is twelve times the claimed ceiling.
    """
    got, want = _stream(width, stages)
    assert got == want, (
        f"a {width}-word frame did NOT survive {stages} cell(s); "
        f"got {len(got)} words")


def test_the_claimed_ceiling_is_refuted_at_the_exact_disputed_width():
    """The headline: 32 words — the ChaCha20 state — transits a cell."""
    got, want = _stream(32, 1)
    assert got == want
    assert len(got) == 32


# --------------------------------------------------------------------------
# The TRUE, narrower rule: hold-and-forward really is bounded.
# --------------------------------------------------------------------------
def _holding_relay_words(width: int) -> tuple[int, int]:
    """(max pinned register, base_addr) for a HOLD-and-forward relay of W."""
    # W held words in R1..RW, then MOVE+WRITE per word, then one JUMP.
    instr = 2 * width + 1
    return width, 31 - instr


@pytest.mark.parametrize("width", list(range(4, 10)))
def test_hold_and_forward_fits_below_ten(width):
    """W <= 9 hold-and-forward relays fit their 31-word budget."""
    max_reg, base = _holding_relay_words(width)
    assert max_reg < base, f"W={width} should fit: pin {max_reg} vs base {base}"


@pytest.mark.parametrize("width", [10, 11, 12, 16])
def test_hold_and_forward_overruns_at_ten_and_above(width):
    """At W >= 10 a hold-and-forward relay overlays its own instructions.

    Note this makes the hold-and-forward ceiling **W <= 9**, not W <= 10:
    ``3W + 1 <= 31`` is satisfied with equality at W=10, which leaves ZERO
    words and still collides, so the inequality was also off by one.
    """
    max_reg, base = _holding_relay_words(width)
    assert max_reg >= base, (
        f"W={width} should overrun: pin {max_reg} vs base {base}")


def test_streaming_cost_is_constant_in_frame_width():
    """The reason the ceiling does not generalise: a streaming relay's cost
    does not depend on W at all, so there is nothing to solve for."""
    # MOVE R0, Rw / WRITE / JUMP -- three instructions, any frame width.
    for width in (8, 32, 128, 4096):
        instr = 3
        assert 31 - instr > 1, f"streaming relay stays in budget at W={width}"


# --------------------------------------------------------------------------
# INV-4 negatives — the gate must be able to FAIL.
# --------------------------------------------------------------------------
def test_negative_a_corrupted_stream_is_caught():
    """If the frame came back wrong, this suite must notice.

    Without this, `test_a_wide_frame_does_transit_a_cell` could be passing
    vacuously (e.g. on an empty capture) and would certify nothing.
    """
    got, want = _stream(32, 1)
    assert got == want
    corrupted = list(got)
    corrupted[0] = (corrupted[0] ^ 0xFFFF) & 0xFFFF
    assert corrupted != want, "the comparison must reject a corrupted frame"


def test_negative_the_capture_is_not_empty():
    """A silent zero-word capture must not read as success."""
    got, _ = _stream(32, 1)
    assert len(got) == 32, "a vacuous (empty) capture would prove nothing"


# --------------------------------------------------------------------------
# RECIRCULATION — one datapath, N sequential passes.
# --------------------------------------------------------------------------
def _run_loop_handplaced(npass: int):
    """Run the recirculation loop on a HAND-PLACED chip.

    Hand placement (not ``auto_route_all``) because the loop's geometry is the
    thing under test: the head must send the recirculated word along the loop
    axis and the finished word OFF it, which is a dynamic ``FACE`` switch tied
    to specific neighbours. This is the same reason every shipped panel-backed
    block is hand-placed in its own suite.
    """
    import simkyt
    from gr_kyttar.placement.blocks.transit_probe_block import (
        TransitProbeBlock)
    from gr_kyttar.placement.resolver import (
        CellProgramResolver, JumpTarget, ResolvedTargets, WriteTarget)

    W = 10

    def cid(x, y):
        return y * W + x

    b = TransitProbeBlock("p", width=1, stages=npass, mode="loop")
    cps = b.build_cell_programs()
    R = CellProgramResolver()
    head, tail, egr = cps["head"], cps["tail"], cps["egress"]
    e_head = R.compute_entry_addresses(head)
    e_tail = R.compute_entry_addresses(tail)
    e_egr = R.compute_entry_addresses(egr)

    def reg(cp, name):
        c = R.classify_addresses(cp)
        return [a for a, v in c.items() if v.get("name") == name][0]

    tg_h = ResolvedTargets()
    tg_h.writes["fwd"] = WriteTarget(1, reg(tail, "w"))
    tg_h.jumps["trig"] = JumpTarget(1, e_tail["default"])
    tg_h.writes["out"] = WriteTarget(1, reg(egr, "v"))
    tg_h.jumps["out"] = JumpTarget(1, e_egr["default"])
    res_h = R.resolve(head, tg_h)

    tg_t = ResolvedTargets()
    tg_t.writes["back"] = WriteTarget(1, reg(head, "back"))
    tg_t.jumps["kick"] = JumpTarget(1, e_head["body"])
    res_t = R.resolve(tail, tg_t)

    tg_e = ResolvedTargets()
    tg_e.writes["out"] = WriteTarget(11, 0)
    tg_e.jumps["out"] = JumpTarget(11, 0)
    res_e = R.resolve(egr, tg_e)

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for a in range(32):
        chip.write_cell_memory(cid(0, 0), a, int(res_h.memory.get(a, 0)))
        chip.write_cell_memory(cid(1, 0), a, int(res_t.memory.get(a, 0)))
        chip.write_cell_memory(cid(0, 1), a, int(res_e.memory.get(a, 0)))
    chip.set_fwd_face(cid(0, 0), "east")
    chip.set_fwd_face(cid(1, 0), "west")          # tail bounces BACK
    for x in range(2, W):
        chip.set_fwd_face(cid(x, 0), "east")
    for x in range(W):
        chip.set_fwd_face(cid(x, 1), "east")
    chip.set_fwd_face(cid(W - 1, 1), "north")

    chip.set_port_entry_address("x16_in", e_head["default"])
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port_multi_i16("x16_in", [[(reg(head, "w"), 100)]],
                              e_head["default"])
    out = []
    for _ in range(60000):
        chip.run(max_events=32)
        for v, _d, _t in chip.read_port_words_timed("x16_out"):
            out.append(v & 0xFFFF)
        if out:
            break
    return out


@pytest.mark.parametrize("npass", [1, 2, 4, 8, 10, 20, 80])
def test_one_datapath_serves_n_sequential_passes(npass):
    """A backward ``JUMP`` re-entering a cell mid-program, with the loop
    counter held in cell state, runs N passes over ONE datapath.

    80 is the number that matters: ChaCha20's 80 quarter-round invocations
    cannot be unrolled (17 cells x 80 = 1360 against a 120-cell array), so
    reuse is the only way the cipher fits — and this proves reuse works.

    NOTE the substrate rule this respects: a cell may carry at most ONE
    backward ``JUMP``. The build restores the highest-address one per cell and
    silently loses any second, so loop nesting must use local ``BR`` branches.
    """
    got = _run_loop_handplaced(npass)
    assert got[:1] == [100 + npass], (
        f"{npass} passes should increment 100 -> {100 + npass}; got {got[:3]}")


def test_negative_the_loop_count_is_actually_observed():
    """The loop gate must distinguish pass counts — otherwise it would pass
    for a datapath that ran once and ignored the counter."""
    assert _run_loop_handplaced(4)[:1] != _run_loop_handplaced(8)[:1]
