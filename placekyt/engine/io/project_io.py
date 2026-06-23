"""``.kyt`` project file load/save (the architecture notes §2.1).

Round-trips through ruamel so unknown fields, key ordering, comments, and
formatting are preserved (§2.1). The strategy:

  * **Load** parses the document and maps the *known* sections onto the
    :class:`Project` model. The raw ruamel document is retained on
    ``project.extra['_doc']`` so unknown fields survive.
  * **Save** mutates that retained document in place — updating known sections,
    deleting keys for absent optional fields (the "no explicit null" rule) —
    then dumps it. A project created fresh (no retained document) gets a new
    document built from scratch.

Path references (stimulus, golden, flowgraph, board config) are NOT validated
or opened here — validation happens lazily at simulation/bridge start (§2.1).
``load_project`` only records the declared strings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from model.block import Block
from model.chip import ChipInstance
from model.connection import (
    AUTO_ROUTE,
    NET_DATA_TRIGGER,
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
    Endpoint,
    InterChipConnection,
    PanelConnection,
    RoutePoint,
)
from model.enums import Face, IQFormat, Modulation, PortDirection
from model.panel import PanelPort, SramPanel
from model.placement import InstrOverride, Placement, PlacedCell, TransitCell
from model.project import (
    BoardRef,
    FaceOverride,
    FpgaModelRef,
    Project,
    ProjectMetadata,
    SimulationConfig,
)

from ._mapping import opt, opt_seq, require, require_mapping
from .errors import SchemaError, UnsupportedFormatVersion
from .safe_yaml import dump_yaml, load_yaml, load_yaml_str

# Highest ``format_version`` this build understands. Newer files are rejected
# (§2.1: "The IDE rejects projects with format_version newer than supported").
SUPPORTED_FORMAT_VERSION = 1

_DOC_KEY = "_doc"  # where the retained ruamel document lives in Project.extra


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


def load_project(path: str | Path) -> Project:
    doc = load_yaml(path)
    return _project_from_doc(doc, source=str(path))


def project_from_str(text: str, *, source: str = "<string>") -> Project:
    doc = load_yaml_str(text, source=source)
    return _project_from_doc(doc, source=source)


def _project_from_doc(doc: Any, *, source: str) -> Project:
    doc = require_mapping(doc, source)

    meta_node = require_mapping(require(doc, "project", source), f"{source}.project")
    fmt = int(opt(meta_node, "format_version", 1))
    if fmt > SUPPORTED_FORMAT_VERSION:
        raise UnsupportedFormatVersion(
            f"{source}: format_version {fmt} is newer than this build supports "
            f"({SUPPORTED_FORMAT_VERSION}). Upgrade placeKYT to open this project."
        )

    metadata = ProjectMetadata(
        name=str(opt(meta_node, "name", "Untitled")),
        version=str(opt(meta_node, "version", "1.0")),
        author=str(opt(meta_node, "author", "")),
        created=str(opt(meta_node, "created", "")),
        modified=str(opt(meta_node, "modified", "")),
        format_version=fmt,
    )

    board = None
    board_node = opt(doc, "board")
    if board_node is not None:
        board_node = require_mapping(board_node, f"{source}.board")
        board = BoardRef(
            name=str(opt(board_node, "name", "")),
            config=str(opt(board_node, "config", "")),
        )

    chips = [
        _chip_from_node(c, source=f"{source}.chips[{i}]")
        for i, c in enumerate(opt_seq(doc, "chips", source))
    ]

    inter_chip = [
        _inter_chip_from_node(c, source=f"{source}.inter_chip_connections[{i}]")
        for i, c in enumerate(opt_seq(doc, "inter_chip_connections", source))
    ]

    panels = [
        _panel_from_node(p, source=f"{source}.panels[{i}]")
        for i, p in enumerate(opt_seq(doc, "panels", source))
    ]

    panel_conns = [
        _panel_connection_from_node(c, source=f"{source}.panel_connections[{i}]")
        for i, c in enumerate(opt_seq(doc, "panel_connections", source))
    ]

    blocks = [
        _block_from_node(b, source=f"{source}.blocks[{i}]")
        for i, b in enumerate(opt_seq(doc, "blocks", source))
    ]

    connections = [
        _connection_from_node(c, source=f"{source}.connections[{i}]")
        for i, c in enumerate(opt_seq(doc, "connections", source))
    ]

    mode_switching = _mode_switching_from_node(
        opt(doc, "mode_switching", {}), source=f"{source}.mode_switching"
    )

    simulation = _simulation_from_node(
        opt(doc, "simulation", {}), source=f"{source}.simulation"
    )

    project = Project(
        metadata=metadata,
        chip_type=str(opt(doc, "chip_type", "")),
        board=board,
        chips=chips,
        inter_chip_connections=inter_chip,
        panels=panels,
        panel_connections=panel_conns,
        blocks=blocks,
        connections=connections,
        mode_switching=mode_switching,
        simulation=simulation,
    )
    # Retain the raw document so unknown fields / comments / order round-trip.
    project.extra[_DOC_KEY] = doc
    # A freshly loaded project is not dirty and has not been built.
    project.project_dirty = False
    project.build_dirty = True
    return project


def _chip_from_node(node: Any, *, source: str) -> ChipInstance:
    node = require_mapping(node, source)
    pos = opt(node, "position", {})
    return ChipInstance(
        id=int(require(node, "id", source)),
        label=str(opt(node, "label", "")),
        position_x=float(opt(pos, "x", 0.0)),
        position_y=float(opt(pos, "y", 0.0)),
        type_name=(str(node["type"]) if node.get("type") else None),
    )


def _inter_chip_from_node(node: Any, *, source: str) -> InterChipConnection:
    node = require_mapping(node, source)
    frm = require_mapping(require(node, "from", source), f"{source}.from")
    to = require_mapping(require(node, "to", source), f"{source}.to")
    return InterChipConnection(
        from_chip=int(require(frm, "chip", f"{source}.from")),
        from_port=str(require(frm, "port", f"{source}.from")),
        to_chip=int(require(to, "chip", f"{source}.to")),
        to_port=str(require(to, "port", f"{source}.to")),
    )


def _panel_from_node(node: Any, *, source: str) -> SramPanel:
    node = require_mapping(node, source)
    pos = opt(node, "position", {})
    ports_node = opt(node, "ports", None)
    if ports_node:
        ports = [_panel_port_from_node(p, source=f"{source}.ports[{i}]")
                 for i, p in enumerate(ports_node)]
    else:
        from model.panel import _default_ports
        ports = _default_ports()
    from model.panel import DEFAULT_PANEL_WORDS
    return SramPanel(
        id=int(require(node, "id", source)),
        label=str(opt(node, "label", "")),
        position_x=float(opt(pos, "x", 0.0)),
        position_y=float(opt(pos, "y", 0.0)),
        size_words=int(opt(node, "size_words", DEFAULT_PANEL_WORDS)),
        ports=ports,
        mirrored=bool(opt(node, "mirrored", False)),
    )


def _panel_port_from_node(node: Any, *, source: str) -> PanelPort:
    node = require_mapping(node, source)
    from model.panel import PORT_WIDTH_X16
    return PanelPort(
        name=str(require(node, "name", source)),
        direction=PortDirection(str(require(node, "direction", source))),
        width=int(opt(node, "width", PORT_WIDTH_X16)),
        face=Face.from_str(str(opt(node, "face", "west"))),
    )


def _panel_connection_from_node(node: Any, *, source: str) -> PanelConnection:
    node = require_mapping(node, source)
    panel = require_mapping(require(node, "panel", source), f"{source}.panel")
    chip = require_mapping(require(node, "chip", source), f"{source}.chip")
    return PanelConnection(
        panel=int(require(panel, "id", f"{source}.panel")),
        panel_port=str(require(panel, "port", f"{source}.panel")),
        chip=int(require(chip, "id", f"{source}.chip")),
        chip_port=str(require(chip, "port", f"{source}.chip")),
    )


def _block_from_node(node: Any, *, source: str) -> Block:
    node = require_mapping(node, source)
    placement = None
    place_node = opt(node, "placement")
    if place_node is not None:
        placement = _placement_from_node(place_node, source=f"{source}.placement")
    # params is preserved as a plain dict copy (values may be int/float/str/bool).
    params_node = opt(node, "params", {})
    params = dict(params_node) if params_node else {}
    return Block(
        name=str(require(node, "name", source)),
        type=str(require(node, "type", source)),
        library=(str(node["library"]) if node.get("library") else None),
        version=(str(node["version"]) if node.get("version") else None),
        params=params,
        placement=placement,
        color=(str(node["color"]) if node.get("color") else None),
    )


def _placement_from_node(node: Any, *, source: str) -> Placement:
    node = require_mapping(node, source)
    cells = [
        PlacedCell(
            # Preserve the original scalar type (int or string) so integer
            # cell ids round-trip unquoted. §2.1 permits either form.
            cell_id=require(require_mapping(c, f"{source}.cells[{i}]"),
                            "cell_id", source),
            x=int(require(c, "x", source)),
            y=int(require(c, "y", source)),
            face=Face.from_str(str(require(c, "face", source))),
        )
        for i, c in enumerate(opt_seq(node, "cells", source))
    ]
    transit = [
        TransitCell(
            x=int(require(require_mapping(t, f"{source}.transit_cells[{i}]"),
                          "x", source)),
            y=int(require(t, "y", source)),
            face=Face.from_str(str(require(t, "face", source))),
        )
        for i, t in enumerate(opt_seq(node, "transit_cells", source))
    ]
    overrides: dict = {}
    for i, o in enumerate(opt_seq(node, "instr_overrides", source)):
        om = require_mapping(o, f"{source}.instr_overrides[{i}]")
        cid = require(om, "cell_id", source)
        addr = int(require(om, "addr", source))
        ov = InstrOverride(
            hop=(int(om["hop"]) if om.get("hop") is not None else None),
            dest=(int(om["dest"]) if om.get("dest") is not None else None),
            entry=(int(om["entry"]) if om.get("entry") is not None else None),
            dest_config=bool(om.get("dest_config", False)),
        )
        if not ov.is_empty:
            overrides.setdefault(cid, {})[addr] = ov
    orientation = [str(k) for k in opt_seq(node, "orientation", source)]
    return Placement(
        chip=int(require(node, "chip", source)),
        cells=cells,
        transit_cells=transit,
        instr_overrides=overrides,
        orientation=orientation,
    )


def _endpoint_from_node(node: Any, *, source: str) -> Endpoint:
    node = require_mapping(node, source)
    if "chip_port" in node:
        cp = require_mapping(node["chip_port"], f"{source}.chip_port")
        return ChipPortEndpoint(
            chip=int(require(cp, "chip", f"{source}.chip_port")),
            port=str(require(cp, "port", f"{source}.chip_port")),
        )
    if "block" in node:
        return BlockEndpoint(
            block=str(node["block"]),
            port=str(require(node, "port", source)),
        )
    raise SchemaError(
        f"{source}: endpoint must have either 'block' or 'chip_port'."
    )


def _connection_from_node(node: Any, *, source: str) -> Connection:
    node = require_mapping(node, source)
    route = _route_from_node(opt(node, "route"), source=f"{source}.route")
    modulation = (
        Modulation.from_str(str(node["modulation"]))
        if node.get("modulation")
        else None
    )
    iq_format = (
        IQFormat.from_str(str(node["iq_format"])) if node.get("iq_format") else None
    )
    code_rate = node.get("code_rate")
    out_tag = node.get("out_tag")
    # ``kind`` is optional — absent means the default ``data+trigger`` (WRITE+JUMP),
    # so pre-Phase-2 .kyt files load unchanged.
    kind = node.get("kind")
    return Connection(
        name=str(require(node, "name", source)),
        source=_endpoint_from_node(require(node, "from", source), source=f"{source}.from"),
        target=_endpoint_from_node(require(node, "to", source), source=f"{source}.to"),
        route=route,
        modulation=modulation,
        code_rate=(float(code_rate) if code_rate is not None else None),
        iq_format=iq_format,
        out_tag=(int(out_tag) if out_tag is not None else None),
        kind=(str(kind) if kind is not None else NET_DATA_TRIGGER),
    )


def _route_from_node(node: Any, *, source: str):
    if node is None:
        return None
    if isinstance(node, str):
        if node == AUTO_ROUTE:
            return AUTO_ROUTE
        raise SchemaError(f"{source}: route string must be {AUTO_ROUTE!r}.")
    points = []
    for i, p in enumerate(node):
        p = require_mapping(p, f"{source}[{i}]")
        points.append(RoutePoint(int(require(p, "x", source)),
                                 int(require(p, "y", source))))
    return points


def _mode_switching_from_node(node: Any, *, source: str) -> dict[str, list[FaceOverride]]:
    if not node:
        return {}
    node = require_mapping(node, source)
    modes: dict[str, list[FaceOverride]] = {}
    for mode_name, mode_node in node.items():
        mode_node = require_mapping(mode_node, f"{source}.{mode_name}")
        overrides = []
        for i, fo in enumerate(opt_seq(mode_node, "face_overrides", f"{source}.{mode_name}")):
            fo = require_mapping(fo, f"{source}.{mode_name}.face_overrides[{i}]")
            overrides.append(
                FaceOverride(
                    chip=int(require(fo, "chip", source)),
                    x=int(require(fo, "x", source)),
                    y=int(require(fo, "y", source)),
                    face=Face.from_str(str(require(fo, "face", source))),
                )
            )
        modes[str(mode_name)] = overrides
    return modes


def _simulation_from_node(node: Any, *, source: str) -> SimulationConfig:
    if not node:
        return SimulationConfig()
    node = require_mapping(node, source)
    fpga_models = []
    for i, fm in enumerate(opt_seq(node, "fpga_models", source)):
        fm = require_mapping(fm, f"{source}.fpga_models[{i}]")
        fpga_models.append(
            FpgaModelRef(
                connection=str(require(fm, "connection", source)),
                type=str(require(fm, "type", source)),
                params=dict(opt(fm, "params", {})),
            )
        )
    return SimulationConfig(
        default_stimulus=_opt_str(node, "default_stimulus"),
        golden_output=_opt_str(node, "golden_output"),
        golden_bits=_opt_str(node, "golden_bits"),
        golden_symbols=_opt_str(node, "golden_symbols"),
        gnuradio_flowgraph=_opt_str(node, "gnuradio_flowgraph"),
        fpga_latency_ns=float(opt(node, "fpga_latency_ns", 20.0)),
        fpga_models=fpga_models,
    )


def _opt_str(node: Any, key: str) -> str | None:
    val = node.get(key)
    return str(val) if val else None


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #


def save_project(project: Project, path: str | Path) -> None:
    """Serialize ``project`` to ``path`` as ``.kyt`` YAML.

    Mutates the retained ruamel document in place (preserving unknown fields,
    comments, and key order) when one exists; otherwise builds a fresh document.
    Clears ``project_dirty`` on success (§4.2). ``build_dirty`` is unaffected.
    """
    doc = project.extra.get(_DOC_KEY)
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()
        project.extra[_DOC_KEY] = doc
    _write_project_into_doc(project, doc)
    dump_yaml(doc, path)
    project.project_dirty = False


def _set(doc: CommentedMap, key: str, value: Any) -> None:
    doc[key] = value


def _set_optional(doc: CommentedMap, key: str, value: Any) -> None:
    """Write ``value`` under ``key`` if present; otherwise delete the key.

    Implements §2.1's "absent fields are NOT written as null" rule — an unset
    optional is removed entirely rather than emitted as ``key: null``.
    """
    if value is None:
        doc.pop(key, None)
    else:
        doc[key] = value


def _write_project_into_doc(project: Project, doc: CommentedMap) -> None:
    meta = doc.get("project")
    if not isinstance(meta, CommentedMap):
        meta = CommentedMap()
        doc["project"] = meta
    m = project.metadata
    meta["format_version"] = m.format_version
    meta["name"] = m.name
    meta["version"] = m.version
    _set_optional(meta, "author", m.author or None)
    _set_optional(meta, "created", m.created or None)
    _set_optional(meta, "modified", m.modified or None)

    doc["chip_type"] = project.chip_type

    if project.board is not None:
        board = doc.get("board")
        if not isinstance(board, CommentedMap):
            board = CommentedMap()
            doc["board"] = board
        board["name"] = project.board.name
        _set_optional(board, "config", project.board.config or None)
    else:
        doc.pop("board", None)

    # Reuse existing nodes (keyed by identifier) so per-item unknown fields and
    # comments survive round-trip; fall back to fresh nodes for new items.
    chip_nodes = _index_existing(doc.get("chips"), lambda n: n.get("id"))
    _set_list_or_remove(
        doc,
        "chips",
        [_chip_to_node(c, chip_nodes.get(c.id)) for c in project.chips],
    )

    _set_list_or_remove(
        doc,
        "inter_chip_connections",
        [_inter_chip_to_node(c) for c in project.inter_chip_connections],
    )

    panel_nodes = _index_existing(doc.get("panels"), lambda n: n.get("id"))
    _set_list_or_remove(
        doc,
        "panels",
        [_panel_to_node(p, panel_nodes.get(p.id)) for p in project.panels],
    )

    _set_list_or_remove(
        doc,
        "panel_connections",
        [_panel_connection_to_node(c) for c in project.panel_connections],
    )

    block_nodes = _index_existing(doc.get("blocks"), lambda n: n.get("name"))
    doc["blocks"] = _seq(
        _block_to_node(b, block_nodes.get(b.name)) for b in project.blocks
    )

    conn_nodes = _index_existing(doc.get("connections"), lambda n: n.get("name"))
    doc["connections"] = _seq(
        _connection_to_node(c, conn_nodes.get(c.name)) for c in project.connections
    )

    _set_mode_switching(doc, project)
    _set_simulation(doc, project)


def _index_existing(seq: Any, key_fn) -> dict:
    """Index an existing list-of-mappings document node by a key function.

    Returns ``{key: node}`` for reuse during save so unknown fields/comments on
    individual list items are preserved. Non-mapping or keyless items are skipped.
    """
    out: dict = {}
    if isinstance(seq, CommentedSeq):
        for item in seq:
            if isinstance(item, CommentedMap):
                k = key_fn(item)
                if k is not None:
                    out[k] = item
    return out


def _reuse_or_new(existing: Any) -> CommentedMap:
    """Return ``existing`` if it is a mapping to mutate, else a fresh one."""
    return existing if isinstance(existing, CommentedMap) else CommentedMap()


def _seq(items) -> CommentedSeq:
    seq = CommentedSeq()
    seq.extend(items)
    return seq


def _set_list_or_remove(doc: CommentedMap, key: str, items: list) -> None:
    if items:
        doc[key] = _seq(items)
    else:
        doc.pop(key, None)


def _chip_to_node(chip: ChipInstance, existing: Any = None) -> CommentedMap:
    node = _reuse_or_new(existing)
    node["id"] = chip.id
    _set_optional(node, "label", chip.label or None)
    _set_optional(node, "type", chip.type_name)
    pos = node.get("position")
    if not isinstance(pos, CommentedMap):
        pos = CommentedMap()
        pos.fa.set_flow_style()
        node["position"] = pos
    pos["x"] = chip.position_x
    pos["y"] = chip.position_y
    return node


def _inter_chip_to_node(c: InterChipConnection) -> CommentedMap:
    node = CommentedMap()
    node["from"] = _flow_map({"chip": c.from_chip, "port": c.from_port})
    node["to"] = _flow_map({"chip": c.to_chip, "port": c.to_port})
    return node


def _panel_to_node(p: SramPanel, existing: Any = None) -> CommentedMap:
    from model.panel import DEFAULT_PANEL_WORDS

    node = _reuse_or_new(existing)
    node["id"] = p.id
    _set_optional(node, "label", p.label or None)
    # size_words is only written when it differs from the default full array.
    _set_optional(node, "size_words",
                  p.size_words if p.size_words != DEFAULT_PANEL_WORDS else None)
    _set_optional(node, "mirrored", True if p.mirrored else None)
    pos = node.get("position")
    if not isinstance(pos, CommentedMap):
        pos = CommentedMap()
        pos.fa.set_flow_style()
        node["position"] = pos
    pos["x"] = p.position_x
    pos["y"] = p.position_y
    node["ports"] = _seq(
        _flow_map({
            "name": port.name,
            "direction": port.direction.value,
            "width": port.width,
            "face": port.face.value,
        })
        for port in p.ports
    )
    return node


def _panel_connection_to_node(c: PanelConnection) -> CommentedMap:
    node = CommentedMap()
    node["panel"] = _flow_map({"id": c.panel, "port": c.panel_port})
    node["chip"] = _flow_map({"id": c.chip, "port": c.chip_port})
    return node


def _block_to_node(b: Block, existing: Any = None) -> CommentedMap:
    node = _reuse_or_new(existing)
    node["name"] = b.name
    node["type"] = b.type
    _set_optional(node, "library", b.library)
    _set_optional(node, "version", b.version)
    _set_optional(node, "color", b.color)
    if b.params:
        params = node.get("params")
        if not isinstance(params, CommentedMap):
            params = CommentedMap()
            node["params"] = params
        # Update known param values in place; drop params no longer present.
        for k, v in b.params.items():
            params[k] = v
        for stale in [k for k in params if k not in b.params]:
            params.pop(stale, None)
    else:
        node.pop("params", None)
    if b.placement is not None:
        node["placement"] = _placement_to_node(b.placement, node.get("placement"))
    else:
        node.pop("placement", None)
    return node


def _placement_to_node(p: Placement, existing: Any = None) -> CommentedMap:
    node = _reuse_or_new(existing)
    node["chip"] = p.chip
    node["cells"] = _seq(
        _flow_map({"cell_id": c.cell_id, "x": c.x, "y": c.y, "face": c.face.value})
        for c in p.cells
    )
    if p.transit_cells:
        node["transit_cells"] = _seq(
            _flow_map({"x": t.x, "y": t.y, "face": t.face.value})
            for t in p.transit_cells
        )
    else:
        node.pop("transit_cells", None)
    rows = []
    for cid, by_addr in p.instr_overrides.items():
        for addr, ov in sorted(by_addr.items()):
            if ov.is_empty:
                continue
            row = {"cell_id": cid, "addr": addr}
            if ov.hop is not None:
                row["hop"] = ov.hop
            if ov.dest is not None:
                row["dest"] = ov.dest
                if ov.dest_config:
                    row["dest_config"] = True
            if ov.entry is not None:
                row["entry"] = ov.entry
            rows.append(_flow_map(row))
    if rows:
        node["instr_overrides"] = _seq(rows)
    else:
        node.pop("instr_overrides", None)
    # The applied D4 orientation(s) (auto-orient / manual transform). MUST persist:
    # a folded block's in-program FACE constants are transformed by this at build
    # time (build._apply_orientation_face_words). Dropping it on save makes a loaded
    # .kyt build with un-oriented face constants → a feedback WRITE fires the wrong
    # way (the (5,1) stray-exec bug).
    if p.orientation:
        node["orientation"] = _seq(list(p.orientation))
    else:
        node.pop("orientation", None)
    return node


def _endpoint_to_node(ep: Endpoint) -> CommentedMap:
    if isinstance(ep, ChipPortEndpoint):
        return _flow_map({"chip_port": _flow_map({"chip": ep.chip, "port": ep.port})})
    return _flow_map({"block": ep.block, "port": ep.port})


def _connection_to_node(c: Connection, existing: Any = None) -> CommentedMap:
    node = _reuse_or_new(existing)
    node["name"] = c.name
    node["from"] = _endpoint_to_node(c.source)
    node["to"] = _endpoint_to_node(c.target)
    if c.route == AUTO_ROUTE:
        node["route"] = AUTO_ROUTE
    elif isinstance(c.route, list) and c.route:
        node["route"] = _seq(_flow_map({"x": p.x, "y": p.y}) for p in c.route)
    else:
        node.pop("route", None)
    _set_optional(node, "modulation", c.modulation.value if c.modulation else None)
    _set_optional(node, "code_rate", c.code_rate)
    _set_optional(node, "iq_format", c.iq_format.value if c.iq_format else None)
    _set_optional(node, "out_tag", c.out_tag)
    # Only emit ``kind`` when non-default, so existing .kyt files stay byte-clean.
    _set_optional(node, "kind",
                  c.kind if c.kind != NET_DATA_TRIGGER else None)
    return node


def _set_mode_switching(doc: CommentedMap, project: Project) -> None:
    if not project.mode_switching:
        doc.pop("mode_switching", None)
        return
    ms = CommentedMap()
    for mode_name, overrides in project.mode_switching.items():
        mode = CommentedMap()
        mode["face_overrides"] = _seq(
            _flow_map({"chip": o.chip, "x": o.x, "y": o.y, "face": o.face.value})
            for o in overrides
        )
        ms[mode_name] = mode
    doc["mode_switching"] = ms


def _set_simulation(doc: CommentedMap, project: Project) -> None:
    s = project.simulation
    has_any = any(
        v is not None
        for v in (
            s.default_stimulus,
            s.golden_output,
            s.golden_bits,
            s.golden_symbols,
            s.gnuradio_flowgraph,
        )
    ) or bool(s.fpga_models)
    if not has_any:
        doc.pop("simulation", None)
        return
    sim = doc.get("simulation")
    if not isinstance(sim, CommentedMap):
        sim = CommentedMap()
        doc["simulation"] = sim
    _set_optional(sim, "default_stimulus", s.default_stimulus)
    _set_optional(sim, "golden_output", s.golden_output)
    _set_optional(sim, "golden_bits", s.golden_bits)
    _set_optional(sim, "golden_symbols", s.golden_symbols)
    _set_optional(sim, "gnuradio_flowgraph", s.gnuradio_flowgraph)
    sim["fpga_latency_ns"] = s.fpga_latency_ns
    if s.fpga_models:
        sim["fpga_models"] = _seq(
            _flow_map(
                {"connection": fm.connection, "type": fm.type, "params": dict(fm.params)}
            )
            for fm in s.fpga_models
        )
    else:
        sim.pop("fpga_models", None)


def _flow_map(d: dict) -> CommentedMap:
    """A mapping rendered in flow style (``{a: 1, b: 2}``) for compact rows."""
    m = CommentedMap()
    m.update(d)
    m.fa.set_flow_style()
    return m
