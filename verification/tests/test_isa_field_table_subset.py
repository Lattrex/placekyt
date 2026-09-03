# SPDX-License-Identifier: GPL-3.0-or-later
"""INV-34 — ISA conformance: shift counts are IMMEDIATE instruction fields.

Per the architecture spec (v0.11 §4.10), a shift/rotate word is
``OP | ROT[11] | RSVD[10] | CNT[9:6] | SRC[5:0]``: CNT is an immediate count
(0-15) and bit[10] is reserved. There is no register-count shift form — the
assembler rejects ``[Rm]`` count syntax and the decoder treats bit[10] as
reserved.

This gate source-scans every block module so a data-dependent shift can never
land in a cell program. Data-dependent shift amounts are expressed with
immediate-shift constructions instead: fixed-position extraction over a
left-aligned working register (VaricodeEncoder), an arithmetic identity such
as ``x << b == x + x*b`` for a 0/1 count (VaricodeDecoder), a CMP-guarded
``SHL #1`` (CMP tests without touching R0), or a shift-by-one loop.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BLOCKS_DIR = _ROOT / "runtime" / "python" / "gr_kyttar" / "placement" / "blocks"

# Matches a register-count shift/rotate in an assembly template:
#   SHL R0, [R7]   /   SHR R0, [R{state:n}]   /   ROL x, [Rm]
_REG_COUNT_SHIFT = re.compile(r"\b(SHL|SHR|ROL|ROR)\b[^,\n]*,\s*\[R", re.I)


def test_shift_counts_are_immediate():
    """No block may use a ``[Rm]`` shift count — CNT[9:6] is an immediate
    field (INV-34). Restructure with the immediate-shift patterns in the
    module docstring."""
    hits = {}
    for f in sorted(_BLOCKS_DIR.glob("*.py")):
        lines = [i + 1 for i, line in enumerate(f.read_text().splitlines())
                 if _REG_COUNT_SHIFT.search(line)]
        if lines:
            hits[f.name] = lines
    assert not hits, (
        f"register-count shift syntax found in: {hits}. Shift counts are "
        "immediate instruction fields (#0-15, INV-34) — the assembler "
        "rejects [Rm].")


def test_assembler_rejects_register_count_syntax():
    """The simulator's assembler must reject ``[Rm]`` shift counts outright
    (the root guard — nothing that assembles can carry a data-dependent
    shift)."""
    import sys
    rt = str(_ROOT / "runtime" / "python")
    if rt not in sys.path:
        sys.path.insert(0, rt)
    import simkyt

    assert simkyt.Program.from_source("t", "SHR R3, #5\nHALT", 0).get_words()
    for bad in ("SHR R3, [R5]", "SHL R3, [R5]", "ROR R4, [R6]"):
        try:
            simkyt.Program.from_source("t", bad + "\nHALT", 0)
        except Exception:
            continue
        raise AssertionError(f"assembler accepted {bad!r} — the immediate-only "
                             "guard is gone")
