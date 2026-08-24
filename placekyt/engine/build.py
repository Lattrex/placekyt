# SPDX-License-Identifier: GPL-3.0-or-later
"""Build pipeline adapter — Project → bitstream.

This is a THIN ADAPTER over the existing gr_kyttar placement/bitstream code
(§0.1). It does not re-implement routing, resolving, or bitstream generation —
it translates the placeKYT project model into the structures those tools expect,
calls them, and packages the result.

Pipeline per chip:
    project Block + Placement  ──►  gr_kyttar PlacedBlock(Shape, anchor)
    BlockCatalog.instantiate    ──►  gr_kyttar BlockDefinition (cell programs)
    ChipType                    ──►  gr_kyttar ArrayConfig (+ PortConfig)
                                          │
                                  Router.route()  ──►  CellMap
                                          │
                          BitstreamGenerator.load_cell_map().generate()
                                          │
                                     Bitstream.words  (uint16 list)

The placeKYT model stores EXPLICIT per-cell placement (the user places each
cell), whereas gr_kyttar's Placement is built from a Shape (ordered relative
offsets) plus an anchor. We bridge by taking each block's cell list in order,
using the first cell as the anchor, and constructing a Shape from each cell's
offset to that anchor. This preserves the exact manual placement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gr_kyttar.placement.placer import Placement as GrPlacement
from gr_kyttar.placement.region import ArrayConfig, PortConfig
from gr_kyttar.placement.region import Face as GrFace
from gr_kyttar.placement.region import PortDirection as GrPortDirection
from gr_kyttar.placement.router import Router
from gr_kyttar.placement.shapes import Shape
from gr_kyttar.bitstream.generator import BitstreamGenerator

from model.board import Board
from model.chip_type import ChipType
from model.project import Project

from .catalog import BlockCatalog
from .drc import (DRCError, DRCResult, check_project, error as drc_error,
                  warning as drc_warning)
from .errors import EngineError

# BuildError is an alias of the shared DRCError so a build surfaces one uniform
# error type whether the finding came from the DRC pass or from generation.
BuildError = DRCError


class BuildAbort(EngineError):
    """A generation step hit a condition it must not paper over (e.g. an exit
    cell that cannot hold its fan-out form). RAISED (unlike :class:`BuildError`,
    a DRC finding VALUE) and caught by the per-chip build loop, which surfaces
    it as a named ``build_failed`` error. Raising the DRCError alias was a
    latent crash — DRCError is a dataclass finding, not an exception."""


@dataclass
class ChipBuild:
    """Build output for a single chip."""

    chip_id: int
    words: list[int] = field(default_factory=list)
    cell_count: int = 0  # programmed + routing cells used
    # Per-cell resolved program: (x, y) -> {"entry": int, "memory": [32 words]}
    # — feeds the Inspector memory/assembly view (§3.3).
    cells: dict = field(default_factory=dict)
    # Per PORT→block input-net HOST-injection landing, keyed by connection name:
    # ``{conn: {"cell": (x,y), "entry": int, "hop": int, "data_addrs": [reg,...]}}``.
    # Resolved from the BUILT corridor faces + broker entries (NOT a manhattan straight
    # line), so engine.port_config.stream_targets steers each shared-port stream to the
    # cell/entry/hop the routed corridor actually delivers to. A net that rides its
    # corridor straight to the block resolves to the block cell+entry; one diverted at
    # a broker resolves to that broker's deliver entry. ``hop`` is the raw 5-bit field.
    input_landings: dict = field(default_factory=dict)
    # Per-batch (packet-boundary) state resets, resolved from the PLACED design's
    # StateVars flagged ``reset_per_batch``: a list of ``(x, y, addr, value)`` — the
    # cell grid position, the register address, and the cold-start value. The host
    # backdoor-writes each of these into the cell memory at the START of every
    # process_batch (each RPC = one packet boundary), returning a persistently-hosted
    # receiver's loop MEMORY (Costas phase/freq, Gardner timing accumulators, the
    # matched-filter delay lines) to a cold start for a fresh packet — WITHOUT
    # resetting/reprogramming the whole chip. Resolved from the ACTUAL placed cells +
    # v2 register allocation (so it works for the auto-P&R modem, not just a hand
    # build). Empty when no block flags any reset state.
    batch_reset_writes: list = field(default_factory=list)
    # RELAY cells consumed by OVER-BUDGET (>31-hop) nets, keyed by connection name:
    # ``{conn: [(x, y), ...]}`` in route order. A relay is a plain routing cell the
    # word LANDS on and is re-emitted from with a fresh hop budget (§1.4 #3) — it
    # costs one array cell per 30 hops of route, so the cost is reported here (and
    # surfaced by ``relay_cost``) rather than being invisible.
    relay_cells: dict = field(default_factory=dict)

    @property
    def relay_cost(self) -> int:
        """Total array cells spent on >31-hop relays for this chip."""
        return sum(len(v) for v in self.relay_cells.values())


@dataclass
class BuildResult:
    """Result of building a project (§4.1 ``project.build()``)."""

    chips: dict[int, ChipBuild] = field(default_factory=dict)
    errors: list[DRCError] = field(default_factory=list)
    warnings: list[DRCError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def words(self, chip_id: int = 0) -> list[int]:
        """Convenience: the word list for one chip (default chip 0)."""
        return self.chips[chip_id].words if chip_id in self.chips else []


class BuildEngine:
    """Drives the build pipeline for a project.

    ``chip_type_paths`` maps chip-type names to their YAML files — the engine
    layer owns chip-type registry resolution (§2.3), so the caller supplies the
    path(s). For a single-type project, a lone path is accepted.
    """

    def __init__(
        self,
        catalog: BlockCatalog,
        chip_type_paths: dict[str, str] | str,
    ):
        self.catalog = catalog
        if isinstance(chip_type_paths, str):
            self._chip_type_paths = {"*": chip_type_paths}
        else:
            self._chip_type_paths = dict(chip_type_paths)

    def _chip_type_path(self, chip_type_name: str) -> str:
        if chip_type_name in self._chip_type_paths:
            return self._chip_type_paths[chip_type_name]
        if "*" in self._chip_type_paths:  # single-type convenience
            return self._chip_type_paths["*"]
        raise KeyError(
            f"no chip-type YAML path registered for {chip_type_name!r}"
        )

    # -- public API -----------------------------------------------------------

    def build(
        self,
        project: Project,
        chip_types: dict[str, ChipType],
        board: Board | None = None,
    ) -> BuildResult:
        """Build every chip in the project.

        Runs project-level DRC FIRST (§5.2); if any DRC error is found, the
        build stops before generation and returns all findings (errors block
        generation, warnings/infos are carried through). On a clean DRC the
        bitstream is generated per chip and ``build_dirty`` is cleared.

        ``chip_types`` maps chip-type name → loaded :class:`ChipType`. ``board``
        (optional) enables the ``inter_chip_not_wired`` DRC check.
        """
        result = BuildResult()

        # 0. SRAM-panel designs: re-derive every placement-dependent panel
        # parameter (push-read descriptors, crossover track hops/entries, the
        # RAW keyer's emit/done targets) from the CURRENT routes — so a user-
        # moved / hand-rerouted panel chain builds with correct values instead
        # of silently keeping the ones the auto-P&R baked for the old geometry.
        # Same philosophy as faces: the routes are the truth; the build derives.
        if project.panels:
            try:
                from .panel_pnr import refresh_panel_params
                for _n in refresh_panel_params(project, self.catalog):
                    result.warnings.append(drc_warning(
                        "panel_param_refreshed",
                        f"panel parameter re-derived from routes: {_n}",
                        chip=0, x=0, y=0))
            except Exception as _pe:  # noqa: BLE001 — surface, never silent
                result.warnings.append(drc_warning(
                    "panel_param_refresh_failed",
                    f"could not re-derive panel parameters from routes: {_pe}",
                    chip=0, x=0, y=0))

        # 1. Project-level DRC. Collect everything; errors block generation.
        # check_project now folds in the BUS DRC (§1.3/§5.3: face conflicts + the
        # single-cell input==output deadlock hazard + dual-input-same-face) when given
        # the catalog — the SAME checks that used to be re-run only here. Passing the
        # catalog makes the DRC panel/badge and this build gate report one identical
        # error list (no more "panel clean / sim aborts with 1 DRC error").
        drc = check_project(project, chip_types, board, catalog=self.catalog)
        result.errors.extend(drc.errors)
        result.warnings.extend(drc.warnings)
        # utilization INFOs are reported separately; not added to errors/warnings.
        if not result.ok:
            return result  # do not generate when DRC has errors

        # 2. Generation, per chip.
        chip_ids = [c.id for c in project.chips] or [0]
        for chip_id in chip_ids:
            chip = project.chip(chip_id)
            type_name = (
                (chip.type_name if chip and chip.type_name else project.chip_type)
            )
            ct = chip_types.get(type_name)
            if ct is None:
                result.errors.append(drc_error(
                    "unknown_chip_type",
                    f"chip-type {type_name!r} not provided to build()",
                    chip=chip_id,
                ))
                continue
            try:
                self._build_chip(project, chip_id, type_name, ct, result)
            except Exception as exc:  # noqa: BLE001 — surface as a build error
                result.errors.append(
                    drc_error("build_failed", str(exc), chip=chip_id)
                )

        if result.ok:
            project.build_dirty = False
        return result

    # -- per-chip build -------------------------------------------------------

    def _build_chip(
        self,
        project: Project,
        chip_id: int,
        type_name: str,
        chip_type: ChipType,
        result: BuildResult,
    ) -> None:
        # Blocks placed on this chip.
        blocks_here = [
            b
            for b in project.blocks
            if b.placement is not None and b.placement.chip == chip_id
        ]

        conns_here = [c for c in project.connections]
        gr_placement, block_defs, errs, gr_blocks = self._translate(
            blocks_here, conns_here, chip_type, chip_id)
        result.errors.extend(errs)
        if errs:
            return

        config = _array_config(chip_type)
        # Router must know the I/O port names so it routes the sink block's
        # output to the output port and fixes up the output WRITE hop count
        # (without these it cannot target x16_out — data never exits).
        in_port = _first_port(chip_type, "input")
        out_port = _first_port(chip_type, "output")
        router = Router(config, input_port=in_port, output_port=out_port)
        # placeKYT OWNS routing — the user's drawn route waypoints are the truth.
        # ``skip_io_routing`` stops the Router from A*-fabricating input/output
        # and block→block paths (which invented phantom routes, e.g. a bogus
        # row-0 path, regardless of what the user drew).
        cell_map = router.route(gr_placement, block_defs, skip_io_routing=True)

        # The Router (with I/O routing skipped) no longer sets a block's OUTPUT
        # exit-cell face — that used to be a side effect of A*-routing to the
        # output port. Restore the block's AUTHORED faces from the model
        # placement (default_layout faces + any user rotations), so the exit
        # cell points where the block intends. Drawn routes + abutment below
        # still override per the actual connections.
        _apply_block_cell_faces(cell_map, blocks_here)

        # Transform any block's IN-PROGRAM face constants by its orientation: a
        # `MOVE [FACE], const` selects an ABSOLUTE direction, so when the placer
        # rotates/mirrors a block (auto-orient toward the bus), that direction
        # must rotate identically — same D4 map the cell `.face` already got.
        _apply_orientation_face_words(cell_map, blocks_here, gr_blocks)

        # Face every cell STRICTLY from the user's drawn routes (each routed
        # connection's waypoints), and set each routed block's exit hop from its
        # route length (unless the block authors its own hops). Nothing is
        # invented — a cell with no route on it stays unfaced.
        _apply_routes(cell_map, gr_placement, blocks_here, conns_here,
                      chip_type, gr_blocks, self.catalog, project)

        # Program the BUS BROKER cells (AUTO_PNR_DESIGN §1.2): a routed net whose
        # final waypoint is a free routing cell abutting the target block taps the
        # bus through a programmed broker (flip→relay→restore), NOT through the
        # block's own cells. This emits the broker program into those cells and
        # re-points the SOURCE block's exit WRITE/JUMP at the broker (dest 0 = the
        # broker's burst reg, entry = its deliver entry, hop = route distance). It
        # runs RIGHT AFTER _apply_routes (which faced the plain bus cells + set the
        # source exit toward the target) and OVERRIDES the source exit toward the
        # broker for bus-routed nets. Plain corridor/abutment nets (route ends ON
        # the target) have no broker and are untouched.
        broker_conn_entry, broker_conn_burst, fanout_abut_conns = _apply_brokers(
            cell_map, gr_placement, blocks_here, conns_here,
            project, chip_id, chip_type, gr_blocks, self.catalog)

        # Resolve single-fwd_face CONFLICT cells (§1.2/§1.3): where two routed nets
        # must leave one PLAIN routing cell in DIFFERENT directions (the (9,0) corner
        # where the Costas→Gardner transit goes WEST while the slicer→x16_out egress
        # needs EAST), a static face silently corrupts one stream. Promote each such
        # cell to a programmed CROSSOVER demux (the proven CrossoverBlock primitive):
        # each net lands via its own JUMP entry + exit face and re-emits onward. Runs
        # AFTER _apply_brokers so the source exit carries its final dest/entry/hop
        # (the crossover SPLITS that delivery at the contended cell).
        _apply_crossovers(cell_map, gr_placement, blocks_here, conns_here,
                          project, chip_id, chip_type, gr_blocks, self.catalog)

        # OVER-BUDGET (>31-hop) ROUTES (§1.4 #3): the hop field is 5 bits, so ONE
        # WRITE/JUMP pair cannot address a cell more than 31 hops away. Split such a
        # net at intermediate RELAY cells — the word LANDS on a plain routing cell,
        # which re-emits it with a FRESH budget toward the rest of the route. Runs
        # after brokers/crossovers (the source exit now carries its FINAL
        # dest/entry/hop, which the LAST relay reproduces) and before the universal
        # transit program (so a relay cell keeps its relay program). Relay cells cost
        # array area, so the build reports which cells each net consumed.
        relay_cells = _apply_relays(cell_map, gr_placement, blocks_here,
                                    conns_here, project, chip_id, chip_type,
                                    gr_blocks, self.catalog)

        # A DUAL-FACE output cell (e.g. the Costas `rotate`) emits its INTERNAL
        # handoffs on one face and its TAP output on a ROUTE-DETERMINED face. The
        # route's exit direction is now on the cell's `fwd_face` (set by routes/
        # brokers above), so copy it into the cell's `face_tap` in-program constant
        # — that's the face the program flips to before the tap WRITE. (No-op for a
        # block whose output cell has no `face_tap` word, or whose tap face already
        # matches its internal face, e.g. a standalone Costas with unconsumed tap.)
        _apply_rotate_tap_face(cell_map, gr_placement, blocks_here, gr_blocks)

        # Close any block's INTERNAL feedback (e.g. Costas pd_pi -> phase) through
        # its own transit return path BEFORE the exit-hop default, so the feedback
        # output is recognised + skipped by the @1-abutment defaulting below.
        feedback_blocks = _apply_internal_feedback(
            cell_map, gr_placement, blocks_here, gr_blocks, self.catalog)

        _default_unrouted_exit_hops(cell_map, gr_placement, blocks_here,
                                    conns_here, gr_blocks, self.catalog,
                                    feedback_blocks=feedback_blocks,
                                    skip_conns=fanout_abut_conns)

        # RE-ASSERT the block's AUTHORED internal-forward face on any cell that is a
        # SOURCE of an internal_connections handoff (a mid-block cell forwarding to
        # the next cell of its OWN block — e.g. a multi-cell FIR's cell m → cell m+1).
        # An INCOMING inter-block route/abutment whose TARGET is that same cell (a
        # block whose landing cell is also an internal forwarder — the complex FIR's
        # cell0 receives the mixer's packet AND forwards to cell1) can overwrite the
        # cell's fwd_face with the route direction, killing the internal wavefront.
        # The block's authored PlacedCell.face (already orientation-rotated) is the
        # correct internal direction, so restore it LAST for those cells only.
        _reassert_internal_forward_faces(cell_map, blocks_here, gr_blocks)

        # Inter-chip hop resolution: a block on THIS chip routing to a chip
        # output port that is wired to ANOTHER chip's input port should hand off
        # all the way into the downstream block on that chip. The hop count is
        # continuous across the boundary (the interconnect is not a hop), so it
        # is this chip's exit distance + the next chip's route distance.
        _apply_inter_chip_hops(cell_map, gr_placement, blocks_here, project,
                               chip_id, chip_type, self.catalog)

        # Apply per-instruction overrides AFTER the Router's auto-fixup: the
        # hop count / dest / entry of a WRITE/JUMP are the instruction's own
        # properties (§3.3). The user's chosen values win over the route-derived
        # ones. Overrides live on each block's placement, keyed by (cell_id,addr).
        ownership = _apply_instr_overrides(cell_map, blocks_here)

        # UNIVERSAL ROUTING-CELL PROGRAM (Reading B, maintainer-approved): every
        # remaining PLAIN TRANSIT spine cell (a cell with a fwd_face but no program)
        # gets the uniform transmit(+relay) program so the whole fabric is made of
        # generic, dynamically-repurposable control cells (enabling §4.2). Runs LAST
        # of the routing passes — after faces/brokers/crossovers/feedback have set
        # every cell's fwd_face — so it ONLY touches cells still face-only, and does
        # NOT disturb their fwd_face (pass-through of HOP<31 words is unchanged; the
        # program's entries fire only at HOP_CNT==31, never for transiting traffic).
        _apply_routing_cell_programs(cell_map)

        # SHARED INPUT-PORT DIVERT (§1.2/§1.3): the chip-input port cell has ONE
        # fwd_face, but a full-duplex port fans out to TWO blocks whose first route
        # steps leave the port in DIFFERENT directions. One stream rides the static
        # face straight; the OTHER must LAND at the port cell and be RELAYED off it.
        # Promote the port cell to a broker for the diverting net(s) — flip toward the
        # net's first waypoint, relay ONE hop to its downstream broker (which finishes
        # the delivery into the block), restore the bus face. Runs AFTER the universal
        # routing program so it REPLACES the port cell's latent transit program with the
        # turn program (fwd_face unchanged → the riding stream is untouched). Returns
        # the diverted nets' host-injection landings (merged into input_landings below).
        port_divert_landings = _apply_port_diverts(
            cell_map, blocks_here, conns_here, project, chip_id, chip_type,
            self.catalog, broker_conn_entry, broker_conn_burst)

        # Reconcile a face-locking rendezvous block's LOCK faces (DualFloatToComplex) to
        # the ROUTED geometry: after all corridor faces/brokers are set, patch its
        # face_i/face_q DataWords + cold-start LOCK to the faces its i/q input nets
        # actually arrive on (the placer only guarantees they DIFFER; the router picks
        # which). Without this the LOCK gates the wrong faces and the rendezvous stalls.
        _apply_rendezvous_input_faces(cell_map, blocks_here, conns_here, project,
                                      self.catalog, gr_blocks)

        # Resolve each PORT→block input net's HOST-injection landing from the BUILT
        # corridor (faces + broker entries) — so the live bridge steers a shared-port
        # stream to the cell/entry/hop the routed corridor actually delivers to, not a
        # manhattan straight line (which the off-port multi-filament auto-layout breaks).
        input_landings = _resolve_input_landings(
            cell_map, blocks_here, conns_here, project, chip_id, chip_type,
            gr_placement, self.catalog, broker_conn_entry, broker_conn_burst)
        # A net that DIVERTS at the shared port cell (relayed off it by the port
        # broker, above) OVERRIDES the straight corridor resolution: the host must land
        # it AT the port cell (its turn entry / hop / burst regs), NOT ride it straight
        # (which would forward it down the OTHER stream's face and lose it).
        input_landings.update(port_divert_landings)

        # Per-cell address classification (data / state / instruction) from the
        # v2 CellProgram of each block, so the Inspector can tell DATA words
        # (coefficients, etc.) from executable instructions (§3.3).
        classes = _classify_cells(blocks_here, gr_blocks)

        # Per-batch state resets (packet-boundary loop-memory cold start): resolve
        # every ``reset_per_batch`` StateVar of every placed block to a concrete
        # (x, y, addr, value) write the host applies at the top of each process_batch.
        batch_reset_writes = _resolve_batch_reset_writes(blocks_here, gr_blocks)

        gen = BitstreamGenerator(self._chip_type_path(type_name))
        gen.load_cell_map(cell_map)
        bitstream = gen.generate()

        result.chips[chip_id] = ChipBuild(
            chip_id=chip_id,
            words=list(bitstream.words),
            cell_count=cell_map.cell_count(),
            cells=_extract_cell_memory(cell_map, ownership, classes),
            input_landings=input_landings,
            batch_reset_writes=batch_reset_writes,
            relay_cells=relay_cells,
        )

        # STRAY-EMISSION DRC (P3.4): a WRITE/JUMP that lands on an EMPTY/unowned
        # cell will stray-execute on the universal forwarding program (data into
        # dead space — the "phantom route" hazard, e.g. a dual-face output cell
        # whose egress FACE didn't follow its drawn route, or an output not yet
        # wired anywhere). Surfaced as a WARNING (named, with the dead cell) so it
        # shows in the GUI findings + names the phantom WITHOUT blocking a build of
        # an in-progress design whose output simply isn't routed yet. The real
        # mis-route is prevented at the source (the output FACE follows the route).
        try:
            from .bus_drc import check_stray_emissions, owned_cells
            own = owned_cells(project, chip_id)
            for v in check_stray_emissions(
                    result.chips[chip_id].cells, own, ct.width, ct.height):
                result.warnings.append(drc_warning(
                    "stray_emission", v.reason,
                    chip=chip_id, x=v.cell[0], y=v.cell[1]))
        except Exception:  # noqa: BLE001 — best-effort; never break a build itself
            pass

    def _translate(
        self,
        blocks: list,
        connections: list,
        chip_type: ChipType,
        chip_id: int,
    ) -> tuple[GrPlacement, list, list[BuildError], dict]:  # noqa: D
        """Translate placeKYT blocks + connections → gr_kyttar Placement +
        BlockDefinitions. Also returns the ``{name: gr_block}`` instance map so
        callers can read each block's v2 metadata (address classification).

        Two passes so the project's CONNECTIONS take effect: (1) instantiate
        every block; (2) apply block→block ``connect_to`` so the Router fixes up
        each source's WRITE/JUMP hop count to the routed destination; then build
        definitions and place. (Per-instruction hop/dest/entry overrides are
        applied later by :func:`_apply_instr_overrides`, after routing.)
        """
        from model.connection import BlockEndpoint, ChipPortEndpoint

        errors: list[BuildError] = []
        placement = GrPlacement()
        block_defs = []

        # Pass 1: instantiate every placed block.
        gr_blocks: dict[str, object] = {}
        anchors: dict[str, tuple[int, int]] = {}
        shapes: dict[str, object] = {}
        for blk in blocks:
            spec = self.catalog.get(blk.type, blk.library)
            if spec is None:
                errors.append(drc_error(
                    "unresolved_block",
                    f"block {blk.name!r}: unknown type {blk.type!r}",
                    chip=chip_id,
                ))
                continue
            cells = blk.placement.cells
            if not cells:
                errors.append(drc_error(
                    "unplaced_cell",
                    f"block {blk.name!r} has a placement but no cells",
                    chip=chip_id,
                ))
                continue
            anchor = (cells[0].x, cells[0].y)
            offsets = [(c.x - anchor[0], c.y - anchor[1]) for c in cells]
            try:
                gr_block = self.catalog.instantiate(blk.type, blk.name, blk.params,
                                                    library=blk.library)
            except Exception as exc:  # noqa: BLE001
                errors.append(drc_error(
                    "block_build_failed", f"block {blk.name!r}: {exc}",
                    chip=chip_id, x=anchor[0], y=anchor[1]))
                continue
            gr_blocks[blk.name] = gr_block
            anchors[blk.name] = anchor
            # A block may declare that its OUTPUT leaves a NON-last cell (e.g. a
            # Costas loop's recovered I exits the rotate cell, which is mid-block).
            # Find that cell's offset by matching cell_id so the Shape's exit_cell
            # is correct (the router applies the output route's hop there, and the
            # GUI marks the right cell). Default None ⇒ last cell, as before.
            exit_offset = None
            out_cid = None
            try:
                out_cid = gr_block.output_cell_id()
            except Exception:  # noqa: BLE001 — older blocks lack the method
                out_cid = None
            if out_cid is not None:
                for c, off in zip(cells, offsets):
                    if getattr(c, "cell_id", None) == out_cid \
                            or str(getattr(c, "cell_id", "")) == str(out_cid):
                        exit_offset = off
                        break
            shapes[blk.name] = Shape(cells=offsets, exit_offset=exit_offset)

        # Pass 2: wire block→block connections so the Router routes between them
        # and fixes up the source block's WRITE/JUMP hop counts (§5.4).
        for conn in connections:
            src, tgt = conn.source, conn.target
            if (isinstance(src, BlockEndpoint) and src.block in gr_blocks
                    and isinstance(tgt, BlockEndpoint) and tgt.block in gr_blocks):
                gr_blocks[src.block].connect_to(gr_blocks[tgt.block])

        # Build definitions + place. placeKYT consumes the CANONICAL v2 block
        # definitions (declarative assembly_template + DataWord/StateVar). The
        # Router auto-detects v2 (template present) and runs CellProgramResolver
        # to produce final memory + resolve WRITE/JUMP. v1 (hand-packed memory,
        # no data/instruction distinction) is obsolete and not used. See §0.1.
        for name, gr_block in gr_blocks.items():
            try:
                block_def = gr_block.get_block_definition()
            except Exception as exc:  # noqa: BLE001
                ax, ay = anchors[name]
                errors.append(drc_error(
                    "block_build_failed", f"block {name!r}: {exc}",
                    chip=chip_id, x=ax, y=ay))
                continue
            # If the block's output leaves a MID-block cell (which also carries
            # internal handoffs), tell the Router to patch only that cell's LAST
            # WRITE for the output hop — not every WRITE (which would clobber the
            # internal handoffs). See BlockDefinition.output_at_last_write.
            try:
                if gr_block.output_cell_id() is not None:
                    block_def.output_at_last_write = True
            except Exception:  # noqa: BLE001
                pass
            try:
                placement.place(block_def, shapes[name], anchors[name])
            except Exception as exc:  # noqa: BLE001 — overlap, etc.
                ax, ay = anchors[name]
                errors.append(drc_error(
                    "overlap", f"block {name!r}: {exc}",
                    chip=chip_id, x=ax, y=ay))
                continue
            block_defs.append(block_def)

        return placement, block_defs, errors, gr_blocks


# --------------------------------------------------------------------------- #
# ChipType → ArrayConfig
# --------------------------------------------------------------------------- #

_FACE_TO_GR = {
    "south": GrFace.SOUTH,
    "east": GrFace.EAST,
    "west": GrFace.WEST,
    "north": GrFace.NORTH,
}


_WRITE = 0x6000
_JUMP = 0x7000


def encode_hop_cnt(hops_away: int) -> int:
    """``@N`` hops-away → the 5-bit HOP_CNT field value (``31 - N``, clamped)."""
    return max(0, min(31, 31 - int(hops_away)))


def decode_hop_cnt(hop_cnt: int) -> int:
    """5-bit HOP_CNT field → ``@N`` hops away (``31 - HOP_CNT``)."""
    return 31 - (int(hop_cnt) & 0x1F)


_WRITE_CONFIG_BIT = 1 << 10  # WRITE.CFG: dest names a CONFIG addr, not a reg
_LOCK_CFG_ADDR = 4           # CONFIG[4] = LOCK (arbiter lock enable); the pipeline-
#                              interlock lock-clear is a backward WRITE.CFG to this addr


def _patch_instr(word: int, ov) -> int:
    """Apply an :class:`InstrOverride` to one WRITE/JUMP word.

    HOP_CNT is bits [9:5]; the dest/entry field is bits [4:0]. ``ov.hop`` is in
    hops-away (``@N``) form. ``ov.dest`` overrides a WRITE's destination
    register (or CONFIG address when ``ov.dest_config`` — sets bit 10);
    ``ov.entry`` overrides a JUMP's entry address (both land in the low 5 bits,
    the same field for the two opcodes).
    """
    opcode = word & 0xF000
    if ov.hop is not None:
        word = (word & ~(0x1F << 5)) | (encode_hop_cnt(ov.hop) << 5)
    target = ov.dest if opcode == _WRITE else ov.entry
    if target is not None:
        word = (word & ~0x1F) | (int(target) & 0x1F)
    if opcode == _WRITE and ov.dest is not None:
        # Only touch the config bit when the dest was explicitly overridden.
        if ov.dest_config:
            word |= _WRITE_CONFIG_BIT
        else:
            word &= ~_WRITE_CONFIG_BIT
    return word & 0xFFFF


_HOP1_CNT = 30  # HOP_CNT for @1 (31 - 1) — hand off to the abutting cell
# fwd_face int (S=0, E=1, W=2, N=3) → (dx, dy) toward the abutting cell.
_FWD_DELTA = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}


def _is_instruction_addr(cfg, addr) -> bool:
    """True if ``addr`` holds an INSTRUCTION (not a data word) in this cell.

    The resolver packs data words at the BOTTOM (addresses below the cell's
    entry) and lays the program at/above ``entry_addr``. So an address below the
    entry is a data word and must NEVER be hop-patched — critical because a data
    word can coincidentally carry an instruction-like top nibble (e.g. the Q15
    constant 0x7FFF has opcode nibble 0x7 = JUMP, and would otherwise be mangled).
    If the cell has no entry (a pure routing/transit cell), every word is real."""
    if cfg.entry_addr is None:
        return True
    return addr >= cfg.entry_addr


def _patch_cell_handoff(cfg, hop, dest=None, entry=None,
                        n_output_writes: int | None = None) -> None:
    """Set WRITE/JUMP INSTRUCTIONs in a cell to a specific ``hop`` (in @N
    hops-away form) and, when given, the dest register (WRITE) / entry addr
    (JUMP). Data words are left untouched (see :func:`_is_instruction_addr`).

    ``n_output_writes`` — patch only the cell's LAST N writes (its OUTGOING
    packet), leaving earlier writes at their resolved hops. This matters
    whenever the exit cell ALSO carries internal-handoff writes, and it is not
    a corner case: every R2SDF FFT stage's ``out`` cell writes its emerging
    pair BACK into its own ``ctl`` and clears that stage's serialize-LOCK with
    a ``WRITE.CFG``, all at @1, before emitting the outgoing packet.

    N is the number of RAILS the block emits, not 1: a complex block's exit
    emits ``out_i`` then ``out_q`` from the same cell and BOTH must carry the
    cross-chip hop. Patching only the last leaves ``out_i`` at its
    single-chip hop — measured, and it kept the 2-die FFT livelocked even
    after the feedback writes were correctly preserved.

    Patching all of them was a real defect on the INTER-CHIP path (measured on
    the 2-die N=128 FFT): the identical die built for ONE chip resolved its
    exit cell to `WRITE @1 x3` (feedback pair + lock-clear) plus `WRITE @19 x2`
    and `JUMP @19` (egress), and ran bit-exact; built as chip 0 of a two-chip
    pair, ALL FIVE writes were rewritten to the cross-chip hop, so the stage's
    ``ctl`` never received its write-back and its lock was never cleared. The
    pipeline livelocked from the second trigger and nothing egressed — while
    the hop and entry were otherwise resolved perfectly, which is what made it
    look like a wiring fault.

    The single-chip path has honoured this since ``output_at_last_write``
    existed; this is the inter-chip path catching up. A simple block whose exit
    cell carries ONLY its output write is unaffected either way, which is why
    the shipped 2-chip gain example never exposed it.
    """
    hop_cnt = encode_hop_cnt(hop)
    addrs = [a for a in cfg.memory
             if _is_instruction_addr(cfg, a)
             and (cfg.memory[a] & 0xF000) in (_WRITE, _JUMP)]
    if n_output_writes:
        writes = sorted(a for a in addrs
                        if (cfg.memory[a] & 0xF000) == _WRITE)
        jumps = [a for a in addrs if (cfg.memory[a] & 0xF000) == _JUMP]
        # The OUTGOING packet is the LAST n writes the cell emits; the
        # trailing JUMP is the handshake that carries it on, so it moves too.
        addrs = writes[-int(n_output_writes):] + jumps
    # RAIL STEERING: a complex packet's rails go to CONSECUTIVE input
    # registers of the downstream block (out_i -> in_regs[0], out_q ->
    # in_regs[1]), exactly as the single-chip abutment path steers them.
    # Forcing every rail to in_regs[0] lands both words in one register and
    # the downstream landing cell never sees a complete sample.
    dests = dest if isinstance(dest, (list, tuple)) else [dest]
    wi = 0
    for addr in addrs:
        word = cfg.memory[addr]
        opcode = word & 0xF000
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        if opcode == _WRITE:
            target = (dests[wi] if wi < len(dests) else dests[-1]) \
                if dests and dests[0] is not None else None
            wi += 1
        else:
            target = entry
        if target is not None:
            word = (word & ~0x1F) | (int(target) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _target_port_index(catalog, target_block, port_name) -> int:
    """Index of ``port_name`` among the target block's INPUT ports (0 for the first
    input, 1 for the second, …). Falls back to 0. Used to steer each rail of an
    abutted complex packet (yi→input 0, yq→input 1) to the right target register."""
    try:
        pm = catalog.port_map(target_block.type, target_block.params,
                              library=target_block.library)
        ins = [p.name for p in pm.ports if p.direction == "in"]
        if port_name in ins:
            return ins.index(port_name)
    except Exception:  # noqa: BLE001 — no port map → first register
        pass
    return 0


def _cell_write_count(cfg) -> int:
    """Number of WRITE instructions in a cell's program (data words excluded)."""
    n = 0
    for addr, word in cfg.memory.items():
        if _is_instruction_addr(cfg, addr) and (word & 0xF000) == _WRITE:
            n += 1
    return n


def _patch_complex_abutment_handoff(cfg, hop, rail_idx, dest, entry=None) -> None:
    """Patch an abutted COMPLEX-PACKET source cell: set the @hop on every WRITE and
    the JUMP (so the whole packet + trigger reach the abutting target), but set the
    DEST register ONLY on the ``rail_idx``-th WRITE — so the I rail lands in the
    target's xi and the Q rail in its xq (two nets from one source cell, each
    handled once, never clobbering R0). The JUMP entry is set on the single JUMP."""
    hop_cnt = encode_hop_cnt(hop)
    write_i = 0
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        opcode = word & 0xF000
        if opcode not in (_WRITE, _JUMP):
            continue
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)   # @hop on all WRITE/JUMP
        if opcode == _WRITE:
            if write_i == rail_idx and dest is not None:
                word = (word & ~0x1F) | (int(dest) & 0x1F)
            write_i += 1
        elif opcode == _JUMP and entry is not None:
            word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _patch_complex_abutment_tail_handoff(cfg, hop, dests, entry=None) -> None:
    """Abutted COMPLEX pair from an output cell that ALSO carries handoffs (the
    serialize-LOCKED NCO/FM ``emit``, whose lock-clear ``WRITE.CFG`` sits AFTER
    the yi/yq rails): steer the LAST ``len(dests)`` DATA WRITEs to the abutting
    target's OWN input registers (``dests``, in emit order) @``hop``, patch the
    LAST JUMP to the target ``entry``, and leave every ``WRITE.CFG`` and earlier
    internal WRITE untouched.

    Without this, the carries-handoffs abutment path ran the SINGLE
    ``_patch_last_write_handoff`` once per rail net — both rails LAST-WINS into
    the one final data WRITE, so yi and yq both wrote the target's R0 and the
    consumer computed (yq·g, 0) (the auto_pnr-abutted locked-FM→ComplexGain
    zero-Q bug, 2026-08-16). Call ONCE (from the I-rail net)."""
    hop_cnt = encode_hop_cnt(hop)
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE
                         and not (w & _WRITE_CONFIG_BIT))
    jump_addrs = sorted(a for a, w in cfg.memory.items()
                        if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP)
    tail = write_addrs[-len(dests):]
    for k, addr in enumerate(tail):
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(dests[k]) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF
    if jump_addrs:
        addr = jump_addrs[-1]
        word = (cfg.memory[addr] & ~(0x1F << 5)) | (hop_cnt << 5)
        if entry is not None:
            word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _patch_complex_output_port_handoff(cfg, hop, base_tag, entry=0) -> None:
    """Patch a COMPLEX-OUTPUT source cell whose two rails (yi, yq) exit the CHIP
    OUTPUT PORT: set the @hop on every WRITE and the JUMP, and give each WRITE its OWN
    dest TAG — the k-th output WRITE gets ``base_tag + k`` (yi→base_tag, yq→base_tag+1).

    This is the OUTPUT-side analogue of the complex INPUT (xi→a0, xq→a1): the port
    demux keys on the WRITE's dest field, so distinct tags keep I and Q as SEPARATE
    captured streams instead of one interleaved [I0,Q0,I1,Q1,…] tag. The waveform then
    plots two clean traces (cos φ, sin φ) rather than a jagged interleaved band."""
    hop_cnt = encode_hop_cnt(hop)
    write_i = 0
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        opcode = word & 0xF000
        if opcode not in (_WRITE, _JUMP):
            continue
        # SKIP a WRITE.CFG (config-bit set): the serialize-LOCK's backward unlock
        # (emit → phase CONFIG[LOCK]) is a WRITE with the CONFIG bit — it is NOT an
        # egress rail. Patching it with the port hop + an output tag would break the
        # lock release (and mis-count the yi/yq rails). Leave it untouched.
        if opcode == _WRITE and (word & _WRITE_CONFIG_BIT):
            continue
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)   # @hop on all WRITE/JUMP
        if opcode == _WRITE:
            word = (word & ~0x1F) | ((int(base_tag) + write_i) & 0x1F)
            write_i += 1
        elif opcode == _JUMP:
            word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _output_cell_carries_handoffs(gr_block) -> bool:
    """True if the block's OUTPUT exit cell ALSO emits internal handoff WRITEs (so
    the output WRITE must be patched ALONE, not every WRITE in the cell).

    Two cases need the "patch only the last WRITE/JUMP" treatment:
      * ``output_cell_id() is not None`` — the output leaves a NON-last cell that
        also carries internal handoffs (e.g. the Costas ``rotate`` cell: yi→pd_pi
        internally AND yi_tap→the bus); the long-standing flag.
      * the output leaves the LAST cell, but that cell is ALSO the source of an
        ``internal_connections`` handoff (e.g. the Gardner ``loop_filter`` cell:
        ``period_fb``→resampler feedback AND ``out``→downstream). Here
        ``output_cell_id()`` is None, yet patching EVERY WRITE in the cell would
        clobber the feedback WRITE and break the loop. Detect it from the netlist.

    The block emits its external output WRITE/JUMP LAST (after the internal
    handoffs), so :func:`_patch_last_write_handoff` / :func:`_patch_last_jump_handoff`
    correctly patch just the output instructions in both cases.
    """
    if gr_block is None:
        return False
    # Determine the OUTPUT exit cell: the explicit output_cell_id() if the block
    # declares one (its output leaves a non-last cell), else the last NON-transit
    # cell (transit_* cells are face-only routing, never the output).
    out_cid = None
    try:
        out_cid = gr_block.output_cell_id()
    except Exception:  # noqa: BLE001 — older blocks lack the method
        out_cid = None
    try:
        internal = list(gr_block.internal_connections() or [])
        if out_cid is not None:
            exit_cid = out_cid
        else:
            layout = gr_block.default_layout() or {}
            block_cids = [cid for cid in layout
                          if not (isinstance(cid, str) and cid.startswith("transit"))]
            if not block_cids:
                return False
            exit_cid = block_cids[-1]
        # "Carries handoffs" iff the exit cell ALSO emits a NON-output instruction
        # alongside its port output, so patching EVERY WRITE would clobber it:
        #   (a) it is the SOURCE of an internal_connection (a data feedback/handoff —
        #       the Costas rotate / Gardner loop_filter), OR
        #   (b) its program contains an inline WRITE.CFG (a config handoff — the
        #       iq_upconvert upmix cell's backward lock-clear), which is NOT a declared
        #       internal_connection.
        # A block that declares output_cell_id() purely to relocate `exit_offset` (so a
        # LATER non-output cell isn't taken as the exit — e.g. ComplexMixer's serialize-
        # lock, whose `unlock` cell is placed AFTER `mixer` and does the WRITE.CFG) but
        # whose OUTPUT cell emits ONLY its genuine yi/yq port rails (no internal WRITE,
        # no WRITE.CFG) is NOT this case: its two rails must BOTH be routed by
        # _patch_complex_output_port_handoff, so return False. (mixer.trig->unlock is an
        # internal_JUMP, not an internal_connection, so it correctly does not trip (a).)
        if any(src == exit_cid for (src, _sp, _d, _dp) in internal):
            return True
        try:
            cps = gr_block.build_cell_programs()
            cp = cps.get(exit_cid)
            tmpl = getattr(cp, "assembly_template", "") if cp is not None else ""
            if "WRITE.CFG" in tmpl:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
    except Exception:  # noqa: BLE001
        return False


def _route_distance(conn) -> int:
    """Hop distance of a routed connection: waypoints-1, +1 for a chip-output
    target (the data must transit through the edge cell to exit, §2.6)."""
    from model.connection import ChipPortEndpoint

    if not conn.is_routed:
        return 0
    # ABUTMENT: source output cell directly abuts the target input cell — a single
    # @1 hop (no corridor waypoints to count).
    if conn.is_abutment:
        return 1
    distance = max(0, len(conn.route) - 1)
    if isinstance(conn.target, ChipPortEndpoint) and conn.target.port.endswith("_out"):
        distance += 1
    return distance


def _phys_distance(conn, phys_pts) -> int:
    """Source-exit hop for a routed connection from its PHYSICAL waypoint path
    (``bus_router._phys_pts`` — a block→block route drawn ONTO the target input cell is
    stripped to the abutting broker). ``len(phys_pts)-1`` hops to the broker, +1 for a
    chip-output target (the data must transit the edge cell to exit). For the
    auto-router's stop-one-short routes ``phys_pts == conn.route`` so this equals
    :func:`_route_distance`."""
    from model.connection import ChipPortEndpoint

    distance = max(0, len(phys_pts) - 1)
    if isinstance(conn.target, ChipPortEndpoint) and conn.target.port.endswith("_out"):
        distance += 1
    return distance


def _output_cross_chip_extra(conn, project, chip_type) -> int:
    """Extra exit hops when a block's OUTPUT port is INTER-CHIP-WIRED to a
    downstream chip whose OWN output continues the chain (2P2S). The word must
    transit the far chip's bus to the CHAIN TAIL, not stop at this chip's x16_out.

    The output mirror of the composite input hop: for each inter-chip wire this
    chip's output port feeds, add (boundary crossing + the far chip's x16_in ->
    x16_out bus width). Walks the chain forward (2P2S depth <= 2; general linear
    chain supported). Chains are homogeneous (all kyttar_10x12), so ``chip_type``
    gives the far chip's bus width too. Returns 0 for a single-chip design or a
    chain-tail chip (no outgoing inter-chip wire from this output port)."""
    from model.connection import ChipPortEndpoint
    if not (isinstance(conn.target, ChipPortEndpoint)
            and conn.target.port.endswith("_out")):
        return 0
    ics = getattr(project, "inter_chip_connections", []) or []
    ip = next((p for p in chip_type.ports if p.direction.value == "input"), None)
    op = next((p for p in chip_type.ports
               if p.direction.value == "output" and p.name.endswith("_out")), None)
    if ip is None or op is None:
        return 0
    bus = abs(op.cell_x - ip.cell_x) + abs(op.cell_y - ip.cell_y) + 1
    extra = 0
    cur_chip, cur_port = conn.target.chip, conn.target.port
    guard = set()
    while cur_chip not in guard:
        guard.add(cur_chip)
        wire = next((ic for ic in ics
                     if ic.from_chip == cur_chip and ic.from_port == cur_port), None)
        if wire is None:
            break
        extra += 1 + bus   # +1 boundary crossing + far chip's bus width
        cur_chip, cur_port = wire.to_chip, op.name
    return extra


# fwd_face int codes (cell_map.Face): S=0, E=1, W=2, N=3.
_FACE_S, _FACE_E, _FACE_W, _FACE_N = 0, 1, 2, 3
_PORT_FACE_CODE = {"south": _FACE_S, "east": _FACE_E,
                   "west": _FACE_W, "north": _FACE_N}


def _CM_FACE(code: int):
    """Map an int face code (S=0,E=1,W=2,N=3) to the cell_map Face enum, so a
    routing cell's ``fwd_face`` is a real Face (has ``.name`` for trace/export)."""
    from gr_kyttar.placement.cell_map import Face as _CMFace
    return {0: _CMFace.SOUTH, 1: _CMFace.EAST,
            2: _CMFace.WEST, 3: _CMFace.NORTH}[int(code)]


def _step_face(x0, y0, x1, y1):
    """fwd_face int from (x0,y0) toward an adjacent (x1,y1), or None."""
    if x1 > x0:
        return _FACE_E
    if x1 < x0:
        return _FACE_W
    if y1 > y0:
        return _FACE_S
    if y1 < y0:
        return _FACE_N
    return None


def _apply_routes(cell_map, gr_placement, blocks, connections, chip_type,
                  gr_blocks, catalog, project) -> None:
    """Face cells STRICTLY from the user's drawn route waypoints (§2.6).

    placeKYT owns routing — the Router fabricates nothing (it is called with
    ``skip_io_routing=True``). For EVERY routed connection on this chip
    (port→block, block→block, block→port), regardless of direction:

      * each route waypoint cell's ``fwd_face`` points to the NEXT waypoint;
      * the FINAL waypoint faces toward the target — a chip-output port's exit
        face when the target is an output port, else toward the target block's
        entry cell;
      * the source block's exit WRITE/JUMP hop is set to the route length so the
        data reaches the target — UNLESS the block authors its own hops
        (``RAW_OUTPUT_HOPS``: an SRAM controller / crossover emits literal @N).

    A connection with no route (e.g. an input-port→block entry, which is just a
    logical entry point) contributes no faces.
    """
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from .bus_router import _phys_pts, abutment_pts

    placed = {b.name for b in blocks}
    ports = {p.name: (p.cell_x, p.cell_y, _PORT_FACE_CODE.get(p.face.value))
             for p in chip_type.ports}

    def _entry_cell_of(block_name):
        pb = gr_placement.placed_blocks.get(block_name)
        return pb.entry_cell if pb is not None else None

    # Complex-pair abutment from a carries-handoffs output cell is patched ONCE
    # per source cell (both rail nets reach the branch; the tail patch steers
    # BOTH rails) — track the cells already done.
    _complex_abut_tail_patched: set = set()

    for conn in connections:
        src, tgt = conn.source, conn.target
        # PHYSICAL path. A routed connection: the broker/face/hop geometry from its
        # drawn waypoints (a block→block route ending ON the target input cell is
        # stripped to the abutting broker). An UNROUTED connection: a direct
        # ABUTMENT — synthesise [src_out_cell, tgt_in_cell] when the source's output
        # cell is adjacent to the target — so a packed layout works without a filler
        # routing cell. Anything else (unrouted, non-adjacent) is skipped.
        if conn.is_abutment:
            # Explicit ABUTMENT route (adjacent I/O cells, no corridor): synthesise
            # the [src_out_cell, tgt_in_cell] @1 handoff. (An ABUTMENT net is now
            # is_routed=True, so it must be dispatched here BEFORE _phys_pts, which
            # would iterate the sentinel string.)
            pts = abutment_pts(project, conn, catalog, ports)
        elif conn.is_routed:
            pts = _phys_pts(project, conn, catalog)
        else:
            # Legacy fallback: an UNROUTED net whose cells happen to abut (pre-sentinel
            # behaviour). Kept so hand-built layouts without an explicit ABUTMENT route
            # still work.
            pts = abutment_pts(project, conn, catalog, ports)
        if not pts:
            continue
        # The face the FINAL waypoint should take toward the target.
        final_face = None
        if isinstance(tgt, ChipPortEndpoint):
            port = ports.get(tgt.port)
            if port is not None:
                final_face = port[2]            # exit via the port's face
        elif isinstance(tgt, BlockEndpoint):
            # The target CELL: the block's entry cell by default — but when the
            # net names a port that lives on a DIFFERENT cell, face toward THAT
            # cell. The SRAM-panel return net is the canonical case: it targets
            # the block's panel-return input (e.g. Varicode 'word' on the emit
            # cell) while the entry/landing cell is the controller at the panel
            # port on the far side of the chip — facing toward the entry cell
            # left the final corridor cell unfaced (default EAST) and the panel
            # push sailed past the consumer.
            ec = None
            gb_t = gr_blocks.get(tgt.block)
            req = None
            if gb_t is not None:
                try:
                    req = gb_t.panel_requirements()
                except Exception:  # noqa: BLE001
                    req = None
            if req and tgt.port == req.get("return_port"):
                mb = next((b for b in blocks if b.name == tgt.block), None)
                if mb is not None and mb.placement is not None:
                    pc = next((c for c in mb.placement.cells
                               if c.cell_id == req.get("return_cell")), None)
                    if pc is not None:
                        ec = (pc.x, pc.y)
            if ec is None:
                ec = _entry_cell_of(tgt.block)
            if ec is not None and pts:
                final_face = _step_face(pts[-1][0], pts[-1][1], ec[0], ec[1])
        # Face each waypoint toward the next; final waypoint toward the target.
        # A waypoint on an EMPTY cell becomes a routing cell (faces only, no
        # program) — that's how the user's drawn path is realised in hardware.
        # A RAW-hop source (crossover/SRAM controller relays) AUTHORS its own
        # emit faces (MOVE [FACE] per track) and its static fwd_face is its
        # TRANSIT face — re-pinning route[0] toward the relay target would
        # misroute every word transiting the relay cell (the duplex-panel tap:
        # its abutment to the RX chain pinned it WEST and the TX ctl feed
        # words riding THROUGH it turned west). Skip route[0] for RAW sources.
        _skip0 = False
        if isinstance(src, BlockEndpoint) and src.block in placed:
            _gb0 = gr_blocks.get(src.block)
            if _gb0 is not None and getattr(_gb0, "RAW_OUTPUT_HOPS", False):
                _pb0 = gr_placement.placed_blocks.get(src.block)
                _skip0 = (_pb0 is not None and pts
                          and tuple(pts[0]) == tuple(_pb0.exit_cell))
        for i, (x, y) in enumerate(pts):
            if i == 0 and _skip0:
                continue
            face = (_step_face(x, y, *pts[i + 1]) if i + 1 < len(pts)
                    else final_face)
            if face is None:
                continue
            cfg = cell_map.get_cell(x, y)
            if cfg is None:
                cell_map.add_routing_cell(x, y, _CM_FACE(face))
            else:
                cfg.fwd_face = _CM_FACE(face)
        # Source block exit hop = route length, UNLESS it authors its own hops.
        if isinstance(src, BlockEndpoint) and src.block in placed:
            gb = gr_blocks.get(src.block)
            if gb is not None and getattr(gb, "RAW_OUTPUT_HOPS", False):
                continue
            pb = gr_placement.placed_blocks.get(src.block)
            if pb is None:
                continue
            # PER-NET exit cell: every router anchors an egress route at the
            # NET's own output cell (route[0] == that net's source-port cell).
            # A MULTI-OUTPUT block (two physically-separate complex output
            # cells — the R2Butterfly sum/diff pairs) routes DIFFERENT nets
            # from DIFFERENT cells; patching the block-level ``pb.exit_cell``
            # for every net would land BOTH nets' handoffs on ONE cell and
            # clobber its other output (proven: the butterfly's second net
            # rewrote the sum tap's WRITE/JUMP with the dump route's hops —
            # zero egress). For a single-output block ``route[0] ==
            # pb.exit_cell`` and this is a no-op.
            ex, ey = _net_source_exit_cell(pb, pts, blocks, src.block)
            cfg = cell_map.get_cell(ex, ey)
            if cfg is None:
                continue
            # Face the net's exit cell toward the first route waypoint
            # (unless the first waypoint IS the exit cell, then toward the 2nd).
            nxt = None
            if pts and pts[0] != (ex, ey):
                nxt = pts[0]
            elif len(pts) > 1:
                nxt = pts[1]
            if nxt is not None:
                f = _step_face(ex, ey, *nxt)
                if f is not None:
                    cfg.fwd_face = f
                    # DUAL-FACE output cell: its `out` WRITE fires on an in-program
                    # MOVE [FACE], R{face_out} flip, NOT on fwd_face (which carries
                    # its feedback). Rewrite that face word to the route's first-hop
                    # direction so `out` follows the route — else it fires on the
                    # baked-in/rotated face_out and shoots into empty cells (the
                    # stray-exec "phantom route" hazard). See output_face_addr().
                    # Applies ONLY to the block-level output cell — the face word
                    # the block declares lives THERE, not on a sibling output cell.
                    ofa = gb.output_face_addr() if gb is not None else None
                    if ofa is not None and (ex, ey) == tuple(pb.exit_cell):
                        cfg.set_memory(int(ofa), int(f))
            # Resolve the handoff target: a block target → its entry/input reg
            # (so the WRITE lands in the next block's input and the JUMP triggers
            # its entry); a chip-output-port target → entry 0 and dest = the
            # connection's output TAG (default 0), so chains that share one output
            # port stay distinguishable on the wire (the captured OutWord.tag).
            dest = entry = 0
            # rail_idx / n_target_ins: when the target is a COMPLEX block (xi/xq =
            # two input regs) fed by an abutted complex PACKET (yi+yq from one source
            # cell over two nets), each rail must land in ITS OWN register — yi→xi(reg0),
            # yq→xq(reg1). Resolve the target register from THIS connection's target
            # PORT (not always t_ins[0]) and patch only the matching source WRITE.
            rail_idx = 0
            n_target_ins = 1
            if isinstance(tgt, BlockEndpoint):
                tb = next((b for b in blocks if b.name == tgt.block), None)
                if tb is not None:
                    t_entry, t_ins = catalog.resolved_io(
                        tb.type, tb.params, library=tb.library)
                    # Per-port entry (multi-entry rendezvous target): the JUMP must
                    # trigger THIS port's entry (dual.q → got_q), not the default.
                    entry = _target_port_entry(catalog, tb, tgt.port, t_entry)
                    n_target_ins = len(t_ins) if t_ins else 1
                    rail_idx = _target_port_index(catalog, tb, tgt.port)
                    dest = (t_ins[rail_idx]
                            if t_ins and rail_idx < len(t_ins)
                            else (t_ins[0] if t_ins else 0))
                # Per-net JUMP-entry override: a multi-entry relay target (the
                # CrossoverBlock) needs each net to pick ITS track — the panel
                # template's egress enters on track_b while the input corridor
                # lands on the default track_a.
                if getattr(conn, "entry_override", None) is not None:
                    entry = int(conn.entry_override)
            elif conn.out_tag is not None:   # chip-output-port target with a tag
                dest = conn.out_tag
            # Is the SOURCE a complex-output cell (yi+yq, two output rails)? Its exit
            # cell emits ≥2 output WRITEs. When it drives the OUTPUT PORT, the two rails
            # must exit with DISTINCT tags (yi→out_tag, yq→out_tag+1) — see below.
            # NOTE: resolved_io returns (entry, INPUT regs); OUTPUT-reg count comes from
            # the block SPEC's output_registers.
            src_is_complex_out = False
            _n_out_regs = 1
            try:
                _sb = next(b for b in blocks if b.name == src.block)
                # Resolve the output-register count WITH the instance's params (INV-6/11):
                # an ORDER-DEPENDENT interface (the order-4 Costas taps a complex pair,
                # order-2 a single rail) is complex ONLY at order 4, and the bare-type
                # spec would mis-read it as order-2 single-rail. Prefer the placed block's
                # gr instance; fall back to the param-blind spec.
                _gbx = gr_blocks.get(src.block)
                if _gbx is not None:
                    _n_out_regs = len(_gbx.interface.output_registers)
                else:
                    _spec = catalog.get(_sb.type, library=_sb.library)
                    _n_out_regs = len(_spec.output_registers) if _spec else 1
                src_is_complex_out = _n_out_regs > 1
            except Exception:  # noqa: BLE001
                pass
            # If the source block declares a MID-block output cell (its output
            # leaves a non-last cell that ALSO carries internal handoffs — e.g. the
            # Costas rotate cell writes yi→pd_pi AND yi_tap→the port), patch ONLY
            # the output WRITE (the LAST WRITE in the cell — the block emits the
            # tap after its internal writes). Patching every WRITE would clobber the
            # internal handoffs (yi/yq → pd_pi) and break the loop.
            gb = gr_blocks.get(src.block)
            # Source-exit hop from the PHYSICAL path (stripped of an on-the-cell target
            # waypoint), so a route drawn onto the target cell still addresses the
            # abutting broker — NOT one cell past it.
            # INVARIANT: the route's first waypoint IS the source's exit cell — every
            # router (cpsat `_reconstruct`, maze `_astar`, and the bus router, which now
            # PREPENDS the exit cell for an egress whose output cell sits off the backbone
            # slice) emits `route[0] == exit_cell`. So `len(pts)-1 (+1 for a chip output)`
            # is the true exit→port hop count with no per-case correction. (If a route ever
            # started one cell downstream of the exit, the egress WRITE would reach the port
            # EDGE cell at hop_cnt 31 and execute there instead of transiting out — 0 egress,
            # the FM-transceiver symptom that traced to the bus router omitting the exit cell.)
            phys_dist = _phys_distance(conn, pts)
            # RELAY-LANDING target (the CrossoverBlock): the word must LAND ON
            # the relay cell itself — its track entry runs there — so undo the
            # strip-to-abutting-broker hop (the stripped form delivered into a
            # broker whose relay resolved wrong registers in the duplex build).
            if isinstance(tgt, BlockEndpoint):
                _tgb = gr_blocks.get(tgt.block)
                if _tgb is not None and getattr(_tgb, "RELAY_LANDING", False):
                    phys_dist += 1
            # CROSS-CHIP OUTPUT (2P2S): if this output port is inter-chip-wired to a
            # downstream chip, the exit word must transit the far chip's bus to the
            # CHAIN TAIL — add the crossing + far bus width so the WRITE/JUMP hop
            # carries it across (the output mirror of the composite input hop). No-op
            # for single-chip / chain-tail outputs.
            phys_dist += _output_cross_chip_extra(conn, project, chip_type)
            _is_complex_port_egress = (
                not isinstance(tgt, BlockEndpoint) and src_is_complex_out
                and _cell_write_count(cfg) > 1)
            if _output_cell_carries_handoffs(gb) and not _is_complex_port_egress:
                if (src_is_complex_out and isinstance(tgt, BlockEndpoint)
                        and n_target_ins > 1 and _cell_write_count(cfg) > 1):
                    # COMPLEX pair abutted into a ≥2-register target from a
                    # carries-handoffs output cell (the serialize-LOCKED NCO/FM
                    # emit — its lock-clear WRITE.CFG trips the flag). The
                    # single last-write patch below LAST-WINS both rail nets
                    # into ONE data WRITE (both rails → target R0; the consumer
                    # computed (yq·g, 0) — the auto_pnr-abutted locked-FM bug,
                    # 2026-08-16). Patch ONCE: last N data WRITEs → the
                    # target's own input regs, last JUMP → its entry; the
                    # WRITE.CFG keeps its resolved unlock hop.
                    if (ex, ey) not in _complex_abut_tail_patched:
                        _complex_abut_tail_patched.add((ex, ey))
                        try:
                            dests = [int(r) for r in t_ins][:_n_out_regs]
                        except Exception:  # noqa: BLE001
                            dests = [dest]
                        if len(dests) < 2:
                            dests = [dest]
                        _patch_complex_abutment_tail_handoff(
                            cfg, phys_dist, dests, entry=entry)
                else:
                    _patch_last_write_handoff(cfg, phys_dist, dest=dest)
                    _patch_last_jump_handoff(cfg, phys_dist, entry=entry)
            elif _is_complex_port_egress:
                # COMPLEX EGRESS to the OUTPUT PORT: the source emits yi+yq (≥2 output
                # WRITEs) straight to x16_out. This takes PRECEDENCE over the
                # output-cell-carries-handoffs path even when the source declares an
                # output_cell_id (e.g. the FrequencyModulator's serialize-LOCK emit
                # cell): that path patches ONE last WRITE with ONE tag, which would give
                # the yi rail the port hop but leave yq (and the emit hop) wrong, so the
                # I/Q never egresses (0 output). The complex handoff below patches BOTH
                # rails' WRITEs with the port hop + distinct tags (and skips the lock's
                # WRITE.CFG via _patch_complex_output_port_handoff's write walk).
                # Give each rail its OWN dest tag
                # (yi→base_tag, yq→base_tag+1) so the port demux keeps I and Q as
                # SEPARATE captured streams (mirrors complex INPUT xi→a0, xq→a1), and
                # the waveform plots two clean traces instead of one interleaved band.
                #
                # The source drives the port on TWO nets (yi→tag N, yq→tag N+1). Both
                # nets reach this branch, but ONE call patches BOTH rails — so patch
                # once, from the LOWER (I-rail) tag as the base, and skip the sibling
                # net (else the second call remaps yi→N+1, yq→N+2 and corrupts it).
                # MULTI-OUTPUT source: group the sibling scan by the net's EXIT
                # CELL, not the whole block — a block with TWO output pairs
                # (R2Butterfly sum/diff) drives the port on nets from TWO cells,
                # and a block-wide min would (a) mis-base the second pair's tags
                # and (b) skip its patch entirely (dest != block-wide base).
                base_tag = dest
                for _oc in connections:
                    _os, _ot = _oc.source, _oc.target
                    if (isinstance(_os, BlockEndpoint) and _os.block == src.block
                            and not isinstance(_ot, BlockEndpoint)
                            and _oc.out_tag is not None):
                        _oc_pts = ([(p.x, p.y) for p in _oc.route]
                                   if getattr(_oc, "route", None) else [])
                        if _net_source_exit_cell(
                                pb, _oc_pts, blocks, src.block) != (ex, ey):
                            continue
                        base_tag = min(base_tag, _oc.out_tag)
                if dest == base_tag:      # this is the I-rail net → patch both rails
                    if _output_cell_carries_handoffs(gb):
                        # FUSED output+handoff cell (the order-4 Costas ``qpd``: an
                        # internal err→pd_pi WRITE FIRST, then the recovered yi_tap/yq_tap
                        # tail). Patching EVERY WRITE (the plain complex-egress path) would
                        # egress the internal ``err`` too and break the loop; patch only
                        # the last N tail WRITEs (the recovered rails) with distinct tags,
                        # leaving the err WRITE on its @1 hop.
                        _patch_last_n_write_handoff(cfg, phys_dist, _n_out_regs,
                                                    base_tag=base_tag)
                        _patch_last_jump_handoff(cfg, phys_dist, entry=0)
                    else:
                        _patch_complex_output_port_handoff(cfg, phys_dist, base_tag,
                                                           entry=0)
                # else: the sibling (Q) net — already handled by the I-rail patch.
            elif n_target_ins > 1 and _cell_write_count(cfg) > 1:
                # COMPLEX PACKET over abutment: the source cell emits ≥2 output
                # WRITEs (yi, yq) to a ≥2-register target. Steer THIS rail's WRITE
                # (the rail_idx-th WRITE) to its own target register; set the hop on
                # all WRITEs/JUMP (abutted → @phys_dist) but the DEST only on the
                # matching WRITE, so the two rails don't clobber each other in R0.
                _patch_complex_abutment_handoff(cfg, phys_dist, rail_idx, dest,
                                                entry=entry)
            else:
                _patch_cell_handoff(cfg, phys_dist, dest=dest,
                                    entry=entry)


def _broker_program(deliveries, bus_face: int):
    """Assemble the BROKER cell program (the §1.2 flip→relay→restore primitive).

    This is the proven ``SplitterBlock`` pattern (``kyttar_block.py:11617``: per-
    entry ``MOVE [FACE], <dir>`` then relay the burst, WRITE+JUMP onward) PLUS the
    slicer's self-restore (flip the face back to the bus direction after relaying).
    Both halves are validated on-chip — we EMIT this pattern parameterized by the
    router's broker assignment, we do NOT invent a new broker.

    ``deliveries`` is the list of :class:`~engine.bus_router.BrokerDelivery` this
    broker performs — ONE per net tapping it. Usually one; a FAN-IN (two streams into
    one input cell, e.g. the Costas phase cell's xi + xq) gives TWO deliveries, each
    its OWN entry (§1.2: two streams to one cell ⇒ two entries on one broker). Per
    incoming burst that LANDS here (HOP_CNT==31 at the broker), the entry the JUMP
    selected runs:
      1. flip the broker's output FACE toward the target's input cell,
      2. relay the burst value (R0) into the block: ``WRITE @1, in_reg``,
      3. trigger the block: ``JUMP @1, in_entry``,
      4. restore the FACE to the bus (through-spine) direction so a LATER transiting
         word continues down the bus.
    A farther-bound word arrives with HOP_CNT<31, so it never enters any entry — the
    broker simply forwards it on ``bus_face`` (its fwd_face), untouched (§1.2).

    Returns ``(entry_addr_by_conn, {addr: word})``: a map from each delivery's
    connection name → its resolved entry address, plus the assembled memory. The
    router used the same resolver, so source and broker agree.
    """
    from gr_kyttar.placement.block import (CellProgram, DataWord, EntryPoint,
                                             Port)
    from gr_kyttar.placement.resolver import CellProgramResolver

    # COALESCE deliveries into GROUPS by (src_cell, in_cell): two nets from the SAME
    # source cell into the SAME target cell are a COMPLEX SAMPLE (e.g. the MF i4's
    # yi+yq into the Costas phase cell) and MUST be relayed as one multi-WRITE +
    # single-JUMP burst — the input-port complex-sample contract — so the target
    # fires ONCE per sample with BOTH operands fresh. Relaying them as two
    # independent WRITE+JUMP deliveries would fire the target TWICE per sample
    # (once per operand, the other stale). A group of one is the ordinary single
    # delivery (unchanged behaviour). Order is preserved (the first net's operand is
    # the first WRITE, matching the source's program order).
    groups: list[list] = []
    index: dict = {}
    for dv in deliveries:
        key = (dv.src_cell, dv.in_cell) if dv.src_cell is not None else (id(dv),)
        if key in index:
            groups[index[key]].append(dv)
        else:
            index[key] = len(groups)
            groups.append([dv])

    # EVERY operand across ALL deliveries lands in its OWN register, allocated GLOBALLY
    # starting at R1 — NEVER R0 (R0 is the accumulator / WRITE source; the relay
    # ``MOVE R0, R<op>`` and the auto-landing both clobber it, so two deliveries sharing
    # R0 wipe each other → the broker relays 0). The per-DELIVERY operand index used to
    # restart at 0 each group, so two independent deliveries both used R0 (the dead
    # compact-modem mapper→upsampler chain). We assign each operand a unique register
    # ``op_reg = 1 + running_index`` and the source's WRITE addresses THAT register. Face
    # data words (bus_face + per-group deliver faces) pack ABOVE the highest operand reg.
    total_operands = sum(len(g) for g in groups)
    data_base = 1 + total_operands             # bus_face + faces start past the operand regs
    data = [DataWord("bus_face", int(bus_face) & 0x3, address=data_base)]
    # One input Port per operand at its global register (R1, R2, ...). The template refers
    # to them by a stable name ``op<gi>_<oi>``; ``burst_reg_by_conn`` records the absolute
    # register so the source patch (``_apply_brokers``) WRITEs to the matching one.
    burst_ports = []
    entries = []
    tmpl_parts = []
    by_conn: dict = {}
    burst_reg_by_conn: dict = {}
    g_face_addr = data_base + 1
    op_index = 0                               # running global operand index (→ reg 1+)
    for gi, group in enumerate(groups):
        label = f"deliver{gi}"
        fname = f"face{gi}"
        # All deliveries in a group share one deliver_face (same target cell).
        data.append(DataWord(fname, int(group[0].deliver_face) & 0x3,
                             address=g_face_addr))
        g_face_addr += 1
        entries.append(EntryPoint(label))
        lines = [f"{label}:", f"    MOVE [FACE], R{{data:{fname}}}"]
        # Relay each operand: WRITE always sends R0, so MOVE the landed operand (in its
        # own R>=1) into R0 first, then WRITE. Distinct global regs keep deliveries apart.
        for dv in group:
            op_reg = 1 + op_index
            op_index += 1
            pname = f"op{gi}_{op_reg}"
            burst_ports.append(Port(pname, register=op_reg))
            lines.append(f"    MOVE R0, R{{in:{pname}}}")
            lines.append(f"    WRITE @1, {int(dv.in_reg)}")
            burst_reg_by_conn[dv.conn] = op_reg   # ABSOLUTE reg the source WRITEs to
        # ONE trigger after ALL operands (the complex-sample contract). Every
        # delivery in a group targets the same cell/entry, so use the first.
        lines.append(f"    JUMP @1, {int(group[0].in_entry)}")
        lines.append("    MOVE [FACE], R{data:bus_face}")
        lines.append("    HALT")
        tmpl_parts.append("\n".join(lines) + "\n")
        for dv in group:
            by_conn[dv.conn] = label             # resolved to an addr below
    prog = CellProgram(
        inputs=burst_ports,
        outputs=[Port("out")],
        entries=entries,
        data=data,
        state=[],
        assembly_template="".join(tmpl_parts),
    )
    resolver = CellProgramResolver()
    resolved = resolver.resolve(prog)
    entry_addrs = resolver.compute_entry_addresses(prog)
    by_conn = {conn: entry_addrs[label]
               for conn, label in by_conn.items() if label in entry_addrs}
    return by_conn, dict(resolved.memory), burst_reg_by_conn


def _apply_brokers(cell_map, gr_placement, blocks, connections, project,
                   chip_id, chip_type, gr_blocks, catalog) -> None:
    """Emit broker programs + re-point sources at them for bus-routed nets (§1.2).

    For each BROKER tap derived from the routed project (:func:`bus_router.broker_plan`
    — a route ending at a free routing cell abutting a target block):
      * program the broker cell (flip→relay→restore via :func:`_broker_program`),
        leaving its ``fwd_face`` = the bus direction so transiting words continue;
      * re-point the SOURCE block's exit WRITE to ``dest=BROKER_BURST_REG`` (R0) and
        its JUMP to ``entry=<broker deliver entry>``, with hop = the route distance
        to the broker, so the source lands the burst AT the broker (whose program
        then relays it @1 into the target). This OVERRIDES the target-addressed
        source patch ``_apply_routes`` applied — for a brokered net the source must
        address the broker, not the block.

    Plain (non-bus) routes — those ending ON the target's own cell — produce no tap,
    so this pass is a no-op for them (the legacy corridor/abutment build is intact).
    """
    from model.connection import BlockEndpoint
    from gr_kyttar.placement.cell_map import CellConfig
    from .bus_router import (BROKER_BURST_REG, broker_plan, broker_through_face,
                             _phys_pts)

    taps = broker_plan(project, chip_id, chip_type, catalog)
    # NOTE: no early return on empty ``taps`` — a fully-ABUTTED design (the
    # abutment-first packs) has NO brokers at all, but the abutted fan-out /
    # replicated-WRITE machinery below must still run: bailing here left a
    # single-rail source feeding BOTH inputs of an abutted join with ONE
    # last-wins WRITE (a0 never written — the fanin2 skewed-pair values).
    # Cells where a FOREIGN net merely TRANSITS this broker (the auto-router packed
    # two corridors onto one broker cell). The broker's restore/bus face MUST serve
    # that foreign through-direction or the foreign stream is silently mis-faced and
    # dies — the modem's MF→Costas (net4) transiting the Upsampler→RRC broker at
    # (2,3), and the Slicer→x16_out egress (net7) transiting the Costas→Gardner broker
    # at (6,9). This OVERRIDES the route-derived face below.
    through_face = broker_through_face(project, chip_id, chip_type, catalog)

    placed = {b.name for b in blocks}

    # Cells that are a block's FEEDBACK transit cell: a broker landing here must
    # RESTORE to the transit's authored (feedback) face — NOT to a through-route
    # face — so the transiting feedback word continues down the feedback lane. For
    # these, the tap's bus_face (derived from the transit's authored face by
    # ``broker_plan``) is authoritative and overrides any route face below.
    feedback_transit_cells: set = set()
    for blk in blocks:
        pl = blk.placement
        if pl is None or pl.chip != chip_id:
            continue
        for t in getattr(pl, "transit_cells", []):
            feedback_transit_cells.add((t.x, t.y))

    # 1. Program each broker cell. A broker is a routing cell that now CARRIES a
    #    program (entry(ies) + memory), distinct from a plain transit cell (face
    #    only). A FAN-IN broker carries one deliver entry per net tapping it.
    conn_entry: dict = {}      # conn name -> its deliver entry address at the broker
    conn_burst_reg: dict = {}  # conn name -> the broker burst reg its source WRITEs to
    for (bx, by), tap in taps.items():
        cfg = cell_map.get_cell(bx, by)
        # The broker's RESTORE / bus face MUST be the through-bus direction so a
        # transiting (HOP<31) word continues correctly. If a THROUGH route already
        # faced this cell (``_apply_routes`` ran first, e.g. the output-egress net
        # passing through), that face IS the bus direction — use it, so the broker
        # restores to it and never breaks the shared stream. Else fall back to the
        # tap's own into-broker direction (a dead-end broker with no through-traffic).
        bus_face = tap.bus_face
        if (bx, by) not in feedback_transit_cells \
                and cfg is not None and getattr(cfg, "fwd_face", None) is not None:
            bus_face = int(cfg.fwd_face)
        # A FOREIGN net transiting this broker pins the restore face to its forwarding
        # direction (it overrides the route face, which serves only this broker's own
        # delivery, not the through-traffic). Without this the foreign stream dies on
        # the broker's static fwd_face (the §1.3 single-fwd_face corruption).
        if (bx, by) in through_face:
            bus_face = int(through_face[(bx, by)])
        by_conn, memory, burst_reg_by_conn = _broker_program(tap.deliveries, bus_face)
        if cfg is None:
            cfg = CellConfig(block_name="_broker")
            cell_map.set_cell(bx, by, cfg)
        cfg.memory.update(memory)
        # entry_addr = the FIRST delivery's entry (the cell's default entry); each
        # source addresses its own delivery's entry via ``conn_entry``.
        cfg.entry_addr = min(by_conn.values()) if by_conn else cfg.entry_addr
        cfg.fwd_face = _CM_FACE(int(bus_face))
        if not getattr(cfg, "block_name", ""):
            cfg.block_name = "_broker"
        conn_entry.update(by_conn)
        conn_burst_reg.update(burst_reg_by_conn)

    # 2. Re-point each brokered net's SOURCE exit at its broker's deliver entry.
    #    GROUP the brokered nets by their source exit cell: a COMPLEX-SAMPLE source
    #    (the MF i4 emitting yi+yq) has TWO nets through one broker entry — its two
    #    WRITEs must address DISTINCT broker burst regs (R0, R1) and it fires ONE
    #    JUMP. A plain single-net source patches its one WRITE+JUMP as before.
    broker_cells = set(taps.keys())
    by_src_cell: dict = {}     # (x,y) exit cell -> list of (conn, distance, b_entry)
    src_meta: dict = {}        # (x,y) exit cell -> (gb, cfg)
    # ABUTTED output rails per source exit cell: a COMPLEX output cell can fan out with
    # ONE rail routed (through a broker) and the OTHER abutting a different target
    # (mixer.yi ROUTED to gain_I, mixer.yq ABUTMENT to gain_Q). The abutted rail is NOT
    # in by_src_cell (no broker), so without this the fan-out is misclassified as a
    # single-net source and the abutted rail's trigger is dropped. Collect abutted output
    # nets here so the fan-out re-sequencing (below) can steer BOTH rails.
    abut_by_src_cell: dict = {}   # (x,y) exit cell -> list of (conn, dest_reg, entry)
    fanout_abut_conns: set = set()  # abutted conns consumed into a mixed fan-out here
    for conn in connections:
        if not getattr(conn, "is_abutment", False):
            continue
        if not isinstance(conn.source, BlockEndpoint) or conn.source.block not in placed:
            continue
        pb = gr_placement.placed_blocks.get(conn.source.block)
        gb = gr_blocks.get(conn.source.block)
        if pb is None or (gb is not None and getattr(gb, "RAW_OUTPUT_HOPS", False)):
            continue
        # Resolve the abutted rail's target register + entry (yq -> consumer.xq etc.).
        # ``a_tgt`` identifies the DESTINATION (block name / port) so the fan-out
        # classifier below can tell a genuine fan-out (rails to DIFFERENT targets)
        # from a plain complex pair (both rails abutting ONE consumer).
        a_dest = a_entry = 0
        a_tgt = None
        if isinstance(conn.target, BlockEndpoint):
            a_tgt = ("block", conn.target.block)
            tb = next((b for b in blocks if b.name == conn.target.block), None)
            if tb is not None:
                t_entry, t_ins = catalog.resolved_io(
                    tb.type, tb.params, library=tb.library)
                # Per-port entry (multi-entry rendezvous target, e.g. dual.q →
                # got_q), falling back to the block default for ordinary blocks.
                a_entry = _target_port_entry(catalog, tb, conn.target.port,
                                             t_entry)
                _ri = _target_port_index(catalog, tb, conn.target.port)
                a_dest = (t_ins[_ri] if t_ins and _ri < len(t_ins)
                          else (t_ins[0] if t_ins else 0))
        elif conn.out_tag is not None:
            a_dest = conn.out_tag
            a_tgt = ("port", getattr(conn.target, "port", None))
        abut_by_src_cell.setdefault(tuple(pb.exit_cell), []).append(
            (conn.name, a_dest, a_entry, a_tgt))
    for conn in connections:
        if not conn.is_routed:
            continue
        if not isinstance(conn.source, BlockEndpoint) or conn.source.block not in placed:
            continue
        # RELAY-LANDING target (CrossoverBlock): the source lands ON the relay
        # cell directly (see _apply_routes) — no broker delivery for this net.
        if isinstance(conn.target, BlockEndpoint):
            _tgb = gr_blocks.get(conn.target.block)
            if _tgb is not None and getattr(_tgb, "RELAY_LANDING", False):
                continue
        # PHYSICAL path: a route drawn ENDING ON the target input cell stops at the
        # abutting broker (the trailing input-cell waypoint is stripped), so the source
        # hop reaches the BROKER — not one cell past it, into the block.
        pts = _phys_pts(project, conn, catalog)
        if not pts or pts[-1] not in broker_cells:
            continue
        if conn.name not in conn_entry:
            continue
        gb = gr_blocks.get(conn.source.block)
        if gb is not None and getattr(gb, "RAW_OUTPUT_HOPS", False):
            continue
        pb = gr_placement.placed_blocks.get(conn.source.block)
        if pb is None:
            continue
        cfg = cell_map.get_cell(*pb.exit_cell)
        if cfg is None:
            continue
        # Distance from the source exit cell to the broker. ``pts`` is the physical
        # route. Two route conventions reach this point and the hop count differs:
        #   * LEGACY/drawn route — ``pts[0]`` IS the source's own exit cell, so the
        #     word's hops to the broker = ``len(pts) - 1`` (waypoints after the exit).
        #   * BUS-v2 backbone route — the source block sits BESIDE the bus and taps in,
        #     so ``pts`` is the BUS SEGMENT and ``pts[0]`` is the TAP cell one hop
        #     DOWNSTREAM of the exit (the exit is NOT in the route). The word still must
        #     hop exit→tap, so the true distance is ``len(pts)`` (that leading hop plus
        #     the in-route hops). Undercounting it by one made the source WRITE/JUMP land
        #     ONE cell short of the broker (the auto-P&R modem's 0-output TX bug).
        # Normalise by prepending the exit cell when it isn't already the route head, so
        # the count is uniformly ``len(full)-1`` for both conventions.
        ex = tuple(pb.exit_cell)
        full = pts if pts[0] == ex else [ex] + list(pts)
        distance = max(0, len(full) - 1)
        by_src_cell.setdefault(ex, []).append(
            (conn.name, distance, conn_entry[conn.name], tuple(pts[-1])))
        src_meta[ex] = (gb, cfg)

    # source cells whose ONLY output nets are ALL abutment (no routed rail reaches the
    # loop below) — handle their mixed/pure-abutment fan-out here too. Union the routed
    # exit cells with the abutted ones.
    _conn_order = {c.name: i for i, c in enumerate(connections)}
    for ex in set(by_src_cell) | set(abut_by_src_cell):
        nets = by_src_cell.get(ex, [])
        abut_rails = abut_by_src_cell.get(ex, [])
        # MIXED FAN-OUT: this complex output cell drives ≥2 DIFFERENT targets where at
        # least one rail ABUTS and at least one ROUTES (mixer.yi→broker→gain_I +
        # mixer.yq→abut→gain_Q). Re-sequence ALL rails (a trigger each) in SOURCE PROGRAM
        # ORDER: a routed rail delivers to its broker burst reg @broker-distance/entry; an
        # abutted rail delivers to its target's OWN input reg @1/target-entry.
        # A FAN-OUT means rails leaving this cell for ≥2 DIFFERENT destinations
        # (at least one routed + one abutted, or two abutted targets). BOTH rails
        # of a plain complex pair abutting ONE consumer are NOT a fan-out — they
        # are the ordinary complex-packet handoff (WRITE re, WRITE im, ONE JUMP)
        # patched by the complex-abutment path; re-sequencing them here gave each
        # rail its own trigger AND double-encoded the @1 hop (encode(30)=1), so
        # the pair sailed 30 cells past its consumer and leaked out the port with
        # dests 0/1 — the channel_selector Conjugate-insertion regression.
        _abut_targets = {t for (_c, _d, _e, t) in abut_rails}
        _meta = src_meta.get(ex)
        _mcfg = _meta[1] if _meta is not None else cell_map.get_cell(*ex)
        _single_write = _mcfg is not None and _cell_write_count(_mcfg) == 1
        # A single-rail source with ≥2 abutted nets into ONE target (gain.out
        # abutting BOTH add inputs) is not a fan-out either, but it still needs
        # the replicated-WRITE treatment — the per-conn abutment patcher would
        # last-wins its single WRITE. Handle it here alongside the fan-outs.
        if abut_rails and (nets or len(_abut_targets) > 1
                           or (_single_write and len(abut_rails) > 1)):
            cfg = _mcfg
            if cfg is None:
                continue
            # (conn_name, hop, dest_reg, entry) per rail, ordered by connection index
            # so the Nth WRITE (yi, yq, …) gets the Nth target. Hops here are RAW
            # distances — _patch_fanout_source_handoff encodes them; an abutted rail
            # is @1 (passing the pre-encoded _HOP1_CNT double-encoded it).
            merged = []
            for (c, d, e, _bc) in nets:
                merged.append((c, d, BROKER_BURST_REG + int(conn_burst_reg.get(c, 0)), e))
            for (c, dest, entry, _t) in abut_rails:
                merged.append((c, 1, int(dest), int(entry)))
            merged.sort(key=lambda t: _conn_order.get(t[0], 1 << 30))
            if _single_write:
                writes = [(h, dr) for (_c, h, dr, _en) in merged]
                if len(_abut_targets) == 1 and not nets:
                    # one target, N inputs: packet form — a single trigger
                    jumps = [(1, merged[0][3])]
                else:
                    jumps = [(h, en) for (_c, h, _dr, en)
                             in sorted(merged, key=lambda t: t[1], reverse=True)]
                _patch_single_rail_multi_handoff(cfg, writes, jumps)
            else:
                _patch_fanout_source_handoff(
                    cfg, [(h, dr, en) for (_c, h, dr, en) in merged])
            # These abutted rails are now re-sequenced HERE (with their own trigger);
            # _default_unrouted_exit_hops must NOT re-patch them (it would clobber the
            # fan-out sequencing back to a single handoff).
            for (c, _dest, _entry, _t) in abut_rails:
                fanout_abut_conns.add(c)
            continue
        if not nets:
            continue
        gb, cfg = src_meta[ex]
        # A COMPLEX output cell (yi/yq from ONE cell) has TWO delivery SHAPES depending
        # on where the rails go (INV-17). The discriminator is the BROKER CELL each net
        # taps, NOT its entry ADDRESS (two DIFFERENT brokers can resolve their sole
        # delivery to the SAME entry addr, e.g. both 25 — so entry-equality would
        # misclassify a fan-out as a packet).
        broker_cells = {n[3] for n in nets}
        # COMPLEX PACKET: 2+ nets into ONE broker (coalesced to a single deliver entry).
        # Each operand WRITEs its own broker burst reg (R1, R2, … by source program
        # order) and ONE JUMP fires the target once with both rails fresh. This is the
        # mixer→Costas / complex→complex path — unchanged.
        if len(nets) > 1 and len(broker_cells) == 1:
            distance = nets[0][1]
            b_entry = nets[0][2]
            ordered = sorted(nets, key=lambda n: conn_burst_reg.get(n[0], 0))
            burst_regs = [BROKER_BURST_REG + conn_burst_reg.get(c, i)
                          for i, (c, _d, _e, _bc) in enumerate(ordered)]
            # SINGLE-rail source feeding N inputs of ONE target (gain.out →
            # add.a0 AND add.a1): replicate the one WRITE per burst reg, ONE
            # trigger — the packet form with a duplicated value.
            if _cell_write_count(cfg) == 1:
                _patch_single_rail_multi_handoff(
                    cfg, [(distance, r) for r in burst_regs],
                    [(distance, b_entry)])
                continue
            # If the complex output cell ALSO carries internal handoffs (the order-4
            # Costas qpd: err/trig→pd_pi internally AND yi_tap/yq_tap→the bus), patch
            # ONLY the TAIL (external) WRITEs + JUMP so the internal err/trig keep their
            # @1 hops — patching every WRITE/JUMP (the pure-output-cell path) would
            # clobber the loop (pd_pi never fires). A pure output cell (MF i4) has no
            # internal handoffs, so it takes the all-WRITEs path unchanged.
            if _output_cell_carries_handoffs(gb):
                _patch_complex_packet_last_handoff(cfg, distance, burst_regs, b_entry)
            else:
                _patch_complex_source_handoff(cfg, distance, burst_regs, b_entry)
            continue
        # FAN-OUT: 2+ rails from one complex output cell to DIFFERENT brokers (2 distinct
        # downstream blocks — the SSB Weaver's mixer.yi→LowPass_I, mixer.yq→LowPass_Q).
        # Each rail needs its OWN trigger, so re-sequence the cell's two WRITEs + the
        # (single, authored) JUMP into `WRITE yi; WRITE yq; JUMP→A; JUMP→B`, steering the
        # Nth rail's WRITE+JUMP to the Nth net's own (hop, burst_reg, entry). Nets are in
        # SOURCE PROGRAM ORDER (connections created in output-port order). Each net is its
        # broker's sole delivery, so its burst reg = BROKER_BURST_REG. The template is
        # UNCHANGED (INV-17): the packet form is the default; this runs only for the
        # 2-different-targets case. Block verification guarantees the extra JUMP fits.
        if len(nets) > 1 and len(broker_cells) > 1:
            specs = [(d, BROKER_BURST_REG + int(conn_burst_reg.get(c, 0)), e)
                     for (c, d, e, _bc) in nets]
            # SINGLE-rail source fanning out to N DIFFERENT targets: replicate
            # the one WRITE per arm + one JUMP per arm (descending hop — the
            # INV-17 FACE-transit rule the complex form uses).
            if _cell_write_count(cfg) == 1:
                by_hop = sorted(specs, key=lambda s: s[0], reverse=True)
                _patch_single_rail_multi_handoff(
                    cfg, [(h, r) for (h, r, _e) in specs],
                    [(h, e) for (h, _r, e) in by_hop])
            else:
                _patch_fanout_source_handoff(cfg, specs)
            continue
        # Single-net source (the ordinary one-operand delivery, unchanged).
        conn_name, distance, b_entry, _bc = nets[0]
        # The source must WRITE to the broker burst reg the broker's delivery for THIS
        # net READS — which is NOT always R0: when the broker also serves OTHER
        # deliveries (a fan-in / a shared tap cell), this net's operand is assigned burst
        # reg ``BROKER_BURST_REG + conn_burst_reg[conn]`` (oi in the broker's delivery
        # list), and the broker relays it via ``MOVE R0, R<that reg>``. Patching the
        # source to R0 unconditionally makes the broker read an EMPTY reg → relay 0 (the
        # compact-modem mapper→upsampler 0-output bug). Use this net's own burst reg.
        dest_reg = BROKER_BURST_REG + int(conn_burst_reg.get(conn_name, 0))
        # Is the SOURCE a COMPLEX 2-rail output cell (yi+yq, ≥2 output registers)?
        # Same discriminator the abutted branch uses (build.py ~951-955): the block
        # SPEC declares >1 output register. A single-rail cell that happens to emit
        # several WRITEs (e.g. IQUpconvert) is NOT complex — so gate on the register
        # count, NOT a bare _cell_write_count (that earlier over-broad guard broke the
        # BPSK modem TX).
        src_is_complex_out = False
        try:
            src_is_complex_out = len(gb.interface.output_registers) > 1
        except Exception:  # noqa: BLE001
            src_is_complex_out = False
        # If the source block declares a MID-block output cell (the Costas rotate
        # writes yi→pd_pi internally AND yi_tap→the bus), patch ONLY the output
        # WRITE (the last WRITE — emitted after the internal handoffs) so the
        # internal feedback WRITEs keep their @1 hops; else patch the cell's
        # exit WRITE + JUMP together.
        if _output_cell_carries_handoffs(gb):
            if src_is_complex_out:
                # COMPLEX source whose output cell ALSO carries handoffs (the
                # serialize-LOCKED NCO/FM emit, the order-4 Costas qpd) wired as a
                # SINGLE net: BOTH rails must still ride the corridor to
                # consecutive burst regs (position de-interleave downstream).
                # Patching only the LAST data write delivered yq alone — the
                # downstream read (yq, 0), the same rail-shift as the packet-path
                # WRITE.CFG bug. The packet-last patcher does the right tail-only
                # treatment (skipping the lock-clear WRITE.CFG and the internal
                # handoffs) for n = the block's output-register count.
                n = len(gb.interface.output_registers)
                _patch_complex_packet_last_handoff(
                    cfg, distance, [dest_reg + i for i in range(n)], b_entry)
            else:
                _patch_last_write_handoff(cfg, distance, dest=dest_reg)
                _patch_last_jump_handoff(cfg, distance, entry=b_entry)
        elif src_is_complex_out and _cell_write_count(cfg) > 1:
            # COMPLEX-PACKET output cell (yi/yq — ≥2 output WRITEs) delivered as a
            # SINGLE brokered net (only the I rail is wired; the Q rail rides the same
            # corridor and is de-interleaved by POSITION downstream). Plain
            # _patch_cell_handoff sets EVERY WRITE to the SAME dest_reg — collapsing yq
            # onto yi's register so the de-interleaved Q is garbage/zero. This is the
            # ORIENTATION-INVARIANCE bug: at identity the output ABUTS its consumer (the
            # abutted 2-rail patcher fires), but after a rotation the same output routes
            # via a BROKER and lands HERE — so the block's Q rail silently broke under
            # rotation. Steer the rails to CONSECUTIVE broker burst regs
            # (dest_reg, dest_reg+1, …) in program/emit order, exactly as the multi-net
            # complex-packet path does (_patch_complex_source_handoff).
            n = _cell_write_count(cfg)
            _patch_complex_source_handoff(
                cfg, distance, [dest_reg + i for i in range(n)], b_entry)
        else:
            _patch_cell_handoff(cfg, distance, dest=dest_reg,
                                entry=b_entry)

    # Hand the resolved broker entries + burst-reg map back so the build can resolve
    # each PORT→block input net's HOST-injection landing (engine.port_config reads it
    # via the BuildResult): a port net whose corridor is DIVERTED at a broker cell
    # (the modem's tx mapper net at (1,1), which the rx corridor pins EAST) must be
    # injected to LAND at that broker's deliver entry — not ridden straight through.
    return dict(conn_entry), dict(conn_burst_reg), fanout_abut_conns


def _target_port_entry(catalog, block, port, default):
    """The JUMP entry a producer into the named target ``port`` must trigger.

    A multi-entry rendezvous cell (the DualFloatToComplex ``got_i``/``got_q``)
    runs DIFFERENT code per input port, so its ports declare their own entry
    (``Port.entry`` → resolved into the PortMap). Every ordinary block keeps its
    single default entry (``default``, from ``resolved_io``)."""
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
        for p in pmap.ports:
            if p.name == port and p.direction == "in" and p.entry is not None:
                return int(p.entry)
    except Exception:  # noqa: BLE001
        pass
    return int(default)


def _target_port_reg(catalog, block, port, in_regs):
    """The single input register a named target ``port`` maps to (via the block's
    PortMap) — for a FLOAT-source net that delivers ONE operand to that rail (e.g.
    the AM up-converter's ``xi`` → R0; xq stays 0). Falls back to the block's first
    input reg if the port can't be resolved to a register."""
    try:
        pmap = catalog.port_map(block.type, block.params, library=block.library)
        for p in pmap.ports:
            if p.name == port and p.direction == "in" and p.register is not None:
                return int(p.register)
    except Exception:  # noqa: BLE001
        pass
    return int(in_regs[0]) if in_regs else 0


def _resolve_input_landings(cell_map, blocks, connections, project, chip_id,
                            chip_type, gr_placement, catalog,
                            broker_conn_entry, broker_conn_burst) -> dict:
    """For each PORT→block INPUT net on this chip, resolve the HOST-injection landing
    ``{conn: {"cell", "entry", "hop", "data_addrs"}}`` from the BUILT corridor.

    The live bridge drives a hosted chip by injecting WRITE(s)+JUMP through the input
    port; the word transits cells on their built ``fwd_face`` until ``HOP_CNT==31``.
    A manhattan straight-line hop (the legacy model) only works when the corridor's
    faces run straight from the port to the block — which the off-port multi-filament
    auto-layout breaks: two input corridors SHARE a cell that one stream pins to a face
    diverting the OTHER (the modem's x16_in→MF rx corridor pins (1,1) EAST, so the
    x16_in→mapper tx word can no longer transit (1,1) NORTH to the mapper).

    So we walk each net's physical corridor against the BUILT faces:

      * If every corridor cell forwards toward the next waypoint AND the final broker
        forwards into the block's input cell, the net RIDES STRAIGHT — land at the
        block's own input cell (entry = block entry, data_addrs = block input regs).
        This delivers a COMPLEX block's xi+xq directly (the host writes both operands),
        exactly the explicit-demo contract.
      * Else, at the FIRST cell whose built ``fwd_face`` does NOT point to the next
        waypoint, the corridor is DIVERTED — the net must LAND THERE, at that cell's
        broker deliver entry for this net (which flips toward the block + relays). The
        operand(s) land in the broker burst reg(s).

    ``hop`` is the raw 5-bit field = ``30 - corridor_index`` (port cell = index 0,
    hop 30; the block input cell sits one past the last corridor waypoint)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from .bus_router import _phys_pts, _target_input_cell

    in_port = next((p for p in chip_type.ports
                    if p.direction.value == "input"), None)
    if in_port is None:
        return {}
    port_cell = (in_port.cell_x, in_port.cell_y)

    def _face_of(cell):
        cfg = cell_map.get_cell(*cell)
        f = getattr(cfg, "fwd_face", None) if cfg is not None else None
        return int(f) if f is not None else None

    landings: dict = {}
    for conn in connections:
        if not (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            continue
        blk = project.block(conn.target.block)
        if blk is None or blk.placement is None or not blk.placement.cells:
            continue
        # CROSS-CHIP stream: this input net enters chip_id's port but its target
        # block is on a DOWNSTREAM chip (it transits this chip's bus to a far gain).
        # It is NOT a landing on THIS chip — engine.port_config.multi_chip_stream_
        # targets resolves its composite cross-chip hop. Skip it here so it doesn't
        # register a bogus same-chip landing (which corrupted the head chip's port
        # config and deadlocked the multi-chip drive).
        if blk.placement.chip != chip_id:
            continue
        pts = _phys_pts(project, conn, catalog) if conn.is_routed else []
        # Unrouted (direct-on-port placement): keep the legacy manhattan model so the
        # proven explicit modem path (block on the port cell) is untouched.
        if not pts or pts[0] != port_cell:
            cell0 = blk.placement.cells[0]
            dist = abs(cell0.x - in_port.cell_x) + abs(cell0.y - in_port.cell_y)
            entry, in_regs = catalog.resolved_io(
                blk.type, blk.params, library=blk.library)
            # A JOIN's non-trigger port arm lands on the block's data-only
            # ``sink`` entry (Connection.entry_override — the same override the
            # block→block handoff honors), so host injection deposits the
            # operand without firing the combiner.
            if getattr(conn, "entry_override", None) is not None:
                entry = int(conn.entry_override)
            landings[conn.name] = {
                "cell": (cell0.x, cell0.y), "entry": int(entry),
                "hop": (30 - dist) & 0x1F,
                "data_addrs": list(in_regs) if in_regs else [0]}
            continue

        in_cell = _target_input_cell(blk, conn.target.port, catalog)
        # Full corridor incl. the block's own input cell as the final straight target.
        full = list(pts)
        if full[-1] != in_cell:
            full.append(in_cell)
        # Find the FIRST corridor cell that mis-forwards (its built fwd_face does not
        # point to the next waypoint). The block input cell (last) has no "next".
        #
        # SKIP the PORT cell (index 0): the host INJECTS the burst at the port (it sets
        # the hop directly), so the port cell does not FORWARD the word on a fwd_face —
        # the first real face-transit is at corridor index 1. Face-checking the port
        # cell mis-fires whenever the port's own I/O face (e.g. NORTH/SOUTH for the edge)
        # differs from the drawn route's first step: a complex fan-in draws xi via one
        # neighbour and xq via another, so ONE net's drawn first step disagrees with the
        # port face and the scan falsely reports a divert at the port → a bogus broker
        # landing (wrong entry/reg) even though the word rides straight to the block. The
        # word's transit is governed by the NEIGHBOUR faces, not the port's, so start the
        # divert scan at index 1.
        divert = None
        for i in range(1, len(full) - 1):
            want = _step_face(full[i][0], full[i][1], full[i + 1][0], full[i + 1][1])
            if want is None or _face_of(full[i]) != want:
                divert = i
                break

        if divert is None:
            # Rides straight to the block input cell — deliver operand(s) there.
            entry, in_regs = catalog.resolved_io(
                blk.type, blk.params, library=blk.library)
            if getattr(conn, "entry_override", None) is not None:
                entry = int(conn.entry_override)   # join non-trigger arm → sink
            idx = len(full) - 1                  # block input cell's corridor index
            # A COMPLEX source injects all input regs (xi+xq); a FLOAT source (AM
            # up-converter's ``xi`` rail) injects ONE operand into the single rail its
            # net targets — reporting both regs would mis-deliver (out=const, corr=nan).
            if in_regs and len(in_regs) > 1 and conn.src_complex is False:
                data_addrs = [_target_port_reg(catalog, blk, conn.target.port, in_regs)]
            else:
                data_addrs = list(in_regs) if in_regs else [0]
            landings[conn.name] = {
                "cell": in_cell, "entry": int(entry), "hop": (30 - idx) & 0x1F,
                "data_addrs": data_addrs}
        else:
            # Diverted at full[divert] — land at that broker's deliver entry for THIS
            # net (it flips toward the block + relays). The operand lands in the broker
            # burst reg the source-patch path assigned this net.
            b_entry = broker_conn_entry.get(conn.name)
            if b_entry is None:
                # No broker entry for this net at the divert cell (shouldn't happen for
                # a routed net); fall back to the straight block landing.
                entry, in_regs = catalog.resolved_io(
                    blk.type, blk.params, library=blk.library)
                if getattr(conn, "entry_override", None) is not None:
                    entry = int(conn.entry_override)
                idx = len(full) - 1
                landings[conn.name] = {
                    "cell": in_cell, "entry": int(entry),
                    "hop": (30 - idx) & 0x1F,
                    "data_addrs": list(in_regs) if in_regs else [0]}
                continue
            from .bus_router import BROKER_BURST_REG
            # A COMPLEX block (>1 input reg) fed from the port through a broker MAY
            # deliver ALL its operands: a COMPLEX source injects N operands then ONE
            # trigger and the broker relays N WRITEs + 1 JUMP (broker_plan expands it
            # into a multi-operand group) — so xi AND xq land (the duplex RX MF).
            # But a FLOAT source (AM up-converter: net targets only the ``xi`` rail,
            # xq stays 0) injects ONE operand: reporting both burst regs mis-delivers
            # (the host writes one operand where the broker expects two → out=const,
            # corr=nan). Size data_addrs by whether the SOURCE is complex.
            #
            # CRITICAL: the operand base is NOT BROKER_BURST_REG (R0). ``_broker_program``
            # allocates every operand its OWN register starting at R1 (``op_reg = 1 +
            # op_index``) — R0 is only the WRITE scratch (``MOVE R0, R<op>`` clobbers it
            # each relay). The broker relay for this net READS from those op regs, so the
            # host MUST inject into them, not R0. ``broker_conn_burst[conn]`` records the
            # LAST operand reg of the group (the dict is overwritten per operand, so the
            # final value is the highest of the N consecutive op regs). The N operands of
            # a coalesced complex delivery are relayed back-to-back → they occupy the N
            # consecutive regs ENDING at that value: [last-(N-1), ..., last].
            _entry2, _in_regs = catalog.resolved_io(
                blk.type, blk.params, library=blk.library)
            last = BROKER_BURST_REG + int(broker_conn_burst.get(conn.name, 0))
            if _in_regs and len(_in_regs) > 1 and conn.src_complex is not False:
                n = len(_in_regs)
                # TWO-COMPLEX-PAIR blocks (AddCC family: 4 input regs = two I/Q
                # pairs) fed by a GRC COMPLEX source: the broker group planned
                # for THIS net is its stream's pair only (see bus_router's
                # src_complex-gated port_complex_regs pair slice), so the host
                # injects exactly 2 operands. Explicitly-wired per-rail nets
                # (src_complex None) keep the legacy full-group sizing.
                if n > 2 and conn.src_complex is True:
                    n = 2
                data_addrs = [last - (n - 1) + i for i in range(n)]
            else:
                data_addrs = [last]
            landings[conn.name] = {
                "cell": full[divert], "entry": int(b_entry),
                "hop": (30 - divert) & 0x1F, "data_addrs": data_addrs}
    return landings


def _read_source_exit(cfg, gb):
    """Read a source block's OUTPUT WRITE/JUMP emission from its exit cell ``cfg``:
    ``(dest, entry, hop)`` — the downstream delivery (WRITE dest reg, JUMP entry
    addr, @N hop) the source currently emits. For a mid-block-output source (the
    Costas ``rotate``, whose exit cell ALSO carries internal handoff WRITEs) the
    output instruction is the HIGHEST-address WRITE/JUMP (emitted last); for a plain
    source every WRITE/JUMP shares one downstream hop, so the highest is fine too.
    Returns ``(None, None, None)`` if the cell has no WRITE/JUMP."""
    write_addrs = [a for a, w in cfg.memory.items()
                   if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE]
    jump_addrs = [a for a, w in cfg.memory.items()
                  if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP]
    dest = entry = hop = None
    if write_addrs:
        w = cfg.memory[max(write_addrs)]
        dest = w & 0x1F
        hop = decode_hop_cnt((w >> 5) & 0x1F)
    if jump_addrs:
        w = cfg.memory[max(jump_addrs)]
        entry = w & 0x1F
        if hop is None:
            hop = decode_hop_cnt((w >> 5) & 0x1F)
    return dest, entry, hop


def _crossover_program(tracks):
    """Assemble a CROSSOVER cell program — the proven :class:`CrossoverBlock` demux
    (``kyttar_block.py``): per crossing net an entry that sets the cell's output
    FACE, then re-emits the landed burst (R0) onward with that net's REMAINING hop
    budget + ORIGINAL downstream dest/entry. Two crossing streams share one cell,
    demuxed by the JUMP entry each source addresses (the per-stream tag, §1.1/§1.4).

    ``tracks`` is a list of ``(conn, exit_face, out_hop, out_dest, out_entry)`` — one
    per crossing net. The burst lands in R0 (the broker/Splitter convention) so the
    source's WRITE dest is R0. Per landed JUMP (HOP==31) the selected entry runs:
      1. ``MOVE [FACE], <exit_face>`` — flip output toward this net's continuation,
      2. ``WRITE @out_hop, out_dest`` — re-emit the burst onward,
      3. ``JUMP  @out_hop, out_entry`` — trigger the continuation (a broker entry,
         or a harmless local entry for a chip-output-port egress),
      4. ``HALT``.

    Returns ``(entry_addr_by_conn, {addr: word})`` (the resolver computes the entry
    addresses; the build re-points each source's JUMP at its track's entry)."""
    from gr_kyttar.placement.block import (CellProgram, DataWord, EntryPoint,
                                             Port)
    from gr_kyttar.placement.resolver import CellProgramResolver

    # face constants pack from addr 1 up; R0 is the burst landing reg.
    data = []
    entries = []
    tmpl_parts = []
    for i, (conn, exit_face, out_hop, out_dest, out_entry) in enumerate(tracks):
        label = f"track{i}"
        fname = f"face{i}"
        data.append(DataWord(fname, int(exit_face) & 0x3, address=1 + i))
        entries.append(EntryPoint(label))
        tmpl_parts.append(
            f"{label}:\n"
            f"    MOVE [FACE], R{{data:{fname}}}\n"
            "    MOVE R0, R{in:burst}\n"
            f"    WRITE @{int(out_hop)}, {int(out_dest)}\n"
            f"    JUMP @{int(out_hop)}, {int(out_entry)}\n"
            "    HALT\n"
        )
    prog = CellProgram(
        inputs=[Port("burst", register=0)],
        outputs=[Port("out")],
        entries=entries,
        data=data,
        state=[],
        assembly_template="".join(tmpl_parts),
    )
    resolver = CellProgramResolver()
    resolved = resolver.resolve(prog)
    entry_addrs = resolver.compute_entry_addresses(prog)
    by_conn = {tracks[i][0]: entry_addrs[f"track{i}"]
               for i in range(len(tracks)) if f"track{i}" in entry_addrs}
    return by_conn, dict(resolved.memory)


def _relay_program(exit_face, out_hop, out_dest, out_entry):
    """Assemble a RELAY cell program — the §1.4 #3 long-route re-launch.

    A route longer than the 5-bit HOP_CNT field (31) cannot be delivered by one
    WRITE/JUMP pair: the source's hop field simply cannot name a cell that far
    away. The fix is to SPLIT the route: the word is addressed to LAND on an
    intermediate routing cell (HOP_CNT==31), which re-emits it with a FRESH hop
    budget toward the remainder of the route.

    This is the SAME primitive :func:`_crossover_program` already proves on-chip
    (land → flip face → re-emit), specialised to ONE track: a relay is a crossover
    with a single stream, whose purpose is a fresh hop count rather than a face
    demux. We reuse the shape rather than invent a second relay opcode.

    The landed word runs:
      1. ``MOVE [FACE], <exit_face>`` — point at the continuation of the route,
      2. ``MOVE R0, R{in:burst}`` — take the landed burst,
      3. ``WRITE @out_hop, out_dest`` — re-emit the PAYLOAD onward,
      4. ``JUMP  @out_hop, out_entry`` — re-emit the TRIGGER onward,
      5. ``HALT``.

    Steps 3+4 preserve the WRITE+JUMP pair semantics exactly: the final
    destination receives the same payload register and the same trigger entry it
    would have received from a hop-legal single-segment route — only the hop
    field differs, which is precisely what the relay exists to refresh.

    ``out_dest``/``out_entry`` are the ORIGINAL downstream delivery (read from the
    source's already-patched exit WRITE/JUMP) when this is the LAST relay on the
    net; for an intermediate relay they address the NEXT relay's burst reg + entry.

    Returns ``(entry_addr, {addr: word})``."""
    from gr_kyttar.placement.block import (CellProgram, DataWord, EntryPoint,
                                             Port)
    from gr_kyttar.placement.resolver import CellProgramResolver

    prog = CellProgram(
        inputs=[Port("burst", register=0)],
        outputs=[Port("out")],
        entries=[EntryPoint("relay")],
        data=[DataWord("face", int(exit_face) & 0x3, address=1)],
        state=[],
        assembly_template=(
            "relay:\n"
            "    MOVE [FACE], R{data:face}\n"
            "    MOVE R0, R{in:burst}\n"
            f"    WRITE @{int(out_hop)}, {int(out_dest)}\n"
            f"    JUMP @{int(out_hop)}, {int(out_entry)}\n"
            "    HALT\n"
        ),
    )
    resolver = CellProgramResolver()
    resolved = resolver.resolve(prog)
    entry_addrs = resolver.compute_entry_addresses(prog)
    return entry_addrs["relay"], dict(resolved.memory)


def _apply_crossovers(cell_map, gr_placement, blocks, connections, project,
                      chip_id, chip_type, gr_blocks, catalog) -> None:
    """Promote single-``fwd_face`` CONFLICT cells to programmed CROSSOVERS (§1.2/§1.3).

    Two routed nets that must leave one PLAIN routing cell in DIFFERENT directions
    cannot share its single ``fwd_face`` — the static-face build silently mis-faces
    one stream (the BPSK-dead-build, the (9,0) corner where Costas→Gardner transits
    WEST while the slicer→x16_out egress needs EAST). :func:`bus_router.crossover_plan`
    names those cells; here each becomes a demux (the proven :class:`CrossoverBlock`):

      * each crossing net LANDS at the cell via its OWN JUMP entry (the per-stream
        tag), re-emitted on its own face with its REMAINING hop budget toward its
        ORIGINAL downstream delivery (read from the source's already-patched exit
        WRITE/JUMP, so build state is the single source of truth);
      * the SOURCE is re-pointed to land AT the crossover (dest=R0, entry=track entry,
        hop = source→crossover distance) instead of running the full route — which
        would have mis-faced the shared cell.

    Runs AFTER :func:`_apply_brokers` (the source exit now carries its final
    dest/entry/hop) and after the route faces are set (which created the conflict this
    resolves). A no-op when no cell is contended (the common fast-path: a plain shared
    bus segment leaves every cell ONE way)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from gr_kyttar.placement.cell_map import CellConfig
    from .bus_router import BROKER_BURST_REG, crossover_plan

    taps = crossover_plan(project, chip_id, chip_type, catalog)
    if not taps:
        return

    placed = {b.name for b in blocks}
    conn_by_name = {c.name: c for c in connections}

    def _source_exit_cfg(conn):
        """The cell_map cell holding the net's SOURCE exit WRITE/JUMP, + the gr_block,
        + the route head distance (source exit cell → crossover index lookup)."""
        src = conn.source
        if isinstance(src, BlockEndpoint) and src.block in placed:
            pb = gr_placement.placed_blocks.get(src.block)
            if pb is None:
                return None, None
            return cell_map.get_cell(*pb.exit_cell), gr_blocks.get(src.block)
        return None, None

    # Build each crossover's track emissions by reading every crossing net's CURRENT
    # source exit (the full downstream delivery), then SPLIT it at the crossover.
    for (cx, cy), tap in taps.items():
        emit_tracks = []        # (conn, exit_face, out_hop, out_dest, out_entry)
        repoints = []           # (conn, head) — source re-point after entry resolve
        for trk in tap.tracks:
            conn = conn_by_name.get(trk.conn)
            if conn is None:
                continue
            scfg, gb = _source_exit_cfg(conn)
            if scfg is None:
                # A port-SOURCE net at a crossover: no block source to re-point (the
                # design's chains are block-sourced past the input splitter). Skip —
                # the residual face conflict is then NAMED by the DRC (P3.4).
                continue
            dest, entry, full_hop = _read_source_exit(scfg, gb)
            if full_hop is None:
                continue
            out_hop = max(1, int(full_hop) - int(trk.head))
            # The port-egress continuation has no JUMP target; keep its entry (0/tag)
            # — harmless. A block continuation keeps the broker's deliver entry.
            out_dest = dest if dest is not None else BROKER_BURST_REG
            out_entry = entry if entry is not None else 0
            emit_tracks.append((trk.conn, trk.exit_face, out_hop, out_dest, out_entry))
            repoints.append((trk.conn, trk.head))

        if not emit_tracks:
            continue
        by_conn, memory = _crossover_program(emit_tracks)

        cfg = cell_map.get_cell(cx, cy)
        if cfg is None:
            cfg = CellConfig(block_name="_crossover")
            cell_map.set_cell(cx, cy, cfg)
        # A crossover LANDS its words (each runs an entry) and the per-track
        # MOVE [FACE] supersedes any static route face; just install the program.
        cfg.memory.update(memory)
        cfg.entry_addr = min(by_conn.values()) if by_conn else cfg.entry_addr
        if not getattr(cfg, "block_name", ""):
            cfg.block_name = "_crossover"
        elif cfg.block_name is None:
            cfg.block_name = "_crossover"

        # Re-point each crossing net's source to LAND at the crossover.
        for (conn_name, head) in repoints:
            conn = conn_by_name[conn_name]
            scfg, gb = _source_exit_cfg(conn)
            if scfg is None or conn_name not in by_conn:
                continue
            t_entry = by_conn[conn_name]
            if _output_cell_carries_handoffs(gb):
                _patch_last_write_handoff(scfg, head, dest=BROKER_BURST_REG)
                _patch_last_jump_handoff(scfg, head, entry=t_entry)
            else:
                _patch_cell_handoff(scfg, head, dest=BROKER_BURST_REG,
                                    entry=t_entry)


def _apply_relays(cell_map, gr_placement, blocks, connections, project,
                  chip_id, chip_type, gr_blocks, catalog) -> dict:
    """Program RELAY cells for OVER-BUDGET (>31-hop) routes (§1.4 #3).

    The hop field is 5 bits, so one WRITE/JUMP pair can address a cell at most 31
    hops away. A longer route is SPLIT: the word is addressed to LAND on an
    intermediate plain routing cell, which re-emits it with a FRESH budget toward
    the remainder of the route. :func:`bus_router.relay_plan` derives the cells
    from the routed project (build-from-design); here each is programmed and the
    upstream emitter is re-pointed at it.

    Per relay:
      * the relay cell gets :func:`_relay_program` — land, flip to the exit face,
        re-emit ``WRITE`` + ``JUMP`` with the next segment's hop budget;
      * the UPSTREAM emitter (the source block's exit cell for the FIRST relay, the
        PREVIOUS relay for the rest) is re-pointed to land at this relay: dest =
        the relay's burst register, entry = the relay's entry, hop = the segment.

    The final relay re-emits the net's ORIGINAL downstream delivery (dest/entry,
    read from the source's already-patched exit WRITE/JUMP), so the destination
    receives exactly the payload register + trigger entry a hop-legal route would
    have delivered — the WRITE+JUMP pair semantics are preserved end to end.

    Runs AFTER :func:`_apply_brokers` and :func:`_apply_crossovers` (the source
    exit now carries its FINAL dest/entry/hop, which is what the last segment must
    reproduce) and BEFORE :func:`_apply_routing_cell_programs` (so a relay cell is
    no longer a "plain transit" cell and keeps its relay program).

    Returns ``{conn_name: [(x, y), ...]}`` — the relay cells used per net, for the
    build report (relays cost array area, so the cost is made visible).
    """
    from model.connection import BlockEndpoint
    from gr_kyttar.placement.cell_map import CellConfig
    from .bus_router import BROKER_BURST_REG, relay_plan

    plan = relay_plan(project, chip_id, chip_type, catalog)
    if not plan:
        return {}

    placed = {b.name for b in blocks}
    conn_by_name = {c.name: c for c in connections}
    used: dict[str, list] = {}

    for conn_name, hops in plan.items():
        conn = conn_by_name.get(conn_name)
        if conn is None:
            continue
        src = conn.source
        if not (isinstance(src, BlockEndpoint) and src.block in placed):
            # A PORT-sourced over-budget net: the host injects at the port, so there
            # is no block exit WRITE/JUMP to re-point. Leave it to the (still
            # failing) hop DRC rather than mis-program it.
            continue
        pb = gr_placement.placed_blocks.get(src.block)
        if pb is None:
            continue
        scfg = cell_map.get_cell(*pb.exit_cell)
        gb = gr_blocks.get(src.block)
        if scfg is None:
            continue
        # The net's FINAL downstream delivery (what the last relay must reproduce).
        dest, entry, _full_hop = _read_source_exit(scfg, gb)
        out_dest = dest if dest is not None else BROKER_BURST_REG
        out_entry = entry if entry is not None else 0

        # Program each relay from the LAST backward, so every relay knows the
        # (already-resolved) entry address of the relay it re-emits into.
        next_dest, next_entry = out_dest, out_entry
        resolved = []           # (cell, entry_addr) in reverse route order
        for hop in reversed(hops):
            r_entry, memory = _relay_program(hop.exit_face, hop.out_hop,
                                             next_dest, next_entry)
            cx, cy = hop.cell
            cfg = cell_map.get_cell(cx, cy)
            if cfg is None:
                cfg = CellConfig(block_name="_relay")
                cell_map.set_cell(cx, cy, cfg)
            cfg.memory.update(memory)
            cfg.entry_addr = r_entry
            if not getattr(cfg, "block_name", ""):
                cfg.block_name = "_relay"
            elif cfg.block_name is None:
                cfg.block_name = "_relay"
            resolved.append(((cx, cy), r_entry))
            # The PREVIOUS emitter must land HERE: burst reg + this relay's entry.
            next_dest, next_entry = BROKER_BURST_REG, r_entry

        # Re-point the SOURCE at the FIRST relay (``next_dest``/``next_entry`` now
        # hold it after the reverse walk), at the first segment's distance.
        first = hops[0]
        if _output_cell_carries_handoffs(gb):
            _patch_last_write_handoff(scfg, first.head, dest=next_dest)
            _patch_last_jump_handoff(scfg, first.head, entry=next_entry)
        else:
            _patch_cell_handoff(scfg, first.head, dest=next_dest,
                                entry=next_entry)
        used[conn_name] = [c for c, _e in reversed(resolved)]
    return used


# --- §1.4 universal routing-cell program (Reading B) -----------------------

# Where the universal program packs its DATA words (kept clear of the broker/
# crossover burst reg R0). bus_face at R1; the relay function has its OWN burst
# landing reg (§1.4 relay-safety: a relay interrupted mid-stream must not corrupt
# the transmit function) at R2.
_UNIV_BUS_FACE_REG = 1
_UNIV_RELAY_BURST_REG = 2


def _universal_routing_program(bus_face: int):
    """Assemble the §1.4 UNIVERSAL routing-cell program (Reading B).

    Every routing cell — including a PLAIN TRANSIT spine cell — carries this one
    uniform, multi-function program so the fabric is made of generic, repurposable
    control cells (enabling §4.2 dynamic reconfiguration later). It embeds two
    fabric-control functions, selected by entry address:

      * ``transmit`` — re-emit a word that LANDED here (HOP_CNT==31) onward on the
        bus (``fwd_face``) with a fresh budget. This is the *explicit program form*
        of transmit-through; the FORWARDING of an ordinary transiting word
        (HOP_CNT<31) is the hardware default via CONFIG[FACE] and does NOT touch
        this program (the hardware routing engine decides execute-vs-forward purely
        on HOP_CNT, never reading memory — proven in ``proto_transit2.py``). So this
        entry is reached ONLY when a word is deliberately addressed to land here.
      * ``relay`` — §1.4 #3: re-launch a long (>31-hop) route with a fresh 31-hop
        budget, using its OWN burst register (``_UNIV_RELAY_BURST_REG``) so an
        interrupted relay can't corrupt the transmit function (§1.4 relay-safety).

    The CRITICAL correctness property (the builds≠computes hazard): a HOP_CNT<31
    word transiting a now-PROGRAMMED transit cell behaves IDENTICALLY to transiting
    a face-only cell — it is forwarded on ``fwd_face`` before the program is ever
    consulted. Neither entry can fire for a transiting word; they fire only at
    HOP_CNT==31. So the static datapath is byte-for-byte unaffected in behaviour
    (§3 invariant); the added value is the LATENT entries for dynamic reconfig.

    ``bus_face`` is the cell's through-bus direction (its ``fwd_face``). Returns
    ``(entry_addr_by_name, {addr: word})``."""
    from gr_kyttar.placement.block import (CellProgram, DataWord, EntryPoint,
                                             Port, StateVar)
    from gr_kyttar.placement.resolver import CellProgramResolver

    bf = int(bus_face) & 0x3
    # transmit: forward R0 onward on the bus (next cell @1, re-trigger its transmit).
    # relay:    forward via the OWN relay burst reg, fresh budget (next cell @1).
    # Both restore FACE to the bus direction so any LATER transiting word continues.
    # The JUMP @1 target re-triggers the DOWNSTREAM cell's transmit entry — which,
    # since every routing cell carries this identical layout, is the SAME entry
    # address as this cell's `transmit`. We can't reference it via {entry:...} (a
    # {entry:} placeholder inside a JUMP operand can't be assembled in the dummy
    # pass), so we resolve with a 0 placeholder then patch the JUMP entry field to
    # the resolved `transmit` address.
    tmpl = (
        "transmit:\n"
        "    MOVE [FACE], R{data:bus_face}\n"
        "    MOVE R0, R{in:burst}\n"
        "    WRITE @1, 0\n"
        "    JUMP @1, 0\n"
        "    MOVE [FACE], R{data:bus_face}\n"
        "    HALT\n"
        "relay:\n"
        "    MOVE [FACE], R{data:bus_face}\n"
        "    MOVE R0, R{state:relay_burst}\n"
        "    WRITE @1, 0\n"
        "    JUMP @1, 0\n"
        "    MOVE [FACE], R{data:bus_face}\n"
        "    HALT\n"
    )
    prog = CellProgram(
        inputs=[Port("burst", register=0)],
        outputs=[Port("out")],
        entries=[EntryPoint("transmit"), EntryPoint("relay")],
        data=[DataWord("bus_face", bf, address=_UNIV_BUS_FACE_REG)],
        state=[StateVar("relay_burst", register=_UNIV_RELAY_BURST_REG)],
        assembly_template=tmpl,
    )
    resolver = CellProgramResolver()
    resolved = resolver.resolve(prog)
    entry_addrs = dict(resolver.compute_entry_addresses(prog))
    memory = dict(resolved.memory)
    # Patch each JUMP @1 (entry field, low 5 bits) to the resolved `transmit`
    # address, so a re-launched word retriggers the next cell's transmit-through.
    t_entry = entry_addrs.get("transmit", 0) & 0x1F
    for addr, word in list(memory.items()):
        if (word & 0xF000) == _JUMP:
            memory[addr] = (word & ~0x1F) | t_entry
    return entry_addrs, memory


def _apply_routing_cell_programs(cell_map) -> None:
    """Emit the §1.4 UNIVERSAL program into EVERY PLAIN TRANSIT routing cell
    (Reading B, maintainer-approved).

    After all faces/brokers/crossovers are set, a *plain transit* spine cell is a
    cell with a ``fwd_face`` but NO program (``is_routing_only()`` — empty memory,
    no entry). Brokers and crossovers already carry their own programs (flip-relay-
    restore / demux) — leave them untouched. Block cells have an owning program —
    untouched. This pass walks the cell map and gives each remaining plain transit
    cell the universal transmit(+relay) program, keyed on its existing ``fwd_face``,
    so the cell is a generic, dynamically-repurposable fabric cell.

    Pass-through is preserved by construction: the program's entries are reachable
    ONLY at HOP_CNT==31 (a deliberately-landed word); an ordinary transiting word
    (HOP_CNT<31) is forwarded on ``fwd_face`` by the hardware routing engine before
    the program is consulted. The cell's ``fwd_face`` is NOT changed, so the bus
    direction — and thus the static datapath — is identical to before."""
    for (col, row), cfg in list(cell_map.cells.items()):
        # Only PLAIN TRANSIT cells: a fwd_face is set, but no program yet.
        if not getattr(cfg, "is_routing_only", lambda: False)():
            continue
        fwd = getattr(cfg, "fwd_face", None)
        if fwd is None:
            continue
        _entries, memory = _universal_routing_program(int(fwd))
        # Carry the program WITHOUT disturbing fwd_face: forwarding of transiting
        # (HOP<31) words still goes out fwd_face untouched. entry_addr left as the
        # transmit entry so a deliberately-landed word defaults to transmit-through.
        cfg.memory.update(memory)
        cfg.entry_addr = _entries.get("transmit", cfg.entry_addr)
        if not getattr(cfg, "block_name", ""):
            cfg.block_name = "_routing"


def _apply_port_diverts(cell_map, blocks, connections, project, chip_id,
                        chip_type, catalog, broker_conn_entry,
                        broker_conn_burst) -> dict:
    """Turn the CHIP-INPUT PORT cell into a BROKER for a port-source net that must
    DIVERT at the port (the shared-input full-duplex corner, §1.2/§1.3).

    A chip has ONE input port cell and ONE ``fwd_face`` on it. When the port fans out
    to TWO blocks whose first route steps leave the port in DIFFERENT directions (the
    duplex modem: RX net leaves the port EAST toward the matched filter, TX net leaves
    SOUTH toward the mapper), only ONE direction can be the static ``fwd_face``. One
    stream (TX) rides that face straight (a HOP<31 transit, untouched); the OTHER (RX)
    would be forwarded the WRONG way and die.

    The host injects each stream AT the port. So the diverting stream must LAND at the
    port cell (HOP_CNT==31, hop field 30) and be RELAYED off it: a broker turn entry
    flips the face toward that net's first waypoint, relays the operand(s) ONE hop to
    the net's DOWNSTREAM broker (the route's next cell, already programmed to deliver
    into the block), then restores ``fwd_face`` so a LATER transiting word (the OTHER
    stream) still forwards on the bus direction.

    This is the multi-cell delivery the ``@1`` adjacent-broker could not do: the port
    is not adjacent to the block — the RX word must transit the intermediate routing
    cell (the downstream broker) before reaching the block. We solve it by chaining
    brokers: the port broker lands the word at the DOWNSTREAM broker (its burst regs +
    deliver entry, ``@1``), and that broker relays it the rest of the way (into the
    block). Both hops are ``@1`` (each broker delivers to its adjacent neighbour), so
    no new opcode is needed — just a second broker on the shared port cell.

    Runs AFTER :func:`_apply_routing_cell_programs` (the port cell already carries the
    universal transit program with ``fwd_face`` = the riding stream's direction). For a
    diverting net it REPLACES that latent program with the broker turn program (same
    ``fwd_face``, so the riding stream's HOP<31 transit is unchanged). A no-op when the
    port fans out one way (every net rides its face — the common single-stream path).

    Returns ``{conn_name: {"cell", "entry", "hop", "data_addrs"}}`` — the host-injection
    landing for each diverted net (land AT the port cell, run its turn entry). The
    build merges these into ``input_landings`` (overriding the straight resolution)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint, ABUTMENT_ROUTE
    from .bus_router import (BROKER_BURST_REG, BrokerDelivery, _phys_pts,
                             _target_input_cell)

    in_port = next((p for p in chip_type.ports
                    if p.direction.value == "input"), None)
    if in_port is None:
        return {}
    port_cell = (in_port.cell_x, in_port.cell_y)
    pcfg = cell_map.get_cell(*port_cell)
    if pcfg is None:
        return {}
    port_face = getattr(pcfg, "fwd_face", None)
    if port_face is None:
        return {}
    port_face = int(port_face)

    # Every port-source input net on this chip, with its route first step. Use the RAW
    # drawn route (conn.route), NOT _phys_pts: _phys_pts STRIPS the trailing input-cell
    # waypoint when the target block ABUTS the port (route == [port, in_cell]), leaving
    # a 1-cell path — which would make the divert skip an abutting fan-out target and
    # forward its word the WRONG way (the cell-(0,0) shared-port bug: TX net [(0,0),
    # (1,0)] into the mapper stripped to [(0,0)] → rode the RX face SOUTH into the RX
    # block). The divert only needs the port cell + the FIRST step direction; keep the
    # full route so an adjacent block is still seen as a fan-out branch.
    port_nets = []
    for conn in connections:
        if not (isinstance(conn.source, ChipPortEndpoint)
                and conn.source.chip == chip_id
                and conn.source.port == in_port.name
                and isinstance(conn.target, BlockEndpoint)):
            continue
        if not conn.is_routed or conn.route == ABUTMENT_ROUTE:
            continue
        pts = [(p.x, p.y) for p in conn.route]
        if not pts or pts[0] != port_cell or len(pts) < 2:
            continue
        step = _step_face(port_cell[0], port_cell[1], pts[1][0], pts[1][1])
        port_nets.append((conn, pts, step))

    # A net whose first step matches the port's static fwd_face RIDES straight — it
    # transits the port on that face (HOP<31), untouched. A net whose first step
    # DIFFERS diverts: it must land at the port and be relayed off it.
    diverting = [(conn, pts, step) for (conn, pts, step) in port_nets
                 if step is not None and step != port_face]
    if not diverting:
        return {}

    # Build the port-cell broker deliveries: one group per diverting net, targeting
    # the net's DOWNSTREAM broker (route cell pts[1]) — its burst regs + deliver entry
    # (already programmed by _apply_brokers). Coalesced by a per-net src_cell sentinel
    # so a COMPLEX net's N operands relay as N WRITEs + ONE JUMP (the complex-sample
    # contract), matching the downstream broker's expectation.
    deliveries = []
    landing_meta = {}   # conn -> (n_operands, downstream_regs)
    for (conn, pts, step) in diverting:
        down_cell = pts[1]
        blk = project.block(conn.target.block)
        _e, in_regs = catalog.resolved_io(blk.type, blk.params, library=blk.library)
        # ABUTTING fan-out target (the cell-(0,0) shared-port bug): the block sits
        # DIRECTLY next to the port (route == [port, in_cell]), so the port broker must
        # relay @1 STRAIGHT into the block's OWN input cell — its own DSP entry + input
        # registers. Detect this by GEOMETRY (the divert's next cell IS the block's input
        # cell). This takes PRECEDENCE over any broker_conn_entry: `_apply_brokers` may
        # have seated a broker AT that same input cell (a spurious relay entry that does
        # NOT run the block's DSP — the word landed in the broker's reg + entry and the
        # mapper never emitted), so we must NOT use it; deliver to the block itself.
        in_cell = _target_input_cell(blk, conn.target.port, catalog)
        _direct_abut = (in_cell is not None and in_cell == down_cell)
        if _direct_abut:
            d_entry = _e                # the block's own landing entry
        else:
            d_entry = broker_conn_entry.get(conn.name)
            if d_entry is None:
                # Genuinely no downstream target (shouldn't happen for a routed
                # port fan-out) — leave it to the straight path.
                continue
        if _direct_abut:
            # Deliver into the BLOCK's OWN input registers (it is the landing cell).
            if in_regs and len(in_regs) > 1 and conn.src_complex is not False:
                n = len(in_regs)
                down_regs = list(in_regs)
            else:
                n = 1
                down_regs = [in_regs[0] if in_regs else 0]
        else:
            # N operands the downstream broker expects for THIS net. broker_conn_burst is
            # the LAST (highest) operand reg of the net's group at the downstream broker;
            # the N operands are the N consecutive regs ending there.
            last = BROKER_BURST_REG + int(broker_conn_burst.get(conn.name, 0))
            if in_regs and len(in_regs) > 1 and conn.src_complex is not False:
                n = len(in_regs)
                down_regs = [last - (n - 1) + i for i in range(n)]
            else:
                n = 1
                down_regs = [last]
        grp_key = ("port_divert", conn.name)
        for r in down_regs:
            deliveries.append(BrokerDelivery(
                conn=conn.name, in_cell=down_cell, in_reg=r,
                in_entry=int(d_entry), deliver_face=int(step), src_cell=grp_key))
        landing_meta[conn.name] = (n, down_regs)

    if not deliveries:
        return {}

    # Assemble the port-cell broker program: restore face = the port's static face
    # (the riding stream's direction) so a transiting word (TX) still forwards right.
    by_conn, memory, burst_reg_by_conn = _broker_program(deliveries, port_face)

    # REPLACE the port cell's latent universal program with the broker turn program.
    # fwd_face is UNCHANGED (still the riding stream's direction) — the riding stream's
    # HOP<31 transit is byte-identical; only a deliberately-landed (diverting) word
    # runs the new turn entry.
    pcfg.memory.clear()
    pcfg.memory.update(memory)
    pcfg.entry_addr = min(by_conn.values()) if by_conn else pcfg.entry_addr
    pcfg.fwd_face = _CM_FACE(port_face)
    if not getattr(pcfg, "block_name", ""):
        pcfg.block_name = "_broker"

    # Host-injection landing for each diverted net: land AT the port cell (hop field
    # 30 == HOP_CNT 31 there), run its turn entry, inject its operand(s) into the
    # port broker's OWN burst regs (the regs _broker_program allocated, R1..RN).
    landings = {}
    for (conn, pts, step) in diverting:
        if conn.name not in by_conn or conn.name not in burst_reg_by_conn:
            continue
        n, _down_regs = landing_meta[conn.name]
        # burst_reg_by_conn is the LAST operand reg the source WRITEs to at this broker
        # (overwritten per operand → highest of the N consecutive regs). The N operands
        # occupy the N consecutive regs ENDING there.
        last = int(burst_reg_by_conn[conn.name])
        data_addrs = [last - (n - 1) + i for i in range(n)]
        landings[conn.name] = {
            "cell": port_cell, "entry": int(by_conn[conn.name]),
            "hop": 30 & 0x1F, "data_addrs": data_addrs}
    return landings


def _patch_last_jump_handoff(cfg, hop, entry=None) -> None:
    """Patch ONLY the highest-address JUMP instruction in ``cfg`` to ``hop`` (and
    optional ``entry``) — the mirror of :func:`_patch_last_write_handoff` for the
    exit trigger of a mid-block-output source (e.g. the Costas rotate cell's yi_tap
    JUMP), leaving any earlier internal-handoff JUMPs intact."""
    hop_cnt = encode_hop_cnt(hop)
    jump_addrs = [a for a, w in cfg.memory.items()
                  if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP]
    if not jump_addrs:
        return
    addr = max(jump_addrs)
    word = cfg.memory[addr]
    word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
    if entry is not None:
        word = (word & ~0x1F) | (int(entry) & 0x1F)
    cfg.memory[addr] = word & 0xFFFF


def _apply_port_route_faces_and_hops(cell_map, gr_placement, blocks,
                                     connections, chip_type, catalog) -> None:
    """Honor user routes to chip-OUTPUT ports (§2.6).

    The Router only routes the sink block to ONE configured output port; a route
    to any other output port (e.g. the south-facing x1_out) is left with the
    wrong cell faces and the source gets a wrong hop. For each block→output-port
    route on this chip:
      * each route waypoint cell's ``fwd_face`` points to the NEXT waypoint,
      * the FINAL waypoint (the port's edge cell) faces the PORT's exit face,
      * the source block's exit WRITE/JUMP hop = the route length (so the data
        actually reaches and exits the port).
    """
    from model.connection import BlockEndpoint, ChipPortEndpoint

    placed = {b.name for b in blocks}
    # Map this chip's port name → (cell_x, cell_y, face_code).
    ports = {p.name: (p.cell_x, p.cell_y, _PORT_FACE_CODE.get(p.face.value))
             for p in chip_type.ports}

    for conn in connections:
        src, tgt = conn.source, conn.target
        if not (isinstance(src, BlockEndpoint) and src.block in placed
                and isinstance(tgt, ChipPortEndpoint)
                and tgt.port.endswith("_out") and conn.is_routed):
            continue
        pts = [(p.x, p.y) for p in conn.route]
        port = ports.get(tgt.port)
        # Face every waypoint toward the next; the last faces the port's exit.
        for i, (x, y) in enumerate(pts):
            cfg = cell_map.get_cell(x, y)
            if cfg is None:
                continue
            if i + 1 < len(pts):
                face = _step_face(x, y, *pts[i + 1])
            elif port is not None:
                face = port[2]  # final cell exits via the port's face
            else:
                face = None
            if face is not None:
                cfg.fwd_face = face
        # Source block exit hop = the route length (reaches + exits the port).
        # The dest carries the connection's output TAG (default 0) so chains that
        # share one output port stay distinguishable on the wire (OutWord.tag).
        pb = gr_placement.placed_blocks.get(src.block)
        if pb is not None:
            # PER-NET exit cell (multi-output source) — see _apply_routes.
            cfg = cell_map.get_cell(
                *_net_source_exit_cell(pb, pts, blocks, src.block))
            if cfg is not None:
                out_dest = conn.out_tag if conn.out_tag is not None else 0
                # If the source declares a MID-block output cell (its output leaves
                # a non-last cell that also carries internal handoffs), patch ONLY
                # the output WRITE (the last WRITE — emitted after the internal
                # ones) so the internal handoffs keep their @1 hops.
                sb = next((b for b in blocks if b.name == src.block), None)
                gb = None
                if sb is not None:
                    try:
                        gb = catalog.instantiate(sb.type, sb.name, sb.params,
                                                 library=sb.library)
                    except Exception:  # noqa: BLE001
                        gb = None
                # Is the source a COMPLEX-OUTPUT cell (>1 output register)? Then its
                # recovered pair (yi_tap, yq_tap) is the program TAIL and BOTH rails must
                # egress the port with distinct tags. A fused output+handoff cell (the
                # order-4 Costas qpd: an internal err→pd_pi WRITE, then the yi_tap/yq_tap
                # tail) can't take the patch-EVERY-WRITE complex path (that would egress
                # err too); the plain last-write patch routes only yq_tap and strands
                # yi_tap on a stale hop colliding with err. Patch the last N tail WRITEs.
                n_out = 1
                try:
                    if gb is not None:
                        n_out = max(1, len(gb.interface.output_registers))
                except Exception:  # noqa: BLE001
                    n_out = 1
                if _output_cell_carries_handoffs(gb):
                    if n_out > 1:
                        _patch_last_n_write_handoff(cfg, _route_distance(conn),
                                                    n_out, base_tag=out_dest)
                    else:
                        _patch_last_write_handoff(cfg, _route_distance(conn),
                                                  dest=out_dest)
                    _patch_last_jump_handoff(cfg, _route_distance(conn), entry=0)
                else:
                    _patch_cell_handoff(cfg, _route_distance(conn),
                                        dest=out_dest, entry=0)


def _apply_inter_chip_hops(cell_map, gr_placement, blocks, project, chip_id,
                           chip_type, catalog) -> None:
    """Patch the exit WRITE/JUMP of a block that feeds the NEXT chip (§5.4).

    Signal path: source block → route to this chip's OUTPUT port → inter-chip
    wire → next chip's INPUT port → route to a block on that chip. The hop count
    is continuous across the boundary (the interconnect itself is not a hop), so

        total hop = (this chip's exit route distance)
                  + (next chip's route distance from its input port to the block)

    and the WRITE dest / JUMP entry are the downstream block's resolved input
    register / entry address. e.g. gain(0,0)→x16_out(9,0)=10, +1 to chip1
    block(1,0) = @11.
    """
    from model.connection import BlockEndpoint, ChipPortEndpoint

    placed = {b.name for b in blocks}
    for conn in project.connections:
        src, tgt = conn.source, conn.target
        # A block on THIS chip routing out to one of this chip's output ports.
        if not (isinstance(src, BlockEndpoint) and src.block in placed
                and isinstance(tgt, ChipPortEndpoint) and tgt.chip == chip_id
                and tgt.port.endswith("_out") and conn.is_routed):
            continue
        # Follow the inter-chip wire from this output port to the next chip's
        # input port.
        wire = next((ic for ic in project.inter_chip_connections
                     if ic.from_chip == chip_id and ic.from_port == tgt.port), None)
        if wire is None:
            continue
        # Find the route on the destination chip from that input port to a block.
        dest_conn = next(
            (c for c in project.connections
             if isinstance(c.source, ChipPortEndpoint)
             and c.source.chip == wire.to_chip and c.source.port == wire.to_port
             and isinstance(c.target, BlockEndpoint) and c.is_routed), None)
        if dest_conn is None:
            continue
        dest_block = project.block(dest_conn.target.block)
        if dest_block is None:
            continue
        # Total continuous hop across the boundary + resolved downstream handoff.
        total = _route_distance(conn) + _route_distance(dest_conn)
        entry, in_regs = catalog.resolved_io(
            dest_block.type, dest_block.params, library=dest_block.library)
        # ALL the downstream input registers, in rail order — the patcher
        # steers rail k to in_regs[k] (see _patch_cell_handoff).
        dest_reg = list(in_regs) if in_regs else None
        # Patch the source block's EXIT cell.
        pb = gr_placement.placed_blocks.get(src.block)
        if pb is None:
            continue
        cfg = cell_map.get_cell(*pb.exit_cell)
        if cfg is not None:
            # MID-BLOCK EXIT: if the source block's exit cell also carries
            # internal-handoff WRITEs (an R2SDF stage's out cell writes its
            # emerging pair back into its own ctl and clears that stage's
            # serialize-LOCK, all at @1), patch ONLY the last WRITE. Derived
            # the same way the single-chip path derives output_at_last_write —
            # a block that declares an output_cell_id() owns a mid-block exit.
            src_block = project.block(src.block)
            n_out = None
            try:
                gb = catalog.instantiate(
                    src_block.type, src_block.name,
                    params=src_block.params, library=src_block.library)
                if gb.output_cell_id() is not None:
                    # RAIL COUNT, from the block's own exit-cell program: the
                    # outgoing packet is the trailing run of writes that are
                    # NOT internal connections. Everything before them (the
                    # feedback pair, the lock-clear WRITE.CFG) keeps its hop.
                    exit_id = gb.output_cell_id()
                    prog = gb.build_cell_programs()[exit_id]
                    internal = {sp for (sc, sp, _d, _p)
                                in gb.internal_connections() if sc == exit_id}
                    names = [o.name for o in prog.outputs
                             if not o.name.startswith("t_")
                             and o.name not in ("trig",)]
                    n_out = 0
                    for nm in reversed(names):
                        if nm in internal:
                            break
                        n_out += 1
                    n_out = n_out or 1
            except Exception:  # noqa: BLE001 — no such contract → patch all
                n_out = None
            _patch_cell_handoff(cfg, total, dest=dest_reg, entry=entry,
                                n_output_writes=n_out)


def _set_cell_hop1(cfg, dest=None, entry=None, preserve_dest_regs=None) -> None:
    """Force every WRITE/JUMP INSTRUCTION in a cell to ``@1`` and, when resolved,
    set the abutting target's register (WRITE ``dest``) / entry address (JUMP
    ``entry``). Data words are left untouched (see :func:`_is_instruction_addr`).

    ``preserve_dest_regs`` (a set of registers) leaves any WRITE already pointing
    at one of them UNTOUCHED — used to keep an internal-feedback WRITE (resolved
    through the transit return path) intact while defaulting the cell's other
    outputs.
    """
    preserve = preserve_dest_regs or set()
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        opcode = word & 0xF000
        if opcode not in (_WRITE, _JUMP):
            continue
        if opcode == _WRITE and (word & 0x1F) in preserve:
            continue  # keep the feedback WRITE's resolved hop + dest
        if opcode == _JUMP and ("jump", word & 0x1F) in preserve:
            continue  # keep a backward return-kick JUMP (resolved by the
            # internal-feedback pass; tagged ("jump", entry) to keep the
            # WRITE-register namespace separate)
        word = (word & ~(0x1F << 5)) | (_HOP1_CNT << 5)
        target = dest if opcode == _WRITE else entry
        if target is not None:
            word = (word & ~0x1F) | (int(target) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _apply_block_cell_faces(cell_map, blocks: list) -> None:
    """Apply each block's AUTHORED per-cell output face from the model placement.

    The model ``PlacedCell.face`` carries the block's default_layout face (plus
    any user rotation/mirror). With the Router's I/O routing skipped, nothing
    else sets a block's *output* exit-cell face, so copy the authored faces onto
    the routed CellMap as the baseline. Drawn routes and abutment defaulting run
    afterwards and override the exit cell where a real connection dictates.
    """
    from model.placement import is_transit_cell

    for blk in blocks:
        if blk.placement is None:
            continue
        for pc in blk.placement.cells:
            face = getattr(pc, "face", None)
            if face is None:
                continue
            code = _PORT_FACE_CODE.get(getattr(face, "value", face))
            if code is None and hasattr(face, "name"):
                code = {"SOUTH": 0, "EAST": 1, "WEST": 2,
                        "NORTH": 3}.get(face.name)
            if code is None:
                continue
            cfg = cell_map.get_cell(pc.x, pc.y)
            if cfg is None:
                # A block-INTERNAL face-only transit cell (layout_rules §5: a
                # first-class block cell with a face and NO program) has no
                # router-created entry — MATERIALIZE it as a routing cell so
                # its authored face reaches the fabric (and the universal
                # routing-program pass covers it like any corridor cell).
                # Without this, a forward-corridor transit (the LMS equalizer's
                # westbound lane) stays at the reset face and silently deflects
                # every transiting word. (The Costas feedback transit never hit
                # this — the feedback TRACER created its entry.)
                if is_transit_cell(pc):
                    cell_map.add_routing_cell(pc.x, pc.y, _CM_FACE(code),
                                              block_name=blk.name)
                continue
            cfg.fwd_face = _CM_FACE(code)


def _reassert_internal_forward_faces(cell_map, blocks: list, gr_blocks: dict) -> None:
    """Restore the AUTHORED per-cell face on cells that FORWARD internally to the
    next cell of their own block, so an incoming inter-block route can't hijack the
    internal wavefront direction (the multi-cell complex-FIR landing cell both
    receives a packet AND forwards to its cell m+1). Only cells that are the SOURCE
    of an ``internal_connections`` handoff are touched — pure output/egress cells,
    whose fwd_face legitimately follows the route, are left alone."""
    from model.connection import BlockEndpoint  # noqa: F401 — parity with siblings

    for blk in blocks:
        if blk.placement is None or not blk.placement.cells:
            continue
        gb = gr_blocks.get(blk.name)
        if gb is None:
            continue
        try:
            internal = list(gb.internal_connections() or [])
        except Exception:  # noqa: BLE001 — no internal handoffs → nothing to guard
            continue
        if not internal:
            continue
        # Cell ids that SOURCE an internal handoff. A block authors these as EITHER
        # an integer index into ``placement.cells`` OR a STRING cell name (the
        # ComplexMixer et al. use named cells: ('phase', ...)). Collect both forms;
        # resolving ONLY the int form silently no-ops for named-cell blocks (their
        # internal-forwarding input cell then keeps whatever face the incoming route
        # last stamped — SOUTH toward the corridor instead of the authored internal
        # direction — so the block's wavefront dies at some orientations).
        fwd_src_ids = {s for (s, _sp, _d, _dp) in internal
                       if isinstance(s, (int, str))}
        cells = blk.placement.cells
        # Resolve each source id to its placement cell: an int is a direct index; a
        # string matches the cell's ``cell_id`` name.
        by_name = {getattr(pc, "cell_id", None): pc for pc in cells}
        for sid in fwd_src_ids:
            if isinstance(sid, int):
                pc = cells[sid] if 0 <= sid < len(cells) else None
            else:
                pc = by_name.get(sid)
            if pc is None:
                continue
            face = getattr(pc, "face", None)
            if face is None:
                continue
            cfg = cell_map.get_cell(pc.x, pc.y)
            if cfg is None:
                continue
            code = _PORT_FACE_CODE.get(getattr(face, "value", face))
            if code is None and hasattr(face, "name"):
                code = {"SOUTH": 0, "EAST": 1, "WEST": 2,
                        "NORTH": 3}.get(face.name)
            if code is not None:
                cfg.fwd_face = _CM_FACE(code)


def _apply_orientation_face_words(cell_map, blocks: list, gr_blocks: dict) -> None:
    """Rewrite a block's in-program FACE constants for its orientation.

    A v2 CellProgram may pick an output direction at runtime with
    ``MOVE [FACE], R{data:face_x}`` where ``face_x`` is a DataWord whose VALUE is
    a hardware face code (S=0,E=1,W=2,N=3) and whose ``is_face`` flag is set. That
    code is an ABSOLUTE direction; when the placer rotates/mirrors the block (e.g.
    serpentine auto-orient), the cell's resting ``.face`` is transformed by
    :meth:`Placement.transform`, and these in-program constants must be
    transformed by the SAME D4 map or the block emits in the wrong direction
    (e.g. Gardner's loop_filter sends its `period_fb` away from the resampler).

    For each block with a recorded ``placement.orientation``, find each face
    DataWord's resolved address (it is authored absolute, or auto-packed by the
    resolver) and remap ``cell_map[cell].memory[addr]`` through the orientation.
    """
    from model.enums import face_code_after

    for blk in blocks:
        kinds = list(getattr(blk.placement, "orientation", []) or [])
        if not kinds:
            continue
        gb = gr_blocks.get(blk.name)
        if gb is None:
            continue
        try:
            cps = gb.build_cell_programs()
        except Exception:  # noqa: BLE001
            continue
        pos_of = {pc.cell_id: (pc.x, pc.y) for pc in blk.placement.cells}
        for cid, cp in cps.items():
            # Face DataWords carry an explicit authored address (a face constant
            # at a fixed slot the `MOVE [FACE]` reads), so use it directly.
            face_words = [d for d in getattr(cp, "data", [])
                          if getattr(d, "is_face", False)
                          and d.address is not None]
            if not face_words:
                continue
            pos = pos_of.get(cid)
            if pos is None:
                continue
            cfg = cell_map.get_cell(*pos)
            if cfg is None:
                continue
            for d in face_words:
                addr = d.address
                if addr not in cfg.memory:
                    continue
                word = cfg.memory[addr]
                cfg.memory[addr] = (
                    face_code_after(word & 0x3, kinds) | (word & ~0x3)) & 0xFFFF


_INTERNAL_FACE_WORD = "face_internal"  # FACE constant for the cell's internal handoffs
_TAP_FACE_WORD = "face_tap"            # FACE constant the external-tap WRITE flips to


def _apply_rotate_tap_face(cell_map, gr_placement, blocks, gr_blocks) -> None:
    """Patch a DUAL-FACE cell's ``face_internal`` / ``face_tap`` FACE constants.

    A cell that emits BOTH internal handoffs AND an external "tap" output (e.g. the
    Costas ``rotate``: yi/yq → pd_pi internally AND yi_tap → a downstream bus) can't
    put them on one ``fwd_face`` when the two go DIFFERENT directions — once the bus
    router faces the cell toward the tap, the internal handoffs would chase the bus and
    starve the loop. The cell's program instead flips its output FACE per emit: internal
    handoffs on ``face_internal``, then the tap on ``face_tap``. Both are authored as
    ``is_face`` DataWords (default value = the ComplexCostasLoop layout's WEST); here
    the build sets them from the ACTUAL placement so the SAME shared cell works in every
    layout (e.g. CoherentRXBlock places rotate facing EAST, not WEST):

      * ``face_internal`` = the cell's RESTING / default_layout face (``placement.cell
        (cid).face``) — the direction toward its abutting internal consumer (pd_pi).
      * ``face_tap``      = the cell's ``fwd_face`` AFTER routes/brokers — the route's
        first-hop exit toward the tap bus when this cell is a routed output source, ELSE
        the resting face (a standalone Costas, or a layout where the tap goes the SAME
        way as the internal handoff, e.g. CoherentBPSKRx — harmless).

    The tap WRITE's hop is patched separately by ``_patch_last_write_handoff`` (it is the
    cell's highest-address WRITE). No-op for any cell with neither face word. Runs for
    ALL cells of ALL blocks (not just ``output_cell_id``) so a fused block whose tap is
    on a NON-output cell (CoherentRXBlock taps yi off pd_pi) is also handled."""
    from model.enums import Face

    def _fcode(face):
        if face is None:
            return None
        return _PORT_FACE_CODE.get(getattr(face, "value", face))

    for blk in blocks:
        gb = gr_blocks.get(blk.name)
        if gb is None or blk.placement is None:
            continue
        try:
            cps = gb.build_cell_programs()
        except Exception:  # noqa: BLE001
            continue
        for pc in blk.placement.cells:
            cp = cps.get(pc.cell_id)
            if cp is None:
                continue
            data = getattr(cp, "data", [])
            internal = next((d for d in data
                             if getattr(d, "name", None) == _INTERNAL_FACE_WORD
                             and getattr(d, "is_face", False)
                             and d.address is not None), None)
            tap = next((d for d in data
                        if getattr(d, "name", None) == _TAP_FACE_WORD
                        and getattr(d, "is_face", False)
                        and d.address is not None), None)
            if internal is None and tap is None:
                continue
            cfg = cell_map.get_cell(pc.x, pc.y)
            if cfg is None:
                continue
            rest = _fcode(getattr(pc, "face", None))        # default_layout face
            fwd = getattr(cfg, "fwd_face", None)            # route-overridden face
            fwd = int(fwd) & 0x3 if fwd is not None else rest
            if internal is not None and rest is not None \
                    and internal.address in cfg.memory:
                cfg.memory[internal.address] = (
                    (cfg.memory[internal.address] & ~0x3) | rest) & 0xFFFF
            if tap is not None and fwd is not None \
                    and tap.address in cfg.memory:
                cfg.memory[tap.address] = (
                    (cfg.memory[tap.address] & ~0x3) | fwd) & 0xFFFF


def _apply_rendezvous_input_faces(cell_map, blocks, connections, project,
                                  catalog, gr_blocks) -> None:
    """Reconcile a face-locking rendezvous block's LOCK faces to the ROUTED geometry.

    A block declaring ``NEEDS_DISTINCT_INPUT_FACES`` (the DualFloatToComplex, the
    FeaturePairJoin) LOCKs to one arrival FACE at a time to pair two independent async
    streams — the face IS the stream identity. Its program authors two ``is_face``
    DataWords (and boots pre-locked via ``initial_lock_face``) with PLACEHOLDER faces,
    but the router decides the ACTUAL arrival geometry (the placer only guarantees the
    two land on DIFFERENT faces). So here, AFTER routes/brokers have set every corridor's
    faces, patch the built cell's FIRST face word to the face the FIRST input net
    actually arrives on, the SECOND to the second net's face, and the cold-start LOCK
    config to the first — else the LOCK gates the wrong faces and the rendezvous stalls
    (0 egress).

    WHICH PORTS/WORDS: the block declares them as
    ``RENDEZVOUS_FACE_PORTS = ((in_port, face_word), (in_port, face_word))`` in
    FIRST-ACCEPTED order — the port whose face the cell boots locked to comes first.
    A block that does not declare it falls back to the DualFloatToComplex names
    (``i``/``face_i``, ``q``/``face_q``), so the original block is unchanged. Hardcoding
    those names here made this pass a SILENT NO-OP for any other rendezvous block (its
    faces kept the authored placeholders, the LOCK gated the wrong faces, and the chain
    produced ZERO output while building and routing perfectly) — exactly the failure
    mode this pass exists to prevent.

    The arrival face is the direction FROM the rendezvous cell back toward the net's last
    physical waypoint (the word travels waypoint->cell, entering from the opposite side).
    For an ABUTTED input (no route waypoints) it is the direction toward the driver's
    output cell. Runs for every ``NEEDS_DISTINCT_INPUT_FACES`` block on the chip; no-op
    for any other block."""
    from model.connection import BlockEndpoint
    from .bus_router import _phys_pts

    for blk in blocks:
        gb = gr_blocks.get(blk.name)
        if gb is None or blk.placement is None or not blk.placement.cells:
            continue
        if not bool(getattr(gb, "NEEDS_DISTINCT_INPUT_FACES", False)):
            continue
        cell0 = blk.placement.cells[0]
        cx, cy = cell0.x, cell0.y
        cfg = cell_map.get_cell(cx, cy)
        if cfg is None:
            continue
        try:
            cps = gb.build_cell_programs()
        except Exception:  # noqa: BLE001
            continue
        cp = cps.get(cell0.cell_id if hasattr(cell0, "cell_id") else 0)
        if cp is None:
            cp = next(iter(cps.values()), None)
        if cp is None:
            continue
        # face DataWords by name -> resolved address in the built memory.
        fword = {getattr(d, "name", None): d.address
                 for d in getattr(cp, "data", [])
                 if getattr(d, "is_face", False) and d.address is not None}

        def _arrival_face(port_name):
            """The face the net targeting ``port_name`` arrives on at the cell."""
            conn = next((c for c in connections
                         if isinstance(c.target, BlockEndpoint)
                         and c.target.block == blk.name
                         and c.target.port == port_name), None)
            if conn is None:
                return None
            pts = _phys_pts(project, conn, catalog) if conn.is_routed else []
            pts = [p for p in pts if isinstance(p, (tuple, list)) and len(p) == 2]
            if pts:
                last = tuple(pts[-1])
                if last == (cx, cy) and len(pts) >= 2:
                    last = tuple(pts[-2])   # step back off the cell itself
                return _step_face(cx, cy, last[0], last[1])
            # Abutted: face toward the driver's output cell.
            src = getattr(conn.source, "block", None)
            if src is None:
                return None
            db = project.block(src)
            if db is None or db.placement is None or not db.placement.cells:
                return None
            oc = _output_cell(db, catalog)
            if oc is None:
                return None
            return _step_face(cx, cy, oc[0], oc[1])

        # The block names its own (input port, face DataWord) pairs, FIRST-ACCEPTED
        # first; default to the DualFloatToComplex names so that block is unchanged.
        spec = getattr(gb, "RENDEZVOUS_FACE_PORTS",
                       (("i", "face_i"), ("q", "face_q")))
        faces = [(_arrival_face(pn), wn) for (pn, wn) in spec]
        # Patch the built memory face words + cold-start LOCK. Only overwrite the 2-bit
        # face field; leave the rest of the word untouched.
        for f, wn in faces:
            if f is not None and wn in fword and fword[wn] in cfg.memory:
                a = fword[wn]
                cfg.memory[a] = (cfg.memory[a] & ~0x3) | (f & 0x3)
        # Cold-start LOCK boots to the FIRST-accepted stream's face.
        if faces and faces[0][0] is not None:
            cfg.initial_lock_face = faces[0][0] & 0x3


def _net_source_exit_cell(pb, pts, blocks, src_block_name):
    """The (x, y) cell a ROUTED net's source WRITE/JUMP actually lives in.

    Every router anchors an egress route at the net's own output cell
    (``route[0]`` == the source PORT's cell).  For a single-output block that
    is ``pb.exit_cell`` and this returns it unchanged.  A MULTI-OUTPUT block
    (two physically-separate output cells, e.g. the R2Butterfly sum/diff
    pairs) sources different nets from different cells: when ``route[0]`` is a
    DIFFERENT cell of the SAME source block, the exit-face/handoff patches
    must land there — patching the block-level exit cell for every net would
    clobber its other output's egress."""
    ex, ey = pb.exit_cell
    if pts:
        p0 = tuple(pts[0])
        if p0 != (ex, ey):
            b = next((b for b in blocks if b.name == src_block_name), None)
            cells = getattr(getattr(b, "placement", None), "cells", None) or []
            if any((c.x, c.y) == p0 for c in cells):
                return p0
    return (ex, ey)


def _output_cell(blk, catalog):
    """The (x,y) of a block's OUTPUT cell (its first output port's cell), or the last
    placed cell. Used to derive an abutted rendezvous input's arrival face."""
    try:
        from .bus_router import _source_output_cell
        oc = _source_output_cell(blk, None, catalog)
        if oc is not None:
            return oc
    except Exception:  # noqa: BLE001
        pass
    cells = getattr(blk.placement, "cells", None) or []
    if not cells:
        return None
    c = cells[-1]
    return (c.x, c.y)


def _default_unrouted_exit_hops(cell_map, gr_placement, blocks: list,
                                connections: list, gr_blocks: dict,
                                catalog, feedback_blocks: dict | None = None,
                                skip_conns: set | None = None
                                ) -> None:
    """Default the EXIT WRITE/JUMP of an unrouted block to ``@1`` (abutment).

    A block with an outgoing project connection (block→block / block→port) had
    its exit hop computed by the Router from the routed distance — keep that. A
    block with NO outgoing connection got the Router's sink-to-port fallback,
    which is wrong for placeKYT (the port path isn't a configured route and may
    not be Manhattan). Default those to ``@1`` so the output abuts to the next
    cell the user places/routes.

    When a block's landing cell physically ABUTS the exit cell in its output-face
    direction, also resolve the handoff TARGET: the WRITE dest → that block's
    input register, the JUMP entry → its entry address. (Without this the dest
    stays the Router's sink default of 0.)

    Only the EXIT cell is touched — internal multi-cell handoffs are resolved by
    the Router from each cell's forward distance. User per-instruction overrides
    are applied later and still win.
    """
    from model.connection import BlockEndpoint

    # Map each block's LANDING-cell position → its resolved (entry, input_reg).
    landing: dict = {}
    for b in blocks:
        if b.placement is None or not b.placement.cells:
            continue
        entry, in_regs = catalog.resolved_io(b.type, b.params, library=b.library)
        lc = b.placement.cells[0]  # landing/entry cell
        landing[(lc.x, lc.y)] = (entry, in_regs[0] if in_regs else None, b.name)

    # A block whose outgoing connection is ROUTED already had its exit faced +
    # hop set by _apply_routes from the drawn waypoints — leave it. A block with
    # an UNROUTED outgoing connection (placed abutting its target) falls through
    # to @1-abutment defaulting here.
    sourced = {c.source.block for c in connections
               if isinstance(c.source, BlockEndpoint) and c.is_routed}
    fb_blocks = feedback_blocks or {}
    for blk in blocks:
        if blk.name in sourced or blk.placement is None:
            continue
        # A block that authors its OWN output WRITE/JUMP hops (e.g. an SRAM
        # controller emitting the panel register protocol, or a crossover relay)
        # opts out of @1-abutment defaulting via RAW_OUTPUT_HOPS — its literal
        # @N hops must survive the build untouched.
        gb = gr_blocks.get(blk.name)
        if gb is not None and getattr(gb, "RAW_OUTPUT_HOPS", False):
            continue
        pb = gr_placement.placed_blocks.get(blk.name)
        if pb is None:
            continue
        ex, ey = pb.exit_cell
        cfg = cell_map.get_cell(ex, ey)
        # Resolve the abutting target. Prefer the exit cell's current output
        # face; if that direction has no abutting block, search the 4 neighbours
        # for one and re-face the exit cell toward it (the Router no longer auto-
        # routes, so an abutting source must find + face its neighbour here).
        dest = entry = None
        fwd = getattr(cfg, "fwd_face", None) if cfg is not None else None
        if fwd is not None and int(fwd) in _FWD_DELTA:
            dx, dy = _FWD_DELTA[int(fwd)]
            tgt = landing.get((ex + dx, ey + dy))
            if tgt is not None and tgt[2] != blk.name:
                entry, dest, _ = tgt
        if entry is None and dest is None:
            for code, (dx, dy) in _FWD_DELTA.items():
                tgt = landing.get((ex + dx, ey + dy))
                if tgt is not None and tgt[2] != blk.name:  # not our own cell
                    entry, dest, _ = tgt
                    if cfg is not None:
                        cfg.fwd_face = _CM_FACE(code)   # face the neighbour
                    break
        if cfg is not None:
            # PRESERVE any internal-feedback WRITE at this exit cell (its hop +
            # dest were already resolved through the transit return path); only
            # default the cell's OTHER outputs (e.g. the Gardner loop_filter's
            # real `out` + local `trig`, alongside its period feedback WRITE).
            preserve = {reg for (pos, reg) in fb_blocks.get(blk.name, set())
                        if pos == (ex, ey)}
            _set_cell_hop1(cfg, dest=dest, entry=entry,
                           preserve_dest_regs=preserve)


def _patch_config_write(cfg, hop, cfg_addr) -> bool:
    """Patch the cell's WRITE.CFG (opcode WRITE + config bit set) to ``@hop`` with
    dest ``cfg_addr`` — used to (re)assert a CONFIG-only backward write (the
    ComplexMixer serialize-LOCK unlock cell's ``WRITE.CFG @N, LOCK``) whose dest
    the router's sink-default may have clobbered. Matches on the config bit alone
    (not the current dest), so it recovers a rewritten word. Returns True if patched."""
    hop_cnt = encode_hop_cnt(hop)
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        if (word & 0xF000) != _WRITE or not (word & _WRITE_CONFIG_BIT):
            continue
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(cfg_addr) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF
        return True
    return False


def _patch_last_write_handoff(cfg, hop, dest=None) -> None:
    """Patch ONLY the highest-address WRITE instruction in ``cfg`` to ``hop`` (and
    optional ``dest``). Used when a block's OUTPUT leaves a mid-block cell that
    ALSO carries internal handoffs: the block emits the output WRITE LAST, so the
    highest-address WRITE is the one bound for the output port — patch just that,
    leaving the earlier internal-handoff WRITEs (already resolved to their @1 hops)
    intact."""
    hop_cnt = encode_hop_cnt(hop)
    write_addrs = [a for a, w in cfg.memory.items()
                   if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE]
    if not write_addrs:
        return
    addr = max(write_addrs)
    word = cfg.memory[addr]
    word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
    if dest is not None:
        word = (word & ~0x1F) | (int(dest) & 0x1F)
    cfg.memory[addr] = word & 0xFFFF


def _patch_last_n_write_handoff(cfg, hop, n, base_tag=0) -> None:
    """Patch the ``n`` HIGHEST-address WRITEs to ``hop`` and give them CONSECUTIVE
    output tags ``base_tag, base_tag+1, …`` in ADDRESS order (the emit/rail order).

    This is the COMPLEX-OUTPUT-to-PORT analogue of :func:`_patch_last_write_handoff`
    for a mid-block output cell that ALSO carries internal handoffs. A fused
    output+PD cell (the order-4 Costas ``qpd``: an internal ``err``→pd_pi WRITE FIRST,
    then the recovered ``yi_tap``, ``yq_tap`` tail) must NOT be patched by
    :func:`_patch_complex_output_port_handoff` (that patches EVERY WRITE, including the
    ``err`` handoff → err would egress the port and the loop would break). Patching
    just the last ONE WRITE (plain last-write) routes only ``yq_tap`` and leaves
    ``yi_tap`` on a stale internal hop that COLLIDES with the ``err`` handoff (the
    recovered I is lost). Patching the last ``n`` (n = the block's output-register
    count) steers BOTH tap rails to the port with distinct tags — mirroring the input
    complex-sample contract (xi→a0, xq→a1) — while the earlier internal ``err`` WRITE
    keeps its @1 hop. The JUMP is patched separately (``_patch_last_jump_handoff``)."""
    if n <= 1:
        return _patch_last_write_handoff(cfg, hop, dest=int(base_tag))
    hop_cnt = encode_hop_cnt(hop)
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE
                         and not (w & _WRITE_CONFIG_BIT))  # skip a lock-clear WRITE.CFG
    tail = write_addrs[-n:]
    for k, addr in enumerate(tail):
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | ((int(base_tag) + k) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _patch_fanout_source_handoff(cfg, specs) -> None:
    """Re-sequence a COMPLEX output cell (``WRITE yi; WRITE yq; JUMP``) into the
    FAN-OUT form (``WRITE yi; WRITE yq; JUMP→A; JUMP→B``) so its two rails reach TWO
    DIFFERENT downstream blocks (INV-17).

    ``specs`` is ``[(hop, dest_reg, entry), …]`` in SOURCE PROGRAM ORDER (rail 0 =
    the first WRITE = yi). The Nth WRITE is steered to spec N's ``(hop, dest_reg)``.
    The cell has ONE authored JUMP; we repurpose it for the LAST rail and add ONE
    extra JUMP (in the cell's single free program word) for the first rail. Both
    WRITEs deliver their data BEFORE either JUMP fires, and the two rails go to
    DIFFERENT broker cells (distinct hops), so neither clobbers the other and the
    JUMP ORDER is irrelevant — each downstream fires independently once its operand
    is present. Only the fan-out (2-different-targets) case reaches here; the
    complex-packet path is untouched.

    Budget: the extra JUMP needs one free word. INV-17 requires every complex-output
    block to be VERIFIED to have that word, so this never overflows at build time; as
    a defensive backstop we still raise if no free program word exists (never silent).
    """
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE)
    jump_addrs = sorted(a for a, w in cfg.memory.items()
                        if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP)
    if len(specs) < 2 or len(write_addrs) < 2 or not jump_addrs:
        return  # not the shape we transform — leave it to the single-net path

    def _set_write(addr, hop, dest):
        w = (cfg.memory[addr] & ~(0x1F << 5)) | (encode_hop_cnt(hop) << 5)
        w = (w & ~0x1F) | (int(dest) & 0x1F)
        cfg.memory[addr] = w & 0xFFFF

    def _make_jump(hop, entry):
        return (_JUMP | (encode_hop_cnt(hop) << 5) | (int(entry) & 0x1F)) & 0xFFFF

    # Steer each rail's WRITE (in program order) to its own broker burst reg + hop.
    # WRITE order is harmless — both operands land at their DISTINCT brokers before
    # any JUMP fires, so a rail's data is present regardless of WRITE sequence.
    for i, (hop, dest_reg, _entry) in enumerate(specs):
        if i < len(write_addrs):
            _set_write(write_addrs[i], hop, dest_reg)

    # JUMP ORDER IS NOT HARMLESS (fan-out FACE-transit hazard). The rails share ONE
    # backbone: a FARTHER rail's trigger JUMP TRANSITS THROUGH the nearer rail's broker
    # cell. If the nearer broker's trigger fires FIRST, that broker flips its output FACE
    # toward ITS delivery target to relay — and the farther rail's JUMP, transiting while
    # that FACE is diverted, is mis-routed into the nearer broker's target instead of
    # continuing down the bus. The farther rail's broker then has its data but NEVER its
    # trigger, so that whole rail silently drops (the mixer.yi→LowPass_I 0-output bug:
    # ri's trigger was swallowed EAST at the rq broker mid-delivery). FIX: fire the rails
    # in DESCENDING hop order — the farthest trigger executes FIRST, transiting every
    # nearer broker while those brokers are still IDLE (FACE = bus/through direction), so
    # each nearer trigger only diverts its OWN broker AFTER the farther JUMPs have already
    # passed. Execution order is ASCENDING ADDRESS (the authored JUMP sits at the lowest
    # JUMP address and runs first; extra JUMPs are placed ABOVE it and run later), so the
    # AUTHORED JUMP gets the LARGEST hop and each successive extra JUMP a smaller one.
    by_hop = sorted(specs, key=lambda s: s[0], reverse=True)  # farthest first
    old_jump = jump_addrs[-1]
    first_hop, _fr, first_entry = by_hop[0]
    cfg.memory[old_jump] = _make_jump(first_hop, first_entry)

    # Place one extra JUMP per remaining rail in free program words above ``old_jump``
    # (which execute AFTER it), in the same descending-hop order.
    free = [a for a in range(old_jump + 1, 32)
            if (cfg.memory.get(a, 0) & 0xFFFF) == 0]
    need = len(specs) - 1
    if len(free) < need:
        raise BuildAbort(
            f"complex output cell '{getattr(cfg, 'block_name', '?')}' cannot fan out: "
            f"needs {need} free program word(s) for the extra rail JUMP(s) but has "
            f"{len(free)} — the block's output cell is over-full. INV-17: a complex-"
            f"output block MUST leave room for the fan-out form and be VERIFIED for it "
            f"at block-verification time, so this never surfaces at chip build.")
    for slot, (hop, _dr, entry) in zip(free, by_hop[1:]):
        cfg.memory[slot] = _make_jump(hop, entry)


def _patch_single_rail_multi_handoff(cfg, writes, jumps) -> None:
    """Rewrite a SINGLE-rail source exit tail so its one R0 value reaches N
    destinations — the block-OUTPUT FAN-OUT form for ordinary (non-complex)
    blocks (``gain.out → add.a0 AND add.a1``, splitter trees, …).

    A single-rail block authors exactly ``… WRITE; JUMP``: the value is in R0
    when the WRITE fires and NOTHING between the WRITE and JUMP touches R0, so
    the WRITE word can simply be REPLICATED — every copy emits the same R0.
    The tail becomes ``WRITE₁ … WRITE_N; JUMP …`` over the authored WRITE word,
    the authored JUMP word, and the free (zero) words after it (execution runs
    into them and halts at the first zero word, exactly the INV-17 fan-out
    convention).

    ``writes`` = ``[(hop, dest_reg), …]`` one per destination; ``jumps`` =
    ``[(hop, entry), …]`` — ONE for a packet (N regs on one target), or one
    per rail for a fan-out, in DESCENDING-hop order (the INV-17 FACE-transit
    rule: the farthest trigger fires first, transiting nearer brokers while
    they are still idle). All data words go out before any trigger, so no
    trigger-diverted broker can mis-route a later rail's data.

    Raises a NAMED error when the tail cannot hold the form (over-full cell,
    or an authored program whose JUMP is not directly after its WRITE) — the
    fix is routing through an explicit relay (kyttar_splitter)."""
    name = getattr(cfg, "block_name", "?")
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE)
    jump_addrs = sorted(a for a, w in cfg.memory.items()
                        if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP)
    if len(write_addrs) != 1 or len(jump_addrs) != 1:
        raise BuildAbort(
            f"output cell '{name}' fans out to {len(writes)} destinations but "
            f"authors {len(write_addrs)} WRITEs/{len(jump_addrs)} JUMPs — the "
            f"single-rail fan-out form needs exactly one of each. Route the "
            f"extra arms through an explicit splitter (kyttar_splitter).")
    w0, j0 = write_addrs[0], jump_addrs[0]
    if j0 != w0 + 1:
        raise BuildAbort(
            f"output cell '{name}' cannot take the fan-out form: instruction(s) "
            f"between its exit WRITE and JUMP could retarget R0, so the WRITE "
            f"cannot be replicated. Route through an explicit splitter "
            f"(kyttar_splitter).")
    slots = [w0, j0]
    a = j0 + 1
    while a < 32 and (cfg.memory.get(a, 0) & 0xFFFF) == 0:
        slots.append(a)
        a += 1
    need = len(writes) + len(jumps)
    if len(slots) < need:
        raise BuildAbort(
            f"output cell '{name}' cannot fan out to {len(writes)} destinations: "
            f"the form needs {need} exit words but only {len(slots)} are "
            f"available — route some arms through an explicit splitter "
            f"(kyttar_splitter).")
    base_write = cfg.memory[w0]
    seq = []
    for hop, dest in writes:
        w = (base_write & ~(0x1F << 5)) | (encode_hop_cnt(hop) << 5)
        w = (w & ~0x1F) | (int(dest) & 0x1F)
        seq.append(w & 0xFFFF)
    for hop, entry in jumps:
        seq.append((_JUMP | (encode_hop_cnt(hop) << 5)
                    | (int(entry) & 0x1F)) & 0xFFFF)
    for addr, word in zip(slots, seq):
        cfg.memory[addr] = word


def _patch_complex_source_handoff(cfg, hop, burst_regs, entry) -> None:
    """Patch a COMPLEX-SAMPLE source exit cell that emits N WRITEs + 1 JUMP.

    A complex-sample source (e.g. the MF i4 emitting yi then yq) WRITEs each operand
    to a DISTINCT broker burst reg and fires ONE JUMP into the broker's coalesced
    deliver entry. The WRITEs are patched IN ADDRESS ORDER (which is the program /
    emit order) to ``burst_regs[0]``, ``burst_regs[1]``, …; every JUMP (normally one)
    is patched to ``entry``. All get the same ``@hop`` (one route to the broker).

    This is the broker counterpart of the input-port complex-sample contract: the
    target then fires ONCE per sample with all operands fresh in its own registers."""
    hop_cnt = encode_hop_cnt(hop)
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE)
    jump_addrs = sorted(a for a, w in cfg.memory.items()
                        if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP)
    for i, addr in enumerate(write_addrs):
        reg = burst_regs[i] if i < len(burst_regs) else burst_regs[-1]
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(reg) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF
    for addr in jump_addrs:
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _patch_complex_packet_last_handoff(cfg, hop, burst_regs, entry) -> None:
    """COMPLEX-PACKET variant of :func:`_patch_complex_source_handoff` for an output
    cell that ALSO carries INTERNAL handoffs (``_output_cell_carries_handoffs``).

    The order-4 (QPSK) Costas ``qpd`` cell is BOTH the loop's phase detector — it
    WRITEs ``err`` to ``pd_pi`` @1 and JUMPs ``trig``→pd_pi internally — AND the
    block's complex output (``yi_tap``/``yq_tap`` WRITEs + ``tap_trig`` JUMP). It
    emits the EXTERNAL rails LAST (after the internal handoffs), so patching EVERY
    WRITE/JUMP (what :func:`_patch_complex_source_handoff` does for a PURE output cell
    like the MF ``i4``) would clobber the internal ``err``/``trig`` and the Costas
    loop never fires (pd_pi silent). Patch ONLY the LAST ``len(burst_regs)`` WRITEs
    (the yi_tap/yq_tap rails, in program order → broker burst regs) and the LAST JUMP
    (tap_trig → the broker deliver entry), leaving the earlier internal handoffs at
    their already-resolved @1 hops.

    This is the complex-packet counterpart of the SINGLE-net
    :func:`_patch_last_write_handoff`/:func:`_patch_last_jump_handoff` pair — the
    same "patch only the tail" treatment, but for the 2-rail packet form.

    A ``WRITE.CFG`` (config bit set) is SKIPPED when selecting the tail: a
    serialize-LOCKED block (NCO/FM, INV-20) places its backward lock-clear
    ``WRITE.CFG`` AFTER the yi/yq rails, so counting it as a "last WRITE"
    steered the CFG word down the data corridor (a stray config write at the
    broker), left yi unpatched at @1, and delivered the pair SHIFTED one rail
    (the downstream block read (yq, 0) — the locked-FM→ComplexGain zero-Q bug,
    2026-08-16). Same treatment as :func:`_patch_last_write_handoff` /
    :func:`_patch_complex_output_port_handoff` (both already skip it)."""
    hop_cnt = encode_hop_cnt(hop)
    write_addrs = sorted(a for a, w in cfg.memory.items()
                         if _is_instruction_addr(cfg, a) and (w & 0xF000) == _WRITE
                         and not (w & _WRITE_CONFIG_BIT))
    jump_addrs = sorted(a for a, w in cfg.memory.items()
                        if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP)
    n = len(burst_regs)
    # The external rail WRITEs are the LAST n DATA WRITEs (after the internal ones).
    for i, addr in enumerate(write_addrs[-n:]):
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(burst_regs[i]) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF
    # The external trigger is the LAST JUMP (tap_trig); the internal trig JUMP is left
    # untouched at its @1 abutment hop to pd_pi.
    if jump_addrs:
        addr = jump_addrs[-1]
        word = cfg.memory[addr]
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF


def _patch_one_handoff(cfg, opcode_wanted, dst_reg, hop, *, entry=None,
                       config=None) -> bool:
    """Patch the SINGLE WRITE (or JUMP) instruction in ``cfg`` whose low-5-bit
    field already equals ``dst_reg`` — setting its ``@N`` ``hop`` (and, for a
    JUMP, its ``entry``). Returns True if a matching instruction was patched.

    Unlike :func:`_set_cell_hop1`/:func:`_patch_cell_handoff` (which rewrite
    EVERY WRITE/JUMP in the cell to one hop), this touches exactly one
    instruction — needed for a cell that emits BOTH a feedback output and a
    local terminate (e.g. the Costas pd_pi cell: its dphase WRITE feeds back @8
    while its trig JUMP stays a local terminate).

    ``config`` (WRITE only) disambiguates a data WRITE from a ``WRITE.CFG``: when
    True, match ONLY words with the config bit set (dest names a CONFIG addr, e.g.
    the pipeline-interlock lock-clear ``WRITE.CFG @N, 4``); when False, ONLY plain
    data WRITEs; when None (default) either matches. This lets the feedback pass
    patch the lock-clear WRITE.CFG's hop to the SAME resolved corridor as the data
    feedback WRITE without touching an unrelated data WRITE to the same reg."""
    hop_cnt = encode_hop_cnt(hop)
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        if (word & 0xF000) != opcode_wanted:
            continue
        if (word & 0x1F) != (int(dst_reg) & 0x1F):
            continue
        if config is not None and opcode_wanted == _WRITE:
            if bool(word & _WRITE_CONFIG_BIT) != bool(config):
                continue
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        if opcode_wanted == _JUMP and entry is not None:
            word = (word & ~0x1F) | (int(entry) & 0x1F)
        cfg.memory[addr] = word & 0xFFFF
        return True
    return False


def _trace_transit_hops(cell_map, start, goal, max_hops=64):
    """Follow ``fwd_face`` links from ``start`` until reaching ``goal``.

    Returns the number of cells traversed (``@N`` to LAND in ``goal``), or None
    if the trace dead-ends / loops / overruns. Used to measure a block-internal
    FEEDBACK return path that runs through the block's own transit cells (the
    cells must already be in the cell_map with their faces set)."""
    pos = start
    visited = set()
    for dist in range(1, max_hops + 1):
        cfg = cell_map.get_cell(pos[0], pos[1])
        if cfg is None or cfg.fwd_face is None:
            return None
        if pos in visited:
            return None  # loop
        visited.add(pos)
        dx, dy = _FWD_DELTA[int(cfg.fwd_face)]
        pos = (pos[0] + dx, pos[1] + dy)
        if pos == goal:
            return dist
    return None


def _trace_feedback_via_transit(cell_map, src_pos, goal, transit_pos,
                                max_hops=64):
    """Measure a block's feedback return path when the SOURCE cell's ``fwd_face``
    can't be followed (it was route-overridden toward the cell's `out` egress).

    A dual-face output cell emits its feedback via an in-program FACE flip toward
    one of the block's FACE-only transit cells — NOT via its (out-bound) resting
    fwd_face. Find the transit cell ADJACENT to ``src_pos`` (the feedback's first
    hop), then follow the authored transit faces (which are never route-overridden)
    to ``goal``. Returns the ``@N`` hop count to LAND in ``goal`` (counting the
    source→transit hop as 1), or None if no adjacent transit cell / no path."""
    # The transit cell abutting the source in any of the 4 directions is the
    # feedback's first hop (the in-program face_fb points at it).
    sx, sy = src_pos
    first = None
    for (dx, dy) in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        nb = (sx + dx, sy + dy)
        if nb in transit_pos:
            first = nb
            break
    if first is None:
        return None
    if first == goal:
        return 1
    # Follow the transit faces from `first` onward (+1 for the source→first hop).
    rest = _trace_transit_hops(cell_map, first, goal, max_hops=max_hops)
    return None if rest is None else rest + 1


def _resolve_state_reg(dst_cp, state_name):
    """The register a named STATE var of a v2 CellProgram resolves to (for an
    internal feedback that targets a persistent state reg, not an input port).
    Returns None if unavailable."""
    try:
        from gr_kyttar.placement.resolver import CellProgramResolver
        regs = CellProgramResolver().compute_state_registers(dst_cp)
        return regs.get(state_name)
    except Exception:  # noqa: BLE001
        return None


def _apply_internal_feedback(cell_map, gr_placement, blocks, gr_blocks,
                             catalog) -> set:
    """Close a block's INTERNAL feedback (e.g. Costas pd_pi → phase) through the
    block's own transit cells.

    A recovery loop's feedback is intrinsic to the block: it ALWAYS returns to
    the same block's own cell, never to another block, and is never user-routed.
    placeKYT's default routing treats the last cell's output as a block EXIT
    (@1-abutment) — wrong for a feedback that loops ~N cells back. This pass:

      1. Materializes the block's FACE-only transit cells into the cell_map so
         the return path is traceable.
      2. For each internal_connection that runs BACKWARD (dst cell precedes src
         in the chain), traces src → dst along the transit faces, resolves the
         dst input register, and patches the src cell's matching WRITE to that
         hop.

    Returns the set of block names whose EXIT output was consumed by feedback,
    so :func:`_default_unrouted_exit_hops` skips @1-defaulting them.
    """
    feedback_blocks: dict = {}
    for blk in blocks:
        gb = gr_blocks.get(blk.name)
        if gb is None or blk.placement is None:
            continue
        ic = getattr(gb, "internal_connections", None)
        conns = ic() if callable(ic) else []
        if not conns:
            continue

        # 1. Add this block's transit cells to the cell_map (FACE only, no prog).
        for t in getattr(blk.placement, "transit_cells", []):
            if cell_map.get_cell(t.x, t.y) is None:
                code = _PORT_FACE_CODE.get(getattr(t.face, "value", t.face))
                if code is None and hasattr(t.face, "name"):
                    code = {"SOUTH": 0, "EAST": 1, "WEST": 2,
                            "NORTH": 3}.get(t.face.name)
                if code is not None:
                    cell_map.add_routing_cell(t.x, t.y, _CM_FACE(code),
                                              block_name=f"{blk.name}._fb")

        # Map cell_id -> (x, y) and -> chain position.
        pos_of = {pc.cell_id: (pc.x, pc.y) for pc in blk.placement.cells}
        # This block's FACE-only transit (feedback return) cell positions.
        transit_pos = {(t.x, t.y)
                       for t in getattr(blk.placement, "transit_cells", [])}
        try:
            cps = gb.build_cell_programs()
        except Exception:  # noqa: BLE001
            continue
        order = list(cps.keys())
        idx_of = {cid: i for i, cid in enumerate(order)}

        # 2. Resolve each BACKWARD internal connection (the feedback edges).
        for (src_cid, _src_port, dst_cid, dst_port) in conns:
            if src_cid not in idx_of or dst_cid not in idx_of:
                continue
            if idx_of[dst_cid] >= idx_of[src_cid]:
                continue  # forward handoff — the resolver already set it
            src_pos = pos_of.get(src_cid)
            dst_pos = pos_of.get(dst_cid)
            if src_pos is None or dst_pos is None:
                continue
            hops = _trace_transit_hops(cell_map, src_pos, dst_pos)
            if hops is None:
                # The source cell's fwd_face may have been route-overridden (a
                # DUAL-FACE output cell — e.g. Gardner's loop_filter — emits its
                # `out` toward a drawn bus route AND its feedback via an in-program
                # FACE flip toward a DIFFERENT face). In that case following the
                # source's (now out-bound) fwd_face misses the feedback lane. Start
                # the trace from the block's own feedback transit cell that abuts
                # the source, then follow the authored transit faces (+1 for the
                # source→transit hop). The transit faces are NEVER route-overridden
                # (they carry no project connection), so this finds the real return
                # path.
                hops = _trace_feedback_via_transit(cell_map, src_pos, dst_pos,
                                                   transit_pos)
            if hops is None and (abs(src_pos[0] - dst_pos[0])
                                 + abs(src_pos[1] - dst_pos[1])) == 1:
                # DIRECT-ABUTMENT feedback: src and dst are edge-adjacent and the
                # src emits the return via an in-program FACE flip toward dst (the
                # dual-face idiom), so neither the fwd_face trace nor a transit
                # trace applies — the corridor is the 1-hop abutment itself,
                # rigid under D4 (e.g. the ChirpGenerator emit -> sweep kick).
                hops = 1
            if hops is None:
                continue  # no traceable return path — leave as-is, don't guess
            cfg = cell_map.get_cell(*src_pos)
            if cfg is None:
                continue
            # A CONFIG-ONLY backward edge (src port ``unlock``) carries NO data — it
            # only clears the dst cell's arbiter LOCK (the ComplexMixer serialize-LOCK:
            # a dedicated `unlock` cell whose sole output is a backward ``WRITE.CFG @N,
            # LOCK``). There is no data WRITE and no dst register to resolve; patch the
            # WRITE.CFG's hop directly and record the cell so the exit-default preserves
            # it. This is a pure fan-in interlock, distinct from a data-feedback loop.
            if _src_port == "unlock":
                # The unlock cell's SOLE output is the backward WRITE.CFG. The router's
                # sink-default may have already rewritten its dest (LOCK addr -> 0) and
                # hop while treating this last-placed cell as a port sink. RESTORE it:
                # find the cell's WRITE.CFG (opcode WRITE + config bit) and set BOTH the
                # resolved corridor hop AND dest = _LOCK_CFG_ADDR (the LOCK register),
                # regardless of the current (possibly clobbered) dest. Then record the
                # cell so the exit-default preserves it.
                if _patch_config_write(cfg, hops, _LOCK_CFG_ADDR):
                    feedback_blocks.setdefault(blk.name, set()).add(
                        (src_pos, _LOCK_CFG_ADDR))
                continue
            # Resolve the dst register: a feedback may target an INPUT port (e.g.
            # Costas dphase) OR an internal STATE var (e.g. Gardner `period` — a
            # persistent, init-valued state reg the loop filter overwrites). Try
            # inputs first, then the resolved state allocation.
            dst_cp = cps[dst_cid]
            dst_reg = None
            for p in getattr(dst_cp, "inputs", []):
                if p.name == dst_port:
                    dst_reg = p.register
                    break
            if dst_reg is None:
                dst_reg = _resolve_state_reg(dst_cp, dst_port)
            if dst_reg is None:
                continue
            # Patch the src cell's WRITE that targets dst_reg to @hops.
            if _patch_one_handoff(cfg, _WRITE, dst_reg, hops, config=False):
                # Record (exit_cell_pos, feedback_dst_reg) so the exit-default
                # below PRESERVES this feedback WRITE while still defaulting the
                # cell's OTHER outputs (e.g. the Gardner loop_filter also emits a
                # real `out` + a local `trig`).
                feedback_blocks.setdefault(blk.name, set()).add(
                    (src_pos, int(dst_reg) & 0x1F))
                # PIPELINE-INTERLOCK lock-clear: if this feedback source ALSO carries
                # a backward ``WRITE.CFG @N, LOCK`` (the Costas pd_pi clearing the
                # phase cell's arbiter LOCK so the next SATURATED sample is released),
                # patch it to the SAME resolved corridor ``hops`` — it rides the same
                # transit path to the same dst cell. Authored ``@1`` is a placeholder;
                # a fixed hop deadlocks any layout whose feedback corridor differs
                # (standalone Costas @2 vs CoherentRX @8). ``_LOCK_CFG_ADDR`` (4) is the
                # CONFIG LOCK register; the WRITE.CFG's dest low-5 = 4, config bit set.
                _patch_one_handoff(cfg, _WRITE, _LOCK_CFG_ADDR, hops, config=True)

        # 3. Resolve each BACKWARD internal JUMP (a self-paced RETURN KICK — e.g.
        # the ChirpGenerator emit cell firing the sweep cell's `iternext` entry
        # once a sample's yi/yq pair has fully left the pipeline). The router
        # already resolved the jump's ENTRY by name and its hop by the trace/
        # manhattan fallback; here we re-derive the hop from the placed corridor
        # (transit trace, else the 1-hop direct abutment) and RECORD the jump as
        # (src_pos, ("jump", entry)) so the exit-default (_set_cell_hop1)
        # PRESERVES it — an exit cell's jumps are otherwise rewritten to the
        # abutting consumer's entry, which would silently kill the iteration.
        # Inert for every block that declares no backward internal jump (the
        # whole prior catalog).
        ij = getattr(gb, "internal_jumps", None)
        jlist = ij() if callable(ij) else []
        for (jsrc, _jport, jdst, jentry) in jlist:
            if jsrc not in idx_of or jdst not in idx_of:
                continue
            if idx_of[jdst] >= idx_of[jsrc]:
                continue  # forward trigger — the resolver already set it
            src_pos = pos_of.get(jsrc)
            dst_pos = pos_of.get(jdst)
            if src_pos is None or dst_pos is None:
                continue
            hops = _trace_transit_hops(cell_map, src_pos, dst_pos)
            if hops is None:
                hops = _trace_feedback_via_transit(cell_map, src_pos, dst_pos,
                                                   transit_pos)
            if hops is None and (abs(src_pos[0] - dst_pos[0])
                                 + abs(src_pos[1] - dst_pos[1])) == 1:
                hops = 1  # direct abutment (in-program FACE flip toward dst)
            if hops is None:
                continue
            cfg = cell_map.get_cell(*src_pos)
            if cfg is None:
                continue
            try:
                from gr_kyttar.placement.resolver import CellProgramResolver
                entry_addr = CellProgramResolver().compute_entry_addresses(
                    cps[jdst]).get(jentry)
            except Exception:  # noqa: BLE001
                entry_addr = None
            if entry_addr is None:
                continue
            # RESTORE, don't just re-hop: the routed-exit patch pass rewrites
            # EVERY jump in the exit cell to the output corridor (hop + the
            # consumer's entry), so matching the kick by its resolved entry can
            # fail — its dest may already be clobbered. The kick is authored as
            # the cell's LAST jump (after the yi/yq tail — the one-sample-at-a-
            # time contract), so restore the HIGHEST-ADDRESS JUMP instruction to
            # the corridor hop + the named backward entry, unconditionally.
            jaddrs = [a for a, w in cfg.memory.items()
                      if _is_instruction_addr(cfg, a) and (w & 0xF000) == _JUMP]
            if not jaddrs:
                continue
            ka = max(jaddrs)
            hop_cnt = encode_hop_cnt(hops)
            cfg.memory[ka] = (_JUMP | (hop_cnt << 5)
                              | (int(entry_addr) & 0x1F)) & 0xFFFF
            feedback_blocks.setdefault(blk.name, set()).add(
                (src_pos, ("jump", int(entry_addr) & 0x1F)))
    return feedback_blocks


def _apply_instr_overrides(cell_map, blocks: list) -> dict:
    """Patch per-instruction WRITE/JUMP overrides into the routed CellMap (§3.3).

    Each block carries ``placement.instr_overrides`` keyed by ``cell_id`` then by
    instruction ``addr``. We translate ``cell_id`` to its physical ``(x, y)`` via
    the placed-cell list and patch the matching memory word.

    Returns an OWNERSHIP map ``{(x, y): (block_name, cell_id)}`` for every block
    cell (whether overridden or not) so the Inspector can correlate a physical
    cell back to its block + instruction overrides.
    """
    ownership: dict = {}
    for blk in blocks:
        if blk.placement is None:
            continue
        for pc in blk.placement.cells:
            ownership[(pc.x, pc.y)] = (blk.name, pc.cell_id)
        for cid, by_addr in blk.placement.instr_overrides.items():
            pc = blk.placement.cell(cid)
            if pc is None:
                continue
            cfg = cell_map.get_cell(pc.x, pc.y)
            if cfg is None:
                continue
            for addr, ov in by_addr.items():
                if ov.is_empty:
                    continue
                word = cfg.memory.get(addr)
                if word is None or (word & 0xF000) not in (_WRITE, _JUMP):
                    continue
                cfg.memory[addr] = _patch_instr(int(word), ov)
    return ownership


def _resolve_batch_reset_writes(blocks: list, gr_blocks: dict) -> list:
    """Resolve every ``reset_per_batch`` StateVar to a concrete ``(x, y, addr,
    value)`` reset write, from the PLACED design.

    For each placed block with a v2 CellProgram, walk its cells; for the cell's
    program, allocate its state registers the SAME way :meth:`resolve` does
    (``compute_state_registers``, which mirrors the built memory image), and for
    every StateVar flagged ``reset_per_batch`` emit a ``(x, y, register, value)``
    tuple. ``value`` is the StateVar's ``reset_value`` when set, else its
    ``initial_value`` (the cold-start value the build already loads into memory).

    The host (SimServer.process_batch) backdoor-writes these into the hosted chip
    at the START of every batch (each RPC = one packet boundary) so a persistently-
    hosted receiver's loop memory cold-starts for each fresh packet. Resolved from
    the actual placed cells + register allocation, so it works for the auto-P&R
    layout (rotated/relocated blocks included) — not just a hand build.
    """
    from gr_kyttar.placement.resolver import CellProgramResolver

    resolver = CellProgramResolver()
    writes: list = []
    for blk in blocks:
        gr_block = gr_blocks.get(blk.name)
        if gr_block is None or blk.placement is None:
            continue
        try:
            cell_programs = gr_block.build_cell_programs()
        except Exception:  # noqa: BLE001 — non-v2 block; nothing to reset
            continue
        for pc in blk.placement.cells:
            cp = cell_programs.get(pc.cell_id)
            if cp is None or not getattr(cp, "assembly_template", ""):
                continue
            flagged = [sv for sv in getattr(cp, "state", [])
                       if getattr(sv, "reset_per_batch", False)]
            # COLD-START DataWords another cell overwrites at runtime (e.g. a
            # tap MIRROR seeded by a same-address DataWord and updated by its
            # master cell) carry their own reset_per_batch flag — re-write the
            # authored value at each packet boundary, exactly like flagged
            # state. Plain coefficients stay unflagged (a batch reset must not
            # revert a live-tuned coefficient).
            flagged_data = [dw for dw in getattr(cp, "data", [])
                            if getattr(dw, "reset_per_batch", False)
                            and dw.address is not None]
            if not flagged and not flagged_data:
                continue
            for dw in flagged_data:
                writes.append((int(pc.x), int(pc.y), int(dw.address),
                               int(dw.value) & 0xFFFF))
            if not flagged:
                continue
            try:
                state_regs = resolver.compute_state_registers(cp)
            except Exception:  # noqa: BLE001 — allocation failure ⇒ skip this cell
                continue
            for sv in flagged:
                addr = state_regs.get(sv.name)
                if addr is None:
                    continue
                val = (sv.reset_value if sv.reset_value is not None
                       else sv.initial_value)
                writes.append((int(pc.x), int(pc.y), int(addr), int(val) & 0xFFFF))
    return writes


def _classify_cells(blocks: list, gr_blocks: dict) -> dict:
    """Classify each block cell's addresses by role (data/state/instruction).

    Returns ``{(x, y): {addr: {"role": str, "name": str|None}}}`` for every
    block cell whose owning block has a v2 CellProgram. The Inspector uses this
    to distinguish DATA words (coefficients — they merely live in memory) from
    executable instructions, even when a data word's bits match a WRITE/JUMP
    opcode (§3.3).

    Cell index = position of the cell in ``placement.cells`` (the same order the
    Shape offsets are built in :meth:`_translate`), which keys the v2
    ``cell_programs`` dict.
    """
    from gr_kyttar.placement.resolver import CellProgramResolver

    resolver = CellProgramResolver()
    out: dict = {}
    for blk in blocks:
        gr_block = gr_blocks.get(blk.name)
        if gr_block is None or blk.placement is None:
            continue
        try:
            cell_programs = gr_block.build_cell_programs()
        except Exception:  # noqa: BLE001 — non-v2 block; leave cells unclassified
            continue
        for idx, pc in enumerate(blk.placement.cells):
            cp = cell_programs.get(idx)
            if cp is None or not getattr(cp, "assembly_template", ""):
                continue
            try:
                out[(pc.x, pc.y)] = resolver.classify_addresses(cp)
            except Exception:  # noqa: BLE001 — classification is best-effort
                continue
    return out


def _extract_cell_memory(cell_map, ownership: dict | None = None,
                         classes: dict | None = None) -> dict:
    """Per-cell resolved program from a routed CellMap (for §3.3 Inspector).

    Returns ``{(x, y): {...}}`` for every configured cell, with ``block`` and
    ``cell_id`` keys identifying the owning block (from ``ownership``), plus a
    ``classes`` map ``{addr: {"role", "name"}}`` (from ``classes``) classifying
    each address as data / state / instruction. Empty cells are absent.
    """
    # Map the gr_kyttar fwd_face int (S=0,E=1,W=2,N=3) to a name.
    _face_name = {0: "south", 1: "east", 2: "west", 3: "north"}
    ownership = ownership or {}
    classes = classes or {}
    out: dict = {}
    for (col, row), cfg in cell_map.cells.items():
        memory = [int(cfg.memory.get(addr, 0)) & 0xFFFF for addr in range(32)]
        fwd = getattr(cfg, "fwd_face", None)
        owner = ownership.get((col, row))
        routing_only = bool(getattr(cfg, "is_routing_only", lambda: False)())
        # A PROGRAMMED ROUTING CELL (bus BROKER / CROSSOVER): no owning block, not a
        # plain transit cell, yet it carries WRITE/JUMP relay instructions. Tag it so
        # the Inspector labels it (not blank) and shows it's the fabric's control logic.
        is_broker = (owner is None and not routing_only
                     and any((w & 0xF000) in (_WRITE, _JUMP) for w in memory))
        out[(col, row)] = {
            "entry": int(getattr(cfg, "entry_addr", 0) or 0),
            "memory": memory,
            # A routing cell's whole "program" is its FACE config (CONFIG[FACE]),
            # which lives outside main memory — surface it so routing cells don't
            # look unprogrammed in the Inspector.
            "face": _face_name.get(int(fwd)) if fwd is not None else None,
            "routing_only": routing_only,
            "block": owner[0] if owner else None,
            "cell_id": owner[1] if owner else None,
            "kind": "broker" if is_broker else None,
            "classes": classes.get((col, row), {}),
        }
    return out


def _first_port(chip_type: ChipType, direction: str) -> str | None:
    """First port name with the given direction ('input'/'output'), or None."""
    for p in chip_type.ports:
        if p.direction.value == direction:
            return p.name
    return None


def _array_config(chip_type: ChipType) -> ArrayConfig:
    """Build a gr_kyttar ArrayConfig (with named ports) from a ChipType."""
    ports: dict[str, PortConfig] = {}
    for p in chip_type.ports:
        ports[p.name] = PortConfig(
            name=p.name,
            direction=(
                GrPortDirection.INPUT
                if p.direction.value == "input"
                else GrPortDirection.OUTPUT
            ),
            cell=(p.cell_x, p.cell_y),
            face=_FACE_TO_GR[p.face.value],
            width=p.width,
        )
    return ArrayConfig(width=chip_type.width, height=chip_type.height, ports=ports)
