# SPDX-License-Identifier: GPL-3.0-or-later
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
# straight to its downstream with the right rail semantics:
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
# byte<->float WIDENING casts: on the chip every stream item is one 16-bit word,
# so uchar_to_float / float_to_uchar between two chip blocks are IDENTITY on the
# wire — pure GRC type glue (e.g. diff_encoder(byte) -> psk_symbol_mapper(float)).
# Spliced transparently: the upstream's own output port wires straight through.
_PASSTHRU_IDS = {"blocks_uchar_to_float", "blocks_float_to_uchar"}
_CONVERTER_IDS = (_F2C_IDS | _C2F_IDS | _NULL_SRC_IDS | _NULL_SINK_IDS
                  | _PASSTHRU_IDS)

# Explicit GRC-id → placeKYT-type overrides where snake→Pascal+Block doesn't match.
_TYPE_OVERRIDES = {
    # The plain-Pascal "SplitterBlock" is the LEGACY duplex landing cell
    # (rx_face/tx_face) — the GRC-facing Splitter is the fan-out relay.
    "kyttar_splitter": "StreamSplitterBlock",
    "kyttar_soft_demodulator": "SoftDemodulatorBlock",
    # Complex AGC: snake->Pascal gives "AgcCcBlock" (wrong case for both the
    # AGC acronym and the CC dtype suffix) — pin the CLASS name explicitly.
    "kyttar_agc_cc": "AGCCCBlock",
    "kyttar_costas_loop": "ComplexCostasLoopBlock",
    "kyttar_gardner_ted": "GardnerTimingRecovery",
    # M&M timing recovery (16-QAM): snake→Pascal gives "MmTimingRecoveryBlock" not
    # "MMTimingRecoveryBlock", so pin it explicitly (override table uses catalog.get).
    "kyttar_mm_timing_recovery": "MMTimingRecoveryBlock",
    # FLL band-edge: snake->Pascal gives "FllBandEdgeBlock" — pin it.
    "kyttar_fll_band_edge": "FLLBandEdgeBlock",
    # LMS equalizer: snake->Pascal gives "LmsEqualizerBlock" — pin it.
    "kyttar_lms_equalizer": "LMSEqualizerBlock",
    "kyttar_complex_to_mag": "ComplexToMagBlock",
    "kyttar_complex_to_arg": "ComplexToArgBlock",
    # QPSKSlicerBlock is hidden in the catalog (uncurated), so the case-insensitive
    # snake→Pascal fallback — which iterates catalog.all() — can't reach it; map it
    # explicitly (the override table uses catalog.get, which sees hidden specs).
    "kyttar_qpsk_slicer": "QPSKSlicerBlock",
    # M17 4FSK modem blocks — map explicitly (snake→Pascal gives "Fsk4..." which
    # the case-insensitive fallback would still match, but the block is uncurated/
    # hidden in the palette, so pin it via the override table which uses catalog.get).
    "kyttar_fsk4_symbol_mapper": "FSK4SymbolMapperBlock",
    "kyttar_fsk4_slicer": "FSK4SlicerBlock",
    "kyttar_fsk4_sync_timing_recovery": "FSK4SyncTimingRecoveryBlock",
    # digital.map_bb per-symbol LUT remap. snake→Pascal gives "MapBbBlock" not
    # "MapBBBlock", so pin it explicitly (the override table uses catalog.get).
    "kyttar_map_bb": "MapBBBlock",
    # 16-QAM modem blocks — snake→Pascal would give "Qam16..." not "QAM16...", so pin
    # them explicitly (the override table uses catalog.get, which sees hidden specs).
    "kyttar_qam16_symbol_mapper": "QAM16SymbolMapperBlock",
    "kyttar_qam16_slicer": "QAM16SlicerBlock",
    "kyttar_qam16_costas_loop": "QAM16ComplexCostasLoopBlock",
    # Freq-xlating FIR: the class is "FreqXlatingFIRBlock" but the verification
    # manifest lists it under the LEGACY short name "FreqXlatingFIR" (which the
    # catalog registers as an alias, see catalog._MANIFEST_ALIASES). Pin the id to the
    # concrete CLASS type_name — the whole build/sim/test path speaks class names; the
    # manifest legacy name is only an alias resolved at catalog.get() boundaries, and the
    # INV-22 gate treats the two as equivalent via _MANIFEST_ALIASES.
    "kyttar_freq_xlating_fir": "FreqXlatingFIRBlock",
    # Add-Const: class "AddConstBlock", manifest legacy name "AddConst" (catalog alias).
    "kyttar_add_const": "AddConstBlock",
    # Two-complex-stream add/sub/multiply: snake→Pascal gives "AddCcBlock"/
    # "SubCcBlock"/"MultiplyCcBlock" (wrong case for the CC dtype suffix) —
    # pin the CLASS names explicitly.
    "kyttar_add_cc": "AddCCBlock",
    "kyttar_sub_cc": "SubCCBlock",
    "kyttar_multiply_cc": "MultiplyCCBlock",
    # Complex FIR (fir_filter_ccf): class == manifest name "ComplexFIRFilterBlock",
    # but snake→Pascal of "kyttar_complex_fir_filter" gives "ComplexFirFilterBlock"
    # (lower-case "ir"), which won't match — pin it explicitly.
    "kyttar_complex_fir_filter": "ComplexFIRFilterBlock",
    "kyttar_iir_biquad": "IIRBiquadBlock",
    "kyttar_conv_encoder_k7": "ConvEncoderK7Block",
    "kyttar_lfsr_scrambler": "LFSRScramblerBlock",
    # Frame CRC-16 (placeKYT-native, no GR counterpart). snake→Pascal of
    # "kyttar_crc16" happens to give "Crc16Block", but pin it explicitly so the
    # mapping never depends on the fallback's casing of the digit suffix.
    "kyttar_crc16": "Crc16Block",
    # pack_k_bits: snake→Pascal gives "PackKBitsBlock" but the mid-word single-letter
    # "k" makes the fallback fragile; pin it explicitly (override uses catalog.get).
    "kyttar_pack_k_bits": "PackKBitsBlock",
    "kyttar_viterbi_bmu": "ViterbiBranchMetricBlock",
    "kyttar_diff_decoder": "DiffDecoderBlock",
    # QuadratureDemod (FM demod): the curated manifest names it by the GR-aligned short
    # name ``QuadratureDemod``, but the concrete class (used everywhere in build/sim) is
    # ``QuadratureDemodBlock`` — resolve to the CLASS name; the manifest name is a catalog
    # alias (see _MANIFEST_ALIASES) and the gate treats them as equivalent.
    "kyttar_quadrature_demod": "QuadratureDemodBlock",
    # SRAM-backed ham blocks + the SRAM controller ([Kyttar]-native, no GR
    # counterpart). snake→Pascal gives "CwKeyerBlock"/"CwDecoderBlock" (wrong case
    # for the CW acronym), so those two MUST be pinned. The others match by name,
    # but these specs may be uncurated/hidden in the catalog — the case-insensitive
    # fallback iterates catalog.all() (visible only), so pin all six via the override
    # table (which uses catalog.get, seeing hidden specs) to be reliable.
    "kyttar_varicode_encoder": "VaricodeEncoderBlock",
    "kyttar_varicode_decoder": "VaricodeDecoderBlock",
    "kyttar_cw_keyer": "CWKeyerBlock",
    "kyttar_cw_decoder": "CWDecoderBlock",
    "kyttar_raised_cosine_envelope": "RaisedCosineEnvelopeBlock",
    "kyttar_sram_controller": "SramControllerBlock",
    # STOCK GNU Radio blocks.repeat (hold-upsampler) maps 1:1 to RepeatBlock —
    # a canonical .grc that uses the stock block imports without a kyttar marker.
    "blocks_repeat": "RepeatBlock",
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
    variables = _grc_variables(grc_blocks)

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
        params = _coerce_params(params, catalog, btype, variables)
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

    Handles:
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
    physical LOCK rendezvous). Its port-0 (I) and port-1 (Q) producers
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
        if gid in _PASSTHRU_IDS:
            # byte<->float glue: wire the upstream's OWN output port straight to
            # each downstream (identity on the chip wire — words are words).
            prod = ins.get("0", [])
            if prod:
                (usrc, usp) = prod[0]
                for (d, dp, _sp) in _consumers_of(name):
                    kept.append([usrc, usp, d, dp])
                spliced_out.add(name)
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
    variables = _grc_variables(grc_blocks)
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
    _INSTANCE_PARAMS.clear()     # grc name → coerced params (for param-correct ports)
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
        params = _coerce_params(params, catalog, btype, variables)
        # GRC flowgraphs are visualization-first: a BPSK slicer feeding a Time
        # Sink wants one 0/1 word per recovered bit (a clean toggle plot), not the
        # block's production default of 16-bit packed words. If the .grc didn't
        # set out_mode explicitly, default it to 'bit' for the imported demo. A
        # .grc that DOES specify out_mode (packed) is respected.
        if (btype == "BPSKSlicerBlock"
                and "out_mode" not in (gb.get("parameters") or {})):
            params["out_mode"] = "bit"
        # RRCPulseShaperBlock is parameterised by firdes (sampling_freq, symbol_rate,
        # alpha, ntaps), but the GRC binding exposes the friendlier alpha/span/sps. The
        # names don't match, so _coerce_params keeps the block's default (sps=4,
        # ntaps=33) even when the .grc set sps=2 -> a mismatched filter that garbles a
        # 2-sps chain. Translate span/sps here: sampling_freq=sps, symbol_rate=1,
        # ntaps=span*sps+1 (firdes.root_raised_cosine length convention).
        if btype == "RRCPulseShaperBlock":
            gparams = gb.get("parameters", {}) or {}
            _sps = _try_int(gparams.get("sps"))
            _span = _try_int(gparams.get("span"))
            if _sps:
                params["sampling_freq"] = float(_sps)
                params["symbol_rate"] = 1.0
                if _span:
                    params["ntaps"] = _span * _sps + 1
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
        _INSTANCE_PARAMS[gname] = params
        placed_idx += 1

    # Connections → logical nets. Drop nets touching a dropped block; map
    # source→block to chip-input→block, block→sink to block→chip-output.
    net_idx = 0
    # The set of explicit block→block port pairs already wired, so a synthesised
    # complex-Q sibling is NOT added when the flowgraph ALREADY wires the Q rail
    # (some demo .grc author the xi/xq + yi/yq rails explicitly — never double-wire).
    wired_pairs: set = set()
    # Per-import stream-id -> output tag map (deterministic, collision-probed,
    # confined to the 5-bit DEST field — see _stream_tag).
    _tag_by_sid: dict = {}
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
                if ssid not in _tag_by_sid:
                    _tag_by_sid[ssid] = _stream_tag(ssid,
                                                    set(_tag_by_sid.values()))
                out_tag = _tag_by_sid[ssid]
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
        sq = _iq_sibling(catalog, sbt, src.port, want_out=True,
                         params=_params_of(catalog, sbt, sname))
        dq = _iq_sibling(catalog, dbt, dst.port, want_out=False,
                         params=_params_of(catalog, dbt, dname))
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

    # COMPLEX BLOCK → CHIP OUTPUT PORT SPLIT: a complex-output block (e.g. a
    # FrequencyModulator emitting yi/yq) feeding the chip output port arrives as ONE
    # gr_complex wire, so only the I-half (yi) net was created above — the Q rail
    # (yq) is then never routed and the FM emits it anyway onto the port, garbling
    # the stream. Synthesise the yq→x16_out net so BOTH rails egress: the build tags
    # them out_tag / out_tag+1 and the live bridge reassembles the interleaved I/Q
    # (engine.sim_bridge complex-egress path). Mirrors the block→block split above,
    # but the target is the chip OUTPUT port, not a second block.
    for c in list(project.connections):
        src, dst = c.source, c.target
        if not (isinstance(src, BlockEndpoint)
                and isinstance(dst, ChipPortEndpoint)
                and getattr(dst, "port", None) == "x16_out"):
            continue
        # src.block is the placeKYT block NAME; look up its TYPE from the project.
        sblk = project.block(src.block)
        sbt = sblk.type if sblk is not None else None
        sq = _iq_sibling(catalog, sbt, src.port, want_out=True,
                         params=(sblk.params if sblk is not None else None))
        if sq is None:
            continue  # not a complex-output rail (a real scalar output) — leave it
        if any(isinstance(o.source, BlockEndpoint)
               and o.source.block == src.block and o.source.port == sq
               for o in project.connections):
            continue  # yq already wired somewhere — don't duplicate
        net_idx += 1
        project.connections.append(Connection(
            f"net{net_idx}",
            source=BlockEndpoint(block=src.block, port=sq),
            target=ChipPortEndpoint(chip=0, port="x16_out"),
            route=None, stream_id=None,
            out_tag=(c.out_tag + 1) if c.out_tag is not None else None))

    # OUTPUT FAN-OUT SPLICE: GNU Radio fans a port out implicitly, but on the
    # chip every extra arm costs the SOURCE cell exit words (one WRITE+JUMP
    # pair), and most single-rail blocks are authored tight (GainBlock: 3 exit
    # words). Splice an explicit StreamSplitterBlock — a near-empty relay authored
    # with a reserved fan-out tail (up to 8 arms) — whenever a single-rail
    # block output feeds ≥2 DIFFERENT blocks or ≥3 inputs, so a plain .grc
    # fan-out just works. Left DIRECT (no splitter): a 2-input same-block
    # fan-in (the packet form fits even a tight source), complex-rail sources
    # (the INV-17 two-rail form owns those), and SplitterBlock sources
    # themselves (their reserved tail IS the fan-out; also keeps this pass
    # from splicing its own output).
    by_out: dict = {}
    for c in project.connections:
        if (isinstance(c.source, BlockEndpoint)
                and isinstance(c.target, BlockEndpoint)):
            by_out.setdefault(("block", c.source.block, c.source.port), []) \
                .append(c)
        elif (isinstance(c.source, ChipPortEndpoint)
                and isinstance(c.target, BlockEndpoint)):
            # PORT fan-out arms group per (port, stream): a duplex port carries
            # SEPARATE streams (tx/rx) — those are not a fan-out and keep the
            # proven ≤2-arm multi-landing injection. ≥3 arms of ONE stream
            # reduce to port→splitter→arms (each piece individually proven).
            by_out.setdefault(("port", c.source.port, c.stream_id), []) \
                .append(c)
    for key, conns in by_out.items():
        if len(conns) < 2:
            continue
        if key[0] == "port":
            if len(conns) < 3:
                continue                  # ≤2 arms: proven multi-landing path
            sblk_name, sport = None, None
        else:
            _k, sblk_name, sport = key
            sblk = project.block(sblk_name)
            if sblk is None or sblk.type in ("StreamSplitterBlock", "SplitterBlock"):
                continue
            if _iq_sibling(catalog, sblk.type, sport, want_out=True,
                           params=sblk.params) is not None:
                continue                  # complex rail — INV-17 territory
            tgt_blocks = {c.target.block for c in conns}
            if len(tgt_blocks) < 2 and len(conns) < 3:
                continue                  # same-target pair: direct packet form
        from model.block import Block
        from model.placement import Placement
        base = sblk_name if sblk_name is not None else key[1].rstrip("_in")
        sp_name = _unique(f"{base}_split", block_map.values(),
                          [b.name for b in project.blocks])
        cells, transit = _default_cells(catalog, "StreamSplitterBlock", {},
                                        placed_idx)
        placed_idx += 1
        sp = Block(sp_name, "StreamSplitterBlock", library=None, params={})
        sp.placement = Placement(chip=0, cells=cells, transit_cells=transit)
        project.blocks.append(sp)
        net_idx += 1
        if key[0] == "port":
            # the spliced feed inherits the arms' stream identity (the live
            # bridge injects by stream_id); the arms become plain block nets.
            feed_src = ChipPortEndpoint(chip=0, port=key[1])
            feed = Connection(
                f"net{net_idx}", source=feed_src,
                target=BlockEndpoint(block=sp_name, port="x"), route=None,
                stream_id=conns[0].stream_id,
                src_complex=getattr(conns[0], "src_complex", False))
            for c in conns:
                c.stream_id = None
        else:
            feed = Connection(
                f"net{net_idx}",
                source=BlockEndpoint(block=sblk_name, port=sport),
                target=BlockEndpoint(block=sp_name, port="x"), route=None)
        project.connections.append(feed)
        for c in conns:
            c.source = BlockEndpoint(block=sp_name, port="out")

    # JOIN TRIGGER ELECTION: a dataflow JOIN (independent arms into one
    # multi-input block — Add/Subtract/Multiply) fires once per arm as imported
    # (each arm's handoff is WRITE+JUMP), double/triple-firing the combiner.
    # Blocks that declare a ``sink`` (data-only HALT) entry support single-fire
    # joins: elect the DEEPEST arm (longest upstream cell path — the last to
    # complete under the per-sample paced drive) as THE trigger and point every
    # other arm's JUMP at ``sink`` via the existing Connection.entry_override.
    _elect_join_triggers(project, catalog)

    # SRAM-PANEL SYNTHESIS (INV-31): a panel-backed block (Varicode, CW keyer)
    # needs the panel + panel_connections + the x1_in push-read return net in the
    # project, or the downstream placer/router has an incomplete design. The
    # block class declares what it needs (panel_requirements); the importer
    # completes the project here so import -> auto_pnr -> build just works.
    from engine.panel_pnr import synthesize_panel
    synthesize_panel(project, catalog)

    return GrcImportResult(project=project, block_map=block_map,
                           unknown=unknown, dropped=dropped)


# -- helpers -------------------------------------------------------------------


def _grc_variables(grc_blocks) -> dict:
    """``{variable name: value string}`` for the flowgraph's ``variable`` blocks,
    so a param that names a variable (``interp: sps``) resolves to its VALUE at
    import (see _coerce_params). Values stay strings — coercion happens per-param
    against the block's default type; an expression value simply fails coercion
    there (the block default is kept, the pre-existing behavior)."""
    out = {}
    for name, gb in grc_blocks.items():
        if gb.get("id") == "variable":
            v = (gb.get("parameters") or {}).get("value")
            if v is not None:
                out[name] = str(v)
    return out

# Stable, deterministic out_tag per stream_id for the shared-output-port demux.
# Known demo ids get fixed tags (rx=5, tx=10);
# any other id derives a small nonzero tag from its name (0 = untagged/default,
# so never assign it). The same id always maps to the same tag, so the input-side
# stream and the output-side tag round-trip consistently.
_STREAM_TAGS = {"rx": 5, "tx": 10}


def _stream_tag(stream_id: str, used: set | None = None) -> int:
    """A deterministic output tag for ``stream_id`` that FITS THE WIRE.

    The tag rides in the exit WRITE's DEST field, which is 5 BITS (0..31) — a
    tag above 31 silently wraps on the chip (tag 36 emitted as dest 4) while
    the host demux compares the full value, so every word of that stream is
    dropped (found via the audio/meter duplex example; the fixed 'rx'/'tx'
    tags 5/10 always fit, which is why the modems never hit it). Derived tags
    are therefore confined to 2..31, with linear probing over ``used`` to keep
    two arbitrary stream ids from colliding within one import."""
    sid = str(stream_id)
    if sid in _STREAM_TAGS:
        return _STREAM_TAGS[sid]
    span = 30                                    # tags 2..31
    tag = (sum(ord(c) for c in sid) % span) + 2
    if used is not None:
        while tag in used:
            tag = (tag - 2 + 1) % span + 2
    return tag


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
        port = _resolve_port(catalog, btype, grc_port, want_out=is_src,
                             params=_params_of(catalog, btype, gname))
        return BlockEndpoint(block=bn, port=port)
    return None


def _btype_of(block_map, gname, catalog):
    """The placeKYT type name for a GRC instance (recorded during the block pass);
    None if unknown, which makes the port resolver fall back to the default."""
    return _INSTANCE_TYPE.get(gname)


# Populated during import: GRC instance name → placeKYT type name, so the
# connection pass can resolve ports against the right PortMap.
_INSTANCE_TYPE: dict = {}
# GRC instance name → coerced params. Port SETS are PARAM-DEPENDENT (the order-4
# Costas exposes yq_tap and the complex Gardner exposes yi_e/yq_e — neither present
# in the default order-2 / real-mode PortMap), so the port resolver MUST build the
# PortMap with the instance's params or a numeric port index (1 → yq_tap) collapses
# onto ports[0] and the Q rail is silently misrouted/dropped.
_INSTANCE_PARAMS: dict = {}


def _try_int(v):
    """Best-effort int of a GRC param string (returns None on a variable/expression)."""
    if v is None:
        return None
    try:
        return int(float(str(v).strip().strip("'\"")))
    except (ValueError, TypeError):
        return None


def _params_of(catalog, btype, gname=None):
    """The instance's coerced params (recorded during the block pass), for building
    a PARAM-CORRECT PortMap. Falls back to the type defaults when the instance is
    unknown (e.g. the sync file-watcher's paramless probe)."""
    if gname is not None and gname in _INSTANCE_PARAMS:
        return _INSTANCE_PARAMS[gname]
    spec = catalog.get(btype) if btype is not None else None
    return spec.default_params() if spec else None


def _resolve_port(catalog, btype, grc_port, *, want_out, params=None):
    """Map a GRC port name to a real block port name, validated against the
    block's PortMap. ``want_out`` picks the output side (source endpoint) vs the
    input side (target endpoint). Falls back to the first port on that side.

    ``params`` selects the PARAM-DEPENDENT port set (order-4 Costas yq_tap, complex
    Gardner yi_e/yq_e); without it a numeric index past the default set collapses."""
    direction = "out" if want_out else "in"
    ports = []
    if btype is not None:
        try:
            pm = catalog.port_map(btype, params)
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
            if not want_out:
                # TWO-COMPLEX-STREAM blocks (>= 2 complete on-cell I/Q input
                # pairs — add_cc/sub_cc: ai/aq + bi/bq): GNURadio's numeric
                # index counts COMPLEX ports, so index 1 is the SECOND STREAM's
                # I-half (bi), NOT the Q-half of the first (aq). Collapse each
                # pair to its I-half for indexing; the I/Q split pass then
                # synthesises the matching Q net per stream. Gated on >= 2
                # pairs so single-pair blocks (xi/xq, in_i/in_q, the dual's
                # i/q) keep the raw positional mapping they always had.
                qhalves = {}
                for nm in ports:
                    q = _iq_sibling(catalog, btype, nm, want_out=False,
                                    params=params)
                    if q:
                        qhalves[nm] = q
                if len(qhalves) >= 2:
                    qset = set(qhalves.values())
                    indexable = [nm for nm in ports if nm not in qset]
                    if 0 <= i < len(indexable):
                        return indexable[i]
            if 0 <= i < len(ports):
                return ports[i]
    return ports[0]


def _join_entry_addr(catalog, blk, entry_name: str):
    """The resolved address of ``blk``'s landing-cell ``entry_name`` entry
    (``join`` = the counting-join entry every arm targets; ``sink`` = the
    legacy data-only HALT), or None when the block does not declare it — the
    capability marker for join support."""
    try:
        from gr_kyttar.placement.resolver import CellProgramResolver
        inst = catalog.instantiate(blk.type, "__join_probe__", blk.params,
                                   library=blk.library)
        cps = inst.build_cell_programs()
        cp = next(p for p in cps.values() if getattr(p, "inputs", None))
        if not any(e.name == entry_name for e in (cp.entries or ())):
            return None
        entries = CellProgramResolver().compute_entry_addresses(cp)
        return int(entries[entry_name])
    except Exception:  # noqa: BLE001 — unresolvable → no join support
        return None


def _sink_entry_addr(catalog, blk):
    """Back-compat alias: the legacy ``sink`` entry address (or None)."""
    return _join_entry_addr(catalog, blk, "sink")


def _elect_join_triggers(project, catalog) -> None:
    """Single-fire JOIN election (see the call site). For every block fed by ≥2
    nets from ≥2 DISTINCT sources: if it declares a ``sink`` entry, the arm with
    the LONGEST upstream cell path keeps the default (compute) entry and every
    other arm gets ``entry_override = sink`` — its JUMP deposits nothing and
    HALTs, so the combiner fires exactly once per sample, after (under the
    per-sample paced drive) all operands have landed. Two arms from ONE source
    (a complex yi/yq pair) are left alone — that pair is already a single
    multi-WRITE + one-JUMP burst.

    LIMIT (documented): election orders arrivals by path depth, which the
    per-sample paced drive realizes deterministically; a SATURATED (slammed)
    drive can race operands across samples — joins are per-sample-paced designs.
    """
    from model.connection import BlockEndpoint, ChipPortEndpoint

    blocks = {b.name: b for b in project.blocks}
    incoming: dict = {}
    for c in project.connections:
        if isinstance(c.target, BlockEndpoint) and c.target.block in blocks:
            incoming.setdefault(c.target.block, []).append(c)

    _cells: dict = {}

    def cell_count(bname):
        if bname not in _cells:
            b = blocks[bname]
            try:
                _cells[bname] = int(catalog.instantiate(
                    b.type, "__depth_probe__", b.params,
                    library=b.library).cell_count)
            except Exception:  # noqa: BLE001
                _cells[bname] = 1
        return _cells[bname]

    _depth: dict = {}

    def depth(bname):
        """Longest upstream path INTO ``bname`` in placed cells (0 = fed by the
        chip input port directly)."""
        if bname in _depth:
            return _depth[bname]
        _depth[bname] = 0                      # cycle guard
        best = 0
        for c in incoming.get(bname, ()):  # noqa: B023
            if isinstance(c.source, BlockEndpoint):
                s = c.source.block
                if s in blocks:
                    best = max(best, depth(s) + cell_count(s))
        _depth[bname] = best
        return best

    for tname, arms in incoming.items():
        def _src_key(c):
            return (c.source.block if isinstance(c.source, BlockEndpoint)
                    else "__PORT__")
        if len(arms) < 2 or len({_src_key(c) for c in arms}) < 2:
            # Arms from ONE source are a single burst (a complex yi/yq pair's
            # multi-WRITE + one JUMP, or a single-rail packet fan-in) — one
            # trigger already, no election/counting needed.
            continue
        # COUNTING JOIN (preferred): every arm targets the ``join`` entry; the
        # combiner fires on the LAST arrival in ANY order. Immune to the
        # equal-depth sibling race (two arms through one splitter) that
        # deepest-arm election cannot order.
        join = _join_entry_addr(catalog, blocks[tname], "join")
        if join is not None:
            for c in arms:
                if getattr(c, "entry_override", None) is None:
                    c.entry_override = join
            continue
        # LEGACY election (blocks with only a ``sink`` entry): deepest arm
        # keeps the compute entry, the rest deposit-and-halt.
        sink = _sink_entry_addr(catalog, blocks[tname])
        if sink is None:
            continue                            # block has no join support
        def _arm_depth(c):
            if isinstance(c.source, BlockEndpoint):
                s = c.source.block
                return depth(s) + cell_count(s) if s in blocks else 0
            return 0                            # port arm: no upstream cells
        trigger = max(arms, key=lambda c: (_arm_depth(c), c.target.port))
        for c in arms:
            if c is trigger or getattr(c, "entry_override", None) is not None:
                continue
            c.entry_override = sink


def _iq_sibling(catalog, btype, port, *, want_out, params=None):
    """The Q-half port name paired with an I-half ``port`` on a complex block, or
    ``None`` when ``port`` is not the I-half of an on-cell I/Q pair.

    A placeKYT complex block exposes its I/Q stream as TWO scalar ports that share
    one cell (and, for inputs, one entry) with consecutive registers — named with an
    ``i``/``q`` marker right after the ``x``/``y`` rail prefix: ``xi``/``xq`` (in),
    ``yi``/``yq`` (out), and the PARAM-DEPENDENT tapped forms ``yi_tap``/``yq_tap``
    (order-4 Costas) and ``yi_e``/``yq_e`` (complex Gardner) where the ``i``/``q`` is
    NOT at the end of the name. GNURadio collapses the pair into ONE complex port, so
    the importer wires only the I-half; this finds the matching Q-half so the second
    (Q) net can be synthesised. Returns ``None`` if there's no such sibling (a real
    scalar port, or an already-Q port) so a plain real link is never double-wired."""
    if btype is None or not port:
        return None
    direction = "out" if want_out else "in"
    try:
        pm = catalog.port_map(btype, params)
    except Exception:  # noqa: BLE001 — no PortMap → no pairing
        return None
    ports = {p.name: p for p in pm.ports if p.direction == direction}
    p = ports.get(port)
    if p is None:
        return None
    # Find the Q-half by flipping the I-half's ``i`` MARKER to ``q``. Two naming
    # conventions coexist across the catalog, so try BOTH and take whichever names a
    # REAL Q port on the SAME cell:
    #   * trailing ``i``  — ``xi``->``xq``, ``yi``->``yq``, ``in_i``->``in_q`` (slicer)
    #   * position-1 ``i`` after an ``x``/``y`` prefix — the PARAM-DEPENDENT tapped
    #     forms ``yi_tap``->``yq_tap`` (order-4 Costas) / ``yi_e``->``yq_e`` (complex
    #     Gardner), where the marker is NOT at the end (a trailing-only rule wrongly
    #     yields ``yi_taq``/``yi_q`` and misses the real Q rail — the QPSK RX import
    #     bug this guards: the Costas/Gardner Q rails silently un-wired).
    #   * ``re``/``im`` — the CONVERTER-class rails (``re``->``im``,
    #     ``out_re``->``out_im``: FloatToComplex, Conjugate, ComplexToReal/Imag).
    #     Without this rule their Q rails silently never get wired: the target
    #     conjugates/selects against a stale 0 and the source's unpatched Q WRITE
    #     leaks to the port (the channel_selector all-zeros bug, and the root of
    #     the long-fragile converter-flavors live recovery).
    cands = []
    if port.endswith("i"):
        cands.append(port[:-1] + "q")
    if len(port) >= 2 and port[0] in ("x", "y") and port[1] == "i":
        cands.append(port[0] + "q" + port[2:])
    if port == "re":
        cands.append("im")
    if port.endswith("_re"):
        cands.append(port[:-3] + "_im")
    qname = next((c for c in cands if c != port and c in ports), None)
    if qname is None:
        return None
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


def _coerce_params(params, catalog, btype, variables=None):
    """Keep only the params the block accepts, coercing GRC string values to the
    spec's default TYPE. GRC stores everything as strings; a value that can't be
    coerced to the default's type — a GRC variable name (``fir_taps``) or a Python
    expression (``firdes.low_pass(...)``) we can't safely evaluate — is OMITTED so
    the block keeps its own default. This is the difference between importing a
    multi-block flowgraph and crashing on a non-scalar/expression param.

    ``variables`` maps the flowgraph's ``variable`` block names to their value
    strings: a param whose value is EXACTLY a variable name is substituted before
    coercion (one level). Without this, ``interp: sps`` silently kept the block
    default — the imported PSK31 repeat ran at interp=4 instead of 8 and the
    envelope at sps=256 instead of 8 (an all-zero TX that LOOKED like a routing
    bug). Expression-valued variables (``len(message)``) still coerce-fail and
    keep the default, as before."""
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
        if variables and s in variables:
            s = str(variables[s]).strip().strip("'\"")
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
    from model.placement import PlacedCell
    from model.enums import Face

    layout = catalog.default_layout(btype, params) or {0: (0, 0, "east")}
    # Spread blocks diagonally so the initial (pre-auto-place) project is valid
    # and non-overlapping; auto-place then flow-orders them.
    ox, oy = (idx * 3) % 8, (idx // 2) % 6
    # Internal routing/feedback cells (``transit_*`` ids) are FIRST-CLASS block
    # cells — emit them as ordinary ``PlacedCell``s alongside the program cells.
    cells = []
    for cid, (dx, dy, face) in layout.items():
        x, y = ox + dx, oy + dy
        cells.append(PlacedCell(cid, x, y, Face.from_str(face)))
    return cells, []


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
