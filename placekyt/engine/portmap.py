"""PortMap — a block's bus-facing I/O geometry (auto-P&R Phase 2, P2.2).

The auto-router needs, per block, where each EXTERNAL port physically sits and
which way it faces — for the INPUT landing cell AND the OUTPUT cell (the broker
symmetry of the auto-P&R design notes §4.1). It also needs the §4.3 hints: which edge of
the block's footprint faces the routing bus, and whether the input and output are
co-located on that edge (so the packer can take the cheap 1-D path).

A :class:`PortMap` is derived from a block's static description — its
``default_layout`` (cell offsets + faces), its landing cell (``resolved_io`` —
where external WRITE/JUMP arrive), and its ``output_cell_id`` (where the result
leaves). It is PURELY GEOMETRIC: offsets are relative to the block origin, so the
same PortMap applies wherever the block is placed. The 8 orientations are obtained
by composing :meth:`PortMap.transformed` (the same x/y + face maths as
``Placement.transform``), so a PortMap tracks ports through rotate/mirror.

This is logical capture only (Phase 2) — it does not place or route. Phase 3's
packer/router consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from model.enums import Face


# The 8 dihedral orientations as sequences of the 4 primitive transforms that
# Placement.transform already implements. "identity" is the as-authored layout.
_TRANSFORMS = ("cw", "ccw", "mirror_h", "mirror_v")


@dataclass(frozen=True)
class PortInfo:
    """One external port of a block, located in block-local (offset) coordinates.

    ``dx, dy`` are the port cell's offset from the block origin (the min corner of
    its footprint, screen-space x-right / y-down — matching ``default_layout``).
    ``face`` is the cell's output direction; for an OUTPUT port this is the edge
    the result leaves through, for an INPUT port it is the landing cell's own
    forward face (informational — data ARRIVES on the opposite edge).
    ``register`` is the landing input register (input ports) or the WRITE dest
    register (output ports). ``entry`` is the JUMP entry address to trigger the
    cell (input ports) — None for pure output ports.
    """

    name: str
    direction: str            # "in" | "out"
    cell_id: Any
    dx: int
    dy: int
    face: Face
    register: Optional[int] = None
    entry: Optional[int] = None

    @property
    def offset(self) -> tuple[int, int]:
        return (self.dx, self.dy)


@dataclass(frozen=True)
class PortMap:
    """A block's external I/O geometry + bus-edge hints (§4.1, §4.3).

    ``ports`` are the external input + output ports in block-local coordinates.
    ``footprint`` is ``(w, h)`` = (max_dx, max_dy) of the block cells, so the
    edges are ``x==0`` (west), ``x==w`` (east), ``y==0`` (north), ``y==h``
    (south). ``bus_facing_edge`` is the footprint edge the router should abut the
    bus to (derived: the edge most I/O ports sit on); ``io_colocated`` is True
    when the input and output ports share that edge and sit within
    ``COLOCATION_SPAN`` cells of each other — the §4.3 cheap-1-D-packing case.
    """

    block_type: str
    ports: tuple[PortInfo, ...]
    footprint: tuple[int, int]
    bus_facing_edge: Optional[Face]
    io_colocated: bool
    # How close (in cells, along the shared edge) input and output must sit to
    # count as co-located. 1 = strictly adjacent; we allow a small span because a
    # multi-cell I/O edge (e.g. two input regs + one output) is still "one tap".
    COLOCATION_SPAN: int = field(default=2, compare=False)

    def inputs(self) -> tuple[PortInfo, ...]:
        return tuple(p for p in self.ports if p.direction == "in")

    def outputs(self) -> tuple[PortInfo, ...]:
        return tuple(p for p in self.ports if p.direction == "out")

    def port(self, name: str) -> Optional[PortInfo]:
        return next((p for p in self.ports if p.name == name), None)

    # -- transforms -----------------------------------------------------------

    def transformed(self, kind: str) -> "PortMap":
        """Apply one primitive transform (``cw``/``ccw``/``mirror_h``/
        ``mirror_v``), returning a NEW PortMap with every port's offset + face
        mapped and the footprint/edge hints recomputed. Mirrors
        ``Placement.transform`` exactly so a PortMap stays consistent with its
        placement under rotation/mirroring."""
        w, h = self.footprint

        def map_xy(x: int, y: int) -> tuple[int, int]:
            u, v = x, y                       # origin is already (0,0)
            if kind == "cw":
                return (h - v, u)
            if kind == "ccw":
                return (v, w - u)
            if kind == "mirror_h":
                return (w - u, y)
            if kind == "mirror_v":
                return (x, h - v)
            raise ValueError(f"unknown transform {kind!r}")

        def map_face(f: Face) -> Face:
            return {
                "cw": f.rotated_cw,
                "ccw": f.rotated_ccw,
                "mirror_h": f.mirrored_h,
                "mirror_v": f.mirrored_v,
            }[kind]

        new_ports = tuple(
            replace(p, dx=map_xy(p.dx, p.dy)[0], dy=map_xy(p.dx, p.dy)[1],
                    face=map_face(p.face))
            for p in self.ports
        )
        # 90° rotations swap footprint w/h.
        new_fp = (h, w) if kind in ("cw", "ccw") else (w, h)
        edge, colo = _derive_bus_edge(new_ports, new_fp, self.COLOCATION_SPAN)
        return replace(self, ports=new_ports, footprint=new_fp,
                       bus_facing_edge=edge, io_colocated=colo)


# -- bus-edge derivation -------------------------------------------------------

def _edge_of(p: PortInfo, footprint: tuple[int, int]) -> Optional[Face]:
    """Which footprint edge the port cell sits on (None if interior / ambiguous).

    A cell on the west column (dx==0) attaches to the bus on the WEST edge, etc.
    A corner cell sits on two edges — we resolve by the port's own face (the
    direction it communicates with the bus), since that is the edge the data
    crosses."""
    w, h = footprint
    on = []
    if p.dx == 0:
        on.append(Face.WEST)
    if p.dx == w:
        on.append(Face.EAST)
    if p.dy == 0:
        on.append(Face.NORTH)
    if p.dy == h:
        on.append(Face.SOUTH)
    if not on:
        return None
    if len(on) == 1:
        return on[0]
    # Corner: prefer the edge matching the port's communicating direction. For an
    # OUTPUT port that's its own face; for an INPUT port data arrives on the
    # opposite of its forward face.
    want = p.face if p.direction == "out" else p.face.opposite
    if want in on:
        return want
    return on[0]


def _derive_bus_edge(
    ports: tuple[PortInfo, ...], footprint: tuple[int, int], span: int,
) -> tuple[Optional[Face], bool]:
    """(bus_facing_edge, io_colocated). The bus-facing edge is the footprint edge
    that the most I/O ports sit on (§4.3). I/O is co-located when at least one
    input and one output sit on that edge within ``span`` cells of each other."""
    # Same-cell I/O is trivially co-located — input and output share ONE tap point
    # on the bus (e.g. a single-cell AGC/slicer, or a block whose landing cell is
    # also its output cell). Resolve the bus edge from that shared cell's face.
    in_cells = {p.cell_id for p in ports if p.direction == "in"}
    out_cells = {p.cell_id for p in ports if p.direction == "out"}
    shared = in_cells & out_cells
    if shared:
        # Edge from any port on the shared cell (prefer the output's egress face).
        sp = next((p for p in ports
                   if p.cell_id in shared and p.direction == "out"), None)
        sp = sp or next(p for p in ports if p.cell_id in shared)
        edge = sp.face if sp.direction == "out" else sp.face.opposite
        return (edge, True)

    by_edge: dict[Face, list[PortInfo]] = {}
    for p in ports:
        e = _edge_of(p, footprint)
        if e is not None:
            by_edge.setdefault(e, []).append(p)
    if not by_edge:
        return (None, False)
    # The bus edge: the edge with the most ports; ties broken by a stable face
    # order (E, S, W, N) — east-facing pipelines are the common modem case.
    order = {Face.EAST: 0, Face.SOUTH: 1, Face.WEST: 2, Face.NORTH: 3}
    edge = min(by_edge, key=lambda e: (-len(by_edge[e]), order[e]))
    here = by_edge[edge]
    ins = [p for p in here if p.direction == "in"]
    outs = [p for p in here if p.direction == "out"]
    colo = False
    # Co-location is measured ALONG the edge (the free axis): for a vertical edge
    # (E/W) that's dy; for a horizontal edge (N/S) that's dx.
    along = (lambda p: p.dy) if edge in (Face.EAST, Face.WEST) else (lambda p: p.dx)
    for pi in ins:
        for po in outs:
            if abs(along(pi) - along(po)) <= span:
                colo = True
                break
        if colo:
            break
    return (edge, colo)


# -- builder -------------------------------------------------------------------

def build_port_map(
    catalog, type_name: str, params: dict[str, Any] | None = None,
    *, library: str | None = None,
) -> PortMap:
    """Derive a block's :class:`PortMap` from its static description.

    External INPUT ports = the inputs of the block's LANDING cell (where external
    WRITE/JUMP arrive — ``resolved_io``). External OUTPUT ports = the outputs of
    the block's ``output_cell_id`` cell that are NOT consumed internally (those
    are what leave the block onto the bus). Offsets + faces come from
    ``default_layout``. Registers/entry come from ``resolved_io``.

    Falls back gracefully: a block with no ``default_layout`` (single-cell, or a
    serpentine-fallback block) yields a 1-cell footprint with its ports at the
    origin facing the layout's declared face (or EAST).
    """
    block = catalog.instantiate(type_name, "__probe__", params, library=library)
    layout = catalog.default_layout(type_name, params, library=library) or {}
    entry, in_regs = catalog.resolved_io(type_name, params, library=library)

    cell_programs = {}
    try:
        cell_programs = block.build_cell_programs() or {}
    except Exception:  # noqa: BLE001
        cell_programs = {}

    internal = list(getattr(block, "internal_connections", lambda: [])() or [])
    internal += list(getattr(block, "internal_jumps", lambda: [])() or [])
    internal_srcs = {(s, sp) for (s, sp, _d, _dp) in internal}
    # A landing-cell input fed by an INTERNAL connection (e.g. the Costas/Coherent
    # feedback ``dphase`` returned to the ``phase`` cell) is NOT an external port —
    # it never reaches the bus. Exclude those.
    internal_dsts = {(d, dp) for (_s, _sp, d, dp) in internal}

    out_cell_id = None
    try:
        out_cell_id = block.output_cell_id()
    except Exception:  # noqa: BLE001
        out_cell_id = None

    # Footprint: the block cells only (transit cells excluded — they aren't ports
    # and a "transit_" id never carries a program). Offsets are normalised so the
    # min corner is the origin (0,0).
    block_cells = {
        cid: pos for cid, pos in layout.items()
        if not (isinstance(cid, str) and cid.startswith("transit"))
    }
    if block_cells:
        minx = min(p[0] for p in block_cells.values())
        miny = min(p[1] for p in block_cells.values())
    else:
        minx = miny = 0

    def _norm(cid):
        pos = block_cells.get(cid)
        if pos is None:
            return (0, 0, Face.EAST)
        dx, dy, face_s = pos[0] - minx, pos[1] - miny, pos[2]
        return (dx, dy, Face.from_str(face_s))

    # The landing cell: first templated cell that declares inputs (matches
    # resolved_io's own selection).
    landing_id = None
    for cid, cp in cell_programs.items():
        if getattr(cp, "assembly_template", "") and getattr(cp, "inputs", None):
            landing_id = cid
            break

    ports: list[PortInfo] = []

    # External inputs = the landing cell's input ports that are NOT fed by an
    # internal connection (those are feedback returns, not bus inputs). The
    # register list from resolved_io is positional over ALL declared inputs, so
    # index by the full input list, then filter.
    if landing_id is not None:
        dx, dy, face = _norm(landing_id)
        for i, p in enumerate(cell_programs[landing_id].inputs):
            if (landing_id, p.name) in internal_dsts:
                continue
            reg = in_regs[i] if i < len(in_regs) else None
            ports.append(PortInfo(p.name, "in", landing_id, dx, dy, face,
                                  register=reg, entry=entry))

    # External outputs = the output cell(s)' outputs not consumed internally. A
    # block usually has ONE output cell (``output_cell_id``); a block with TWO
    # physically-separate output cells — e.g. the complex matched filter, whose I
    # rail ends at ``i3`` and Q rail at ``q3`` — declares ``output_cell_ids()``
    # (plural) so BOTH cells' outputs become routable ports (yi from i3, yq from
    # q3, each feeding the downstream complex block's xi/xq fan-in). When neither is
    # declared, the router's positional default makes the LAST cell the output.
    out_cell_ids = None
    try:
        getter = getattr(block, "output_cell_ids", None)
        out_cell_ids = list(getter()) if getter is not None else None
    except Exception:  # noqa: BLE001
        out_cell_ids = None
    if not out_cell_ids:
        if out_cell_id is None and cell_programs:
            out_cell_id = list(cell_programs.keys())[-1]
        out_cell_ids = [out_cell_id] if out_cell_id is not None else []
    for ocid in out_cell_ids:
        if ocid is None or ocid not in cell_programs:
            continue
        dx, dy, face = _norm(ocid)
        for p in getattr(cell_programs[ocid], "outputs", []):
            if (ocid, p.name) in internal_srcs:
                continue
            ports.append(PortInfo(p.name, "out", ocid, dx, dy, face))

    if block_cells:
        w = max(p[0] for p in block_cells.values()) - minx
        h = max(p[1] for p in block_cells.values()) - miny
    else:
        w = h = 0
    footprint = (w, h)
    edge, colo = _derive_bus_edge(tuple(ports), footprint, PortMap.COLOCATION_SPAN)
    return PortMap(block_type=type_name, ports=tuple(ports), footprint=footprint,
                   bus_facing_edge=edge, io_colocated=colo)
