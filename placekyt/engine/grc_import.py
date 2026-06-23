"""GRC import — build a placeKYT project from a GNURadio .grc flowgraph (P4.2).

The GRC-first end state (AUTO_PNR_DESIGN §8/Phase 4): a user designs an SDR in
GNURadio Companion using the Kyttar block library, and placeKYT imports the
schematic, instantiates the corresponding placeKYT blocks + logical nets, and then
auto-places-and-routes the grid. This module does the IMPORT half — parse the .grc,
map ``kyttar_*`` blocks to placeKYT block types, map ``kyttar_source`` /
``kyttar_sink`` to chip I/O ports, and emit blocks + unrouted logical nets. The
caller runs ``auto_place`` + ``auto_route_all`` to fill the grid.

A .grc is YAML with ``blocks:`` (each ``{id, name, parameters}``) and
``connections:`` (``[[src_block, src_port, dst_block, dst_port], ...]``). Only the
Kyttar DSP blocks become placeKYT blocks; GNURadio source/sink/throttle/GUI
blocks are dropped, except ``kyttar_source``/``kyttar_sink`` which become the
chip input/output ports. A connection between two kept blocks becomes a logical net;
a connection from ``kyttar_source`` to a block becomes a chip-input→block net; a
block→``kyttar_sink`` becomes a block→chip-output net.

Block-id mapping: ``kyttar_<snake>`` → the catalog type ``<Pascal>Block`` (e.g.
``kyttar_gain`` → ``GainBlock``, ``kyttar_dc_blocker`` → ``DCBlockerBlock``),
with a few explicit overrides for non-uniform names. Unknown blocks are reported
(sound — never silently dropped if they look like DSP blocks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# GRC ids that are NOT placeKYT DSP blocks — GNURadio plumbing / our device shims.
# These are dropped from the import (source/sink handled separately below).
_NON_DSP = {
    "variable", "analog_sig_source_x", "blocks_throttle", "qtgui_time_sink_x",
    "qtgui_freq_sink_x", "qtgui_const_sink_x", "blocks_null_sink",
    "blocks_vector_source_x", "blocks_file_source", "blocks_file_sink",
    "kyttar_device", "kyttar_placekyt_device", "kyttar_placekyt_sim_client",
    "kyttar_chip", "kyttar_placekyt_chip", "import", "options",
}
# The GRC source/sink → chip I/O port mapping.
_SOURCE_IDS = {"kyttar_source"}
_SINK_IDS = {"kyttar_sink"}

# Explicit GRC-id → placeKYT-type overrides where snake→Pascal+Block doesn't match.
_TYPE_OVERRIDES = {
    "kyttar_soft_demodulator": "SoftDemodulatorBlock",
    "kyttar_costas_loop": "ComplexCostasLoopBlock",
    "kyttar_gardner_ted": "GardnerTimingRecovery",
    "kyttar_iir_biquad": "IIRBiquadBlock",
    "kyttar_conv_encoder_k7": "ConvEncoderK7Block",
    "kyttar_lfsr_scrambler": "LFSRScramblerBlock",
    "kyttar_viterbi_bmu": "ViterbiBranchMetricBlock",
}


@dataclass
class GrcImportResult:
    """The outcome of importing a .grc into a placeKYT project."""

    project: object                      # the built Project (blocks + nets)
    block_map: dict = field(default_factory=dict)   # grc instance name → block name
    unknown: list = field(default_factory=list)     # (grc_name, grc_id) unmapped
    dropped: list = field(default_factory=list)     # grc ids dropped (plumbing)

    @property
    def ok(self) -> bool:
        return not self.unknown


def _grc_id_to_type(grc_id: str, catalog) -> str | None:
    """Map a GRC block id (``kyttar_gain``) to a placeKYT catalog type
    (``GainBlock``). Override table first, then snake→Pascal + ``Block`` suffix,
    validated against the catalog's actual type names."""
    if grc_id in _TYPE_OVERRIDES:
        cand = _TYPE_OVERRIDES[grc_id]
        return cand if catalog.get(cand) is not None else None
    if not grc_id.startswith("kyttar_"):
        return None
    snake = grc_id[len("kyttar_"):]
    pascal = "".join(p.capitalize() for p in snake.split("_"))
    for cand in (pascal + "Block", pascal):
        if catalog.get(cand) is not None:
            return cand
    # Case-insensitive fallback — the catalog uses e.g. "DCBlockerBlock" (DC
    # uppercase) where snake→Pascal gives "DcBlockerBlock". Match the squashed,
    # case-insensitive name against the catalog's actual type names.
    want = (pascal + "block").lower()
    for spec in catalog.all():
        tn = spec.type_name
        if tn.lower() == want or tn.lower() == pascal.lower():
            return tn
    return None


def import_grc(path, catalog, chip_type: str = "kyttar_10x12",
               *, project_name: str | None = None) -> GrcImportResult:
    """Parse a .grc file and build a placeKYT project of placeKYT blocks + logical
    nets, ready for ``auto_place`` + ``auto_route_all``. Blocks are placed at
    provisional spread-out positions (auto-place reflows them in signal order)."""
    import yaml

    from model.connection import (AUTO_ROUTE, BlockEndpoint, ChipPortEndpoint,
                                  Connection)
    from model.project import Project, ProjectMetadata
    from model.chip import ChipInstance

    p = Path(path)
    data = yaml.safe_load(p.read_text()) or {}
    grc_blocks = {b["name"]: b for b in data.get("blocks", []) if "name" in b}
    conns = data.get("connections", []) or []

    name = project_name or data.get("options", {}).get(
        "parameters", {}).get("title") or p.stem
    project = Project(metadata=ProjectMetadata(name=name), chip_type=chip_type)
    project.chips.append(ChipInstance(0, "Chip 0", 0.0, 0.0))

    # Classify GRC blocks: DSP (→ placeKYT block), source/sink (→ chip port), or
    # dropped plumbing. Unknown kyttar_* blocks are reported.
    block_map: dict = {}         # grc name → placeKYT block name
    role: dict = {}              # grc name → "block" | "source" | "sink" | "drop"
    _INSTANCE_TYPE.clear()       # grc name → placeKYT type (for port resolution)
    unknown, dropped = [], []
    placed_idx = 0
    for gname, gb in grc_blocks.items():
        gid = gb.get("id", "")
        if gid in _SOURCE_IDS:
            role[gname] = "source"
            continue
        if gid in _SINK_IDS:
            role[gname] = "sink"
            continue
        if gid in _NON_DSP:
            role[gname] = "drop"
            dropped.append(gid)
            continue
        btype = _grc_id_to_type(gid, catalog)
        if btype is None:
            role[gname] = "drop"
            if gid.startswith("kyttar_"):
                unknown.append((gname, gid))   # looked like a DSP block
            else:
                dropped.append(gid)
            continue
        role[gname] = "block"
        # Provisional placement spread across the grid (auto-place reflows it).
        from ui.controller import _default_name  # reuse the naming helper
        spec = catalog.get(btype)
        params = dict(gb.get("parameters", {}) or {})
        params = _coerce_params(params, catalog, btype)
        # GRC flowgraphs are visualization-first: a BPSK slicer feeding a Time
        # Sink wants one 0/1 word per recovered bit (a clean toggle plot), not the
        # block's production default of 16-bit packed words. If the .grc didn't
        # set out_mode explicitly, default it to 'bit' for the imported demo. A
        # .grc that DOES specify out_mode (packed) is respected.
        if (btype == "BPSKSlicerBlock"
                and "out_mode" not in (gb.get("parameters") or {})):
            params["out_mode"] = "bit"
        cells, transit = _default_cells(catalog, btype, params, placed_idx)
        from model.block import Block
        blk_name = _unique(_default_name(btype), block_map.values(),
                           [b.name for b in project.blocks])
        block = Block(blk_name, btype,
                      library=spec.library if spec else None, params=params)
        from model.placement import Placement
        block.placement = Placement(chip=0, cells=cells, transit_cells=transit)
        project.blocks.append(block)
        block_map[gname] = blk_name
        _INSTANCE_TYPE[gname] = btype
        placed_idx += 1

    # Connections → logical nets. Drop nets touching a dropped block; map
    # source→block to chip-input→block, block→sink to block→chip-output.
    net_idx = 0
    for entry in conns:
        if len(entry) < 4:
            continue
        sname, sp, dname, dp = entry[0], entry[1], entry[2], entry[3]
        srole, drole = role.get(sname), role.get(dname)
        if srole in (None, "drop") and drole in (None, "drop"):
            continue
        src = _endpoint(sname, srole, block_map, catalog, sp, is_src=True)
        dst = _endpoint(dname, drole, block_map, catalog, dp, is_src=False)
        if src is None or dst is None:
            continue
        net_idx += 1
        project.connections.append(Connection(
            f"net{net_idx}", source=src, target=dst, route=None))

    return GrcImportResult(project=project, block_map=block_map,
                           unknown=unknown, dropped=dropped)


# -- helpers -------------------------------------------------------------------

def _endpoint(gname, role, block_map, catalog, grc_port, *, is_src):
    from model.connection import BlockEndpoint, ChipPortEndpoint

    if role == "source":
        return ChipPortEndpoint(chip=0, port="x16_in")
    if role == "sink":
        return ChipPortEndpoint(chip=0, port="x16_out")
    if role == "block":
        bn = block_map.get(gname)
        if bn is None:
            return None
        # Honor the GRC port name so multi-port blocks (ComplexCostasLoop xi/xq,
        # Gardner xi, BPSKSlicer llr, mixers, QAM) wire correctly — the importer
        # used to hardwire every net to out→sample, which only works for
        # single-port blocks. We resolve the GRC port against the block's actual
        # PortMap (by name; tolerant of GRC label casing). If the GRC port can't
        # be resolved (or the .grc gave a positional name like "0"), fall back to
        # the block's first in/out port — the conventional single-port case.
        btype = _btype_of(block_map, gname, catalog)
        port = _resolve_port(catalog, btype, grc_port, want_out=is_src)
        return BlockEndpoint(block=bn, port=port)
    return None


def _btype_of(block_map, gname, catalog):
    """The placeKYT type name for a GRC instance (recorded during the block pass);
    None if unknown, which makes the port resolver fall back to the default."""
    return _INSTANCE_TYPE.get(gname)


# Populated during import: GRC instance name → placeKYT type name, so the
# connection pass can resolve ports against the right PortMap.
_INSTANCE_TYPE: dict = {}


def _resolve_port(catalog, btype, grc_port, *, want_out):
    """Map a GRC port name to a real block port name, validated against the
    block's PortMap. ``want_out`` picks the output side (source endpoint) vs the
    input side (target endpoint). Falls back to the first port on that side."""
    direction = "out" if want_out else "in"
    ports = []
    if btype is not None:
        try:
            pm = catalog.port_map(btype)
            ports = [p.name for p in pm.ports if p.direction == direction]
        except Exception:  # noqa: BLE001 — no PortMap → fall through to default
            ports = []
    if not ports:
        return "out" if want_out else "sample"
    if grc_port is not None:
        want = str(grc_port).strip().lower()
        for nm in ports:
            if nm.lower() == want:
                return nm
        # A NUMERIC GRC port (e.g. "0", "1") indexes into the named ports in
        # declared order. GNURadio's Python stream ports are integer-only (a
        # gr.sync_block cannot name a stream port — connect((blk, 'yi_tap'), …)
        # raises), so a runnable .grc wires by INDEX while the block's PortMap keeps
        # the meaningful names (xi/xq/yi_tap). This maps that index back to the name
        # so import stays precise (port 0 → xi, port 1 → xq) instead of collapsing.
        if want.isdigit():
            i = int(want)
            if 0 <= i < len(ports):
                return ports[i]
    return ports[0]


def _coerce_params(params, catalog, btype):
    """Keep only the params the block accepts, coercing GRC string values to the
    spec's default TYPE. GRC stores everything as strings; a value that can't be
    coerced to the default's type — a GRC variable name (``fir_taps``) or a Python
    expression (``firdes.low_pass(...)``) we can't safely evaluate — is OMITTED so
    the block keeps its own default. This is the difference between importing a
    multi-block flowgraph and crashing on a non-scalar/expression param."""
    import ast

    spec = catalog.get(btype)
    defaults = spec.default_params() if spec else {}
    # Start from the full defaults so REQUIRED params (e.g. FIR ``coefficients``,
    # which has no constructor default) always have a value — the GRC values
    # below override only where they coerce cleanly.
    out = dict(defaults)
    for k, dv in defaults.items():
        if k not in params:
            continue
        s = str(params[k]).strip().strip("'\"")
        if not s:
            continue
        try:
            if isinstance(dv, bool):
                out[k] = s.lower() in ("true", "1", "yes")
            elif isinstance(dv, int):
                out[k] = int(float(s))
            elif isinstance(dv, float):
                out[k] = float(s)
            elif isinstance(dv, (list, tuple, dict)):
                # Only accept a literal that parses to the SAME container type;
                # a variable name / expression raises and is omitted (default kept).
                val = ast.literal_eval(s)
                if isinstance(val, type(dv)):
                    out[k] = val
            else:
                out[k] = s
        except (ValueError, TypeError, SyntaxError):
            pass  # un-coercible (variable/expression) → keep the block default
    return out


def _default_cells(catalog, btype, params, idx):
    """Provisional cells for a block at a spread-out grid slot (auto-place reflows
    these). Uses the block's default_layout for the shape."""
    from model.placement import PlacedCell, TransitCell
    from model.enums import Face

    layout = catalog.default_layout(btype, params) or {0: (0, 0, "east")}
    # Spread blocks diagonally so the initial (pre-auto-place) project is valid
    # and non-overlapping; auto-place then flow-orders them.
    ox, oy = (idx * 3) % 8, (idx // 2) % 6
    cells, transit = [], []
    for cid, (dx, dy, face) in layout.items():
        x, y = ox + dx, oy + dy
        if isinstance(cid, str) and cid.startswith("transit"):
            transit.append(TransitCell(x, y, Face.from_str(face)))
        else:
            cells.append(PlacedCell(cid, x, y, Face.from_str(face)))
    return cells, transit


def _unique(base, *used_iters):
    used = set()
    for it in used_iters:
        used.update(it)
    if base not in used:
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"
