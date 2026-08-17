# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless FEC protocol-link demo — three streams on ONE chip, END TO END.

The chains (every stage a placed Kyttar block):

  'tx'    : message+pad bytes -> UnpackKBits(8) -> HammingEncoder(4:7)
            -> BlockInterleaver(4x3)            -> interleaved coded bits out
  'txcrc' : the same bytes    -> Crc16(frame_len=12) -> the TX CRC word out
  'rx'    : channel bits      -> BlockInterleaver(4x3, deinterleave)
            -> HammingDecoder(7:4) -> PackKBits(8) -> recovered bytes out

THE STORY: the host channel XORs the TX coded stream with a deterministic
2-bit consecutive burst placed so that, after deinterleaving, its two bits
land in TWO DIFFERENT Hamming(7,4) codewords — one correctable error each.
The recovered bytes carry the exact message, proven by the CRC-16 match
(the chip-computed TX CRC word equals the CRC recomputed over the recovered
message bytes). WITHOUT the interleaver the same burst is a double error
inside ONE codeword — mis-corrected, and the CRC catches it (the gate's
control build proves that on-chip).

All goldens come from ``gr-kyttar/python/kyttar/fec_demo_stim.py`` — the same
module the shipped .grc feeds its sources from — whose mirrors are themselves
cross-checked against the blocks' ``process_reference`` in the gate.

Pipeline: import fec_link.grc -> generic auto place-and-route -> build ->
drive the three streams interleaved per-sample on real simKYT -> demux the
shared output port by each stream's out_tag.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/fec_link/fec_link_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"),
           str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_stim():
    """The repo stim module (the same one the shipped .grc imports), loaded by
    FILE so this venv never touches the kyttar package __init__ (which imports
    gnuradio — present only in the GR interpreter)."""
    import importlib.util
    p = ROOT / "gr-kyttar" / "python" / "kyttar" / "fec_demo_stim.py"
    spec = importlib.util.spec_from_file_location("fec_demo_stim", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stim = _load_stim()

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "fec_link.grc"
KYT_PATH = HERE / "fec_link.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _route_excess(rep):
    """Sum over routed nets of (routed length - endpoint manhattan)."""
    excess = 0
    for r in rep.results:
        if r.ok and r.points and len(r.points) >= 2:
            m = (abs(r.points[0][0] - r.points[-1][0])
                 + abs(r.points[0][1] - r.points[-1][1]))
            excess += max(0, len(r.points) - 1 - m)
    return excess


def _layout_clean(ctrl, project, ct):
    """The auto_pnr acceptance DRC set (crossover plan empty, distinct input
    faces, single-cell in!=out, fan-out port keep-off)."""
    from engine.bus_drc import (_check_dual_input_same_face,
                                _check_single_cell_inout)
    from engine.bus_router import crossover_plan

    if crossover_plan(project, 0, ct, ctrl.catalog):
        return False
    if _check_dual_input_same_face(project, ctrl.catalog):
        return False
    if _check_single_cell_inout(project):
        return False
    return not ctrl._port_fanout_abuts_port(0)


def _refine_crc_placement(project, ctrl, ct, max_excess=4):
    """CORRIDOR-QUALITY refinement (deterministic): the compact packer places
    the 1-cell Crc16Block wirelength-optimally NEXT TO x16_out — which walls
    its own port→crc input corridor behind the TX row, and the router then
    circumnavigates the array (+14 cells over manhattan; the route-quality
    ratchet's hard cap is +8). The placer's objective doesn't see routed
    corridor length, so nudge just the CRC cell toward the input port and keep
    the FIRST candidate whose full re-route is clean with total excess <=
    ``max_excess`` (0 in practice); otherwise the original layout is restored
    verbatim. Every accepted layout is still end-to-end verified downstream."""
    blk = next((b for b in project.blocks if b.type == "Crc16Block"), None)
    if blk is None or blk.placement is None or not blk.placement.cells:
        return
    cell = blk.placement.cells[0]
    orig = (cell.x, cell.y)
    occupied = {(c.x, c.y) for b in project.blocks if b.placement
                for c in b.placement.cells if b is not blk}
    for cand in [(1, 3), (2, 3), (2, 1), (3, 0), (1, 4), (2, 4), orig]:
        if cand != orig and cand in occupied:
            continue
        cell.x, cell.y = cand
        ctrl._clear_chip_routes(0)
        try:
            rep = ctrl.auto_route_all({project.chip_type: ct},
                                      use_bus="always", auto_orient=False,
                                      register=False)
        except Exception:  # noqa: BLE001
            continue
        if cand == orig:
            return                       # fallback: original layout re-routed
        if (rep.ok and _route_excess(rep) <= max_excess
                and _layout_clean(ctrl, project, ct)):
            return
    cell.x, cell.y = orig
    ctrl._clear_chip_routes(0)
    ctrl.auto_route_all({project.chip_type: ct}, use_bus="always",
                        auto_orient=False, register=False)


def import_and_pnr():
    """import the .grc, generic auto-P&R (no panel) + the CRC corridor-quality
    refinement, build."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct})
    if not rep.ok:
        raise RuntimeError(f"auto_pnr failed: {rep.reason}")
    _refine_crc_placement(res.project, ctrl, ct)
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ctrl


def stream_cfgs(project, bres, cat, ctrl=None):
    """{stream_id: {entry_addr, hop_count, data_addrs, out_tag}} — the SAME
    resolver the live GRC server uses (engine.port_config.stream_targets)."""
    from engine.port_config import stream_targets

    if ctrl is None:
        from ui.controller import AppController
        ctrl = AppController(catalog=cat)
        ctrl.project = project
    cfgs = stream_targets(project, ctrl.registry, cat, 0, build_result=bres)
    missing = {"tx", "txcrc", "rx"} - set(cfgs)
    if missing:
        raise RuntimeError(f"streams unresolved: {missing}")
    return cfgs


def input_streams():
    """{stream_id: [input words]} for the demo drive (raw 16-bit words)."""
    return {
        "tx": [b & 0xFFFF for b in stim.tx_bytes()],
        "txcrc": [b & 0xFFFF for b in stim.tx_bytes()],
        "rx": [b & 0xFFFF for b in stim.channel_bits()],
    }


def run_link(bres, cfgs, streams, pipelined=False):
    """Drive the streams on real simKYT and demux x16_out by out_tag.

    ``pipelined=False``: interleave the streams sample-by-sample, each sample
    run to quiescence — the shipped .grc's pacing (``pipelined: 'no'``, exact
    on every layout). ``pipelined=True``: queue every stream's WHOLE burst
    back-to-back, packet-interleaved, in ONE ``queue_words_physical`` and run
    continuously — the saturated drive, proven EXACT on the shipped
    shortest-path .kyt (but it deadlocks the 1:14 rate-expanding tx chain on
    a wandering auto-P&R corridor — see the gate + README).

    Returns {stream_id: [raw words]}."""
    import simkyt

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    tag_of = {sid: int(cfg["out_tag"]) for sid, cfg in cfgs.items()
              if cfg.get("out_tag") is not None}
    by_tag = {t: sid for sid, t in tag_of.items()}
    out = {sid: [] for sid in streams}

    def drain():
        got = chip.read_port_words_timed("x16_out")
        for v, d, _t in got:
            sid = by_tag.get(int(d))
            if sid in out:
                out[sid].append(int(v) & 0xFFFF)
        return bool(got)

    def packet(sid, w):
        cfg = cfgs[sid]
        h = int(cfg["hop_count"])
        return [_wr(h, int(cfg["data_addrs"][0])), int(w) & 0xFFFF,
                _jp(h, int(cfg["entry_addr"]))]

    sids = list(streams)
    n_max = max(len(v) for v in streams.values())
    if pipelined:
        merged = []
        for k in range(n_max):
            for sid in sids:
                if k < len(streams[sid]):
                    merged += packet(sid, streams[sid][k])
        chip.queue_words_physical("x16_in", merged)
        idle = 0
        for _ in range(600000):
            chip.run(max_events=256)
            idle = 0 if drain() else idle + 1
            if idle > 4000:
                break
    else:
        for k in range(n_max):
            for sid in sids:
                if k >= len(streams[sid]):
                    continue
                chip.queue_words_physical("x16_in", packet(sid, streams[sid][k]))
                idle = 0
                for _ in range(120000):
                    chip.run(max_events=64)
                    idle = 0 if drain() else idle + 1
                    if idle > 200:
                        break
        # final flush
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            idle = 0 if drain() else idle + 1
            if idle > 800:
                break
    return out


def goldens():
    """The three expected egress streams (from the shipped stim module)."""
    return {
        "tx": [b & 0xFFFF for b in stim.tx_bits()],
        "txcrc": [stim.chip_crc()],
        "rx": [b & 0xFFFF for b in stim.rx_bytes_expected()],
    }


def main():
    print("1. import fec_link.grc -> generic auto place-and-route -> build ...")
    project, bres, cat, ctrl = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} placed blocks")
    cfgs = stream_cfgs(project, bres, cat, ctrl)
    print("2. drive 'tx' + 'txcrc' + 'rx' interleaved on real simKYT ...")
    got = run_link(bres, cfgs, input_streams())
    want = goldens()
    tx_ok = got["tx"] == want["tx"]
    crc_ok = got["txcrc"] == want["txcrc"]
    rx_ok = got["rx"] == want["rx"]
    off = stim.rx_msg_offset()
    msg = bytes(got["rx"][off:off + len(stim.MESSAGE)]).decode(
        "ascii", "replace") if rx_ok else "<mismatch>"
    host_crc = stim.crc16(got["rx"][off:off + len(stim.MESSAGE)]) \
        if got["rx"] else None
    print(f"   TX coded bits: {len(got['tx'])}/{len(want['tx'])}, "
          f"bit-exact vs golden: {tx_ok}")
    print(f"   TX CRC word (chip): "
          f"{[hex(w) for w in got['txcrc']]} (golden 0x{stim.chip_crc():04X})")
    print(f"   RX recovered through the 2-bit burst: {msg!r}, "
          f"byte-exact: {rx_ok}")
    print(f"   CRC verdict: chip 0x{(got['txcrc'] or [0])[0]:04X} vs "
          f"host-recomputed 0x{(host_crc or 0):04X} — "
          f"{'MATCH' if crc_ok and host_crc == stim.chip_crc() else 'MISMATCH'}")
    ok = tx_ok and crc_ok and rx_ok and host_crc == stim.chip_crc()
    print("RESULT:", "EXACT — burst dispersed, corrected, and CRC-verified"
          if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
