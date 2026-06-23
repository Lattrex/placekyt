"""Project-level Design Rule Checks (the architecture notes §5.2).

This pass validates **project-model invariants** — things computable from the
placeKYT project graph alone, BEFORE bitstream generation. Per §8 (Week 7-8),
these are the categories that `gr_kyttar.placement.Router` does NOT enforce
because Router operates on a `CellMap`, not on the project model:

    overlap, unplaced_cell, block_spans_chips, unrouted, route_gap,
    route_crosses_chips, hop_overflow, long_route, inter_chip_not_wired,
    panel_port_mismatch, unused_port, utilization (INFO)

The remaining §5.2 categories are enforced DOWNSTREAM, where the information
actually lives, and surface as build errors (same `DRCError` type) from
``engine.build``:

    cell_overflow, memory_layout_collision, assembly_error, unresolved_entry,
    interface_port_mismatch, transit_programmed, input_register_conflict,
    fan_out_write, port_direction_mismatch, feedback_target_missing

DRC COLLECTS ALL errors rather than failing on the first, so the user can fix
everything in one pass (§5, Assembly Error Contract; §3 error navigation).
Every error carries ``(chip, x, y)`` where known, for canvas navigation (§4.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from model.board import Board
from model.chip_type import ChipType
from model.connection import (
    AUTO_ROUTE,
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
)
from model.project import Project

# Max hops a packet can travel before being consumed (§2.6). HOP_CNT is a 5-bit
# field; the packet is consumed when it reaches 31, so distance must be <= 31.
MAX_HOPS = 31
# Warn when a route uses more than this many transit cells (§5.2 long_route).
LONG_ROUTE_TRANSIT = 20


class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class DRCError:
    """One DRC finding with a physical location where known (§5.2).

    Shared by the DRC pass and the build pipeline so a build surfaces one
    uniform error list.
    """

    severity: Severity
    category: str
    message: str
    chip: int | None = None
    x: int | None = None
    y: int | None = None

    def __str__(self) -> str:
        loc = ""
        if self.chip is not None:
            loc = f" [chip {self.chip}"
            if self.x is not None and self.y is not None:
                loc += f" ({self.x},{self.y})"
            loc += "]"
        return f"{self.severity.value} {self.category}: {self.message}{loc}"


def error(category: str, message: str, **loc) -> DRCError:
    return DRCError(Severity.ERROR, category, message, **loc)


def warning(category: str, message: str, **loc) -> DRCError:
    return DRCError(Severity.WARNING, category, message, **loc)


def info(category: str, message: str, **loc) -> DRCError:
    return DRCError(Severity.INFO, category, message, **loc)


@dataclass
class DRCResult:
    """Collected DRC findings."""

    findings: list[DRCError] = field(default_factory=list)

    @property
    def errors(self) -> list[DRCError]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[DRCError]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def infos(self) -> list[DRCError]:
        return [f for f in self.findings if f.severity is Severity.INFO]

    @property
    def ok(self) -> bool:
        """True if there are no ERROR-severity findings (warnings allowed)."""
        return not self.errors

    def add(self, finding: DRCError) -> None:
        self.findings.append(finding)


def check_project(
    project: Project,
    chip_types: dict[str, ChipType] | None = None,
    board: Board | None = None,
) -> DRCResult:
    """Run all project-level DRC checks. Collects every finding.

    ``chip_types`` (name → ChipType) enables in-bounds and hop-count checks
    that need the fabric geometry / port positions. ``board`` enables the
    ``inter_chip_not_wired`` check. Both are optional — checks that need missing
    context are skipped (not silently passed: their absence just means fewer
    checks ran).
    """
    result = DRCResult()
    chip_types = chip_types or {}

    _check_placement(project, chip_types, result)
    _check_connections(project, chip_types, result)
    _check_inter_chip(project, board, result)
    _check_panels(project, chip_types, result)
    _check_unused_ports(project, result)
    _report_utilization(project, chip_types, result)
    return result


# --------------------------------------------------------------------------- #
# Placement checks: overlap, unplaced_cell, block_spans_chips, out-of-bounds
# --------------------------------------------------------------------------- #


def _chip_type_for(project: Project, chip_id: int,
                   chip_types: dict[str, ChipType]) -> ChipType | None:
    chip = project.chip(chip_id)
    name = chip.type_name if (chip and chip.type_name) else project.chip_type
    return chip_types.get(name)


def _check_placement(project, chip_types, result: DRCResult) -> None:
    # occupancy map per chip: (x,y) -> list of "block:cell" labels
    occupancy: dict[int, dict[tuple[int, int], list[str]]] = {}

    for blk in project.blocks:
        if blk.placement is None:
            continue
        pl = blk.placement

        if not pl.cells:
            result.add(error(
                "unplaced_cell",
                f"block '{blk.name}' has a placement but no placed cells. "
                "Place its cells on the grid or remove the placement.",
                chip=pl.chip,
            ))
            continue

        # block_spans_chips: a Placement has a single chip field, but guard
        # against transit cells implying another chip is impossible here; the
        # real multi-chip spanning would show as cells with differing chips,
        # which the model can't express per-cell — so this is enforced at the
        # project level by ensuring every block's cells share pl.chip (always
        # true by construction). We still check that pl.chip is a real chip.
        if project.chips and project.chip(pl.chip) is None:
            result.add(error(
                "block_spans_chips",
                f"block '{blk.name}' is placed on chip {pl.chip}, which is not "
                "a chip in this project.",
                chip=pl.chip,
            ))

        ct = _chip_type_for(project, pl.chip, chip_types)
        chip_occ = occupancy.setdefault(pl.chip, {})

        for cell in pl.cells:
            label = f"{blk.name}[{cell.cell_id}]"
            # out-of-bounds (reported under unplaced_cell — cell isn't on a
            # valid grid position)
            if ct is not None and not ct.in_bounds(cell.x, cell.y):
                result.add(error(
                    "unplaced_cell",
                    f"cell {label} at ({cell.x},{cell.y}) is outside the "
                    f"{ct.width}x{ct.height} fabric. Move it onto the grid.",
                    chip=pl.chip, x=cell.x, y=cell.y,
                ))
            chip_occ.setdefault((cell.x, cell.y), []).append(label)

        # transit cells also occupy space (overlap detection includes them)
        for t in pl.transit_cells:
            if ct is not None and not ct.in_bounds(t.x, t.y):
                result.add(error(
                    "unplaced_cell",
                    f"transit cell of '{blk.name}' at ({t.x},{t.y}) is outside "
                    f"the {ct.width}x{ct.height} fabric.",
                    chip=pl.chip, x=t.x, y=t.y,
                ))
            chip_occ.setdefault((t.x, t.y), []).append(f"{blk.name}[transit]")

    # overlap: any position claimed by more than one cell
    for chip_id, occ in occupancy.items():
        for (x, y), labels in occ.items():
            if len(labels) > 1:
                result.add(error(
                    "overlap",
                    f"cells {', '.join(labels)} occupy the same position. "
                    "Move one to a free cell.",
                    chip=chip_id, x=x, y=y,
                ))


# --------------------------------------------------------------------------- #
# Connection checks: unrouted, route_gap, route_crosses_chips, hop_overflow,
#                    long_route
# --------------------------------------------------------------------------- #


def _source_chip(project: Project, conn: Connection) -> int | None:
    """The chip a connection's coordinates live on (the source's chip, §2.1)."""
    src = conn.source
    if isinstance(src, ChipPortEndpoint):
        return src.chip
    if isinstance(src, BlockEndpoint):
        blk = project.block(src.block)
        if blk and blk.placement is not None:
            return blk.placement.chip
    return None


def _check_connections(project, chip_types, result: DRCResult) -> None:
    for conn in project.connections:
        chip_id = _source_chip(project, conn)

        # Chip INPUT-port connections inject data at the edge cell and need no
        # coordinate route (§2.6 step 7a "Chip I/O port reads (input)"). The
        # connected block receives data directly at the port cell.
        if _sourced_from_chip_input(conn):
            continue

        # unrouted: no explicit waypoint route (auto is treated as unrouted in
        # Phase 1 — §2.1). A fly line cannot be built.
        if conn.route is None:
            result.add(error(
                "unrouted",
                f"connection '{conn.name}' has no physical route (fly line). "
                "Draw a route or use auto-route.",
                chip=chip_id,
            ))
            continue
        if conn.route == AUTO_ROUTE:
            result.add(error(
                "unrouted",
                f"connection '{conn.name}' uses route: auto, which is treated "
                "as unrouted in this version. Draw an explicit route.",
                chip=chip_id,
            ))
            continue

        points = conn.route  # list[RoutePoint]

        # route_gap: consecutive waypoints must be N/S/E/W adjacent (§5.2)
        for a, b in zip(points, points[1:]):
            if abs(a.x - b.x) + abs(a.y - b.y) != 1:
                result.add(warning(
                    "route_gap",
                    f"connection '{conn.name}' has non-adjacent waypoints "
                    f"({a.x},{a.y})->({b.x},{b.y}); treated as a fly line until "
                    "corrected.",
                    chip=chip_id, x=a.x, y=a.y,
                ))
                break

        # route_crosses_chips: route coords are implicitly on the source chip;
        # any waypoint outside that chip's fabric is an error (§5.2)
        ct = _chip_type_for(project, chip_id, chip_types) if chip_id is not None else None
        if ct is not None:
            for p in points:
                if not ct.in_bounds(p.x, p.y):
                    result.add(error(
                        "route_crosses_chips",
                        f"connection '{conn.name}' routes through ({p.x},{p.y}), "
                        f"outside chip {chip_id}'s {ct.width}x{ct.height} fabric. "
                        "Inter-chip links use the board wiring, not coordinate "
                        "routes.",
                        chip=chip_id, x=p.x, y=p.y,
                    ))
                    break

        # hop_overflow: distance = number of cells traversed excluding the
        # source but including the target (§2.6). For a waypoint route, that is
        # len(points) - 1. Chip-OUTPUT targets add +1 for the exit hop (§2.6 7a).
        distance = max(0, len(points) - 1)
        if _targets_chip_output(conn):
            distance += 1
        if distance > MAX_HOPS:
            result.add(error(
                "hop_overflow",
                f"route '{conn.name}' is {distance} hops (max {MAX_HOPS}). "
                "Shorten the path or move blocks closer together.",
                chip=chip_id,
            ))

        # long_route: more than 20 transit cells (waypoints excluding the two
        # endpoints) — a warning, not an error (§5.2)
        transit = max(0, len(points) - 2)
        if transit > LONG_ROUTE_TRANSIT:
            result.add(warning(
                "long_route",
                f"route '{conn.name}' uses {transit} transit cells "
                f"(> {LONG_ROUTE_TRANSIT}). Consider moving blocks closer.",
                chip=chip_id,
            ))


def _targets_chip_output(conn: Connection) -> bool:
    return (
        isinstance(conn.target, ChipPortEndpoint)
        and conn.target.port.endswith("_out")
    )


def _sourced_from_chip_input(conn: Connection) -> bool:
    """True if the connection's source is a chip INPUT port (e.g. x16_in).

    Such connections deliver data at the port's edge cell — no coordinate
    route is required (§2.6 step 7a)."""
    return (
        isinstance(conn.source, ChipPortEndpoint)
        and conn.source.port.endswith("_in")
    )


# --------------------------------------------------------------------------- #
# Inter-chip + unused ports + utilization
# --------------------------------------------------------------------------- #


def _check_inter_chip(project, board: Board | None, result: DRCResult) -> None:
    if board is None:
        return  # can't validate without the board's chip_connections
    for ic in project.inter_chip_connections:
        if not board.has_chip_connection(
            ic.from_chip, ic.from_port, ic.to_chip, ic.to_port
        ):
            result.add(error(
                "inter_chip_not_wired",
                f"inter-chip connection {ic.from_chip}.{ic.from_port} -> "
                f"{ic.to_chip}.{ic.to_port} is not a wire in board "
                f"'{board.name}'. Use a link the board physically provides.",
                chip=ic.from_chip,
            ))


def _check_panels(project, chip_types, result: DRCResult) -> None:
    """SRAM/peripheral panel checks (the SRAM panel notes §6):

    * ``panel_port_mismatch`` (ERROR) — a panel↔chip link whose bus widths
      (x16/x1) differ, or whose endpoints don't exist, or that aren't opposite
      directions (a panel output must feed a chip input and vice versa).

    (Address-register coverage is not checked here: ``SramPanel.address_regs``
    is derived from ``size_words``, so a panel is self-consistent by
    construction — there is no reachable overflow to flag.)
    """
    for pc in getattr(project, "panel_connections", None) or []:
        panel = project.panel(pc.panel)
        chip = project.chip(pc.chip)
        if panel is None:
            result.add(error("panel_port_mismatch",
                             f"panel connection references unknown panel "
                             f"{pc.panel}."))
            continue
        if chip is None:
            result.add(error("panel_port_mismatch",
                             f"panel connection references unknown chip "
                             f"{pc.chip}."))
            continue
        pport = panel.port(pc.panel_port)
        ct = _chip_type_for(project, pc.chip, chip_types)
        cport = ct.port(pc.chip_port) if ct else None
        if pport is None:
            result.add(error("panel_port_mismatch",
                             f"panel {pc.panel} has no port "
                             f"'{pc.panel_port}'.", chip=pc.chip))
            continue
        if ct is not None and cport is None:
            result.add(error("panel_port_mismatch",
                             f"chip {pc.chip} has no port "
                             f"'{pc.chip_port}'.", chip=pc.chip))
            continue
        if cport is None:
            continue  # chip type unresolved → can't check width/direction
        if pport.width != cport.width:
            result.add(error(
                "panel_port_mismatch",
                f"panel {pc.panel}.{pc.panel_port} is x{pport.width} but chip "
                f"{pc.chip}.{pc.chip_port} is x{cport.width} — bus widths must "
                "match.", chip=pc.chip))
        same_dir = (pport.direction == cport.direction)
        if same_dir:
            result.add(error(
                "panel_port_mismatch",
                f"panel {pc.panel}.{pc.panel_port} and chip "
                f"{pc.chip}.{pc.chip_port} are both {pport.direction.value} — a "
                "panel output must feed a chip input (and vice versa).",
                chip=pc.chip))


def _check_unused_ports(project, result: DRCResult) -> None:
    """unused_port (WARNING): a placed block whose port name is referenced by
    no connection. We can only check block-side ports that appear in
    connections, so this flags blocks with NO connections at all."""
    connected_blocks: set[str] = set()
    for conn in project.connections:
        for ep in (conn.source, conn.target):
            if isinstance(ep, BlockEndpoint):
                connected_blocks.add(ep.block)
    for blk in project.blocks:
        if blk.placement is not None and blk.name not in connected_blocks:
            result.add(warning(
                "unused_port",
                f"block '{blk.name}' is placed but has no connections. "
                "Connect its ports or remove the block.",
                chip=blk.placement.chip,
            ))


def _report_utilization(project, chip_types, result: DRCResult) -> None:
    """utilization (INFO): cells used / total, per chip (§5.2)."""
    used: dict[int, int] = {}
    for blk in project.blocks:
        if blk.placement is None:
            continue
        used[blk.placement.chip] = used.get(blk.placement.chip, 0) + (
            len(blk.placement.cells) + len(blk.placement.transit_cells)
        )
    chip_ids = [c.id for c in project.chips] or sorted(used)
    for chip_id in chip_ids:
        n = used.get(chip_id, 0)
        ct = _chip_type_for(project, chip_id, chip_types)
        total = ct.cell_count if ct else None
        if total:
            pct = 100.0 * n / total
            result.add(info(
                "utilization",
                f"{n}/{total} cells used ({pct:.1f}%).",
                chip=chip_id,
            ))
        else:
            result.add(info("utilization", f"{n} cells used.", chip=chip_id))
