# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical placement of a block's cells on a chip grid.

Mirrors the ``placement:`` section of a block in the ``.kyt`` schema::

    placement:
      chip: 0
      cells:
        - {cell_id: ff0, x: 7, y: 1, face: west}
        - {cell_id: ff1, x: 6, y: 1, face: west}
        - {cell_id: transit_fb_0, x: 8, y: 0, face: east}

Block-INTERNAL routing/feedback cells are FIRST-CLASS block cells: they live in
the same ``cells:`` list, tagged by a ``transit_*`` ``cell_id`` (a face-only
relay, no program). They share the block's identity/colour and count in its
footprint. Legacy ``.kyt`` files that stored them in a separate ``transit_cells:``
block (without a ``cell_id``) still load — each is given a synthesised
``transit_N`` id and merged into ``cells``. See :func:`is_transit_cell`.

A block with no ``placement`` (or an incomplete one) is "unplaced" — modeled by
``Block.placement is None`` rather than by an empty ``Placement`` here.

These are mutable dataclasses: the canvas edits cell coordinates and faces
through the command system, which writes back into these objects (the data
model is the single source of truth for positions per §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .enums import Face

# A cell identifier is whatever the block definition uses: an int like ``0`` or
# a string like ``"ff0"`` (§2.1 permits both). The original scalar type is
# preserved so integer ids round-trip unquoted through YAML.
CellId = Union[int, str]

# --- D4 orientation canonicalisation -----------------------------------------
# The block-transform ops form the dihedral group D4 (8 elements): any sequence of
# cw/ccw/mirror_h/mirror_v reduces to ONE of them. Left un-reduced, a user nudging a
# block's orientation builds a degenerate stack (e.g. ['mirror_v','cw','ccw','mirror_v']
# = identity) that the build re-applies op-by-op to the in-program FACE constants — a
# latent mis-face bug. We canonicalise the stored ``orientation`` to a minimal op list
# with the IDENTICAL net effect (verified on all 4 faces) after every transform, so the
# stored history is always the shortest equivalent sequence.
_D4_OPS = ("cw", "ccw", "mirror_h", "mirror_v")


def _apply_op(face: Face, kind: str) -> Face:
    return {"cw": face.rotated_cw, "ccw": face.rotated_ccw,
            "mirror_h": face.mirrored_h, "mirror_v": face.mirrored_v}[kind]


def _net_face_perm(kinds) -> tuple:
    """The net face permutation of a kind-sequence, as a hashable tuple keyed by
    (S,E,W,N) → resulting face value."""
    order = (Face.SOUTH, Face.EAST, Face.WEST, Face.NORTH)
    out = list(order)
    for k in kinds:
        out = [_apply_op(f, k) for f in out]
    return tuple(f.value for f in out)


# Canonical D4 element -> its SHORTEST op list. Enumerate all op sequences up to
# length 3 (D4's diameter over these 4 generators is ≤ 3) and keep, per distinct net
# face permutation, the fewest-ops candidate (ties broken lexicographically for
# determinism). Lookup by the net face permutation makes canonicalisation O(1), exact,
# and genuinely minimal (both mirror axes + both rotation senses are candidates).
def _build_canon_table() -> dict:
    import itertools
    table: dict = {}
    for n in range(0, 4):
        for seq in itertools.product(_D4_OPS, repeat=n):
            seq = list(seq)
            perm = _net_face_perm(seq)
            cur = table.get(perm)
            if cur is None or (len(seq), seq) < (len(cur), cur):
                table[perm] = seq
    return table


_CANON_TABLE = _build_canon_table()


def canonicalize_orientation(kinds) -> list[str]:
    """Reduce a D4 op-sequence to its shortest equivalent (identical net effect on
    every face). Returns ``[]`` for a net-identity sequence. Any unknown op leaves the
    list unchanged (defensive — never silently drop an op we can't model)."""
    kinds = list(kinds or [])
    if any(k not in _D4_OPS for k in kinds):
        return kinds
    return list(_CANON_TABLE.get(_net_face_perm(kinds), kinds))


@dataclass
class PlacedCell:
    """One cell of a block pinned to a grid position with an output face.

    ``cell_id`` matches the cell identifier in the block definition's ``cells:``
    list.
    """

    cell_id: CellId
    x: int
    y: int
    face: Face

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)


def is_transit_cell(cell: "PlacedCell") -> bool:
    """True if ``cell`` is a block-INTERNAL routing/feedback cell.

    Internal cells are FIRST-CLASS block cells (carried in ``Placement.cells``
    with the owning block's identity, colour, and footprint) — they are merely
    *tagged* by a ``transit_*`` ``cell_id`` prefix so the build/router/DRC still
    recognise them as face-only routing cells (no program). This is the single
    source of truth for that tag; every consumer keys off it.
    """
    cid = getattr(cell, "cell_id", None)
    return isinstance(cid, str) and cid.startswith("transit")


@dataclass
class TransitCell(PlacedCell):
    """DEPRECATED thin alias kept only for backward-compatible ``.kyt`` loads.

    Internal routing/feedback cells are now first-class :class:`PlacedCell`s
    carried in ``Placement.cells`` with a ``transit_*`` ``cell_id`` (they render
    with the owning block's colour/label and count in its footprint). Historic
    saved files serialised these into a separate ``transit_cells:`` block WITHOUT
    a ``cell_id`` (they were "identified by position"); this subclass lets the
    loader synthesise a ``transit_N`` id for each so old designs still open.

    New code must NOT construct these — append a ``PlacedCell`` with a
    ``transit_*`` id to ``Placement.cells`` instead. See :func:`is_transit_cell`.
    """

    def __init__(self, x: int, y: int, face: Face, cell_id: CellId | None = None):
        super().__init__(cell_id=cell_id, x=x, y=y, face=face)


@dataclass
class InstrOverride:
    """A user override of one WRITE/JUMP instruction's handoff target (§3.3).

    The hop count and destination/entry address of a WRITE/JUMP are properties
    of the *instruction itself*, not of the route — the route is passive
    (it only connects cells; the cell decides where its result lands). The
    build auto-fills these from the route + the downstream block's interface,
    but the user may override any field here.

    Fields are all optional; ``None`` means "use the auto-computed value":

    * ``hop`` — handoff distance in **hops away** (``@N`` assembly semantics,
      NOT the raw ``HOP_CNT`` field; the build encodes ``HOP_CNT = 31 - hop``).
    * ``dest`` — destination address for a WRITE: a data register (R0–R31) or,
      when ``dest_config`` is set, a CONFIG address (C0–C31, e.g. C1=FACE). A
      JUMP cannot target CONFIG, so ``dest_config`` is meaningless for JUMP.
    * ``entry`` — entry address for a JUMP (the downstream block's entry point).
    * ``dest_config`` — True if a WRITE ``dest`` names a CONFIG address (sets the
      WRITE config bit). Defaults False (a normal data-register WRITE).

    Overrides are keyed by ``(cell_id, addr)`` on the owning :class:`Placement`,
    so they travel with the block when it is dragged and round-trip through the
    ``.kyt`` file.
    """

    hop: int | None = None
    dest: int | None = None
    entry: int | None = None
    dest_config: bool = False

    @property
    def is_empty(self) -> bool:
        return (self.hop is None and self.dest is None and self.entry is None
                and not self.dest_config)


class Placement:
    """A block's concrete placement on one chip.

    All cells of a block must lie on a single chip (DRC ``block_spans_chips``);
    that chip is recorded here as ``chip`` (a chip *instance* id within the
    project, not a chip type).

    ``instr_overrides`` holds per-instruction handoff overrides, keyed by
    ``cell_id`` then by instruction address. See :class:`InstrOverride`.

    NOTE — internal cells: block-INTERNAL routing/feedback cells are FIRST-CLASS
    ``PlacedCell``s carried in ``cells`` (tagged by a ``transit_*`` ``cell_id``);
    they share the block's identity/colour and count in its footprint. A
    ``transit_cells=`` constructor keyword is still accepted for backward
    compatibility (it merges those cells into ``cells``, synthesising a
    ``transit_N`` id for a legacy positionless entry), and ``transit_cells`` is a
    read-only filtering PROPERTY so router/DRC read-sites keep working unchanged.

    This is a hand-written ``__init__`` (not ``@dataclass``) precisely so the
    ``transit_cells`` name can be BOTH a constructor keyword and a property.
    """

    __slots__ = ("chip", "cells", "instr_overrides", "orientation")

    def __init__(self, chip: int,
                 cells: "list[PlacedCell] | None" = None,
                 transit_cells: "list | None" = None,
                 instr_overrides: "dict | None" = None,
                 orientation: "list[str] | None" = None):
        self.chip = chip
        self.cells = list(cells) if cells else []
        self.instr_overrides = instr_overrides if instr_overrides is not None else {}
        # Cumulative D4 transforms applied to this placement (in order), so the
        # build can transform a block's IN-PROGRAM face constants (a ``MOVE
        # [FACE], k`` picks an ABSOLUTE direction; when the block is
        # rotated/mirrored that direction must rotate with it). Empty =
        # as-authored. See ``transform``.
        self.orientation = list(orientation) if orientation else []
        self._merge_transit(transit_cells)

    def _merge_transit(self, transit_cells) -> None:
        """Merge a legacy ``transit_cells=`` argument into ``cells``.

        Internal cells are first-class ``PlacedCell``s now. A caller (or an old
        ``.kyt`` loader) may still pass a ``transit_cells`` list of positionless
        cells; give each a synthesised ``transit_N`` id (unless it already has a
        ``transit_*`` id) and append it to ``cells`` so it participates in the
        block's identity, footprint, and rigid transform like any other cell.
        """
        if not transit_cells:
            return
        existing = {c.cell_id for c in self.cells}
        n = sum(1 for c in self.cells if is_transit_cell(c))
        for t in transit_cells:
            cid = getattr(t, "cell_id", None)
            if not (isinstance(cid, str) and cid.startswith("transit")):
                cid = f"transit_{n}"
                while cid in existing:
                    n += 1
                    cid = f"transit_{n}"
                n += 1
            existing.add(cid)
            self.cells.append(PlacedCell(cid, t.x, t.y, t.face))

    def __repr__(self) -> str:  # keep dataclass-like debugging output
        return (f"Placement(chip={self.chip!r}, cells={self.cells!r}, "
                f"instr_overrides={self.instr_overrides!r}, "
                f"orientation={self.orientation!r})")

    def __eq__(self, other) -> bool:
        if not isinstance(other, Placement):
            return NotImplemented
        return (self.chip == other.chip and self.cells == other.cells
                and self.instr_overrides == other.instr_overrides
                and self.orientation == other.orientation)

    @property
    def transit_cells(self) -> list[PlacedCell]:
        """Read-only view of the block's INTERNAL routing/feedback cells.

        These are first-class ``PlacedCell``s carried in ``cells`` and tagged by
        a ``transit_*`` ``cell_id``; this filtering view preserves the historic
        API that router/DRC consumers iterate (``.x``/``.y``/``.face``/``.pos``).
        """
        return [c for c in self.cells if is_transit_cell(c)]

    def override(self, cell_id: CellId, addr: int) -> "InstrOverride | None":
        """Return the override for ``(cell_id, addr)``, or ``None`` if absent."""
        return self.instr_overrides.get(cell_id, {}).get(addr)

    def set_override(self, cell_id: CellId, addr: int,
                     ov: "InstrOverride | None") -> None:
        """Set (or clear, when ``ov`` is None/empty) one instruction override."""
        if ov is None or ov.is_empty:
            cell = self.instr_overrides.get(cell_id)
            if cell is not None:
                cell.pop(addr, None)
                if not cell:
                    self.instr_overrides.pop(cell_id, None)
            return
        self.instr_overrides.setdefault(cell_id, {})[addr] = ov

    def cell(self, cell_id: CellId) -> PlacedCell | None:
        """Return the placed cell with the given id, or ``None`` if absent."""
        for c in self.cells:
            if c.cell_id == cell_id:
                return c
        return None

    def occupied_positions(self) -> set[tuple[int, int]]:
        """Every grid position this placement occupies (all cells, including the
        internal ``transit_*`` routing/feedback cells now carried in ``cells``).

        Used by project-level overlap detection before the engine's DRC runs.
        """
        return {c.pos for c in self.cells}

    def bounding_box(self) -> tuple[int, int, int, int] | None:
        """``(min_x, min_y, max_x, max_y)`` over ALL cells, or ``None`` if empty.

        Internal ``transit_*`` routing/feedback cells are FIRST-CLASS block cells:
        they count in the block's footprint (the box the canvas uses for
        selection, zoom-to-fit, and auto-place area), just like any program cell.
        """
        if not self.cells:
            return None
        xs = [c.x for c in self.cells]
        ys = [c.y for c in self.cells]
        return (min(xs), min(ys), max(xs), max(ys))

    def full_bounding_box(self) -> tuple[int, int, int, int] | None:
        """Alias of :meth:`bounding_box` — internal cells are now first-class, so
        the footprint already spans every cell (kept as the transform pivot API)."""
        return self.bounding_box()

    def transform(self, kind: str) -> None:
        """Rotate/mirror this placement in place, pivoting on its full footprint
        (block + transit cells) and re-anchoring at the same top-left corner so
        the block stays put. Each cell's ``face`` is transformed to match so
        routing semantics are preserved. ``kind`` is one of ``"cw"`` / ``"ccw"``
        (90° rotations) / ``"mirror_h"`` / ``"mirror_v"``.

        Coordinates are screen-space (x right, y DOWN). After a 90° rotation the
        footprint's width/height swap; the cells are re-normalised so the
        minimum corner returns to the original ``(min_x, min_y)``.
        """
        box = self.full_bounding_box()
        if box is None:
            return
        minx, miny, maxx, maxy = box
        w, h = maxx - minx, maxy - miny

        def map_xy(x: int, y: int) -> tuple[int, int]:
            u, v = x - minx, y - miny           # local coords within the box
            if kind == "cw":
                return minx + (h - v), miny + u
            if kind == "ccw":
                return minx + v, miny + (w - u)
            if kind == "mirror_h":
                return minx + (w - u), y
            if kind == "mirror_v":
                return x, miny + (h - v)
            raise ValueError(f"unknown transform {kind!r}")

        def map_face(f: Face) -> Face:
            return {
                "cw": f.rotated_cw,
                "ccw": f.rotated_ccw,
                "mirror_h": f.mirrored_h,
                "mirror_v": f.mirrored_v,
            }[kind]

        for c in self.cells:
            c.x, c.y = map_xy(c.x, c.y)
            c.face = map_face(c.face)
        # (Internal transit_* cells live in ``cells`` now — transformed above.)
        # Record the transform so the build can apply the SAME D4 map to the
        # block's in-program face constants (the cell `.face` above is the
        # resting/layout face; a `MOVE [FACE], const` inside the program names an
        # absolute direction that must rotate identically). Canonicalise the stored
        # history to its shortest D4-equivalent so a redundant nudge sequence (e.g.
        # mirror→rotate→un-rotate→un-mirror = identity) collapses instead of leaving a
        # degenerate stack the build re-applies op-by-op (bug B).
        self.orientation.append(kind)
        self.orientation[:] = canonicalize_orientation(self.orientation)
