# SPDX-License-Identifier: GPL-3.0-or-later
"""Repo-wide static gates for two defect classes that BUILD CLEANLY and RUN
WRONG — found on FFT64, applicable to every multi-cell block.

1. **INV-33 (overlap half)** — a cell that is EXACTLY 32/32 words has its
   pinned STATE laid on top of its own first instruction. The resolver's own
   guard only compares DATA against the instruction base, never state, so the
   cell assembles, loads, runs ONCE, and then zeroes the word the next trigger
   enters at. Symptom: the block emits one sample and goes quiescent.

2. **INV-35 (dead entry)** — in the multi-entry dispatch idiom a cell's PATH
   identity travels as WHICH ENTRY the next cell is jumped at. An
   ``EntryPoint`` that no ``internal_jumps`` edge targets is unreachable: the
   cell still fits, still runs, and takes the wrong path forever. On FFT64
   this made 2 of 32 twiddle slots wrong and put the entire odd-bin half of
   every frame out.

Both are STATIC — no chip, no simulator — and both would have caught the
FFT64 faults before a single build.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_cell_program_reachability.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "runtime" / "python") not in sys.path:
    sys.path.insert(0, str(_ROOT / "runtime" / "python"))

from gr_kyttar.placement.block import (  # noqa: E402
    CellProgram, DataWord, EntryPoint, Port, StateVar)
from gr_kyttar.placement.resolver import CellProgramResolver  # noqa: E402


def instruction_region_overlaps(cp: CellProgram):
    """``({name: addr}, base_addr)`` for data/state inside the instructions."""
    res = CellProgramResolver()
    base = 31 - res.count_instructions(cp)
    bad = {}
    for d in (cp.data or ()):
        if d.address is not None and d.address >= base:
            bad[f"data:{d.name}"] = d.address
    data_map = res._allocate_data(cp.data)
    gap = list(range(max(data_map.values(), default=-1) + 1, base))
    for name, addr in res._allocate_state(cp.state, gap).items():
        if addr >= base:
            bad[f"state:{name}"] = addr
    return bad, base


def unreachable_entries(block):
    """``{cell: [entry, ...]}`` for entries no internal jump targets.

    Only cells with MORE THAN ONE entry are checked: a single-entry cell is
    always reached by whatever jumps at it (and by the block's own external
    entry), so it cannot have a dead dispatch path.
    """
    try:
        progs = block.build_cell_programs()
        jumps = block.internal_jumps()
    except (NotImplementedError, AttributeError):
        return {}
    targeted = {}
    for edge in jumps:
        if len(edge) < 4:
            continue
        _src, _port, dst, entry = edge[0], edge[1], edge[2], edge[3]
        targeted.setdefault(dst, set()).add(entry)
    # The block's own external entry lands on its input cell's first entry.
    dead = {}
    for cid, cp in progs.items():
        names = [e.name for e in (cp.entries or ())]
        if len(names) < 2:
            continue
        hit = targeted.get(cid, set())
        missing = [n for n in names if n not in hit]
        # The FIRST entry doubles as the cell's default and may legitimately
        # be entered from outside the block (the landing cell).
        missing = [n for n in missing if n != names[0]]
        if missing:
            dead[cid] = missing
    return dead


# --------------------------------------------------------------- the blocks
def _fft_blocks():
    from gr_kyttar.placement.blocks.fft16_block import FFT16Block
    from gr_kyttar.placement.blocks.fft_large import FFT64Block
    return [("FFT16Block", FFT16Block("probe")),
            ("FFT64Block", FFT64Block("probe"))]


@pytest.mark.parametrize("name", ["FFT16Block", "FFT64Block"])
def test_no_cell_pins_state_into_its_instruction_region(name):
    """INV-33 overlap: nothing lives at or above ``31 - instr_count``."""
    blk = dict(_fft_blocks())[name]
    bad = {}
    for cid, cp in blk.build_cell_programs().items():
        if not cp.assembly_template:
            continue
        over, base = instruction_region_overlaps(cp)
        if over:
            bad[cid] = (over, base)
    assert not bad, f"{name}: data/state inside the instruction region: {bad}"


@pytest.mark.parametrize("name", ["FFT16Block", "FFT64Block"])
def test_every_declared_entry_is_reachable(name):
    """INV-35: every non-default entry of a multi-entry cell is jumped at."""
    blk = dict(_fft_blocks())[name]
    dead = unreachable_entries(blk)
    assert not dead, (
        f"{name}: declared entries no internal jump targets (dead dispatch "
        f"paths): {dead}")


# ------------------------------------------------------------- INV-4 teeth
def test_overlap_gate_has_teeth():
    """A cell one word over must be REPORTED, at its exact address."""
    cp = CellProgram(
        inputs=[Port("a", register=1)],
        outputs=[Port("y")],
        entries=[EntryPoint("default")],
        data=[DataWord(f"d{i}", i, address=2 + i) for i in range(20)],
        state=[StateVar("s", register=22, initial_value=0)],
        assembly_template=(
            "default:\n"
            "    MOVE R{state:s}, R{in:a}\n"
            "    ADD R{state:s}, R{data:d0}\n"
            "    MOVE R{state:s}, R0\n"
            "    MOVE R0, R{state:s}\n"
            "    {write:y}\n"),
    )
    over, base = instruction_region_overlaps(cp)
    # 5 instructions -> base 26; state at 22 is BELOW it, so this one is fine.
    assert over == {} and base == 26, (over, base)
    # Now pin the state INTO the instruction region and it must be caught.
    cp2 = CellProgram(
        inputs=cp.inputs, outputs=cp.outputs, entries=cp.entries,
        data=cp.data,
        state=[StateVar("s", register=27, initial_value=0)],
        assembly_template=cp.assembly_template)
    over2, base2 = instruction_region_overlaps(cp2)
    assert over2 == {"state:s": 27} and base2 == 26, (over2, base2)


def test_dead_entry_gate_has_teeth():
    """A block whose cell declares an entry nothing jumps at must be caught —
    modelled on the EXACT pre-fix FFT64 shape (swap wired only to `num`)."""
    class _FakeBlock:
        def build_cell_programs(self):
            two = CellProgram(
                inputs=[Port("k", register=1)],
                outputs=[Port("y"), Port("t_n"), Port("t_t")],
                entries=[EntryPoint("num"), EntryPoint("triv")],
                assembly_template=(
                    "num:\n"
                    "    MOVE R0, R{in:k}\n"
                    "    {write:y}\n"
                    "    {jump:t_n}\n"
                    "    HALT\n"
                    "triv:\n"
                    "    MOVE R0, R{in:k}\n"
                    "    {write:y}\n"
                    "    {jump:t_t}\n"),
            )
            src = CellProgram(
                inputs=[Port("k", register=1)],
                outputs=[Port("k_f"), Port("trig")],
                entries=[EntryPoint("default")],
                assembly_template=(
                    "default:\n"
                    "    MOVE R0, R{in:k}\n"
                    "    {write:k_f}\n"
                    "    {jump:trig}\n"),
            )
            return {"swap": src, "sign": two}

        def internal_jumps(self):
            return [("swap", "trig", "sign", "num")]      # `triv` never hit

    dead = unreachable_entries(_FakeBlock())
    assert dead == {"sign": ["triv"]}, dead
