# SPDX-License-Identifier: GPL-3.0-or-later
"""FEC protocol-link example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

Three streams on ONE placed chip, every stage a Kyttar block:

  'tx'    : bytes -> UnpackKBits(8) -> HammingEncoder(4:7) -> BlockInterleaver
            (4x3) -> interleaved coded bits (tagged egress)
  'txcrc' : the same bytes -> Crc16(frame_len=12) -> the TX CRC word
  'rx'    : burst-corrupted channel bits -> BlockInterleaver(4x3, deinterleave)
            -> HammingDecoder(7:4) -> PackKBits(8) -> the recovered bytes

THE STORY this gate proves ON THE CHIP: a 2-bit consecutive channel burst that
is a fatal double error inside one Hamming(7,4) codeword gets DISPERSED by the
interleaver into two codewords (one correctable error each); the recovered
bytes carry the exact message and the chip-computed TX CRC word equals the CRC
recomputed over them. The INV-4 example-level mutation is the NO-INTERLEAVER
CONTROL: the same burst on the same chains minus the interleaver pair breaks
the message AND the CRC match — the interleaver is load-bearing, not
decorative. A second mutation (mismatched deinterleaver geometry) pins the
gate's sensitivity to the interleaver params.

Saturation policy (justified per the blocks' contracts + measured): every
block in all three chains passes its per-block saturated gate
(test_pipeline_saturation RATE_1IN / REAL_1IN), and on the SHIPPED
shortest-path .kyt the whole merged three-stream saturated drive is proven
EXACT here (test_shipped_kyt_saturated_merged_exact). The .grc nonetheless
ships per-sample paced (`pipelined: 'no'`) for ROBUSTNESS OF THE IMPORT PATH:
a plain GUI auto-P&R of this design can accept a layout whose port->crc
corridor circumnavigates the array (+14 cells over manhattan — the
route-quality ratchet's documented saturated-drive hazard), and on THAT
layout the net 1:14 rate-EXPANDING 'tx' chain deadlocks input-side under the
saturated whole-burst drive (measured 2026-08-16: 1/252 words, the
expanding-chain deadlock class of lessons 2026-07-27). Per-sample pacing is
exact on every layout; the shipped .kyt (built by build_kyt.py, which nudges
the CRC cell so all corridors are shortest-path) is additionally
saturated-proven.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "fec_link"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fec_link_demo import (  # noqa: E402
    CHIP_YAML, GRC_PATH, GR_PYTHON, KYT_PATH, _jp, _wr, goldens,
    import_and_pnr, input_streams, run_link, stim, stream_cfgs)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")

MSG = stim.message_bytes()
OFF = stim.rx_msg_offset()


@pytest.fixture(scope="module")
def built():
    project, bres, cat, ctrl = import_and_pnr()
    return project, bres, cat, ctrl


@pytest.fixture(scope="module")
def link_out(built):
    """The three egress streams through the imported+placed chip, driven
    per-sample interleaved (the shipped .grc's pacing)."""
    project, bres, cat, ctrl = built
    cfgs = stream_cfgs(project, bres, cat, ctrl)
    return run_link(bres, cfgs, input_streams())


def test_import_pnr_build_ok(built):
    project, bres, cat, ctrl = built
    assert bres.ok
    assert len(project.blocks) == 7
    assert not project.panels            # generic sweep, no panel
    # PARAM-DRIFT PIN: the .grc's CRC params must actually REACH the placed
    # block (the importer silently keeps a block default on any un-coercible
    # param — the sps=256 class of bug; hex literals coerce since the
    # int(s, 0) fallback).
    crc = next(b for b in project.blocks if b.type == "Crc16Block")
    assert crc.params["frame_len"] == stim.crc_frame_len() == len(MSG)
    assert crc.params["poly"] == stim.CRC_POLY
    assert crc.params["init"] == stim.CRC_INIT
    il = [b for b in project.blocks if b.type == "BlockInterleaverBlock"]
    assert sorted(bool(b.params["deinterleave"]) for b in il) == [False, True]
    assert all(b.params["rows"] == 4 and b.params["cols"] == 3 for b in il)
    # SPLICE PIN (blocks_short_to_float passthru): the crc16 -> kyttar_sink
    # net must survive the short->float display cast with its own out_tag.
    from model.connection import BlockEndpoint, ChipPortEndpoint
    crc_out = [c for c in project.connections
               if isinstance(c.source, BlockEndpoint)
               and c.source.block == crc.name
               and isinstance(c.target, ChipPortEndpoint)
               and c.target.port == "x16_out"]
    assert len(crc_out) == 1 and crc_out[0].out_tag is not None


def test_stim_mirrors_match_block_references():
    """The stim module's pure-Python goldens (which the shipped .grc embeds as
    its stimulus AND this gate asserts against) must be bit-identical to the
    verified blocks' own process_reference chain — the stim is anchored to the
    block contracts, not self-consistent."""
    import numpy as np
    from gr_kyttar.placement.blocks.block_interleaver_block import \
        BlockInterleaverBlock
    from gr_kyttar.placement.blocks.crc16_block import Crc16Block
    from gr_kyttar.placement.blocks.hamming_decoder_block import \
        HammingDecoderBlock
    from gr_kyttar.placement.blocks.hamming_encoder_block import \
        HammingEncoderBlock
    from gr_kyttar.placement.blocks.pack_k_bits_block import PackKBitsBlock
    from gr_kyttar.placement.blocks.unpack_k_bits_block import UnpackKBitsBlock

    txb = stim.tx_bytes()
    bits = [int(v) for v in
            UnpackKBitsBlock("u", k=8).process_reference_q15(txb)]
    s = list(HammingEncoderBlock("e").process_reference_q15(bits))
    assert s == stim.coded_bits()
    t = BlockInterleaverBlock("i", rows=4, cols=3).process_reference_q15(s)
    assert list(t) == stim.tx_bits()
    crc_ref = Crc16Block("c", frame_len=stim.crc_frame_len()).process_reference(
        np.asarray(txb))
    assert [int(v) & 0xFFFF for v in crc_ref] == [stim.chip_crc()]
    chan = stim.channel_bits()
    d = BlockInterleaverBlock("d", rows=4, cols=3,
                              deinterleave=True).process_reference_q15(chan)
    dec = [int(v) for v in HammingDecoderBlock("h").process_reference(d)]
    import numpy as _np
    rxb = [int(v) & 0xFFFF for v in
           PackKBitsBlock("p", k=8).process_reference(_np.asarray(dec))]
    assert rxb == stim.rx_bytes_expected()
    assert rxb[OFF:OFF + len(MSG)] == MSG   # ...and the burst was corrected


def test_whole_chain_exact_through_burst(link_out):
    """TX bit-exact, the on-chip CRC word exact, and the RX chain recovers the
    EXACT message THROUGH the injected 2-bit burst — all on the placed+routed
    chip, all three streams interleaved."""
    want = goldens()
    assert link_out["tx"] == want["tx"], "TX coded bits not bit-exact"
    assert link_out["txcrc"] == [stim.chip_crc()], (
        f"chip CRC {link_out['txcrc']} != golden 0x{stim.chip_crc():04X}")
    assert link_out["rx"] == want["rx"], "RX bytes not exact"
    got_msg = link_out["rx"][OFF:OFF + len(MSG)]
    assert got_msg == MSG, "message not recovered through the burst"
    # the frame verdict: chip TX CRC == CRC recomputed over the recovered bytes
    assert stim.crc16(got_msg) == link_out["txcrc"][0]


def _drive_shipped(pipelined=False, streams=None):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    cfgs = stream_cfgs(project, bres, cat)
    return run_link(bres, cfgs, streams or input_streams(),
                    pipelined=pipelined)


def test_shipped_kyt_runs_end_to_end():
    got = _drive_shipped()
    want = goldens()
    assert got == want
    assert stim.crc16(got["rx"][OFF:OFF + len(MSG)]) == got["txcrc"][0]


def test_shipped_kyt_saturated_reducing_streams_exact():
    """SATURATED whole-burst drive of the two rate-REDUCING chains, alone on a
    fresh chip each (the sequential-schedule saturated view): both must be
    EXACT — their blocks' saturation contracts (Crc16 REAL_1IN; interleaver /
    decoder / packer RATE_1IN, all feed-forward, no feedback corridor) say
    back-to-back drive serializes on the link handshakes."""
    import simkyt
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok
    cfgs = stream_cfgs(project, bres, cat)
    want = goldens()
    for sid in ("txcrc", "rx"):
        chip = simkyt.Chip.from_yaml(CHIP_YAML)
        chip.load_bitstream_physical(bres.words(0))
        cfg = cfgs[sid]
        h = int(cfg["hop_count"])
        words = []
        for w in input_streams()[sid]:
            words += [_wr(h, int(cfg["data_addrs"][0])), int(w) & 0xFFFF,
                      _jp(h, int(cfg["entry_addr"]))]
        chip.queue_words_physical("x16_in", words)
        out, idle = [], 0
        for _ in range(400000):
            chip.run(max_events=256)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(int(v) & 0xFFFF for v, d, _t in got
                           if int(d) == int(cfg["out_tag"]))
            else:
                idle += 1
            if idle > 3000 or len(out) >= len(want[sid]):
                break
        assert out == want[sid], (
            f"stream {sid!r} saturated drive diverged "
            f"({len(out)}/{len(want[sid])} words)")


def test_shipped_kyt_saturated_merged_exact():
    """The STRONGEST chain-level saturation proof on the shipped .kyt: all
    THREE streams' whole bursts queued back-to-back, packet-interleaved, in
    ONE physical word queue with a single continuous run — every stream must
    come back EXACT. This also pins the route-quality ratchet's hazard claim
    from the other side: the same drive on a wandering (+14 excess) auto-P&R
    corridor deadlocked the expanding 'tx' chain at 1/252 words (measured
    2026-08-16), which is why the .grc ships per-sample paced while the
    SHIPPED shortest-path layout is saturated-proven."""
    got = _drive_shipped(pipelined=True)
    assert got == goldens(), (
        "saturated merged drive diverged on the shipped .kyt — a pipelining "
        "hazard the per-sample gate cannot see")


def test_stream_tag_never_collides_with_fixed_tags():
    """Unit pin for the importer fix: a hash-derived stream tag must never
    land on a FIXED tag ('rx'=5 / 'tx'=10), even with an empty used-set —
    'txcrc' hashes onto 10 and must probe past it, whether or not 'tx' has
    been assigned yet."""
    from engine.grc_import import _STREAM_TAGS, _stream_tag

    fixed = set(_STREAM_TAGS.values())
    assert _stream_tag("txcrc", set()) not in fixed
    assert _stream_tag("txcrc", set()) == _stream_tag("txcrc", {10})
    # fixed ids keep their pinned round-trip values
    assert _stream_tag("tx", set()) == 10 and _stream_tag("rx", set()) == 5


# ------------------------------------------------------------- MUTATION (INV-4)
def _no_interleaver_variant(tmp_path):
    """The .grc minus BOTH interleavers (henc wired straight to the tx sink,
    rx source straight to the decoder cast) — the example-level control."""
    doc = yaml.safe_load(GRC_PATH.read_text())
    doc["blocks"] = [b for b in doc["blocks"]
                     if b["name"] not in ("k_ileave", "k_dileave")]
    conns = []
    for c in doc["connections"]:
        if c[0] == "henc_b2f" and c[2] == "k_ileave":
            conns.append(["henc_b2f", "0", "tx_sink", "0"])
        elif c[0] == "rx_src" and c[2] == "k_dileave":
            conns.append(["rx_src", "0", "dil_f2b", "0"])
        elif "k_ileave" in (c[0], c[2]) or "k_dileave" in (c[0], c[2]):
            continue
        else:
            conns.append(c)
    doc["connections"] = conns
    p = tmp_path / "fec_link_no_interleaver.grc"
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


def test_mutation_no_interleaver_control_FAILS(tmp_path):
    """THE MONEY-SHOT NEGATIVE, on-chip: the SAME 2-bit burst, the SAME coded
    stream, minus the interleaver pair — the burst is now a double error
    inside ONE codeword, the decoder mis-corrects it, the recovered message
    is WRONG, and the CRC verdict catches it. Proves the interleaver is
    load-bearing (INV-4 at example level)."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(_no_interleaver_variant(tmp_path)), cat)
    assert res.ok, res.unknown
    assert len(res.project.blocks) == 5
    # STREAM-TAG COLLISION PIN: in this variant BOTH tx-side sink edges are
    # converter-spliced, so tag assignment order follows the splice order —
    # which used to be set-ordered (PYTHONHASHSEED-dependent), and
    # 'txcrc' hash-derives onto the FIXED 'tx' tag (10). On the losing coin
    # flip both egress nets shared tag 10 and both sinks demuxed ONE stream.
    # Fixed: derived tags never land on a fixed tag + sorted splicing.
    tags = sorted(c.out_tag for c in res.project.connections
                  if c.out_tag is not None)
    assert len(tags) == len(set(tags)) == 3, f"egress tags collide: {tags}"
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct})
    assert rep.ok, rep.reason
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    cfgs = stream_cfgs(res.project, bres, cat, ctrl)
    # The control channel: WITHOUT the interleaver the TX egress IS the coded
    # stream (no startup zero block, no alignment prefix needed — the stream
    # starts codeword-aligned), and the SAME burst offsets hit it directly.
    s = stim.coded_bits()
    chan = list(s)
    for k in range(stim.BURST_LEN):
        chan[stim.BURST_AT + k] ^= 1
    got = run_link(bres, cfgs, {
        "tx": [b & 0xFFFF for b in stim.tx_bytes()],
        "txcrc": [b & 0xFFFF for b in stim.tx_bytes()],
        "rx": chan,
    })
    # TX minus the interleaver emits the plain coded stream; CRC unchanged.
    assert got["tx"] == s
    assert got["txcrc"] == [stim.chip_crc()]
    got_msg = got["rx"][:len(MSG)]
    assert len(got_msg) == len(MSG)
    assert got_msg != MSG, (
        "control unexpectedly recovered the message — the interleaver would "
        "be decorative")
    # ... and the CRC VERDICT catches the corruption (the mis-corrected
    # double error lands in the message bytes, deterministically).
    assert stim.crc16(got_msg) != got["txcrc"][0]
    # deterministic mis-correction: burst at coded 28,29 = d3,d2 of codeword
    # 4 -> syndrome flips p0 -> nibble 4 (byte 2 high nibble) is wrong.
    assert got_msg[2] != MSG[2] and got_msg[:2] == MSG[:2]


def test_mutation_mismatched_deinterleaver_geometry_FAILS(built):
    """A 3x4 deinterleaver against the 4x3 interleaver must break recovery —
    the gate sees the interleaver geometry, it is not decorative."""
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    project, bres, cat, ctrl = built
    dil = next(b for b in project.blocks
               if b.type == "BlockInterleaverBlock"
               and b.params.get("deinterleave"))
    old = dict(dil.params)
    try:
        dil.params.update(rows=3, cols=4)
        ct = load_chip_type(CHIP_YAML)
        bres2 = BuildEngine(cat, CHIP_YAML).build(project,
                                                  {project.chip_type: ct})
        assert bres2.ok
        cfgs = stream_cfgs(project, bres2, cat, ctrl)
        got = run_link(bres2, cfgs, {"rx": input_streams()["rx"]})
        assert got["rx"][OFF:OFF + len(MSG)] != MSG, (
            "gate blind to a mismatched deinterleaver geometry")
    finally:
        dil.params.clear()
        dil.params.update(old)


# --------------------------------------------- the REAL GR-client loop (§5b)
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _serve(kyt):
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(kyt)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=58950)
    assert bound == 58950, f"port 58950 busy (bound {bound})"
    return ctrl, sim


def _run_flowgraph(grc, secs=90):
    runner = _ROOT / "verification" / "grc_userpath_run.py"
    r = subprocess.run(
        [GR_PYTHON, str(runner), str(grc), str(secs)],
        capture_output=True, text=True, timeout=secs + 240,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    sinks = {}
    for line in r.stdout.splitlines():
        if line.startswith("SINK "):
            parts = line.split()
            sinks[parts[1]] = [float(x) for x in parts[2:]]
    assert r.returncode == 0 and sinks, (
        f"generated flowgraph failed (rc={r.returncode}):\n"
        f"{r.stdout[-1200:]}\n{r.stderr[-1500:]}")
    return sinks


def _words(floats):
    """kyttar_sink emits word/32768 floats — back to raw 16-bit words."""
    return [int(round(v * 32768.0)) & 0xFFFF for v in floats]


def _assert_repeats(got, want, label):
    """server_repeat=True loops the genuine one-batch result for the scopes;
    assert at least one full burst arrived AND the loop is a clean repetition
    of it (data integrity, not a fake stream)."""
    assert len(got) >= len(want), (
        f"{label}: only {len(got)}/{len(want)} words recovered")
    reps = -(-len(got) // len(want))
    assert got == (want * reps)[:len(got)], (
        f"{label}: recovered stream is not a clean repetition of the golden "
        f"burst (head {got[:16]}... want {want[:16]}...)")


def test_shipped_grc_user_path(qapp):
    """THE USER PATH, end to end: host the SHIPPED .kyt exactly as the GUI's
    'Run as GNURadio Server' does (port 58950, the .grc's baked bind),
    GRC-generate the SHIPPED .grc, run it under the real GNU Radio
    interpreter, and assert on what the three kyttar sinks actually
    recovered — the .grc's own embedded stimulus, no hand-written client."""
    ctrl, sim = _serve(KYT_PATH)
    try:
        sinks = _run_flowgraph(GRC_PATH)
    finally:
        sim.stop_gnuradio_server()
    want = goldens()
    _assert_repeats(_words(sinks.get("tx_sink", [])), want["tx"], "tx_sink")
    _assert_repeats(_words(sinks.get("crc_sink", [])), want["txcrc"],
                    "crc_sink")
    rx = _words(sinks.get("rx_sink", []))
    _assert_repeats(rx, want["rx"], "rx_sink")
    # the frame verdict the epy panel shows, recomputed from the recovered
    # stream itself: chip CRC word == CRC-16 over the recovered message bytes
    got_msg = rx[OFF:OFF + len(MSG)]
    assert got_msg == MSG
    assert stim.crc16(got_msg) == _words(sinks["crc_sink"])[0] \
        == stim.chip_crc()
