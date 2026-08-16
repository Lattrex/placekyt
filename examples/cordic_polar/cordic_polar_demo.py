# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless CORDIC polar demo — END TO END on one array.

ONE complex signal (an amplitude-modulated rotating phasor, the .grc's
``iq_stim``) drives TWO placed CORDIC vectoring chains sharing the chip via
the stream-id duplex:

  stream 'mag':  I/Q → ComplexToMagBlock → envelope   (the AM envelope)
  stream 'arg':  I/Q → ComplexToArgBlock → phase      (half-turn sawtooth)

Proof structure: the chip output of each stream must be BIT-EXACT to the
block's ``process_reference`` (whose GR equivalence is proven with derived
tolerances in verification/tests/test_cordic_blocks.py), and the demo also
prints the direct comparison against the float truth so the accuracy is
visible (mag ≤ 40 LSB, arg ≤ 0.006 rad for |v| ≥ 0.1 — the gate bounds).

The two chains are independent and feed-forward; the demo drives them
per-sample interleaved (mag sample, arg sample, ...) and attributes egress
words by drive phase — the same alternation the GRC "interleaved" duplex
schedule produces.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/cordic_polar/cordic_polar_demo.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "cordic_polar.grc"
KYT_PATH = HERE / "cordic_polar.kyt"

BURST_LEN = 256

# == the .grc's iq_stim, kept literally in sync ==
IQ_STIM = [
    (0.25 + 0.55 * (0.5 + 0.5 * math.sin(2 * math.pi * 2 * n / BURST_LEN)))
    * complex(math.cos(2 * math.pi * 10 * n / BURST_LEN),
              math.sin(2 * math.pi * 10 * n / BURST_LEN))
    for n in range(BURST_LEN)
]


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# GUIDED anchors (hand-tuned like the fsk4/qam16 modem examples — 47 block
# cells + two duplex corridors is beyond the generic packer's search):
#   arg (8x4) at (1,1): rows 1-4, cols 1-8;
#   mag (9x2) at (0,6): rows 6-7, cols 0-8.
# Free corridors by construction: the col-0 input spine (x16_in (0,0) ->
# both landings), row 5 between the blocks, row 8 below mag, and the col-9
# north spine to x16_out (9,0) shared by both egresses.
_ANCHORS = {"ComplexToArgBlock": (1, 1), "ComplexToMagBlock": (0, 6)}


def import_and_pnr():
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from model.placement import Placement
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    for b in res.project.blocks:
        anchor = _ANCHORS.get(b.type)
        if anchor is None:
            continue
        cells, _ = ctrl.default_cells(b.type, b.library, 0,
                                      anchor[0], anchor[1], b.params)
        b.placement = Placement(0, cells)
    rep = ctrl.auto_route_all({res.project.chip_type: ct}, auto_orient=False)
    if not rep.ok:
        bad = "; ".join(f"{r.name}:{r.reason}" for r in rep.results if not r.ok)
        raise RuntimeError(f"route failed: {bad}")
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct


def _landings_by_stream(project, build_result):
    from model.connection import ChipPortEndpoint

    lands = build_result.chips[0].input_landings
    by_sid = {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint)
                and getattr(c, "stream_id", None) and c.name in lands):
            by_sid[c.stream_id] = lands[c.name]
    return by_sid


def run_streams(project, build_result, iq):
    """Per-sample interleaved drive of both streams; egress attributed by
    drive phase (each sample fully quiesces before the next)."""
    import simkyt

    by_sid = _landings_by_stream(project, build_result)
    assert set(by_sid) == {"mag", "arg"}, sorted(by_sid)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out = {"mag": [], "arg": []}

    def drive(sid, c):
        lin = by_sid[sid]
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(c.real),
            _wr(lin["hop"], lin["data_addrs"][1]), _q15(c.imag),
            _jp(lin["hop"], lin["entry"])])
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out[sid].extend(_s16(w) for w, _d, _t in got)
            else:
                idle += 1
            if idle > 200:
                break

    for c in iq:
        drive("mag", c)
        drive("arg", c)
    return out["mag"], out["arg"]


def reference_outputs(iq):
    from gr_kyttar.placement.blocks import ComplexToArgBlock, ComplexToMagBlock

    pairs = [(_q15(c.real), _q15(c.imag)) for c in iq]
    mag = [_s16(int(w)) for w in ComplexToMagBlock("r").process_reference(pairs)]
    arg = [_s16(int(w)) for w in ComplexToArgBlock("r").process_reference(pairs)]
    return mag, arg


def main():
    print("1. import cordic_polar.grc -> auto place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks")
    print("2. drive both streams per-sample interleaved on real simKYT ...")
    got_mag, got_arg = run_streams(project, bres, IQ_STIM)
    ref_mag, ref_arg = reference_outputs(IQ_STIM)
    mag_exact = got_mag == ref_mag
    arg_exact = got_arg == ref_arg
    print(f"   mag: {len(got_mag)}/{len(ref_mag)} samples, "
          f"bit-exact vs reference: {mag_exact}")
    print(f"   arg: {len(got_arg)}/{len(ref_arg)} samples, "
          f"bit-exact vs reference: {arg_exact}")
    # visibility: worst error vs float truth (the verification gates' bounds)
    wm = max(abs(m / 32768.0 - abs(c)) * 32768
             for m, c in zip(got_mag, IQ_STIM))
    wa = max(abs((a / 32768.0 * math.pi - math.atan2(c.imag, c.real)
                  + math.pi) % (2 * math.pi) - math.pi)
             for a, c in zip(got_arg, IQ_STIM) if abs(c) >= 0.1)
    print(f"   vs float truth: mag worst {wm:.1f} LSB (gate 40), "
          f"arg worst {wa:.5f} rad (gate 0.006)")
    ok = mag_exact and arg_exact and wm <= 40 and wa <= 0.006
    print("RESULT:", "ON-CHIP CORDIC MATCHES — envelope and phase recovered"
          if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
