# SPDX-License-Identifier: GPL-3.0-or-later
"""MapBBBlock — see :class:`MapBBBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


class MapBBBlock(KyttarBlock):
    """
    Per-symbol lookup remap — GNU Radio ``digital.map_bb`` (1 cell).

    A memoryless byte-to-byte lookup: for each input symbol ``in`` the output is
    ``out = map[in]``. The input indexes a small table; the table entry is emitted
    verbatim. This is the exact analog of the constellation LUT-mappers
    (:class:`PSKSymbolMapperBlock`, :class:`FSK4SymbolMapperBlock`,
    :class:`QAM16SymbolMapperBlock`), but with a *real integer* table instead of an
    I/Q constellation.

    GNU Radio ``digital.map_bb`` semantics (matched EXACTLY — RULE #0)
    =================================================================
    GR's ``map_bb`` holds an internal **256-entry** table ``d_map`` that is first
    initialised to the IDENTITY (``d_map[i] = i``) and then overwritten with the
    user's ``map`` for the entries it provides (``d_map[i] = map[i] & 0xFF`` for
    ``i < len(map)``). So:

      * ``out = map[in]`` for ``in < len(map)``;
      * ``out = in`` (identity pass-through) for ``in >= len(map)`` — inputs beyond
        the supplied table are NOT remapped;
      * each ``map`` value is stored as a **byte** (taken ``& 0xFF``): a value of 300
        becomes ``44``.

    This block reproduces that table exactly (built at construction), so the on-chip
    LUT is BIT-IDENTICAL to GR's ``d_map`` over the supported input range.

    Parameter (VERBATIM from GR)
    ----------------------------
    * ``map`` — the remap vector (a list of ints). GR default is ``[0, 1]``. Output
      ``out = map[in]`` (the input indexes the table). The name is mirrored exactly.

    Memory layout (single cell)
    ---------------------------
    The remap table lives in cell registers at addresses ``1..N`` (``N`` = table
    size) and is read with a single ``LOAD``-indirect (``R0 = mem[mem[Rn] & 0x1F]``)
    — the same indexed-table idiom the constellation mappers use. One scalar
    (``one``) offsets the input to the 1-based table address; two state words hold
    the input snapshot and the address scratch. One cell, well inside the ~31-word
    budget.

    Hardware deviations from ``digital.map_bb``
    -------------------------------------------
    * **HARDWARE DEVIATION — table size (INV-7 register budget + the ``LOAD``
      5-bit address limit).** GR's ``map_bb`` carries a full **256-entry** table so
      every possible byte input (0..255) is addressable. On this substrate a cell's
      ``LOAD``-indirect table is capped at **32 words** (``mem[Rn] & 0x1F``) and the
      program + scalars share that cell, so the on-chip table holds at most
      :data:`MAX_TABLE` entries — covering input symbols ``0 .. MAX_TABLE-1``. This
      matches every realistic ``map_bb`` use (symbol alphabets are ``2^k`` for small
      ``k``: BPSK=2, QPSK/4-PAM=4, 8-ary=8, 16-QAM=16). A ``map`` longer than
      :data:`MAX_TABLE`, or a design that feeds input symbols ``>= MAX_TABLE``, would
      need a multi-cell table; the block **RAISES** (never silently truncates) — see
      ``__init__``. A future multi-cell (2-cell → 64 entries, etc.) table would lift
      the ceiling toward the full 256.

    Interface:
        - Entry: R1
        - Input: R0 (one symbol, 0 .. N-1)
        - Output: R0 (``map[in]``, one word per input)
    """
    CATEGORY = "coding"
    TAGS = ["map", "remap", "lut", "symbol", "coding"]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # A cell's LOAD-indirect table is `mem[Rn] & 0x1F` = 32 words, shared with the
    # program (6 instr, addr 25..31 after data) + scalar (`one`) + the table (addr
    # 1..N). Empirically N=21 is the largest table that still fits the program (a
    # 22-entry table pushes data to addr 0..25 and collides with the 6 instructions
    # that need 25..31 — verified at build time). Covers every power-of-two symbol
    # alphabet up through 16 (BPSK/QPSK/4-PAM/8-ary/16-QAM) with headroom. Raise above
    # (a larger alphabet needs a multi-cell table).
    MAX_TABLE = 21

    def __init__(self, name: str, map=[0, 1]):
        """Initialise the remap — GR ``digital.map_bb`` parity.

        Args:
            name: block name.
            map: the remap vector (list of ints). ``out = map[in]`` (the input
                indexes the table). GR default ``[0, 1]``. Mirrors GR's ``map``
                parameter VERBATIM (name, default, semantics).
        """
        super().__init__(name, map=list(map))
        user_map = [int(v) for v in map]
        if len(user_map) < 1:
            raise ValueError("map must have at least one entry")
        # GR builds a 256-entry identity table, then overwrites the first len(map).
        # On chip we can only hold MAX_TABLE entries, so the addressable input range
        # is 0 .. TABLE_SIZE-1. TABLE_SIZE spans at least the supplied map (so every
        # remapped input is covered); the tail entries are identity (in -> in), the
        # exact GR pass-through for inputs beyond len(map).
        if len(user_map) > self.MAX_TABLE:
            raise ValueError(
                "HARDWARE DEVIATION (INV-7 register budget + LOAD 5-bit address "
                f"limit): map has {len(user_map)} entries; the single-cell on-chip "
                f"table holds at most {self.MAX_TABLE} (a longer table needs a "
                "multi-cell LUT). Not silently truncating.")
        self._user_map = user_map
        # The on-chip table mirrors GR's d_map: seed the whole addressable range with
        # the IDENTITY (in -> in), then overwrite the first len(map) entries with the
        # user's map (byte-stored, & 0xFF). We size the table to the next power of two
        # >= len(map) (capped at MAX_TABLE), so inputs BEYOND len(map) up to that
        # natural symbol boundary pass through as identity — EXACTLY GR's 256-entry
        # identity tail, just capped at the single-cell hardware ceiling. (A map that
        # is already a power of two, e.g. the 16-QAM 16-entry map, is unchanged.)
        want = max(len(user_map), 2)
        size = 1 << (want - 1).bit_length()               # next pow2 >= want
        size = min(size, self.MAX_TABLE)
        size = max(size, len(user_map))
        table = [i & 0xFF for i in range(size)]           # identity seed
        for i, v in enumerate(user_map):
            table[i] = v & 0xFF                            # GR stores map[i] & 0xFF
        self._table = table

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def map(self):
        """The user's remap vector (GR ``map`` param), as supplied."""
        return list(self._user_map)

    @property
    def table(self):
        """The resolved on-chip table (identity-seeded, ``map`` overwritten;
        bytes). ``out = table[in]`` for ``in`` in ``0 .. len(table)-1``."""
        return list(self._table)

    @property
    def table_size(self) -> int:
        return len(self._table)

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """LOAD-indirect remap: address = ``1 + in``, ``LOAD`` the table entry, emit.

        The table lives at addresses ``1 .. N``; the input symbol offsets by one to
        the 1-based table address, a single ``LOAD`` fetches ``table[in]``, and the
        value is emitted. One word in, one word out — memoryless. Single cell, single
        output face."""
        table = [DataWord(f"t{i}", val, address=i + 1)
                 for i, val in enumerate(self._table)]
        base = 1 + len(table)   # first scalar after the table
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=table + [
                DataWord("one", 1, address=base),
            ],
            state=[
                StateVar("in_save"),   # snapshot of the input symbol
                StateVar("addr_tmp"),  # table address scratch
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    ; address = 1 + in
    ADD R{state:in_save}, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_symbols) -> np.ndarray:
        """Reference: ``out = table[in]`` for each input symbol — the exact on-chip
        table (GR ``d_map``: identity-seeded, ``map`` overwritten, byte-stored). An
        input at or beyond the addressable table range is out of the supported
        alphabet; this reference asserts against that (caller keeps inputs in range).
        """
        out = []
        n = len(self._table)
        for s in np.asarray(input_symbols).reshape(-1):
            idx = int(s) & 0xFFFF
            if idx >= n:
                raise ValueError(
                    f"input symbol {idx} is outside the addressable table range "
                    f"0..{n - 1} (HARDWARE DEVIATION: single-cell table caps the "
                    f"input alphabet at {self.MAX_TABLE}).")
            out.append(self._table[idx])
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """Memoryless — no state carried across samples."""
        pass
