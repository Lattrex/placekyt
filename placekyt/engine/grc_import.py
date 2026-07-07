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

# LOGICAL-ONLY dtype converters (stock GNU Radio blocks). These make a real GRC
# flowgraph type-check where a float stream meets a complex block; they are NEVER
# placed as cells — the importer SPLICES them out, wiring the converter's upstream
# straight to its downstream with the right rail semantics
# (dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md):
#   * float_to_complex, 1 real (Q = null_source): wire the I upstream -> the
#     downstream complex block's xi (xq stays 0). No cell. [SSB's case]
#     (2 independent real producers -> a DualFloatToComplex block, added §later.)
#   * complex_to_real / complex_to_float: the upstream complex block's output cell
#     shapes the WRITE/JUMP emission (1 rail -> 1 WRITE-JUMP dropping Q, or 2 rails
#     -> fan-out). Transparent on the wire.
#   * null_source / null_sink: zero-driver / dev-null markers; consumed, never placed.
_F2C_IDS = {"blocks_float_to_complex"}
_C2F_IDS = {"blocks_complex_to_real", "blocks_complex_to_float"}
_NULL_SRC_IDS = {"blocks_null_source", "analog_null_source"}
_NULL_SINK_IDS = {"blocks_null_sink"}
_CONVERTER_IDS = _F2C_IDS | _C2F_IDS | _NULL_SRC_IDS | _NULL_SINK_IDS

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


def grc_block_params(path, catalog) -> dict:
    """``{placeKYT block name: coerced params}`` for the DSP blocks in a .grc —
    WITHOUT building a project. Uses the SAME classification, naming (``_unique``
    /``_default_name``), and ``_coerce_params`` as :func:`import_grc`, so the keys
    line up with an imported design's block names. Used by the GRC-sync file
    watcher to diff a re-saved .grc against the placed design (detect drift on
    SAVE, before any run)."""
    import yaml
    from ui.controller import _default_name

    p = Path(path)
    data = yaml.safe_load(p.read_text()) or {}
    grc_blocks = {b["name"]: b for b in data.get("blocks", []) if "name" in b}

    out: dict = {}
    names_used: list = []
    for gname, gb in grc_blocks.items():
        gid = gb.get("id", "")
        if gid in _SOURCE_IDS or gid in _SINK_IDS or gid in _NON_DSP:
            continue
        btype = _grc_id_to_type(gid, catalog)
        if btype is None:
            continue
        params = dict(gb.get("parameters", {}) or {})
        params = _coerce_params(params, catalog, btype)
        if (btype == "BPSKSlicerBlock"
                and "out_mode" not in (gb.get("parameters") or {})):
            params["out_mode"] = "bit"
        blk_name = _unique(_default_name(btype), names_used)
        names_used.append(blk_name)
        out[blk_name] = params
    return out


def _splice_converters(conns, grc_blocks):
    """Rewrite the GRC connection list to REMOVE logical-only dtype converters,
    wiring each converter's real upstream straight to its real downstream.

    Handles (dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md):
      * ``float_to_complex`` whose Q input (port 1) is a ``null_source`` (or is
        unconnected) — the SINGLE-real case: wire the I upstream (port 0 producer)
        straight to the converter's downstream (the complex block's xi). No cell.
      * ``complex_to_real`` / ``complex_to_float`` — the complex upstream wires
        straight to the float downstream(s); the upstream block's output cell
        shapes the rail emission (INV-17).
      * ``null_source`` / ``null_sink`` edges — dropped entirely.

    A ``float_to_complex`` fed by TWO independent real producers (no null_source on
    Q) is NOT spliced: it is kept in the connection list AND its name is returned in
    ``dual_f2c`` so the block pass places a ``DualFloatToComplexBlock`` for it (the
    physical LOCK rendezvous — dev_docs §4). Its port-0 (I) and port-1 (Q) producers
    wire to the block's ``i`` / ``q`` inputs; its output wires downstream via ``out``.

    Returns ``(rewritten_conns, dual_f2c_names)``.
    """
    def _id(name):
        return (grc_blocks.get(name, {}) or {}).get("id", "")

    # Index edges by source and by dest for transitive splicing.
    edges = [(e[0], str(e[1]), e[2], str(e[3])) for e in conns if len(e) >= 4]
    conv_names = {n for n in grc_blocks if _id(n) in _CONVERTER_IDS}
    dual_f2c: set = set()   # f2c names that become a DualFloatToComplex block
    if not conv_names:
        return conns, dual_f2c

    def _producers_into(name):
        """Edges feeding ``name``, keyed by dest port -> (src, src_port)."""
        out = {}
        for s, sp, d, dp in edges:
            if d == name:
                out.setdefault(dp, []).append((s, sp))
        return out

    def _consumers_of(name):
        return [(d, dp, sp) for s, sp, d, dp in edges if s == name]

    kept = []
    spliced_out = set()  # converter names fully consumed
    for name in conv_names:
        gid = _id(name)
        if gid in _NULL_SRC_IDS or gid in _NULL_SINK_IDS:
            spliced_out.add(name)
            continue
        ins = _producers_into(name)
        if gid in _F2C_IDS:
            # I = port '0' producer; Q = port '1' producer (or a null_source).
            i_prod = ins.get("0", [])
            q_prod = ins.get("1", [])
            q_is_null = (not q_prod) or all(
                _id(s) in _NULL_SRC_IDS for s, _ in q_prod)
            if i_prod and q_is_null:
                # SINGLE-real: wire the I producer -> each downstream (the complex
                # block's xi input). The complex block treats xq as 0.
                (isrc, isp) = i_prod[0]
                for (d, dp, _sp) in _consumers_of(name):
                    kept.append([isrc, isp, d, dp])
                spliced_out.add(name)
            elif i_prod and q_prod:
                # TWO real producers -> a physical DualFloatToComplex block. Keep the
                # f2c in the connection list (its port-0/1 inputs and output edges
                # stay) and flag it so the block pass places the block for it.
                dual_f2c.add(name)
            continue
        if gid in _C2F_IDS:
            # complex upstream (port '0') -> each float downstream, transparently.
            # The converter's OUTPUT port picks the rail: 0 = real (I), 1 = imag (Q).
            # complex_to_real has only output 0 (the I rail). We pass the converter
            # output index as the spliced SOURCE port; _resolve_port maps it into the
            # upstream complex block's named output ports (out_i @ index 0, out_q @ 1),
            # so the I consumer gets out_i and the Q consumer gets out_q.
            c_prod = ins.get("0", [])
            if c_prod:
                (csrc, _csp) = c_prod[0]
                for (d, dp, conv_out_port) in _consumers_of(name):
                    kept.append([csrc, conv_out_port, d, dp])
                spliced_out.add(name)
            continue

    if not spliced_out and not kept:
        return conns, dual_f2c
    # Rebuild: drop every edge touching a spliced-out converter, add the rewrites.
    result = []
    for e in conns:
        if len(e) < 4:
            continue
        if e[0] in spliced_out or e[2] in spliced_out:
            continue
        result.append(list(e))
    result.extend(kept)
    return result, dual_f2c


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
    # Splice out LOGICAL-ONLY dtype converters: rewrite the connection list so a
    # converter's upstream wires straight to its downstream (the converter is never
    # placed). See _splice_converters.
    conns, dual_f2c_names = _splice_converters(conns, grc_blocks)

    name = project_name or data.get("options", {}).get(
        "parameters", {}).get("title") or p.stem
    project = Project(metadata=ProjectMetadata(name=name), chip_type=chip_type)
    project.chips.append(ChipInstance(0, "Chip 0", 0.0, 0.0))

    # Classify GRC blocks: DSP (→ placeKYT block), source/sink (→ chip port), or
    # dropped plumbing. Unknown kyttar_* blocks are reported.
    block_map: dict = {}         # grc name → placeKYT block name
    role: dict = {}              # grc name → "block" | "source" | "sink" | "drop"
    src_stream: dict = {}        # grc source name → its stream_id param (or "")
    src_complex: dict = {}       # grc source name → injects a complex (I/Q) sample?
    sink_stream: dict = {}       # grc sink name → its stream_id param (or "")
    _INSTANCE_TYPE.clear()       # grc name → placeKYT type (for port resolution)
    unknown, dropped = [], []
    placed_idx = 0
    for gname, gb in grc_blocks.items():
        gid = gb.get("id", "")
        if gid in _SOURCE_IDS:
            role[gname] = "source"
            # SHARED-INPUT-PORT DUPLEX: remember the source's stream_id so the
            # x16_in→block net it feeds carries it (so the live bridge resolves
            # each stream to its own block via engine.port_config.stream_targets).
            sid = str(gb.get("parameters", {}).get("stream_id", "") or "").strip()
            sid = sid.strip("'\"")
            src_stream[gname] = sid
            # Does this source inject a COMPLEX (I/Q) sample or a single real float?
            # ``complex_in: complex`` ⇒ interleaved xi+xq packet (deliver all input
            # regs of the complex target block); ``complex_in: float`` (or absent)
            # ⇒ ONE real operand into a single rail (deliver only that rail's reg).
            ci = str(gb.get("parameters", {}).get("complex_in", "") or "").strip()
            src_complex[gname] = ci.strip("'\"").lower() == "complex"
            continue
        if gid in _SINK_IDS:
            role[gname] = "sink"
            ssid = str(gb.get("parameters", {}).get("stream_id", "") or "").strip()
            sink_stream[gname] = ssid.strip("'\"")
            continue
        if gname in dual_f2c_names:
            # A float_to_complex fed by TWO real producers: place the physical
            # DualFloatToComplex LOCK rendezvous (its port-0/1 inputs -> i/q, output
            # -> out). Its edges were kept by _splice_converters.
            btype = "DualFloatToComplexBlock"
        elif gid in _NON_DSP:
            role[gname] = "drop"
            dropped.append(gid)
            continue
        else:
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
    # The set of explicit block→block port pairs already wired, so a synthesised
    # complex-Q sibling is NOT added when the flowgraph ALREADY wires the Q rail
    # (some demo .grc author the xi/xq + yi/yq rails explicitly — never double-wire).
    wired_pairs: set = set()
    split_candidates: list = []   # (sname, dname, src, dst) deferred until all wired
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
        # Carry the source's stream_id onto the x16_in→block input net only
        # (source role + block target), so the live bridge keys this stream's
        # injection. Other nets stay single-stream (stream_id None).
        sid = src_stream.get(sname) if srole == "source" else None
        # Carry the source's complex-ness onto the chip-input→block net so the
        # build/port_config size host-injection data_addrs correctly: a complex
        # source delivers all of the target complex block's input regs; a float
        # source delivers only the single rail its net targets.
        scpx = src_complex.get(sname) if srole == "source" else None
        # SHARED-OUTPUT-PORT DEMUX: a block→sink net whose sink names a stream_id
        # gets a DISTINCT out_tag (a stable small int per stream_id) so the two
        # chains sharing x16_out are separable on the wire — the build sets the
        # exit WRITE's dest to this tag, and the live bridge demuxes each sink's
        # words by it (engine.port_config.stream_targets reads it back). Keyed the
        # SAME way as the input stream_id so the round trip lines up.
        out_tag = None
        if drole == "sink":
            ssid = sink_stream.get(dname)
            if ssid:
                out_tag = _stream_tag(ssid)
        project.connections.append(Connection(
            f"net{net_idx}", source=src, target=dst, route=None,
            stream_id=(sid or None), out_tag=out_tag, src_complex=scpx))
        if isinstance(src, BlockEndpoint) and isinstance(dst, BlockEndpoint):
            wired_pairs.add((src.block, src.port, dst.block, dst.port))
            split_candidates.append((sname, dname, src, dst))

    # COMPLEX (I/Q) BLOCK→BLOCK EDGE SPLIT: GNURadio represents a complex stream as ONE
    # port, so a complex link between two complex placeKYT blocks (e.g. the MF ``yi``→
    # Costas ``xi``) imports as a SINGLE net carrying only the I operand. But a placeKYT
    # complex block has TWO scalar input regs (xi=R0, xq=R1) at one cell/entry and the
    # source emits TWO outputs (yi, yq) — both MUST be wired or the target derotates
    # against a stale Q and never locks (the auto-P&R RX regression). When the resolved
    # ports are the I-half of an I/Q pair on BOTH ends, synthesise the matching Q net
    # (yq→xq) — exactly the two-delivery wiring the explicit modem demo hand-builds.
    # Deferred + deduped: a .grc that ALREADY wires the Q rail keeps its own net (never
    # double-delivered, which would fire the target twice / corrupt the derotation).
    for sname, dname, src, dst in split_candidates:
        sbt = _btype_of(block_map, sname, catalog)
        dbt = _btype_of(block_map, dname, catalog)
        sq = _iq_sibling(catalog, sbt, src.port, want_out=True)
        dq = _iq_sibling(catalog, dbt, dst.port, want_out=False)
        if sq is None or dq is None:
            continue
        if (src.block, sq, dst.block, dq) in wired_pairs:
            continue  # the Q rail is already explicitly wired — don't duplicate
        net_idx += 1
        wired_pairs.add((src.block, sq, dst.block, dq))
        project.connections.append(Connection(
            f"net{net_idx}",
            source=BlockEndpoint(block=src.block, port=sq),
            target=BlockEndpoint(block=dst.block, port=dq),
            route=None, stream_id=None, out_tag=None))

    return GrcImportResult(project=project, block_map=block_map,
                           unknown=unknown, dropped=dropped)


# -- helpers -------------------------------------------------------------------

# Stable, deterministic out_tag per stream_id for the shared-output-port demux.
# Known demo ids get fixed tags matching engine.bpsk_modem_demo (rx=5, tx=10);
# any other id derives a small nonzero tag from its name (0 = untagged/default,
# so never assign it). The same id always maps to the same tag, so the input-side
# stream and the output-side tag round-trip consistently.
_STREAM_TAGS = {"rx": 5, "tx": 10}


def _stream_tag(stream_id: str) -> int:
    sid = str(stream_id)
    if sid in _STREAM_TAGS:
        return _STREAM_TAGS[sid]
    # Deterministic small nonzero tag (1..63) for an arbitrary stream id.
    return (sum(ord(c) for c in sid) % 62) + 2


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


def _iq_sibling(catalog, btype, port, *, want_out):
    """The Q-half port name paired with an I-half ``port`` on a complex block, or
    ``None`` when ``port`` is not the I-half of an on-cell I/Q pair.

    A placeKYT complex block exposes its I/Q stream as TWO scalar ports that share
    one cell (and, for inputs, one entry) with consecutive registers — named with an
    ``i``/``q`` suffix: ``xi``/``xq`` (in), ``yi``/``yq`` (out). GNURadio collapses
    the pair into ONE complex port, so the importer wires only the I-half; this finds
    the matching Q-half so the second (Q) net can be synthesised. Returns ``None`` if
    there's no such sibling (a real scalar port, or an already-Q port) so a plain real
    link is never double-wired."""
    if btype is None or not port:
        return None
    direction = "out" if want_out else "in"
    try:
        pm = catalog.port_map(btype)
    except Exception:  # noqa: BLE001 — no PortMap → no pairing
        return None
    ports = {p.name: p for p in pm.ports if p.direction == direction}
    p = ports.get(port)
    if p is None or not port.endswith("i"):
        return None
    qname = port[:-1] + "q"
    q = ports.get(qname)
    if q is None:
        return None
    # Same cell, and (inputs) same entry — a genuine on-cell I/Q pair.
    if getattr(p, "cell_id", None) != getattr(q, "cell_id", None):
        return None
    if (not want_out
            and getattr(p, "entry", None) != getattr(q, "entry", None)):
        return None
    return qname


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
            elif dv is None:
                # An UNTYPED default (e.g. an optional ``symbol_table=None``): there
                # is no target type to coerce to, so DON'T stringify (str(None) ->
                # 'None' would make a re-coerced param drift from the real None and
                # falsely flag the block out-of-sync). Parse the literal if it is
                # one (so 'None' -> None, '5' -> 5, '[1,2]' -> [1,2]); otherwise
                # keep the raw string (a real string value the GRC set).
                try:
                    out[k] = ast.literal_eval(s)
                except (ValueError, SyntaxError):
                    out[k] = s
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
