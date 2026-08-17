# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless complex-math demo — AddCC / SubCC / MultiplyCC on ONE array.

Two analytic complex tones (f_a = 10/256, f_b = 17/256 cyc/sample, amplitude
0.45, Q15-grid-snapped) drive the three two-complex-stream arithmetic blocks
placed on one chip. Each block has its OWN ingress stream pair ('sum'/'b_add',
'diff'/'b_sub', 'prod'/'b_mul' — a complex stream cannot fan out on-chip, the
fan-out relay is single-rail), and the block's landing cell pairs the two
per-sample packets with its counting join, in any arrival order — the
two-external-complex-stream client contract.

Verified EXACT (bit-for-bit) against each block's own
``process_reference_q15``, and the mixer claim is asserted bin-sharp: the
chip's product stream is a single tone whose dominant DFT bin is
f_a + f_b = 27/256 (multiplying analytic tones ADDS frequencies — the
classic up-conversion beat-note), NOT f_a or f_b.

Egress: each block's yi/yq rails leave on consecutive tags
(out_tag, out_tag+1), collected in emit order = the interleaved I/Q stream.
The block's recovered stream rides its FIRST input's stream reply (the
deterministic out_tag-ownership rule in engine.port_config.stream_targets),
so each sink names the block's first-port stream: 'sum', 'diff', 'prod'.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/complex_math/complex_math_demo.py
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
    """The repo stim module (the same one the shipped .grc imports)."""
    import importlib.util
    p = ROOT / "gr-kyttar" / "python" / "kyttar" / "cmath_demo_stim.py"
    spec = importlib.util.spec_from_file_location("cmath_demo_stim", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stim = _load_stim()

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "complex_math.grc"
KYT_PATH = HERE / "complex_math.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

# stream pairing: {output stream (first input, owns the out_tag): second input}
PAIRS = {"sum": "b_add", "diff": "b_sub", "prod": "b_mul"}
BLOCK_OF = {"sum": "AddCCBlock", "diff": "SubCCBlock", "prod": "MultiplyCCBlock"}


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def import_and_pnr():
    """import the .grc, generic auto place-and-route, build."""
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
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ctrl


def stream_cfgs(project, bres, cat, ctrl=None):
    """The live server's own resolver (engine.port_config.stream_targets):
    six ingress streams; 'sum'/'diff'/'prod' own each chain's complex two-tag
    egress, the 'b_*' partners resolve out_tag=None (deterministic ownership)."""
    from engine.port_config import stream_targets

    if ctrl is None:
        from ui.controller import AppController
        ctrl = AppController(catalog=cat)
        ctrl.project = project
    cfgs = stream_targets(project, ctrl.registry, cat, 0, build_result=bres)
    missing = (set(PAIRS) | set(PAIRS.values())) - set(cfgs)
    if missing:
        raise RuntimeError(f"streams unresolved: {missing}")
    for owner in PAIRS:
        if cfgs[owner].get("out_tag") is None or not cfgs[owner].get(
                "complex_out"):
            raise RuntimeError(f"stream {owner!r} does not own a complex "
                               f"two-tag egress: {cfgs[owner]}")
        if cfgs[PAIRS[owner]].get("out_tag") is not None:
            raise RuntimeError(
                f"partner stream {PAIRS[owner]!r} claims an out_tag — the "
                f"deterministic ownership rule is broken")
    return cfgs


def run_streams(project, bres, cat, ctrl=None, a=None, b=None):
    """Drive all six streams per-sample interleaved; return
    {'sum'|'diff'|'prod': [interleaved I,Q signed words]} demuxed by the
    owner's (out_tag, out_tag+1)."""
    import simkyt

    cfgs = stream_cfgs(project, bres, cat, ctrl)
    a = a if a is not None else stim.tone_a()
    b = b if b is not None else stim.tone_b()

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    by_tag = {}
    for owner in PAIRS:
        t = int(cfgs[owner]["out_tag"])
        by_tag[t] = owner
        by_tag[t + 1] = owner
    out = {owner: [] for owner in PAIRS}

    def drain():
        got = chip.read_port_words_timed("x16_out")
        for v, d, _t in got:
            sid = by_tag.get(int(d))
            if sid in out:
                out[sid].append(_s16(int(v)))
        return bool(got)

    order = [("b_add", b), ("sum", a), ("b_sub", b), ("diff", a),
             ("b_mul", b), ("prod", a)]
    for k in range(len(a)):
        for sid, vec in order:
            z = vec[k]
            cfg = cfgs[sid]
            h = int(cfg["hop_count"])
            da = [int(x) for x in cfg["data_addrs"]]
            chip.queue_words_physical("x16_in", [
                _wr(h, da[0]), _q15(z.real), _wr(h, da[1]), _q15(z.imag),
                _jp(h, int(cfg["entry_addr"]))])
            idle = 0
            for _ in range(120000):
                chip.run(max_events=256)
                idle = 0 if drain() else idle + 1
                if idle > 40:
                    break
    idle = 0
    for _ in range(120000):
        chip.run(max_events=256)
        idle = 0 if drain() else idle + 1
        if idle > 400:
            break
    return out


def references(a=None, b=None):
    """{'sum'|'diff'|'prod': [interleaved I,Q signed words]} from each block's
    own bit-exact process_reference_q15 (the verified predictors)."""
    from gr_kyttar.placement.blocks._base import float_to_q15
    from gr_kyttar.placement.blocks.add_sub_cc_block import (AddCCBlock,
                                                             SubCCBlock)
    from gr_kyttar.placement.blocks.multiply_cc_block import MultiplyCCBlock

    a = a if a is not None else stim.tone_a()
    b = b if b is not None else stim.tone_b()
    ai = [float_to_q15(z.real) for z in a]
    aq = [float_to_q15(z.imag) for z in a]
    bi = [float_to_q15(z.real) for z in b]
    bq = [float_to_q15(z.imag) for z in b]
    cls = {"sum": AddCCBlock, "diff": SubCCBlock, "prod": MultiplyCCBlock}
    refs = {}
    for name, c in cls.items():
        yi, yq = c("r").process_reference_q15(ai, aq, bi, bq)
        flat = []
        for i in range(len(yi)):
            flat += [_s16(int(yi[i]) & 0xFFFF), _s16(int(yq[i]) & 0xFFFF)]
        refs[name] = flat
    return refs


def dominant_bin(iq_words):
    """The dominant DFT bin of an interleaved-I/Q signed-word stream."""
    import numpy as np
    z = (np.array(iq_words[0::2], dtype=float)
         + 1j * np.array(iq_words[1::2], dtype=float))
    return int(np.argmax(np.abs(np.fft.fft(z))))


def main():
    print("1. import complex_math.grc -> generic auto place-and-route -> "
          "build ...")
    project, bres, cat, ctrl = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} placed "
          f"blocks, 6 streams")
    print("2. drive the three stream pairs interleaved on real simKYT ...")
    got = run_streams(project, bres, cat, ctrl)
    refs = references()
    ok = True
    for name in ("sum", "diff", "prod"):
        exact = got[name] == refs[name]
        ok = ok and exact
        print(f"   {name:4s}: {len(got[name])//2}/{len(refs[name])//2} "
              f"complex samples, bit-exact vs the block reference: {exact}")
    k = dominant_bin(got["prod"])
    mixer_ok = (k == stim.BIN_A + stim.BIN_B
                and k not in (stim.BIN_A, stim.BIN_B))
    ok = ok and mixer_ok
    print(f"   mixer: product dominant DFT bin {k}/256 "
          f"(f_a+f_b = {stim.BIN_A + stim.BIN_B}/256) — "
          f"{'CONFIRMED' if mixer_ok else 'WRONG'}")
    print("RESULT:", "EXACT — all three chip streams bit-match their "
          "references; the mixer adds the tone frequencies"
          if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
