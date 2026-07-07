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
               slack_y: int | None = None, tap_reserve: bool = False,
               wirelength: bool = False, random_seed: int | None = None,
               abutment_first: bool = False) -> PlacePlan:
    """Compute a wirelength-minimising placement for the blocks on ``chip`` using
    CP-SAT. ``placer`` is a configured :class:`~engine.autoplace.AutoPlacer` (its
    footprint / port-map / chip-port / feedback providers are reused). Raises
    :class:`CpSatPlacerUnavailable` if OR-Tools is absent or no legal placement is
    found within ``max_time_s`` (the caller then falls back to the serpentine placer).

    ``channel_slack`` inflates each block's bounding box by that many cells on the
    right/bottom so the solver keeps a routing gap around blocks (the place<->route
    loop raises it on a routing failure).

    ``abutment_first`` (compact FIXED designs): make DIRECT ABUTMENT the primary goal.
    For each driver->consumer edge a boolean ``abut`` is true iff their output/input
    cells are orthogonally ADJACENT (Manhattan distance 1); the objective MAXIMISES the
    abutted-edge count first (wirelength stays a small secondary tie-break). The solver
    then chains blocks into abutted filaments — data flows cell-to-cell with NO routing
    cell (the router's ``is_abutment`` path), and the compact chains leave free broker
    neighbours for the genuine fan-ins. This is the efficient layout for a FIXED
    transceiver (Weaver/AM/FM); the bus topology is for dynamic reconfiguration.
    ``tap_reserve`` is ignored in this mode (abutment replaces the reservation)."""
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
    # Face code (S=0,E=1,W=2,N=3) -> outward unit step (dx,dy), screen coords (y down).
    _FACE_STEP = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}
    _FACE_NAME = {"south": 0, "s": 0, "east": 1, "e": 1, "west": 2, "w": 2,
                  "north": 3, "n": 3}

    def _face_code(f):
        """Normalise a face (enum, int, or name str S/E/W/N) to code 0..3, or None."""
        if f is None:
            return None
        v = getattr(f, "value", None)
        if isinstance(v, int):
            return v
        if isinstance(f, int):
            return f
        return _FACE_NAME.get(str(getattr(f, "name", f)).lower())

    def _io_faces(b, k):
        """The (input_face, output_face) of the FIRST in/out port after orientation
        ``k`` (face codes S/E/W/N), or (None, None). The router taps a broker on the
        cell OUTWARD of the I/O cell along this face, so a placement that keeps that
        cell free is routable."""
        pm = placer._oriented_port_map(b, k)
        if pm is None:
            return (None, None)
        ins, outs = pm.inputs(), pm.outputs()
        fi = ins[0].face if ins else None
        fo = outs[0].face if outs else None
        return (_face_code(fi), _face_code(fo))

    def candidates(b):
        kinds = (_FEEDBACK_KINDS if placer._has_internal_feedback(b) else _ALL_KINDS)
        out = []
        seen_wh = {}
        for k in kinds:
            w, h = placer._oriented_wh(b, k)
            io = placer._io_offsets(b, k)   # ((ix,iy),(ox,oy)) or None
            faces = _io_faces(b, k)         # (in_face, out_face)
            # De-dup orientations that give the SAME box AND io + faces.
            key = (w, h, io, faces)
            if key in seen_wh:
                continue
            seen_wh[key] = True
            out.append((k, w, h, io, faces))
        return out

    model = cp_model.CpModel()
    x_intervals, y_intervals = [], []
    bx, by = {}, {}          # block -> min-corner IntVar
    kind_of = {}             # block -> selected-kind expression pieces
    tap_cells = {}           # block -> (in_tap_x, in_tap_y, out_tap_x, out_tap_y)
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
        want_io = wirelength or (max(channel_slack, slack_x or 0, slack_y or 0) == 0)
        if want_io:
            icx = model.NewIntVar(0, W, f"icx_{b.name}")
            icy = model.NewIntVar(0, H, f"icy_{b.name}")
            ocx = model.NewIntVar(0, W, f"ocx_{b.name}")
            ocy = model.NewIntVar(0, H, f"ocy_{b.name}")
        # TAP cells (the router's broker-tap sites): the cell OUTWARD of the input I/O
        # cell along its face, and likewise for the output. Reserving these as 1x1
        # keep-outs in the no-overlap set guarantees each net has a free abutting cell to
        # broker/egress through — the exact thing the bus router needs — at a cost of
        # ~2 cells per block instead of a full (infeasible) ring. Their positions follow
        # the selected orientation. tap_reserve toggles the feature (slack-0 default on).
        itx = model.NewIntVar(0, W, f"itx_{b.name}")
        ity = model.NewIntVar(0, H, f"ity_{b.name}")
        otx = model.NewIntVar(0, W, f"otx_{b.name}")
        oty = model.NewIntVar(0, H, f"oty_{b.name}")
        for j, (k, w, h, io, faces) in enumerate(cands):
            lit = model.NewBoolVar(f"o_{b.name}_{j}")
            lits.append((lit, k, w, h, io, faces))
            model.Add(wv == w).OnlyEnforceIf(lit)
            model.Add(hv == h).OnlyEnforceIf(lit)
            iox, ioy = (io[0] if io and io[0] else (0, 0))
            oox, ooy = (io[1] if io and io[1] else (0, 0))
            if want_io:
                # I/O cell = min-corner + this orientation's offsets (else corner).
                model.Add(icx == xv + iox).OnlyEnforceIf(lit)
                model.Add(icy == yv + ioy).OnlyEnforceIf(lit)
                model.Add(ocx == xv + oox).OnlyEnforceIf(lit)
                model.Add(ocy == yv + ooy).OnlyEnforceIf(lit)
            # Tap cell = I/O cell + outward face step (clamped in-bounds by the tap
            # interval's own [0,W]/[0,H] domain; an off-grid tap just isn't reserved).
            fin, fout = faces
            idx, idy = _FACE_STEP.get(fin, (0, 0))
            odx, ody = _FACE_STEP.get(fout, (0, 0))
            model.Add(itx == xv + iox + idx).OnlyEnforceIf(lit)
            model.Add(ity == yv + ioy + idy).OnlyEnforceIf(lit)
            model.Add(otx == xv + oox + odx).OnlyEnforceIf(lit)
            model.Add(oty == yv + ooy + ody).OnlyEnforceIf(lit)
        model.AddExactlyOne(l for l, *_ in lits)
        tap_cells[b.name] = (itx, ity, otx, oty)
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

    # TAP KEEP-OUT: every block's input + output TAP cell must be FREE of block bodies
    # (a broker/egress site for the router). Build a SECOND no-overlap set = the REAL
    # block boxes (w x h, no slack) + each tap as a 1x1 interval; then no block covers a
    # tap. Taps may coincide with each OTHER (a shared corridor cell) so they are NOT
    # mutually exclusive — model that by giving every tap the SAME interval group only
    # against blocks. AddNoOverlap2D can't express "A vs B but not B vs C", so instead we
    # forbid each tap from lying inside each block box directly (a compact disjunction).
    def _forbid_inside(cxv, cyv, tagn):
        for b2 in blocks:
            x2, y2 = bx[b2.name], by[b2.name]
            L = model.NewBoolVar(f"tk_l_{tagn}_{b2.name}")
            R = model.NewBoolVar(f"tk_r_{tagn}_{b2.name}")
            A = model.NewBoolVar(f"tk_a_{tagn}_{b2.name}")
            Bd = model.NewBoolVar(f"tk_b_{tagn}_{b2.name}")
            for (lit, k, w, h, io, faces) in kind_of[b2.name]:
                # tap strictly left of / above the box, or box left-of/above the tap.
                model.Add(cxv < x2).OnlyEnforceIf([lit, L])
                model.Add(cyv < y2).OnlyEnforceIf([lit, A])
                model.Add(cxv >= x2 + w).OnlyEnforceIf([lit, R])
                model.Add(cyv >= y2 + h).OnlyEnforceIf([lit, Bd])
            model.AddBoolOr([L, R, A, Bd])

    # TAP RESERVATION — reserve a FREE broker/egress site outward of each net-endpoint
    # I/O cell so the maze router always has a tap. The maze router routes a source→
    # broker corridor and delivers into the target; a boxed I/O cell (buried in its own
    # snake with no free neighbour) is exactly what makes it fail "no corridor from the
    # source to the tap" / "no free broker cell". Reserving the OUTPUT tap (a free cell
    # outward of each source's output cell) AND the INPUT tap (outward of each target's
    # input cell) guarantees the router a corridor endpoint on both sides.
    #
    # Enabled ONLY when ``tap_reserve`` is requested (the maze-router place<->route
    # path). It costs ~2 cells/block vs a full ring; the maze router's ABUTMENT handling
    # means an abutting driver body is still a legal tap, so the reservation is a
    # PREFERENCE realised as a keep-out only where it stays feasible (the solver relaxes
    # to a compact pack via the reserve sweep if the keep-out is infeasible at reserve 1).
    if tap_reserve and max(channel_slack, slack_x or 0, slack_y or 0) == 0:
        targets = {t for _, t in ((placer._driver_of.get(b.name), b.name)
                                  for b in blocks) if _ is not None}
        sources = {s for s in (placer._driver_of.get(b.name) for b in blocks)
                   if s is not None}
        # FAN-IN targets (≥2 incoming edges, e.g. IQUpconvert xi+xq / ComplexToFloat
        # re+im) genuinely NEED a free input tap: their two drivers can't BOTH abut one
        # cell, so at least one delivers via a broker (a free abutting cell). Reserving a
        # tap for JUST the fan-in inputs (a small set) is far more likely feasible than a
        # per-block ring. ``tap_reserve == "fanin"`` reserves only those.
        indeg: dict = {}
        for b in blocks:
            drv = placer._driver_of.get(b.name)
            if drv is not None:
                indeg[b.name] = indeg.get(b.name, 0) + 1
        # a block may be fed by MULTIPLE nets from one driver (complex xi/xq) — count the
        # logical connections into it instead for a true fan-in signal.
        try:
            from model.connection import BlockEndpoint as _BE
            conn_indeg: dict = {}
            for conn in placer._project.connections:
                if isinstance(conn.target, _BE):
                    conn_indeg[conn.target.block] = \
                        conn_indeg.get(conn.target.block, 0) + 1
            fanin = {n for n, c in conn_indeg.items() if c >= 2}
        except Exception:  # noqa: BLE001
            fanin = set(indeg)
        reserve_in = tap_reserve in (True, "both", "in", "fanin", "out+fanin")
        reserve_out = tap_reserve in (True, "both", "out", "out+fanin")
        fanin_only = tap_reserve in ("fanin", "out+fanin")
        for b in blocks:
            itx, ity, otx, oty = tap_cells[b.name]
            want_in = (b.name in fanin) if fanin_only else (b.name in targets)
            if reserve_in and want_in:
                _forbid_inside(itx, ity, f"it_{b.name}")
            if reserve_out and (b.name in sources
                                or placer._out_port_of.get(b.name) is not None):
                _forbid_inside(otx, oty, f"ot_{b.name}")

        # Keep the INWARD-neighbour cell of every USED chip I/O port FREE (a port tap):
        # a block boxing the cell just inside the port walls ingress/egress off the port
        # (the IQUpconvert-boxes-x16_out case). Reserving these ≤4 cells is cheap and fixes
        # the recurring egress/ingress "no corridor to the port" failures.
        used_ports = set()
        for ep in list(placer._in_port_of.values()) \
                + list(placer._out_port_of.values()):
            if ep is not None:
                used_ports.add(tuple(ep))
        for (pcx, pcy) in used_ports:
            # the inward neighbours (all on-grid orthogonal cells) — reserve the ones
            # that lie inside the array so at least one stays a free approach lane.
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (pcx + ddx, pcy + ddy)
                if 0 <= nb[0] < W and 0 <= nb[1] < H:
                    _forbid_inside(model.NewConstant(nb[0]),
                                   model.NewConstant(nb[1]), f"pa_{nb[0]}_{nb[1]}")
                    break  # one free approach lane per port is enough

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
            for (lit, k, w, h, io, faces) in kind_of[b.name]:
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
    abut_bools = []   # per-edge "output/input cells are adjacent" (abutment_first)

    def manhattan(ax, ay, bx_, by_, cap, *, edge=False):
        dx = model.NewIntVar(0, cap, "")
        dy = model.NewIntVar(0, cap, "")
        model.AddAbsEquality(dx, ax - bx_)
        model.AddAbsEquality(dy, ay - by_)
        terms.append(dx)
        terms.append(dy)
        # For a block->block dataflow edge, a boolean that is TRUE iff the driver's
        # output cell and the consumer's input cell are orthogonally ADJACENT (Manhattan
        # distance exactly 1) — i.e. the pair can ABUT (route-free @1 handoff).
        if edge:
            dsum = model.NewIntVar(0, 2 * cap, "")
            model.Add(dsum == dx + dy)
            ab = model.NewBoolVar("")
            model.Add(dsum == 1).OnlyEnforceIf(ab)
            model.Add(dsum != 1).OnlyEnforceIf(ab.Not())
            abut_bools.append(ab)

    # The wirelength objective (and the I/O-cell vars it needs) only exist at slack 0 —
    # a slack>0 model is solved for a LEGAL packing only (feasibility unblocks the
    # router; the objective would make an inflated model too slow to even satisfy).
    slack_max = max(channel_slack, slack_x or 0, slack_y or 0)
    CAP = W + H
    if slack_max == 0 or wirelength:
        for b in blocks:
            drv = placer._driver_of.get(b.name)
            if drv is not None and drv in in_cx:
                manhattan(out_cx[drv], out_cy[drv], in_cx[b.name], in_cy[b.name], CAP,
                          edge=True)
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
        # SINGLE-CELL IN != OUT FACE (§5.3 deadlock prevention). A one-cell block that
        # both RECEIVES from an upstream BLOCK and DRIVES a downstream BLOCK has its
        # input arrive and its output leave through the SAME physical cell. Daisy-chaining
        # is fine — data comes in one face and out a DIFFERENT face — but if the placer
        # puts the driver and the consumer on the SAME side of the cell, the input arrives
        # on the very face the output drives, and both contend on one single-outstanding
        # link → the single_cell_inout_deadlock the build DRC (bus_drc._check_single_cell_
        # inout) rejects. The router honours the geometry, so PLACEMENT must keep the two
        # neighbours on different sides. Forbid driver-cell and consumer-cell from sharing
        # the same side of the single cell: encode the sign of (neighbour - cell) on each
        # axis and require the (dx_sign, dy_sign) arrival vector != the drive vector.
        # (Only meaningful with the I/O-cell vars, i.e. this slack-0 / wirelength branch.)
        _consumer_of = {}
        for _c in placer._project.connections:
            _s, _t = _c.source, _c.target
            if hasattr(_s, "block") and hasattr(_t, "block") \
                    and _s.block in names and _t.block in names:
                _consumer_of.setdefault(_s.block, _t.block)
        for b in blocks:
            pl = b.placement
            if pl is None or len(pl.cells) != 1:
                continue                              # multi-cell: in/out are distinct cells
            drv = placer._driver_of.get(b.name)       # upstream BLOCK (not a chip port)
            con = _consumer_of.get(b.name)            # downstream BLOCK
            if drv is None or con is None:
                continue                              # a port-fed or terminal cell is exempt
            if drv not in out_cx or con not in in_cx or b.name not in in_cx:
                continue
            cx, cy = in_cx[b.name], in_cy[b.name]     # == out_cx/out_cy (one cell)
            # The DRC face is the ORTHOGONAL step from the cell to the input broker (arrival)
            # vs to the output's first waypoint (drive) — both single-axis steps; a deadlock
            # is arrival-face == drive-face. Encode each neighbour's SIGN on both axes
            # (lt/gt = strictly less/greater than the cell coord); a face is the (sign_x,
            # sign_y) pair (exactly one axis non-zero for an adjacent cell). Two neighbours
            # share a face iff BOTH sign pairs match. Require they DIFFER on at least one of
            # the four sign bits — this allows N-vs-W (an L bend: signs (0,-1) vs (-1,0)
            # differ) and opposite sides (a straight daisy-chain), forbidding only the
            # genuine same-face case. Applied as a hard rule ONLY for the abutted geometry
            # (the driver/consumer cell IS the broker); for a brokered non-abutted neighbour
            # the router still has face freedom, so we keep this a placement PREFERENCE
            # there by not forcing it (the build-clean acceptance gate in auto_pnr catches
            # any residual). Since abutment-first drives most of these edges to abut, the
            # hard rule below removes the flakiness at its source.
            def _sign_bits(nx, ny, tag):
                lx = model.NewBoolVar(f"scl_{tag}_lx")
                gx = model.NewBoolVar(f"scl_{tag}_gx")
                ly = model.NewBoolVar(f"scl_{tag}_ly")
                gy = model.NewBoolVar(f"scl_{tag}_gy")
                model.Add(nx < cx).OnlyEnforceIf(lx)
                model.Add(nx >= cx).OnlyEnforceIf(lx.Not())
                model.Add(nx > cx).OnlyEnforceIf(gx)
                model.Add(nx <= cx).OnlyEnforceIf(gx.Not())
                model.Add(ny < cy).OnlyEnforceIf(ly)
                model.Add(ny >= cy).OnlyEnforceIf(ly.Not())
                model.Add(ny > cy).OnlyEnforceIf(gy)
                model.Add(ny <= cy).OnlyEnforceIf(gy.Not())
                return (lx, gx, ly, gy)
            a = _sign_bits(out_cx[drv], out_cy[drv], f"{b.name}_in")
            d = _sign_bits(in_cx[con], in_cy[con], f"{b.name}_out")
            neqs = []
            for k, (ab, db) in enumerate(zip(a, d)):
                ne = model.NewBoolVar(f"scl_{b.name}_ne{k}")
                model.Add(ab != db).OnlyEnforceIf(ne)
                model.Add(ab == db).OnlyEnforceIf(ne.Not())
                neqs.append(ne)
            model.AddBoolOr(neqs)   # sign patterns differ => faces cannot coincide

        # ABUTMENT-FIRST: maximise the number of abutted (route-free) dataflow edges as
        # the PRIMARY objective; total wirelength stays a small secondary tie-break so
        # the non-abutting nets still route short. Weight the abut count far above the
        # wirelength sum (each abutment saves a whole corridor, worth >> one cell of wire).
        if abutment_first and abut_bools:
            ABUT_W = 2 * (W + H)   # one abutment outweighs any single wire term
            model.Maximize(ABUT_W * sum(abut_bools) - sum(terms))
        elif terms:
            model.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_time_s)
    solver.parameters.num_search_workers = 8
    if random_seed is not None:
        # Diversify the solution across attempts (integrated place<->route retries a few
        # seeds and keeps the first fully-routable layout). A different seed + a single
        # worker explores a different optimum among the many equal-wirelength packings.
        solver.parameters.random_seed = int(random_seed)
        solver.parameters.num_search_workers = 1
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
        for (lit, k, w, h, io, faces) in kind_of[b.name]:
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
