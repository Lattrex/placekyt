# SPDX-License-Identifier: GPL-3.0-or-later
"""PROBE (untracked): two-panel-client LZ4 encoder + decoder on ONE chip.

Hand placement 'config G': encoder (15 cells) pure-translated to cols 2-8
rows 5-7 (CTL (8,7), natural faces); decoder (8 cells) pure-translated to
cols 2-6 rows 9-10 (CTL (6,10), natural faces). Four CrossoverBlocks fork the
shared corridors:
  xoI (0,1): I2 (cmp input) transits SOUTH; I1 (raw input) lands + re-emits E
  xoT (1,8): I2 transits EAST;  R1 (enc pushes) land + re-emit N -> RET
  xoR (1,11): R2 (dec pushes) transit EAST; R1 land + re-emit N -> xoT
  xoO (8,9): P1 (enc panel words) transit SOUTH; O2 (dec egress) lands +
             re-emits EAST onto col 9 north to x16_out
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")

ENC_WINDOW = 32768
ENC_HASH_BITS = 12
DEC_WINDOW = 65536      # the decoder's PROVEN configuration (addr_base 0)
DEC_BASE = 0            # measured: ctl addr_base offsets ONLY the lookup path
                        # (writes auto-increment from 0), so a based RW client
                        # reads a region it never wrote. The decoder instead
                        # shares [0, len) with the encoder history SEQUENTIALLY:
                        # every decoder read is of an address the decoder itself
                        # wrote earlier in the same batch (append-before-fetch).

TAG_ENC, TAG_DEC = 1, 2


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def payload_1k(seed=7):
    import random
    rnd = random.Random(seed)
    rep = (b"KYTTAR LZ4 STREAM! " * 27)[:512]
    return list(rep) + [rnd.randrange(256) for _ in range(512)]


def build_project():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.panel_pnr import _resolve_cell, synthesize_panel
    from model.block import Block
    from model.chip import ChipInstance
    from model.connection import (BlockEndpoint, ChipPortEndpoint, Connection,
                                  RoutePoint)
    from model.enums import Face
    from model.placement import Placement, PlacedCell
    from model.project import Project, ProjectMetadata

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    p = Project(metadata=ProjectMetadata(name="lz4_stream"),
                chip_type="kyttar_10x12")
    p.chips = [ChipInstance(0, "C0")]

    dec = Block("dec", "LZ4DecoderBlock", library="lattrex.official",
                params={"window_words": DEC_WINDOW, "addr_base": DEC_BASE})
    enc = Block("enc", "LZ4EncoderBlock", library="lattrex.official",
                params={"window_words": ENC_WINDOW, "hash_bits": ENC_HASH_BITS})
    p.blocks = [dec, enc]     # decoder FIRST: refresh_panel_params does backed[0]

    # ---- hand placement: pure translations of the proven default_layouts.
    enc_inst = cat.instantiate("LZ4EncoderBlock", "enc", enc.params,
                               library="lattrex.official")
    dec_inst = cat.instantiate("LZ4DecoderBlock", "dec", dec.params,
                               library="lattrex.official")
    enc_lay = enc_inst.default_layout()
    dec_lay = dec_inst.default_layout()
    # encoder CTL (cell 11) at (8,7); layout CTL is at rel (0,0)
    enc.placement = Placement(0, [
        PlacedCell(cid, dx + 8, dy + 7, Face.from_str(f))
        for cid, (dx, dy, f) in sorted(enc_lay.items())])
    # decoder CTL (cell 6) at (6,10); layout CTL at rel (4,1)
    dec.placement = Placement(0, [
        PlacedCell(cid, dx + 2, dy + 9, Face.from_str(f))
        for cid, (dx, dy, f) in sorted(dec_lay.items())])

    # ---- resolve entries/registers we must target
    enc_ret_entries, enc_ret_named, _ = _resolve_cell(cat, enc, 1)   # C_RET
    enc_in_entry, enc_in_regs = cat.resolved_io("LZ4EncoderBlock", enc.params,
                                                library="lattrex.official")
    dec_emit_entries, dec_emit_named, _ = _resolve_cell(cat, dec, 5)  # EMIT

    def xo(name, x, y, face, **params):
        b = Block(name, "CrossoverBlock", library="lattrex.official",
                  params=params,
                  placement=Placement(0, [PlacedCell(0, x, y,
                                                     Face.from_str(face))]))
        p.blocks.append(b)
        return b

    # placeholder track params; filled below once xo entries resolve
    xoI = xo("xoI", 0, 1, "south", face_a="east", hop_a=6,
             restore_face="south")
    xoT = xo("xoT", 1, 8, "east", face_a="north", hop_a=2,
             restore_face="east")
    xoR = xo("xoR", 1, 11, "east", face_a="north", hop_a=3,
             restore_face="east")
    xoO = xo("xoO", 8, 9, "south", face_a="east", hop_a=11,
             restore_face="south")

    xoI_entries, xoI_named, _ = _resolve_cell(cat, xoI, 0)
    xoT_entries, xoT_named, _ = _resolve_cell(cat, xoT, 0)
    xoR_entries, xoR_named, _ = _resolve_cell(cat, xoR, 0)
    xoO_entries, xoO_named, _ = _resolve_cell(cat, xoO, 0)
    relay_reg = {n: int(named.get("relay", 20))
                 for n, named in (("xoI", xoI_named), ("xoT", xoT_named),
                                  ("xoR", xoR_named), ("xoO", xoO_named))}
    print("xo entries:", {k: v for k, v in (("xoI", xoI_entries),
                                            ("xoT", xoT_entries),
                                            ("xoR", xoR_entries),
                                            ("xoO", xoO_entries))})
    print("relay regs:", relay_reg)
    print("enc ret entries/regs:", enc_ret_entries, enc_ret_named)
    print("enc input entry/regs:", enc_in_entry, enc_in_regs)
    print("dec emit entries/regs:", dec_emit_entries, dec_emit_named)

    xoI.params.update(dest_a=int(enc_in_regs[0]), entry_a=int(enc_in_entry))
    xoT.params.update(dest_a=int(enc_ret_named["v"]),
                      entry_a=int(enc_ret_entries["word"]))
    xoR.params.update(dest_a=relay_reg["xoT"],
                      entry_a=int(xoT_entries["track_a"]))
    xoO.params.update(dest_a=TAG_DEC, entry_a=0)

    # enc read descriptors: pushes land ON xoR (transit (0,11), land (1,11))
    enc.params["read_wr_desc"] = _wr(29, relay_reg["xoR"])
    enc.params["read_jp_desc"] = _jp(29, int(xoR_entries["track_a"]))
    enc.params["panel_hop"] = 6
    enc.params["emit_hop"] = 10
    enc.params["out_dest"] = TAG_ENC
    enc.params["emit_entry"] = 0
    # dec read descriptors: direct landing on EMIT after 6 transits
    dec.params["read_wr_desc"] = _wr(25, int(dec_emit_named["b"]))
    dec.params["read_jp_desc"] = _jp(25, int(dec_emit_entries["emit_mat"]))
    dec.params["panel_hop"] = 5
    dec.params["emit_hop"] = 4
    dec.params["out_dest"] = relay_reg["xoO"]
    dec.params["emit_entry"] = int(xoO_entries["track_a"])

    def rp(pts):
        return [RoutePoint(x, y) for (x, y) in pts]

    C = Connection
    BE, CPE = BlockEndpoint, ChipPortEndpoint
    p.connections = [
        # inputs
        C("i2", CPE(0, "x16_in"), BE("dec", "byte"), stream_id="cmp",
          route=rp([(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6),
                    (0, 7), (0, 8), (1, 8), (2, 8), (3, 8), (4, 8)])),
        C("i1a", CPE(0, "x16_in"), BE("xoI", "in"), stream_id="raw",
          route=rp([(0, 0), (0, 1)]),
          entry_override=int(xoI_entries["track_a"])),
        C("i1b", BE("xoI", "out"), BE("enc", "b"),
          route=rp([(1, 1), (2, 1), (2, 2), (2, 3), (2, 4)])),
        # panel returns
        C("r2", CPE(0, "x1_in"), BE("dec", "b"),
          route=rp([(0, 11), (1, 11), (2, 11), (3, 11), (4, 11)])),
        C("r1a", CPE(0, "x1_in"), BE("xoR", "in"),
          route=rp([(0, 11), (1, 11)]),
          entry_override=int(xoR_entries["track_a"])),
        C("r1b", BE("xoR", "out"), BE("xoT", "in"),
          route=rp([(1, 10), (1, 9), (1, 8)]),
          entry_override=int(xoT_entries["track_a"])),
        C("r1c", BE("xoT", "out"), BE("enc", "v"),
          route=rp([(1, 7)])),
        # egress
        C("o1", BE("enc", "egress"), CPE(0, "x16_out"), out_tag=TAG_ENC,
          route=rp([(6, 5), (6, 4), (6, 3), (6, 2), (6, 1), (6, 0), (7, 0),
                    (8, 0), (9, 0)])),
        C("o2a", BE("dec", "out"), BE("xoO", "in"),
          route=rp([(5, 9), (6, 9), (7, 9), (8, 9)]),
          entry_override=int(xoO_entries["track_a"])),
        C("o2b", BE("xoO", "out"), CPE(0, "x16_out"), out_tag=TAG_DEC,
          route=rp([(9, 9), (9, 8), (9, 7), (9, 6), (9, 5), (9, 4), (9, 3),
                    (9, 2), (9, 1), (9, 0)])),
        # controller-to-panel corridors (face realization)
        C("p1", BE("enc", "egress"), CPE(0, "x1_out"),
          route=rp([(8, 8), (8, 9), (8, 10), (8, 11), (9, 11)])),
        C("p2", BE("dec", "out"), CPE(0, "x1_out"),
          route=rp([(6, 11), (7, 11), (8, 11), (9, 11)])),
    ]

    acts = synthesize_panel(p, cat)
    print("synthesize_panel:", acts)
    # drop any auto-added unrouted return net for the encoder (r1a/b/c covers it)
    before = len(p.connections)
    p.connections[:] = [
        c for c in p.connections
        if not (isinstance(c.source, CPE) and c.source.port == "x1_in"
                and isinstance(c.target, BE) and c.target.block == "enc")
        or c.name == "r1a"]
    # note: r1a source is x1_in but target xoR, keep everything except
    # x1_in->enc synthesized net
    p.connections[:] = [
        c for c in p.connections
        if not (isinstance(c.source, CPE) and c.source.port == "x1_in"
                and isinstance(c.target, BE) and c.target.block == "enc")]
    if len(p.connections) != before:
        print(f"dropped {before - len(p.connections)} synthesized return nets")

    snap = {b.name: dict(b.params) for b in p.blocks}
    bres = BuildEngine(cat, CHIP_YAML).build(p, {"kyttar_10x12": ct})
    for b in p.blocks:
        if dict(b.params) != snap[b.name]:
            diff = {k: (snap[b.name].get(k), b.params[k])
                    for k in set(b.params) | set(snap[b.name])
                    if snap[b.name].get(k) != b.params.get(k)}
            print(f"BUILD CHANGED {b.name} params: {diff}")
    if not bres.ok:
        print("BUILD ERRORS:")
        for e in bres.errors[:15]:
            print("  ", e)
        raise SystemExit(1)
    print("build OK; landings:", {k: v for k, v in
                                  bres.chips[0].input_landings.items()})
    return p, bres, cat, ct


def run_roundtrip(p, bres, payload):
    import simkyt
    from engine.sram_panel import SramPanelDevice

    lands = bres.chips[0].input_landings
    lin_raw = lands["i1a"]
    lin_cmp = lands["i2"]
    panel = p.panels[0]
    dev = SramPanelDevice(size_words=panel.size_words,
                          addr_regs=panel.address_regs,
                          auto_inc_read=bool(getattr(panel, "auto_inc_read",
                                                     False)))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.register_panel("x1_out", "x1_in", dev)

    out = {TAG_ENC: [], TAG_DEC: []}
    state = {"stop": None, "nudges": 0}

    def pump(limit, rounds):
        idle = 0
        for _ in range(rounds):
            st = chip.run(max_events=256)
            if isinstance(st, dict):
                state["stop"] = st.get("stop_reason", state["stop"])
                if state["stop"] == "Deadlock":
                    return False
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for v, d, _t in got:
                    out.setdefault(int(d), []).append(int(v) & 0xFFFF)
            else:
                if chip.is_idle and chip.any_panel_ack_pending() \
                        and chip.release_output_ack("x1_out"):
                    state["nudges"] += 1
                    idle = 0
                    continue
                idle += 1
                if idle > limit:
                    return True
        return True

    def inject(lin, val):
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), int(val) & 0xFFFF,
            _jp(lin["hop"], lin["entry"])])

    print(f"raw landing {lin_raw}, cmp landing {lin_cmp}")
    for i, b in enumerate(payload):
        inject(lin_raw, b)
        if not pump(60, 2000):
            print(f"DEADLOCK during pass-1 byte {i}")
            return None, None, state
    inject(lin_raw, 256)              # EOB sentinel
    tail_idle = max(1200, 30 * len(payload))
    if not pump(tail_idle, tail_idle + 200000):
        print("DEADLOCK during pass 2")
        return None, None, state
    cmp_bytes = list(out[TAG_ENC])
    print(f"encoder emitted {len(cmp_bytes)} bytes "
          f"(stop={state['stop']}, nudges={state['nudges']})")
    if not cmp_bytes:
        return cmp_bytes, [], state
    for i, b in enumerate(cmp_bytes):
        inject(lin_cmp, b)
        if not pump(120, 6000):
            print(f"DEADLOCK during decode byte {i}")
            return cmp_bytes, out[TAG_DEC], state
    pump(2000, 20000)
    return cmp_bytes, out[TAG_DEC], state


def main():
    p, bres, cat, ct = build_project()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"cells used: {used}/120")
    payload = payload_1k()
    from gr_kyttar.placement.blocks.lz4_encoder_block import encode_model
    exp_cmp, stats = encode_model(payload, ENC_WINDOW, ENC_HASH_BITS)
    print(f"model: {len(exp_cmp)} compressed bytes for {len(payload)} in")
    cmp_bytes, dec_bytes, state = run_roundtrip(p, bres, payload)
    if cmp_bytes is None:
        raise SystemExit("wedged")
    print(f"cmp == model: {cmp_bytes == exp_cmp}")
    print(f"decoded {len(dec_bytes or [])} bytes; round trip exact: "
          f"{dec_bytes == payload}")
    if dec_bytes and dec_bytes != payload:
        mism = [i for i in range(min(len(dec_bytes), len(payload)))
                if dec_bytes[i] != payload[i]]
        print(f"mismatches: {len(mism)}; first at {mism[:8]}")
        for i in mism[:8]:
            print(f"  [{i}] got {dec_bytes[i]} want {payload[i]}")
        # golden decode of the chip's own compressed stream
        from gr_kyttar.placement.blocks.lz4_decoder_block import decode_model
        gold, _ = decode_model(cmp_bytes, DEC_WINDOW)
        print(f"golden(decode(cmp)) == payload: {list(gold) == payload}")
    print("last stop_reason:", state["stop"])


if __name__ == "__main__":
    main()
