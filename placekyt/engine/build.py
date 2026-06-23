"""Build pipeline adapter — Project → bitstream (the architecture notes §5.1).

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
from .drc import DRCError, DRCResult, check_project, error as drc_error

# BuildError is an alias of the shared DRCError so a build surfaces one uniform
# error type whether the finding came from the DRC pass or from generation.
BuildError = DRCError


@dataclass
class ChipBuild:
    """Build output for a single chip."""

    chip_id: int
    words: list[int] = field(default_factory=list)
    cell_count: int = 0  # programmed + routing cells used
    # Per-cell resolved program: (x, y) -> {"entry": int, "memory": [32 words]}
    # — feeds the Inspector memory/assembly view (§3.3).
    cells: dict = field(default_factory=dict)


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

        # 1. Project-level DRC. Collect everything; errors block generation.
        drc = check_project(project, chip_types, board)
        result.errors.extend(drc.errors)
        result.warnings.extend(drc.warnings)
        # BUS DRC (§1.3/§5.3): face conflicts + the single-cell input==output deadlock
        # hazard. An offending single-cell bus-fed block (a router fallback that could
        # not split its faces, or a hand-laid route) is a NAMED build error — never a
        # silent unsafe build (P3.4). Needs the catalog (broker/crossover derivation).
        try:
            from .bus_drc import check_project_bus
            for v in check_project_bus(project, chip_types, self.catalog):
                if getattr(v, "kind", None) == "single_cell_inout":
                    result.errors.append(drc_error(
                        "single_cell_inout_deadlock", str(v),
                        chip=0, x=v.cell[0], y=v.cell[1]))
        except Exception:  # noqa: BLE001 — bus DRC is best-effort context
            pass
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
        _apply_brokers(cell_map, gr_placement, blocks_here, conns_here,
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
                                    feedback_blocks=feedback_blocks)

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

        # §1.4 UNIVERSAL ROUTING-CELL PROGRAM (Reading B, CM-approved): every
        # remaining PLAIN TRANSIT spine cell (a cell with a fwd_face but no program)
        # gets the uniform transmit(+relay) program so the whole fabric is made of
        # generic, dynamically-repurposable control cells (enabling §4.2). Runs LAST
        # of the routing passes — after faces/brokers/crossovers/feedback have set
        # every cell's fwd_face — so it ONLY touches cells still face-only, and does
        # NOT disturb their fwd_face (pass-through of HOP<31 words is unchanged; the
        # program's entries fire only at HOP_CNT==31, never for transiting traffic).
        _apply_routing_cell_programs(cell_map)

        # Per-cell address classification (data / state / instruction) from the
        # v2 CellProgram of each block, so the Inspector can tell DATA words
        # (coefficients, etc.) from executable instructions (§3.3).
        classes = _classify_cells(blocks_here, gr_blocks)

        gen = BitstreamGenerator(self._chip_type_path(type_name))
        gen.load_cell_map(cell_map)
        bitstream = gen.generate()

        result.chips[chip_id] = ChipBuild(
            chip_id=chip_id,
            words=list(bitstream.words),
            cell_count=cell_map.cell_count(),
            cells=_extract_cell_memory(cell_map, ownership, classes),
        )

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


def _patch_cell_handoff(cfg, hop, dest=None, entry=None) -> None:
    """Set every WRITE/JUMP INSTRUCTION in a cell to a specific ``hop`` (in @N
    hops-away form) and, when given, the dest register (WRITE) / entry addr
    (JUMP). Data words are left untouched (see :func:`_is_instruction_addr`)."""
    hop_cnt = encode_hop_cnt(hop)
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        opcode = word & 0xF000
        if opcode not in (_WRITE, _JUMP):
            continue
        word = (word & ~(0x1F << 5)) | (hop_cnt << 5)
        target = dest if opcode == _WRITE else entry
        if target is not None:
            word = (word & ~0x1F) | (int(target) & 0x1F)
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
    try:
        if gr_block.output_cell_id() is not None:
            return True
    except Exception:  # noqa: BLE001 — older blocks lack the method
        pass
    # The output exit cell = the last NON-transit cell of the block (the default
    # exit; transit_* cells are face-only routing, never the output). Is it the
    # source of any internal connection (a feedback/handoff WRITE)?
    try:
        layout = gr_block.default_layout() or {}
        block_cids = [cid for cid in layout
                      if not (isinstance(cid, str) and cid.startswith("transit"))]
        if not block_cids:
            return False
        exit_cid = block_cids[-1]
        internal = list(gr_block.internal_connections() or [])
        return any(src == exit_cid for (src, _sp, _d, _dp) in internal)
    except Exception:  # noqa: BLE001
        return False


def _route_distance(conn) -> int:
    """Hop distance of a routed connection: waypoints-1, +1 for a chip-output
    target (the data must transit through the edge cell to exit, §2.6)."""
    from model.connection import ChipPortEndpoint

    if not conn.is_routed:
        return 0
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

    for conn in connections:
        src, tgt = conn.source, conn.target
        # PHYSICAL path. A routed connection: the broker/face/hop geometry from its
        # drawn waypoints (a block→block route ending ON the target input cell is
        # stripped to the abutting broker). An UNROUTED connection: a direct
        # ABUTMENT — synthesise [src_out_cell, tgt_in_cell] when the source's output
        # cell is adjacent to the target — so a packed layout works without a filler
        # routing cell. Anything else (unrouted, non-adjacent) is skipped.
        if conn.is_routed:
            pts = _phys_pts(project, conn, catalog)
        else:
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
            ec = _entry_cell_of(tgt.block)
            if ec is not None and pts:
                final_face = _step_face(pts[-1][0], pts[-1][1], ec[0], ec[1])
        # Face each waypoint toward the next; final waypoint toward the target.
        # A waypoint on an EMPTY cell becomes a routing cell (faces only, no
        # program) — that's how the user's drawn path is realised in hardware.
        for i, (x, y) in enumerate(pts):
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
            cfg = cell_map.get_cell(*pb.exit_cell)
            if cfg is None:
                continue
            # Face the source block's EXIT cell toward the first route waypoint
            # (unless the first waypoint IS the exit cell, then toward the 2nd).
            ex, ey = pb.exit_cell
            nxt = None
            if pts and pts[0] != (ex, ey):
                nxt = pts[0]
            elif len(pts) > 1:
                nxt = pts[1]
            if nxt is not None:
                f = _step_face(ex, ey, *nxt)
                if f is not None:
                    cfg.fwd_face = f
            # Resolve the handoff target: a block target → its entry/input reg
            # (so the WRITE lands in the next block's input and the JUMP triggers
            # its entry); a chip-output-port target → entry 0 and dest = the
            # connection's output TAG (default 0), so chains that share one output
            # port stay distinguishable on the wire (the captured OutWord.tag).
            dest = entry = 0
            if isinstance(tgt, BlockEndpoint):
                tb = next((b for b in blocks if b.name == tgt.block), None)
                if tb is not None:
                    t_entry, t_ins = catalog.resolved_io(
                        tb.type, tb.params, library=tb.library)
                    entry = t_entry
                    dest = t_ins[0] if t_ins else 0
            elif conn.out_tag is not None:   # chip-output-port target with a tag
                dest = conn.out_tag
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
            phys_dist = _phys_distance(conn, pts)
            if _output_cell_carries_handoffs(gb):
                _patch_last_write_handoff(cfg, phys_dist, dest=dest)
                _patch_last_jump_handoff(cfg, phys_dist, entry=entry)
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

    # Each operand of a complex group lands in its OWN burst reg (R0, R1, ...) so the
    # source's distinct WRITEs don't clobber one another. Face data words pack ABOVE
    # the highest burst reg used so they never collide with a landing reg.
    max_operands = max((len(g) for g in groups), default=1)
    data_base = max_operands                  # bus_face + faces start past the regs
    data = [DataWord("bus_face", int(bus_face) & 0x3, address=data_base)]
    burst_ports = [Port(f"burst{r}", register=r) for r in range(max_operands)]
    entries = []
    tmpl_parts = []
    by_conn: dict = {}
    burst_reg_by_conn: dict = {}
    g_face_addr = data_base + 1
    for gi, group in enumerate(groups):
        label = f"deliver{gi}"
        fname = f"face{gi}"
        # All deliveries in a group share one deliver_face (same target cell).
        data.append(DataWord(fname, int(group[0].deliver_face) & 0x3,
                             address=g_face_addr))
        g_face_addr += 1
        entries.append(EntryPoint(label))
        lines = [f"{label}:", f"    MOVE [FACE], R{{data:{fname}}}"]
        # Relay each operand to its consumer register; WRITE always sends R0, so MOVE
        # each landed operand into R0 first. Distinct burst regs keep them separate.
        for oi, dv in enumerate(group):
            lines.append(f"    MOVE R0, R{{in:burst{oi}}}")
            lines.append(f"    WRITE @1, {int(dv.in_reg)}")
            burst_reg_by_conn[dv.conn] = oi      # which source WRITE -> which reg
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
    from .bus_router import BROKER_BURST_REG, broker_plan, _phys_pts

    taps = broker_plan(project, chip_id, chip_type, catalog)
    if not taps:
        return

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
    for conn in connections:
        if not conn.is_routed:
            continue
        if not isinstance(conn.source, BlockEndpoint) or conn.source.block not in placed:
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
        # Distance from the source exit cell to the broker = physical waypoints-1.
        distance = max(0, len(pts) - 1)
        ex = tuple(pb.exit_cell)
        by_src_cell.setdefault(ex, []).append(
            (conn.name, distance, conn_entry[conn.name]))
        src_meta[ex] = (gb, cfg)

    for ex, nets in by_src_cell.items():
        gb, cfg = src_meta[ex]
        # A COMPLEX-SAMPLE source: 2+ brokered nets from one exit cell that the broker
        # COALESCED into a single deliver entry (same b_entry). Patch each operand's
        # WRITE to its own broker burst reg (R0, R1, ... — by source program order)
        # and the single JUMP to the coalesced entry. The hop is shared (one route).
        entries = {e for (_c, _d, e) in nets}
        if len(nets) > 1 and len(entries) == 1:
            distance = nets[0][1]
            b_entry = nets[0][2]
            # Order operands by the broker's burst-reg assignment (which preserves
            # the connection / source-WRITE order), then patch the Nth WRITE -> reg N.
            ordered = sorted(nets, key=lambda n: conn_burst_reg.get(n[0], 0))
            burst_regs = [BROKER_BURST_REG + conn_burst_reg.get(c, i)
                          for i, (c, _d, _e) in enumerate(ordered)]
            _patch_complex_source_handoff(cfg, distance, burst_regs, b_entry)
            continue
        # Single-net source (the ordinary one-operand delivery, unchanged).
        conn_name, distance, b_entry = nets[0]
        # If the source block declares a MID-block output cell (the Costas rotate
        # writes yi→pd_pi internally AND yi_tap→the bus), patch ONLY the output
        # WRITE (the last WRITE — emitted after the internal handoffs) so the
        # internal feedback WRITEs keep their @1 hops; else patch the cell's
        # exit WRITE + JUMP together.
        if _output_cell_carries_handoffs(gb):
            _patch_last_write_handoff(cfg, distance, dest=BROKER_BURST_REG)
            _patch_last_jump_handoff(cfg, distance, entry=b_entry)
        else:
            _patch_cell_handoff(cfg, distance, dest=BROKER_BURST_REG,
                                entry=b_entry)


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
        this program (``routing.rs::route_packet`` decides execute-vs-forward purely
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
    (Reading B, CM-approved).

    After all faces/brokers/crossovers are set, a *plain transit* spine cell is a
    cell with a ``fwd_face`` but NO program (``is_routing_only()`` — empty memory,
    no entry). Brokers and crossovers already carry their own programs (flip-relay-
    restore / demux) — leave them untouched. Block cells have an owning program —
    untouched. This pass walks the cell map and gives each remaining plain transit
    cell the universal transmit(+relay) program, keyed on its existing ``fwd_face``,
    so the cell is a generic, dynamically-repurposable fabric cell.

    Pass-through is preserved by construction: the program's entries are reachable
    ONLY at HOP_CNT==31 (a deliberately-landed word); an ordinary transiting word
    (HOP_CNT<31) is forwarded on ``fwd_face`` by the hardware before the program is
    consulted (``routing.rs``). The cell's ``fwd_face`` is NOT changed, so the bus
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
            cfg = cell_map.get_cell(*pb.exit_cell)
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
                if _output_cell_carries_handoffs(gb):
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
        dest_reg = in_regs[0] if in_regs else None
        # Patch the source block's EXIT cell.
        pb = gr_placement.placed_blocks.get(src.block)
        if pb is None:
            continue
        cfg = cell_map.get_cell(*pb.exit_cell)
        if cfg is not None:
            _patch_cell_handoff(cfg, total, dest=dest_reg, entry=entry)


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
    for blk in blocks:
        if blk.placement is None:
            continue
        for pc in blk.placement.cells:
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


def _default_unrouted_exit_hops(cell_map, gr_placement, blocks: list,
                                connections: list, gr_blocks: dict,
                                catalog, feedback_blocks: dict | None = None
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


def _patch_one_handoff(cfg, opcode_wanted, dst_reg, hop, *, entry=None) -> bool:
    """Patch the SINGLE WRITE (or JUMP) instruction in ``cfg`` whose low-5-bit
    field already equals ``dst_reg`` — setting its ``@N`` ``hop`` (and, for a
    JUMP, its ``entry``). Returns True if a matching instruction was patched.

    Unlike :func:`_set_cell_hop1`/:func:`_patch_cell_handoff` (which rewrite
    EVERY WRITE/JUMP in the cell to one hop), this touches exactly one
    instruction — needed for a cell that emits BOTH a feedback output and a
    local terminate (e.g. the Costas pd_pi cell: its dphase WRITE feeds back @8
    while its trig JUMP stays a local terminate)."""
    hop_cnt = encode_hop_cnt(hop)
    for addr, word in list(cfg.memory.items()):
        if not _is_instruction_addr(cfg, addr):
            continue
        if (word & 0xF000) != opcode_wanted:
            continue
        if (word & 0x1F) != (int(dst_reg) & 0x1F):
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
            if hops is None:
                continue  # no traceable return path — leave as-is, don't guess
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
            cfg = cell_map.get_cell(*src_pos)
            if cfg is None:
                continue
            # Patch the src cell's WRITE that targets dst_reg to @hops.
            if _patch_one_handoff(cfg, _WRITE, dst_reg, hops):
                # Record (exit_cell_pos, feedback_dst_reg) so the exit-default
                # below PRESERVES this feedback WRITE while still defaulting the
                # cell's OTHER outputs (e.g. the Gardner loop_filter also emits a
                # real `out` + a local `trig`).
                feedback_blocks.setdefault(blk.name, set()).add(
                    (src_pos, int(dst_reg) & 0x1F))
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
