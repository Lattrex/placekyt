#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless CSS (chirp-spread-spectrum) demo — the WHOLE receive spine on one
placeKYT array, driven end to end, with its negative control ON THE CHIP.

    x16_in ──▶ ConjChirpMixer(n=16)  ──▶ FFT16 ──▶ ComplexToMagSquared
           ──▶ Delay(1) ──▶ BinArgmax(16) ──▶ x16_out

Two segments of ONE continuous burst go through that ONE placed chain:

  * segment A — the framed message at **+10 dB** SNR (attenuation 0.5):
    every symbol decodes, the message reads back exactly;
  * segment B — the SAME message at **-10 dB**: the decode collapses. Same
    chain, same run, same chip — the control is real hardware behaviour, not
    a host-side story.

The decode map is ``s = brev4(index)``: FFT16 emits bins in bit-reversed DIF
order, so the winning bin index must be 4-bit-reversed to get the symbol.

THE ALIGNMENT DELAY IS LOAD-BEARING (the system-level insight this example
ships): FFT16's streaming latency is N-1 = 15 == -1 (mod 16), so BinArgmax's
16-sample frames would STRADDLE two FFT frames. One extra real-rail sample of
delay — ``Delay(1)`` — lands every argmax frame on exactly one FFT frame.
Remove it and the decode breaks (gated as a mutation).

WHERE THE ON-CHIP / HOST BOUNDARY SITS (honest statement):
  * ON-CHIP (one placed + routed chip, real corridors and hand-offs): the
    whole RECEIVE spine — dechirp, FFT16, magnitude, alignment delay, argmax.
  * HOST-SIDE (numpy, bit-exact to the TX blocks' own chip-verified integer
    goldens): the TRANSMITTER (ChirpSymbolMapperBlock + ChirpGeneratorBlock)
    and the channel (attenuation + AWGN, then Q15 quantization). This is an
    RX example; it does NOT claim a transmitter on the chip. See the README
    for why (the RX spine alone is 82 of the array's 120 cells — 60 block
    cells plus its routing corridors).

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/css_transceiver/css_transceiver_demo.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_stim():
    """The repo stim module (the same one the shipped .grc imports), loaded by
    FILE so this venv never touches the kyttar package __init__ (which imports
    gnuradio — present only in the GR interpreter)."""
    p = ROOT / "gr-kyttar" / "python" / "kyttar" / "css_demo_stim.py"
    spec = importlib.util.spec_from_file_location("css_demo_stim", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stim = _load_stim()

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "css_transceiver.grc"
KYT_PATH = HERE / "css_transceiver.kyt"

N = stim.N          # 16 samples per chirp symbol == FFT size == alphabet size
STREAM = "rx"       # the single duplex stream the .grc names


# --- word helpers -------------------------------------------------------------

def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# --- the placed chain ---------------------------------------------------------

# The PINNED geometry (block type -> anchor cell), identity orientation. This
# is the CSS receive-spine layout the system gate measured; see build_kyt.py's
# module docstring for the measured reason a generic auto-place of this design
# does NOT work (it rotates the 44-cell FFT16 CCW and the chain deadlocks).
PINNED_LAYOUT = {
    "ConjChirpMixerBlock": (0, 1),
    "FFT16Block": (2, 2),
    "ComplexToMagSquaredBlock": (0, 9),
    "DelayBlock": (0, 11),
    "BinArgmaxBlock": (2, 11),
}


def _assert_chirp_len(project):
    """The chirp length the CHIP is built for must equal the stimulus module's.

    THE FAILURE THIS CATCHES (it cost a full debug cycle). The placeKYT importer
    evaluates a block's params WITHOUT the flowgraph's ``stim`` module — that is
    a GNU Radio import, resolved only in the GR interpreter. So a ``.grc`` that
    writes ``n: stim.N`` does not fail loudly: the importer falls back to the
    yml DEFAULT (128) and the chip is built for a 128-sample chirp while the
    host transmits 16-sample chirps. Everything downstream still "works" —
    import ok, route ok, build ok — and the chip emits a handful of garbage
    words. The ``.grc`` therefore carries a LITERAL ``n_css`` and this guard
    asserts the two agree, so any future drift is a loud failure here instead of
    a silent one on the chip.
    """
    for blk in project.blocks:
        want = None
        if blk.type in ("ConjChirpMixerBlock", "BinArgmaxBlock"):
            want = int(blk.params.get("n", -1))
        if want is not None and want != N:
            raise RuntimeError(
                f"{blk.type} was imported with n={want} but the stimulus uses "
                f"n={N}. The .grc's block params must be LITERALS — the "
                f"importer cannot evaluate stim.* and silently falls back to "
                f"the yml default.")


def _pin_geometry(ctrl, project):
    """Re-place every imported block at its PINNED anchor in IDENTITY
    orientation, keeping the imported nets untouched."""
    from model.placement import Placement

    for blk in project.blocks:
        anchor = PINNED_LAYOUT.get(blk.type)
        if anchor is None:
            raise RuntimeError(
                f"no pinned anchor for {blk.type!r} — the .grc gained a block "
                f"the pinned layout does not cover; extend PINNED_LAYOUT")
        cells, _transit = ctrl.default_cells(
            blk.type, blk.library, 0, anchor[0], anchor[1], blk.params)
        blk.placement = Placement(chip=0, cells=cells, orientation=[])


def import_and_pnr():
    """Import the shipped .grc for its TOPOLOGY, pin the proven GEOMETRY,
    route and build. Also asserts NO routed corridor transits a chip-port cell
    (a routed-'ok' chip that silently swallows injections)."""
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
    _assert_chirp_len(res.project)
    _pin_geometry(ctrl, res.project)
    rep = ctrl.auto_route_all({res.project.chip_type: ct})
    if not rep.ok:
        raise RuntimeError(
            "routing failed: "
            + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    port_cells = {(p.cell_x, p.cell_y) for p in ct.ports}
    n_routed = 0
    for c in res.project.connections:
        pts = c.route if isinstance(c.route, list) else None
        if pts and len(pts) > 2:
            n_routed += 1
            hit = [(p.x, p.y) for p in pts[1:-1] if (p.x, p.y) in port_cells]
            if hit:
                raise RuntimeError(
                    f"corridor {c.name} transits port cell(s) {hit} — "
                    f"injections would be swallowed")
    if n_routed == 0:
        raise RuntimeError("no routed corridors — the port-transit check saw "
                           "nothing (it must never be vacuous)")
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ctrl


def load_shipped():
    """Load the SHIPPED .kyt (what the user opens in placeKYT) and build it."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    project = load_project(str(KYT_PATH))
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = project
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "shipped .kyt build failed: "
            + "; ".join(str(e) for e in bres.errors[:5]))
    return project, bres, cat, ctrl


# --- driving the real chip ----------------------------------------------------

def run_stream(project, bres, cat, ctrl=None, samples=None, saturated=True):
    """Drive the placed chain on real simKYT and return the recovered raw
    index words. ``saturated=True`` queues the WHOLE burst back to back (one
    continuous run — the real streaming condition)."""
    import simkyt
    from engine.port_config import stream_targets

    if ctrl is None:
        from ui.controller import AppController
        ctrl = AppController(catalog=cat)
        ctrl.project = project
    cfgs = stream_targets(project, ctrl.registry, cat, 0, build_result=bres)
    if STREAM not in cfgs:
        raise RuntimeError(f"stream {STREAM!r} unresolved (got {sorted(cfgs)})")
    cfg = cfgs[STREAM]
    hop = int(cfg["hop_count"]) & 0x1F
    entry = int(cfg["entry_addr"])
    addrs = [int(a) for a in cfg["data_addrs"]]
    if samples is None:
        samples = stim.rx_burst()

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    if saturated:
        stream = []
        for c in samples:
            stream += [_wr(hop, addrs[0]), _q15(c.real),
                       _wr(hop, addrs[1]), _q15(c.imag), _jp(hop, entry)]
        chip.queue_words_physical("x16_in", stream)
        res = chip.run(max_events=max(2_000_000, 60_000 * len(samples)))
        if not res.get("completed", False):
            raise RuntimeError(
                f"saturated run did not complete: {res.get('stop_reason')} "
                f"after {res.get('events_processed')} events")
        return [int(v) & 0xFFFF
                for (v, _d, _t) in chip.read_port_words_timed("x16_out")]

    out = []
    chip.set_port_entry_address("x16_in", entry)
    for c in samples:
        chip.inject_data_physical([_q15(c.real)], target_hop_cnt=hop,
                                  target_addr=addrs[0])
        chip.run(max_events=6000)
        chip.inject_data_physical([_q15(c.imag)], target_hop_cnt=hop,
                                  target_addr=addrs[1])
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=400_000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return out


# --- the composed integer golden (the five RX blocks' own references) ---------

def golden_rx(samples):
    """The RX spine's composed bit-exact integer golden: mixer -> FFT16 ->
    |.|^2 -> Delay(1) -> argmax, each stage the BLOCK's own reference."""
    import numpy as np
    from gr_kyttar.placement.blocks.bin_argmax_block import BinArgmaxBlock
    from gr_kyttar.placement.blocks.complex_mag_block import (
        ComplexToMagSquaredBlock)
    from gr_kyttar.placement.blocks.conj_chirp_mixer_block import (
        ConjChirpMixerBlock)
    from gr_kyttar.placement.blocks.fft16_block import fft16_streaming_reference

    y = ConjChirpMixerBlock("m", n=N).process_reference_q15(
        np.asarray(samples, dtype=complex))
    f = fft16_streaming_reference(y)
    mag = ComplexToMagSquaredBlock("g").process_reference_q15(
        [a for a, _ in f], [b for _, b in f])
    aligned = [0] + list(mag[:-1])                  # the Delay(1) alignment
    return [w & 0xFFFF for w in
            BinArgmaxBlock("a", n=N).process_reference_q15(aligned)]


# --- decoding + scoring -------------------------------------------------------

def segments(index_words):
    """Split the recovered index stream into the two segments (A: +10 dB,
    B: -10 dB) — the spine is n:1, so one word per n input samples."""
    w = stim.seg_samples() // N
    return index_words[:w], index_words[w:2 * w]


def score(seg_words):
    """(decoded symbols, symbol errors, SER, recovered text) for one segment,
    against the transmitted framed symbols."""
    tx = stim.framed_symbols()[:stim.n_data_symbols()]
    dec = stim.decode(seg_words)
    errs = sum(1 for a, b in zip(dec, tx) if a != b)
    text = stim.symbols_to_text(dec[stim.K:])
    return dec, errs, errs / len(tx), text


def main():
    print("1. import css_transceiver.grc -> generic auto place-and-route "
          "-> build ...")
    project, bres, cat, ctrl = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} placed "
          f"blocks (the whole CSS receive spine on ONE chip)")

    print(f"2. drive the shipped {stim.burst_len()}-sample burst "
          f"(+10 dB segment, then the -10 dB control) SATURATED on real "
          f"simKYT ...")
    got = run_stream(project, bres, cat, ctrl)
    exp = golden_rx(stim.rx_burst())
    exact = got == exp
    print(f"   recovered {len(got)}/{stim.n_out_words()} index words; "
          f"bit-exact vs the composed golden: {exact}")

    a, b = segments(got)
    dec_a, err_a, ser_a, text_a = score(a)
    dec_b, err_b, ser_b, text_b = score(b)
    print(f"3. segment A (+10 dB): {err_a} symbol errors / "
          f"{len(dec_a)} -> SER {ser_a:.4f}, message {text_a!r}")
    print(f"   segment B (-10 dB, the on-chip negative control): {err_b} "
          f"errors -> SER {ser_b:.4f}, message {text_b!r}")

    ok = (exact and ser_a == 0.0 and text_a == stim.MESSAGE and ser_b > 0.2)
    print("RESULT:", f"EXACT — {stim.MESSAGE!r} recovered at +10 dB "
          f"(SER 0); the -10 dB control collapses (SER "
          f"{ser_b:.2f})" if ok else "FAILED")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
