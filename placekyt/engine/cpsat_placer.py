"""CP-SAT wirelength-minimising placer (replaces the serpentine band-packer).

The old ``AutoPlacer._pack_compact`` laid blocks in horizontal bands, reserving whole
rows for channels and forcing every band to its tallest block's height — which wastes
half a 10x12 array and then reports "does not fit" on a design that occupies only ~53%
of the cells (the SSB Weaver: 64 block cells, 56 free). This module models placement as
a proper 2D packing problem and lets OR-Tools CP-SAT solve it optimally-ish:

- Each block is an oriented RECTANGLE (its bounding box after a chosen D4 transform).
- ``AddNoOverlap2D`` forbids any two block boxes from overlapping.
- Boxes stay in bounds and OFF the chip I/O-port cells (which must remain free bus taps).
- The objective MINIMISES total Manhattan wirelength between connected blocks' I/O cells
  (short nets => routable + compact), so the solver naturally leaves routing room where
  the wirelength cost is already paid.

Returns a :class:`~engine.autoplace.PlacePlan` — a drop-in for ``AutoPlacer.plan`` — so
the controller applies it, the D1 legality gate validates it, and the existing bus/broker
router routes it, all unchanged. Feedback blocks may rotate: the build rotates their
in-program ``MOVE [FACE]`` constants with the cells (VERIFIED bit-exact), so orientation
is a free variable for every block (feedback blocks are limited to identity + 90° turns,
never a mirror, which flips handedness the face-word map does not model).

If OR-Tools is unavailable or the model is infeasible/timed-out, the caller falls back to
the serpentine placer (kept for that reason).
"""

from __future__ import annotations

from .autoplace import PlacePlan


class CpSatPlacerUnavailable(RuntimeError):
    """OR-Tools not importable / model not solvable — caller should fall back."""


def _cp_model():
    try:
        from ortools.sat.python import cp_model  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise CpSatPlacerUnavailable(
            "OR-Tools (ortools) is required for the CP-SAT placer") from exc
    return cp_model


# Orientation candidates. Feedback blocks: identity + 90° turns only (a mirror flips
# handedness the build's face-word rotation does not model). Others: all of D4.
_FEEDBACK_KINDS = (None, "cw", "ccw")
_ALL_KINDS = (None, "cw", "ccw", "mirror_h", "mirror_v")


def plan_cpsat(placer, chip: int, *, max_time_s: float = 10.0,
               channel_slack: int = 0, slack_x: int | None = None,
               slack_y: int | None = None) -> PlacePlan:
    """Compute a wirelength-minimising placement for the blocks on ``chip`` using
    CP-SAT. ``placer`` is a configured :class:`~engine.autoplace.AutoPlacer` (its
    footprint / port-map / chip-port / feedback providers are reused). Raises
    :class:`CpSatPlacerUnavailable` if OR-Tools is absent or no legal placement is
    found within ``max_time_s`` (the caller then falls back to the serpentine placer).

    ``channel_slack`` inflates each block's bounding box by that many cells on the
    right/bottom so the solver keeps a routing gap around blocks (the place<->route
    loop raises it on a routing failure)."""
    cp_model = _cp_model()
    W, H = placer._width, placer._height

    blocks = [b for b in placer._project.blocks
              if b.placement is not None and b.placement.chip == chip
              and b.placement.cells]
    if not blocks:
        return PlacePlan(positions={}, order=[], orientations={}, spine=[])
    names = {b.name for b in blocks}

    # Reuse the serpentine placer's graph analysis: flow order + who-drives-whom +
    # which chip ports each block touches (for the wirelength terms + free-port cells).
    order, backward = placer._flow_order(blocks, names)
    placer._blk_of = {b.name: b for b in blocks}
    placer._driver_of, placer._in_port_of, placer._out_port_of = \
        placer._neighbour_maps(names)

    # Chip I/O port cells must stay FREE (bus taps) — blocks may not cover them.
    port_cells = set()
    for ep_cell in list(placer._in_port_of.values()) + list(placer._out_port_of.values()):
        if ep_cell is not None:
            port_cells.add(tuple(ep_cell))

    # Per block: the candidate orientations with their oriented (w, h) AND the input /
    # output cell offsets (dx, dy) from the block's min corner — used for wirelength.
    def candidates(b):
        kinds = (_FEEDBACK_KINDS if placer._has_internal_feedback(b) else _ALL_KINDS)
        out = []
        seen_wh = {}
        for k in kinds:
            w, h = placer._oriented_wh(b, k)
            io = placer._io_offsets(b, k)   # ((ix,iy),(ox,oy)) or None
            # De-dup orientations that give the SAME box AND io (identity vs a mirror
            # that doesn't change the footprint) to shrink the model.
            key = (w, h, io)
            if key in seen_wh:
                continue
            seen_wh[key] = True
            out.append((k, w, h, io))
        return out

    model = cp_model.CpModel()
    x_intervals, y_intervals = [], []
    bx, by = {}, {}          # block -> min-corner IntVar
    kind_of = {}             # block -> selected-kind expression pieces
    # For wirelength we need each block's chosen input/output cell coords. Model them
    # as IntVars tied to (bx,by) + the offset of the SELECTED orientation.
    in_cx, in_cy, out_cx, out_cy = {}, {}, {}, {}

    for b in blocks:
        cands = candidates(b)
        # One boolean per candidate orientation; exactly one active.
        lits = []
        # oriented size selected via the active candidate
        wv = model.NewIntVar(1, W, f"w_{b.name}")
        hv = model.NewIntVar(1, H, f"h_{b.name}")
        xv = model.NewIntVar(0, W, f"x_{b.name}")
        yv = model.NewIntVar(0, H, f"y_{b.name}")
        # The I/O-cell vars + their per-orientation enforcement are ONLY needed for the
        # wirelength objective (slack 0). At slack>0 we solve a LEAN feasibility model
        # (packing only) — building these bloats it enough to stall even feasibility.
        want_io = (max(channel_slack, slack_x or 0, slack_y or 0) == 0)
        if want_io:
            icx = model.NewIntVar(0, W, f"icx_{b.name}")
            icy = model.NewIntVar(0, H, f"icy_{b.name}")
            ocx = model.NewIntVar(0, W, f"ocx_{b.name}")
            ocy = model.NewIntVar(0, H, f"ocy_{b.name}")
        for j, (k, w, h, io) in enumerate(cands):
            lit = model.NewBoolVar(f"o_{b.name}_{j}")
            lits.append((lit, k, w, h, io))
            model.Add(wv == w).OnlyEnforceIf(lit)
            model.Add(hv == h).OnlyEnforceIf(lit)
            if want_io:
                # I/O cell = min-corner + this orientation's offsets (else corner).
                iox, ioy = (io[0] if io and io[0] else (0, 0))
                oox, ooy = (io[1] if io and io[1] else (0, 0))
                model.Add(icx == xv + iox).OnlyEnforceIf(lit)
                model.Add(icy == yv + ioy).OnlyEnforceIf(lit)
                model.Add(ocx == xv + oox).OnlyEnforceIf(lit)
                model.Add(ocy == yv + ooy).OnlyEnforceIf(lit)
        model.AddExactlyOne(l for l, *_ in lits)
        # In-bounds (+ channel slack on the far edges, clamped to the array).
        model.Add(xv + wv <= W)
        model.Add(yv + hv <= H)
        # Interval vars for AddNoOverlap2D use size = oriented dim (+ slack, but the
        # box itself must fit, so slack only widens the *no-overlap* extent).
        sx = channel_slack if slack_x is None else slack_x
        sy = channel_slack if slack_y is None else slack_y
        xdim = model.NewIntVar(1, W + sx, f"xd_{b.name}")
        ydim = model.NewIntVar(1, H + sy, f"yd_{b.name}")
        model.Add(xdim == wv + sx)
        model.Add(ydim == hv + sy)
        xe = model.NewIntVar(0, W + sx, f"xe_{b.name}")
        ye = model.NewIntVar(0, H + sy, f"ye_{b.name}")
        model.Add(xe == xv + xdim)
        model.Add(ye == yv + ydim)
        xiv = model.NewIntervalVar(xv, xdim, xe, f"xi_{b.name}")
        yiv = model.NewIntervalVar(yv, ydim, ye, f"yi_{b.name}")
        x_intervals.append(xiv)
        y_intervals.append(yiv)
        bx[b.name], by[b.name] = xv, yv
        wv_, hv_ = wv, hv
        kind_of[b.name] = lits
        if want_io:
            in_cx[b.name], in_cy[b.name] = icx, icy
            out_cx[b.name], out_cy[b.name] = ocx, ocy

    model.AddNoOverlap2D(x_intervals, y_intervals)

    # Keep chip port cells free: no block box may cover a port cell. Per block per
    # port cell, the box lies entirely left/right/above/below the port cell.
    for b in blocks:
        xv, yv = bx[b.name], by[b.name]
        for (pcx, pcy) in port_cells:
            left = model.NewBoolVar(f"pf_l_{b.name}_{pcx}_{pcy}")
            right = model.NewBoolVar(f"pf_r_{b.name}_{pcx}_{pcy}")
            above = model.NewBoolVar(f"pf_a_{b.name}_{pcx}_{pcy}")
            below = model.NewBoolVar(f"pf_b_{b.name}_{pcx}_{pcy}")
            # left/above use the per-orientation width/height via the active lit.
            for (lit, k, w, h, io) in kind_of[b.name]:
                model.Add(xv + w <= pcx).OnlyEnforceIf([lit, left])
                model.Add(yv + h <= pcy).OnlyEnforceIf([lit, above])
            model.Add(xv >= pcx + 1).OnlyEnforceIf(right)
            model.Add(yv >= pcy + 1).OnlyEnforceIf(below)
            model.AddBoolOr([left, right, above, below])

    # Objective: total Manhattan wirelength over the netlist. For each driver->consumer
    # block edge, |out_cell(driver) - in_cell(consumer)|. Plus terminal->chip-port and
    # port->head terms (the ports are fixed cells). Plus a light compaction pull toward
    # the input-port corner so a sparse array still packs near the source.
    terms = []

    def manhattan(ax, ay, bx_, by_, cap):
        dx = model.NewIntVar(0, cap, "")
        dy = model.NewIntVar(0, cap, "")
        model.AddAbsEquality(dx, ax - bx_)
        model.AddAbsEquality(dy, ay - by_)
        terms.append(dx)
        terms.append(dy)

    # The wirelength objective (and the I/O-cell vars it needs) only exist at slack 0 —
    # a slack>0 model is solved for a LEGAL packing only (feasibility unblocks the
    # router; the objective would make an inflated model too slow to even satisfy).
    slack_max = max(channel_slack, slack_x or 0, slack_y or 0)
    CAP = W + H
    if slack_max == 0:
        for b in blocks:
            drv = placer._driver_of.get(b.name)
            if drv is not None and drv in in_cx:
                manhattan(out_cx[drv], out_cy[drv], in_cx[b.name], in_cy[b.name], CAP)
            pin = placer._in_port_of.get(b.name)
            if pin is not None:
                manhattan(model.NewConstant(int(pin[0])),
                          model.NewConstant(int(pin[1])),
                          in_cx[b.name], in_cy[b.name], CAP)
            pout = placer._out_port_of.get(b.name)
            if pout is not None:
                manhattan(out_cx[b.name], out_cy[b.name],
                          model.NewConstant(int(pout[0])),
                          model.NewConstant(int(pout[1])), CAP)
        if terms:
            model.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time_s)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise CpSatPlacerUnavailable(
            f"CP-SAT found no legal placement (status={solver.StatusName(status)})")

    positions, orientations = {}, {}
    for b in blocks:
        xv, yv = bx[b.name], by[b.name]
        px, py = int(solver.Value(xv)), int(solver.Value(yv))
        # recover the chosen orientation
        chosen = None
        for (lit, k, w, h, io) in kind_of[b.name]:
            if solver.Value(lit):
                chosen = k
                break
        positions[b.name] = (chip, px, py)
        orientations[b.name] = chosen

    # A loose spine hint (flow-ordered block min corners; the router's threader only
    # needs an ordered set of interior waypoints). Uses out-cell when available (slack
    # 0), else the block corner (lean feasibility model).
    spine = []
    for n in order:
        if n in out_cx:
            spine.append((int(solver.Value(out_cx[n])), int(solver.Value(out_cy[n]))))
        elif n in bx:
            spine.append((int(solver.Value(bx[n])), int(solver.Value(by[n]))))

    return PlacePlan(positions=positions, order=order, orientations=orientations,
                     spine=spine, backward_edges=backward)
