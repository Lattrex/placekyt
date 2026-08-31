# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless LZ4 stream demo — compression END TO END on one array.

Two SRAM-panel-backed blocks on the SAME 10x12 chip — the panel design limit —
each with its own stream:

  raw : 1 KB payload + the end-of-block sentinel (256) -> LZ4EncoderBlock
        -> the compressed LZ4 block on x16_out (tag 'raw'). The payload
        switches character mid-stream: 512 highly repetitive bytes, then 512
        random bytes.
  cmp : the compressed bytes -> LZ4DecoderBlock -> the recovered payload on
        x16_out (tag 'cmp').

The two streams are chained by the CLIENT (the compressed bytes come off the
chip and are re-injected), not by an on-chip net — on purpose. The SRAM panel
protocol is single-outstanding per WORD, so two controllers bursting at once
would interleave register writes at the port merge and corrupt each other's
transactions. The per-sample paced server (the panel contract) makes the
client hand-off temporally exclusive BY CONSTRUCTION: the whole encode runs
to quiescence inside the sentinel injection's settle, so the first compressed
byte cannot reach the decoder until the encoder is idle.

PANEL ADDRESSING (measured, load-bearing): ``SramControllerBlock.addr_base``
offsets ONLY the lookup path — the write counter always starts at 0 — so a
read-write client cannot be relocated to a based region. The decoder
therefore runs in its proven ``addr_base=0`` configuration and reuses
``[0, len)`` SEQUENTIALLY after the encoder: every decoder read is of an
address the decoder itself wrote earlier in the same batch (the format's
append-before-fetch invariant), never an encoder leftover. The aliasing gate
in ``verification/tests/test_lz4_stream_example.py`` proves this by decoding
a stream that DISAGREES with what the encoder left in the panel.

GOLDENS: ``encode_model`` (the encoder's pinned model — its output is also
accepted by the independent reference C decoder in the block's own suite) and
``decode_model`` (the pinned LZ4 block-format transcription). The round trip
is asserted byte-exact over the full 1 KB.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/lz4_stream/lz4_stream_demo.py
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"),
           str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "lz4_stream.grc"
KYT_PATH = HERE / "lz4_stream.kyt"

# ---- the stimulus: 512 highly repetitive bytes, then 512 random bytes.
PAYLOAD_REP = list((b"KYTTAR LZ4 STREAM! " * 27)[:512])
_r = random.Random(7)
PAYLOAD_RND = [_r.randrange(256) for _ in range(512)]
PAYLOAD = PAYLOAD_REP + PAYLOAD_RND
EOB = 256                      # the encoder's out-of-band end-of-block word

# ---- the panel split (the encoder's two regions are disjoint by class rule;
# the decoder shares [0, len) sequentially — see the module docstring).
ENC_WINDOW = 32768
ENC_HASH_BITS = 12
DEC_WINDOW = 65536


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def goldens():
    """(compressed golden, per-half compressed sizes) from the pinned model."""
    from gr_kyttar.placement.blocks.lz4_encoder_block import encode_model
    full, _ = encode_model(PAYLOAD, ENC_WINDOW, ENC_HASH_BITS)
    rep, _ = encode_model(PAYLOAD_REP, ENC_WINDOW, ENC_HASH_BITS)
    rnd, _ = encode_model(PAYLOAD_RND, ENC_WINDOW, ENC_HASH_BITS)
    return full, len(rep), len(rnd)


def apply_hand_pnr(project, cat, ct):
    """Hand-place both panel clients + the four crossover forks, draw every
    corridor, and derive the placement-dependent parameters.

    The two folds are PURE TRANSLATIONS of the blocks' proven
    ``default_layout`` shapes (translation preserves every internal edge):
    encoder CTL at (8,7) (cells cols 2-8, rows 5-7), decoder CTL at (6,10)
    (cells cols 2-6, rows 9-10). One x1 port pair serves both controllers
    (their to-panel corridors merge same-direction into (9,11)); the shared
    x1_in/x16_in corridors fork at CrossoverBlocks, the only cell class two
    corridors may share:

      xoI (0,1):  cmp input transits SOUTH; raw input lands, re-emits EAST
      xoT (1,8):  cmp input transits EAST;  enc pushes land, re-emit N -> RET
      xoR (1,11): dec pushes transit EAST;  enc pushes land, re-emit N -> xoT
      xoO (8,9):  enc panel words transit SOUTH; the decoder egress lands,
                  re-emits EAST onto col 9 north to x16_out
    """
    from engine.panel_pnr import _resolve_cell
    from model.block import Block
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell

    enc = next((b for b in project.blocks if b.type == "LZ4EncoderBlock"),
               None)
    dec = next((b for b in project.blocks if b.type == "LZ4DecoderBlock"),
               None)
    if enc is None or dec is None:
        raise RuntimeError("expected an LZ4EncoderBlock and an "
                           "LZ4DecoderBlock in the imported design")
    # The DECODER must be the first panel-backed block: the build-time
    # ``refresh_panel_params`` re-derives descriptors for ``backed[0]`` only,
    # and its direct-landing formula matches the decoder's return corridor
    # exactly. With the ENCODER first it would also rewrite the first
    # crossover's ``entry_a`` from the encoder's CONTROLLER-cell entry map —
    # measured: the raw stream then lands on the ingest cell with the wrong
    # entry and the chip runs to QueueEmpty having written nothing.
    rest = [b for b in project.blocks if b is not enc and b is not dec]
    project.blocks[:] = [dec, enc] + rest

    # stream ids / output tags from the imported nets (the importer assigns a
    # deterministic out_tag per stream_id)
    def _tag(block_name):
        for c in project.connections:
            if (isinstance(c.source, BlockEndpoint)
                    and c.source.block == block_name
                    and isinstance(c.target, ChipPortEndpoint)
                    and getattr(c, "out_tag", None) is not None):
                return int(c.out_tag)
        raise RuntimeError(f"no tagged x16_out net for {block_name}")

    tag_enc, tag_dec = _tag(enc.name), _tag(dec.name)

    # ---- placement: pure translations of the proven folds
    enc_lay = cat.instantiate(enc.type, enc.name, enc.params,
                              library=enc.library).default_layout()
    dec_lay = cat.instantiate(dec.type, dec.name, dec.params,
                              library=dec.library).default_layout()
    enc.placement = Placement(0, [
        PlacedCell(cid, dx + 8, dy + 7, Face.from_str(f))
        for cid, (dx, dy, f) in sorted(enc_lay.items())])
    dec.placement = Placement(0, [
        PlacedCell(cid, dx + 2, dy + 9, Face.from_str(f))
        for cid, (dx, dy, f) in sorted(dec_lay.items())])

    # ---- the crossover forks
    def xo(name, x, y, face, **params):
        b = project.block(name)
        if b is None:
            b = Block(name, "CrossoverBlock", library=enc.library,
                      params=params)
            project.blocks.append(b)
        else:
            b.params.update(params)
        b.placement = Placement(0, [PlacedCell(0, x, y, Face.from_str(face))])
        return b

    xoI = xo("xoI", 0, 1, "south", face_a="east", hop_a=6,
             restore_face="south")
    xoT = xo("xoT", 1, 8, "east", face_a="north", hop_a=2,
             restore_face="east")
    xoR = xo("xoR", 1, 11, "east", face_a="north", hop_a=3,
             restore_face="east")
    xoO = xo("xoO", 8, 9, "south", face_a="east", hop_a=11,
             restore_face="south")

    enc_ret_entries, enc_ret_named, _ = _resolve_cell(cat, enc, 1)
    enc_in_entry, enc_in_regs = cat.resolved_io(enc.type, enc.params,
                                                library=enc.library)
    dec_emit_entries, dec_emit_named, _ = _resolve_cell(cat, dec, 5)
    xoI_entries, xoI_named, _ = _resolve_cell(cat, xoI, 0)
    xoT_entries, xoT_named, _ = _resolve_cell(cat, xoT, 0)
    xoR_entries, xoR_named, _ = _resolve_cell(cat, xoR, 0)
    xoO_entries, xoO_named, _ = _resolve_cell(cat, xoO, 0)

    xoI.params.update(dest_a=int(enc_in_regs[0]), entry_a=int(enc_in_entry))
    xoT.params.update(dest_a=int(enc_ret_named["v"]),
                      entry_a=int(enc_ret_entries["word"]))
    xoR.params.update(dest_a=int(xoT_named.get("relay", 20)),
                      entry_a=int(xoT_entries["track_a"]))
    xoO.params.update(dest_a=tag_dec, entry_a=0)

    # ---- placement-derived block params
    enc.params["read_wr_desc"] = _wr(29, int(xoR_named.get("relay", 20)))
    enc.params["read_jp_desc"] = _jp(29, int(xoR_entries["track_a"]))
    enc.params["panel_hop"] = 6      # (8,8) (8,9) (8,10) (8,11) (9,11) + exit
    enc.params["emit_hop"] = 10      # 9-cell egress corridor + the port exit
    enc.params["out_dest"] = tag_enc
    enc.params["emit_entry"] = 0
    dec.params["read_wr_desc"] = _wr(25, int(dec_emit_named["b"]))
    dec.params["read_jp_desc"] = _jp(25, int(dec_emit_entries["emit_mat"]))
    dec.params["panel_hop"] = 5      # (6,11) (7,11) (8,11) (9,11) + exit
    dec.params["emit_hop"] = 4       # (5,9) (6,9) (7,9) + land on xoO
    dec.params["out_dest"] = int(xoO_named.get("relay", 20))
    dec.params["emit_entry"] = int(xoO_entries["track_a"])

    # ---- corridors: replace every imported/synthesized chip-port net with the
    # hand-drawn set (the importer's unrouted logical nets carry only the
    # stream identity, which is preserved above / below).
    def rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    BE, CPE, C = BlockEndpoint, ChipPortEndpoint, Connection
    keep = [c for c in project.connections
            if isinstance(c.source, BlockEndpoint)
            and isinstance(c.target, BlockEndpoint)
            and not {c.source.block, c.target.block} & {enc.name,
                                                        dec.name}]
    project.connections[:] = keep + [
        # inputs (x16_in): the cmp trunk runs down col 0 and east along row 8;
        # the raw stream lands on xoI and is re-emitted into the input column.
        C("i2", CPE(0, "x16_in"), BE(dec.name, "byte"), stream_id="cmp",
          route=rp([(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6),
                    (0, 7), (0, 8), (1, 8), (2, 8), (3, 8), (4, 8)])),
        C("i1a", CPE(0, "x16_in"), BE("xoI", "in"), stream_id="raw",
          route=rp([(0, 0), (0, 1)]),
          entry_override=int(xoI_entries["track_a"])),
        C("i1b", BE("xoI", "out"), BE(enc.name, "b"),
          route=rp([(1, 1), (2, 1), (2, 2), (2, 3), (2, 4)])),
        # panel push-read returns (x1_in)
        C("r2", CPE(0, "x1_in"), BE(dec.name, "b"),
          route=rp([(0, 11), (1, 11), (2, 11), (3, 11), (4, 11)])),
        C("r1a", CPE(0, "x1_in"), BE("xoR", "in"),
          route=rp([(0, 11), (1, 11)]),
          entry_override=int(xoR_entries["track_a"])),
        C("r1b", BE("xoR", "out"), BE("xoT", "in"),
          route=rp([(1, 10), (1, 9), (1, 8)]),
          entry_override=int(xoT_entries["track_a"])),
        C("r1c", BE("xoT", "out"), BE(enc.name, "v"),
          route=rp([(1, 7)])),
        # egress (x16_out)
        C("o1", BE(enc.name, "egress"), CPE(0, "x16_out"), out_tag=tag_enc,
          route=rp([(6, 5), (6, 4), (6, 3), (6, 2), (6, 1), (6, 0), (7, 0),
                    (8, 0), (9, 0)])),
        C("o2a", BE(dec.name, "egress"), BE("xoO", "in"),
          route=rp([(5, 9), (6, 9), (7, 9), (8, 9)]),
          entry_override=int(xoO_entries["track_a"])),
        C("o2b", BE("xoO", "out"), CPE(0, "x16_out"), out_tag=tag_dec,
          route=rp([(9, 9), (9, 8), (9, 7), (9, 6), (9, 5), (9, 4), (9, 3),
                    (9, 2), (9, 1), (9, 0)])),
        # controller-to-panel corridors (face realization; both merge into the
        # (9,11) exit — a routing cell merges same-direction inbound faces)
        C("p1", BE(enc.name, "egress"), CPE(0, "x1_out"),
          route=rp([(8, 8), (8, 9), (8, 10), (8, 11), (9, 11)])),
        C("p2", BE(dec.name, "egress"), CPE(0, "x1_out"),
          route=rp([(6, 11), (7, 11), (8, 11), (9, 11)])),
    ]
    return {"tag_enc": tag_enc, "tag_dec": tag_dec}


def import_and_pnr():
    """import the .grc -> hand P&R -> build. Returns (project, bres, cat, ct,
    tags)."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    tags = apply_hand_pnr(res.project, cat, ct)
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct, tags


def load_and_build(kyt_path=KYT_PATH):
    """Load the SHIPPED .kyt and build it (the path the hosted GUI runs)."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(kyt_path)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return project, bres, cat, ct


def stream_map(project, bres):
    """({stream_id: input landing}, {out_tag: stream_id})."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    lands = bres.chips[0].input_landings
    by_sid, tag_to_sid = {}, {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint) and c.source.port == "x16_in"
                and getattr(c, "stream_id", None) and c.name in lands):
            by_sid[c.stream_id] = lands[c.name]
        if (isinstance(c.target, ChipPortEndpoint)
                and c.target.port == "x16_out"
                and getattr(c, "out_tag", None) is not None):
            src_t = getattr(project.block(c.source.block), "type", "")
            tag_to_sid[int(c.out_tag)] = (
                "raw" if src_t == "LZ4EncoderBlock" else "cmp")
    if set(by_sid) != {"raw", "cmp"} or set(tag_to_sid.values()) != {"raw",
                                                                     "cmp"}:
        raise RuntimeError(f"stream map incomplete: in={sorted(by_sid)}, "
                           f"out={sorted(set(tag_to_sid.values()))}")
    return by_sid, tag_to_sid


def run_roundtrip(project, bres, payload=PAYLOAD):
    """Drive the raw stream (payload + sentinel) per-sample on real simKYT
    with a real SramPanelDevice, collect the compressed bytes, then drive them
    per-sample into the decoder stream. Returns
    ``(compressed bytes, decoded bytes, info)``.

    ``info`` carries the INV-56 evidence (every settle ``stop_reason``), the
    panel-ack nudge count, and the pass-2 emission timeline
    ``[(t_ns, n_bytes_so_far)]`` — the on-chip measurement of the encoder's
    DATA-DEPENDENT output rate.
    """
    import simkyt
    from engine.sram_panel import SramPanelDevice

    by_sid, tag_to_sid = stream_map(project, bres)
    panel = project.panels[0]
    dev = SramPanelDevice(size_words=panel.size_words,
                          addr_regs=panel.address_regs,
                          auto_inc_read=bool(getattr(panel, "auto_inc_read",
                                                     False)))
    dev.mem.update({int(a): int(w) & 0xFFFF
                    for a, w in (panel.image or {}).items()})
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.register_panel("x1_out", "x1_in", dev)

    out = {"raw": [], "cmp": []}
    timeline = []               # (t_ns, cumulative compressed bytes)
    info = {"stops": set(), "settle": set(), "nudges": 0,
            "timeline": timeline}

    def pump(limit, rounds):
        """Run until the idle threshold. INV-56: the reason is read every
        call; a mid-drain bounded run legitimately reports EventLimit, but
        the reason AT SETTLE (recorded in ``info['settle']`` on exit) must be
        QueueEmpty — Deadlock anywhere aborts immediately."""
        idle = 0
        last = None
        for _ in range(rounds):
            st = chip.run(max_events=256)
            if isinstance(st, dict):
                stop = st.get("stop_reason")
                if stop:
                    last = str(stop)
                    info["stops"].add(last)
                if stop == "Deadlock":
                    raise RuntimeError("chip DEADLOCKED (INV-56)")
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for v, d, t in got:
                    sid = tag_to_sid.get(int(d))
                    if sid:
                        out[sid].append(int(v) & 0xFFFF)
                        if sid == "raw":
                            timeline.append((int(t), len(out["raw"])))
            else:
                # re-issue a dropped panel-ack release (the measured
                # run(max_events) boundary artifact; lossless — see the
                # encoder suite's pump)
                if chip.is_idle and chip.any_panel_ack_pending() \
                        and chip.release_output_ack("x1_out"):
                    info["nudges"] += 1
                    idle = 0
                    continue
                idle += 1
                if idle > limit:
                    info["settle"].add(str(last))
                    return

    def inject(sid, val):
        lin = by_sid[sid]
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), int(val) & 0xFFFF,
            _jp(lin["hop"], lin["entry"])])

    # PASS 1 (one panel write per byte, no output) + the sentinel, whose
    # settle runs the WHOLE pass-2 scan to quiescence.
    for b in payload:
        inject("raw", b)
        pump(60, 2000)
    inject("raw", EOB)
    tail = max(1200, 30 * len(payload))
    pump(tail, tail + 200000)
    cmp_bytes = list(out["raw"])
    # the decode: one compressed byte per settle — the encoder is idle
    # throughout (temporal exclusivity of the two panel clients).
    for b in cmp_bytes:
        inject("cmp", b)
        pump(120, 6000)
    pump(2000, 20000)
    return cmp_bytes, list(out["cmp"]), info


def rate_buckets(timeline, buckets=8):
    """Compressed-bytes-emitted per time bucket across pass 2 (the on-chip
    output-rate-over-time evidence). Returns a list of per-bucket counts."""
    if not timeline:
        return []
    t0, t1 = timeline[0][0], timeline[-1][0]
    span = max(1, t1 - t0)
    counts = [0] * buckets
    for t, _n in timeline:
        counts[min(buckets - 1, (t - t0) * buckets // span)] += 1
    return counts


def main():
    print("1. load the shipped lz4_stream.kyt -> build ...")
    project, bres, cat, ct = load_and_build()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks")
    exp_cmp, rep_len, rnd_len = goldens()
    print(f"2. drive the 1 KB payload (512 repetitive + 512 random) "
          f"per-sample ...")
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres)
    print(f"   encoder: {len(cmp_bytes)} compressed bytes for "
          f"{len(PAYLOAD)} in ({100.0 * len(cmp_bytes) / len(PAYLOAD):.1f}%)"
          f" — model-exact: {cmp_bytes == exp_cmp}")
    print(f"   per-half (model): repetitive 512 -> {rep_len} bytes "
          f"({100.0 * rep_len / 512:.1f}%), random 512 -> {rnd_len} "
          f"bytes ({100.0 * rnd_len / 512:.1f}%)")
    print(f"   pass-2 emission per time-eighth: "
          f"{rate_buckets(info['timeline'])}   <- the output rate follows "
          f"the DATA")
    print(f"   decoder: {len(dec_bytes)} bytes recovered — round trip "
          f"byte-exact: {dec_bytes == PAYLOAD}")
    print(f"   settle stop_reasons: {sorted(info['settle'])} "
          f"(mid-drain: {sorted(info['stops'])}), panel-ack "
          f"nudges: {info['nudges']}")
    ok = (dec_bytes == PAYLOAD and cmp_bytes == exp_cmp
          and info["settle"] == {"QueueEmpty"})
    print("RESULT:", "EXACT — full 1 KB round trip through both panel "
          "clients on one array" if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
