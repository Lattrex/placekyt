# SPDX-License-Identifier: GPL-3.0-or-later
"""OUTPUT FAN-OUT — whole-chain gates for every supported shape (AGENTS.md §5b).

History (2026-08-10): a block output feeding two targets BUILT silently and
delivered only ONE arm ("last wins" patching); ≥3 arms failed routing outright
("port fan-out ≤2 arms" / "no block fan-out" eng limits). This file pins the
whole mechanism that lifted those limits:

  * fanin2  — gain.out → BOTH add inputs, DIRECT (no splitter): the packet
    form (replicated WRITE + one JUMP) fits even a 3-exit-word source;
  * block3  — gain.out → 3 different gains: the importer AUTO-SPLICES a
    StreamSplitterBlock (a tight source cell cannot hold 3 arms);
  * split3  — the same chain with the splitter placed EXPLICITLY in the .grc;
  * port3   — the chip INPUT PORT fans one stream to 3 blocks: spliced to
    port → splitter → arms (the port fork itself is proven only to 2);
  * the over-budget DIRECT form fails with a NAMED BuildAbort naming
    kyttar_splitter (never a silent wrong-answer build).

The joins downstream are COUNTING joins (Add/Multiply's ``join`` entry): the
combiner fires on the LAST arm arrival in ANY order — sibling arms through
one splitter have EQUAL depth, so the old deepest-arm election could not
order them (the stale-operand race this gate's exact expectations catch).

Every run compares BIT-EXACTLY against the composed Q15 references.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
_SRC_GRC = _ROOT / "examples" / "channel_selector" / "channel_selector.grc"

SIG = [0.1, -0.2, 0.3, 0.05, 0.4]


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _mulq(a, g):
    return (a * g) >> 15


def _blk(name, bid, extra=None):
    p = {"affinity": "", "alias": "", "comment": "", "maxoutbuf": "0",
         "minoutbuf": "0", "device_id": '"kyttar_0"'}
    p.update(extra or {})
    return {"name": name, "id": bid, "parameters": p,
            "states": {"bus_sink": False, "bus_source": False,
                       "bus_structure": None, "coordinate": [400, 300],
                       "rotation": 0, "state": "enabled"}}


def _write_grc(tmp_path, fname, blocks, connections):
    """A minimal flowgraph on the channel_selector's source/sink plumbing."""
    doc = yaml.safe_load(_SRC_GRC.read_text())
    keep = {"imports", "samp_rate", "sig", "burst_len", "rf_vec", "rf_in",
            "rf_out", "scope"}
    doc["blocks"] = [b for b in doc["blocks"]
                     if b["id"] in ("import", "options") or b["name"] in keep]
    doc["blocks"] += blocks
    doc["connections"] = ([["rf_vec", "0", "rf_in", "0"]] + connections
                          + [["rf_out", "0", "scope", "0"]])
    out = tmp_path / fname
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


_GAINS = [_blk("g1", "kyttar_gain", {"gain": "0.5"}),
          _blk("ga", "kyttar_gain", {"gain": "0.5"}),
          _blk("gb", "kyttar_gain", {"gain": "0.25"}),
          _blk("gc", "kyttar_gain", {"gain": "0.125"}),
          _blk("add1", "kyttar_add"), _blk("add2", "kyttar_add")]

_TREE = [["ga", "0", "add1", "0"], ["gb", "0", "add1", "1"],
         ["add1", "0", "add2", "0"], ["gc", "0", "add2", "1"],
         ["add2", "0", "rf_out", "0"]]


def _pnr_build(grc_path):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(grc_path), cat)
    assert res.ok, res.unknown
    proj = res.project
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = proj
    rep = ctrl.auto_pnr({proj.chip_type: ct})
    assert rep.ok, getattr(rep, "reason", "")
    bres = BuildEngine(cat, CHIP_YAML).build(proj, {proj.chip_type: ct})
    return proj, bres, cat, ctrl


def _run(proj, bres, cat, ctrl):
    """Drive SIG per-sample through the stream-landing protocol (the same
    injection the live bridge performs) and return signed output words."""
    import simkyt
    from engine.port_config import stream_targets

    st = stream_targets(proj, ctrl.registry, cat, 0, build_result=bres)
    tgt = next(iter(st.values()))
    ls = tgt.get("landings") or [
        {"hop_count": tgt["hop_count"], "data_addrs": tgt["data_addrs"],
         "entry_addr": tgt["entry_addr"]}]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    out = []
    for v in SIG:
        xi = _q15(v)
        for l in ls:
            chip.inject_data_physical([xi], target_hop_cnt=int(l["hop_count"]),
                                      target_addr=int(l["data_addrs"][0]))
            chip.run(max_events=3000)
            chip.inject_jump_physical(target_hop_cnt=int(l["hop_count"]),
                                      entry_addr=int(l["entry_addr"]))
            chip.run(max_events=120000)
        for w, _d, _t in chip.read_port_words_timed("x16_out"):
            out.append(_s16(w))
    return out


def _exp_tree(pre_gain: bool):
    """add2 = 0.5h + (0.25h + 0.125h were summed by add1) with h = the value
    entering the three arms (0.5x when g1 precedes, x for the port fan-out)."""
    exp = []
    for v in SIG:
        h = _mulq(_s16(_q15(v)), _q15(0.5)) if pre_gain else _s16(_q15(v))
        exp.append(_mulq(h, _q15(0.5)) + _mulq(h, _q15(0.25))
                   + _mulq(h, _q15(0.125)))
    return exp


def test_fanin2_direct_packet(tmp_path):
    """gain.out → add.a0 AND add.a1, no splitter: the replicated-WRITE packet
    form on the tight source cell; out = 2·(0.5x)."""
    grc = _write_grc(tmp_path, "fanin2.grc",
                     [_blk("g1", "kyttar_gain", {"gain": "0.5"}),
                      _blk("adder", "kyttar_add")],
                     [["rf_in", "0", "g1", "0"],
                      ["g1", "0", "adder", "0"], ["g1", "0", "adder", "1"],
                      ["adder", "0", "rf_out", "0"]])
    proj, bres, cat, ctrl = _pnr_build(grc)
    assert bres.ok, [str(e) for e in bres.errors[:2]]
    assert not [b for b in proj.blocks if b.type == "StreamSplitterBlock"], \
        "a same-target pair must stay DIRECT (no splice)"
    out = _run(proj, bres, cat, ctrl)
    assert out == [2 * _mulq(_s16(_q15(v)), _q15(0.5)) for v in SIG]


def test_block_fanout3_auto_spliced(tmp_path):
    grc = _write_grc(tmp_path, "block3.grc", _GAINS,
                     [["rf_in", "0", "g1", "0"],
                      ["g1", "0", "ga", "0"], ["g1", "0", "gb", "0"],
                      ["g1", "0", "gc", "0"]] + _TREE)
    proj, bres, cat, ctrl = _pnr_build(grc)
    assert bres.ok, [str(e) for e in bres.errors[:2]]
    spl = [b for b in proj.blocks if b.type == "StreamSplitterBlock"]
    assert len(spl) == 1, "importer must splice exactly one splitter"
    assert _run(proj, bres, cat, ctrl) == _exp_tree(pre_gain=True)


def test_block_fanout3_explicit_splitter(tmp_path):
    grc = _write_grc(tmp_path, "split3.grc",
                     _GAINS + [_blk("spl", "kyttar_splitter")],
                     [["rf_in", "0", "g1", "0"], ["g1", "0", "spl", "0"],
                      ["spl", "0", "ga", "0"], ["spl", "0", "gb", "0"],
                      ["spl", "0", "gc", "0"]] + _TREE)
    proj, bres, cat, ctrl = _pnr_build(grc)
    assert bres.ok, [str(e) for e in bres.errors[:2]]
    spl = [b for b in proj.blocks if b.type == "StreamSplitterBlock"]
    assert len(spl) == 1, "the explicit splitter must not be re-spliced"
    assert _run(proj, bres, cat, ctrl) == _exp_tree(pre_gain=True)


def test_port_fanout3_spliced(tmp_path):
    grc = _write_grc(tmp_path, "port3.grc", _GAINS[1:],
                     [["rf_in", "0", "ga", "0"], ["rf_in", "0", "gb", "0"],
                      ["rf_in", "0", "gc", "0"]] + _TREE)
    proj, bres, cat, ctrl = _pnr_build(grc)
    assert bres.ok, [str(e) for e in bres.errors[:2]]
    spl = [b for b in proj.blocks if b.type == "StreamSplitterBlock"]
    assert len(spl) == 1, "≥3 same-stream port arms must splice a splitter"
    assert _run(proj, bres, cat, ctrl) == _exp_tree(pre_gain=False)


def test_overfull_direct_fanout_is_named_error(tmp_path):
    """Bypassing the splice (a hand-built project fanning a tight cell to 3
    targets) must fail LOUDLY with the splitter hint — never build silently
    wrong. Constructed by un-splicing an imported project."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint
    from ui.controller import AppController

    grc = _write_grc(tmp_path, "direct3.grc", _GAINS,
                     [["rf_in", "0", "g1", "0"],
                      ["g1", "0", "ga", "0"], ["g1", "0", "gb", "0"],
                      ["g1", "0", "gc", "0"]] + _TREE)
    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(grc), cat)
    proj = res.project
    spl = next(b for b in proj.blocks if b.type == "StreamSplitterBlock")
    src = next(c for c in proj.connections
               if getattr(c.target, "block", None) == spl.name).source
    proj.connections = [c for c in proj.connections
                        if getattr(c.target, "block", None) != spl.name]
    for c in proj.connections:
        if getattr(c.source, "block", None) == spl.name:
            c.source = BlockEndpoint(block=src.block, port=src.port)
    proj.blocks = [b for b in proj.blocks if b.name != spl.name]
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = proj
    rep = ctrl.auto_pnr({proj.chip_type: ct})
    assert rep.ok, getattr(rep, "reason", "")
    bres = BuildEngine(cat, CHIP_YAML).build(proj, {proj.chip_type: ct})
    assert not bres.ok
    msg = "; ".join(str(e) for e in bres.errors)
    assert "kyttar_splitter" in msg, f"expected the splitter hint, got: {msg}"


# --------------------------------------------------------------------------- #
#  THE LIVE BRIDGE'S MULTI-ARM INJECTION SHAPE                                 #
# --------------------------------------------------------------------------- #
#
# ``SimServer._stream_words`` builds the raw WRITE/DATA/JUMP burst for a whole
# stream (the saturated/pipelined drive). It grew multi-arm + COMPLEX support so
# a hosted chip could drive a complex stream that fans out to arms of DIFFERENT
# arity — the gru_classifier ingress (an (Re, Im) pair into the power broker AND
# a lone Re into the ZCR arm). Two properties have to hold, and neither is
# observable from an example-level gate:
#
#   1. every SINGLE-ARM stream keeps its exact pre-existing word stream (every
#      pre-existing design is single-arm — a silent change here would corrupt
#      all of them at once);
#   2. the multi-arm COMPLEX shape is the proven INGRESS PROTOCOL: the pair is
#      ONE delivery (both rails written, then ONE JUMP), the real-rail arm is
#      its own delivery.
#
# The generator is re-derived here rather than imported: _stream_words is a
# closure over process_batch's locals, so it cannot be called directly. Keeping
# a mirror honest is the point — if the real one changes shape, the example's
# hosted gate fails and this explains why.


def _bridge_words(complex_, raw, n, seg, hop, a0, a1, entry, landings=None):
    """Mirror of SimServer._stream_words (engine/sim_bridge.py)."""
    def _w(h, a):
        return (0x6 << 12) | ((h & 0x1F) << 5) | (int(a) & 0x1F)

    def _j(h, e):
        return (0x7 << 12) | ((h & 0x1F) << 5) | (int(e) & 0x1F)

    def _q15v(x):
        return max(-32768, min(32767, int(round(float(x) * 32768.0)))) & 0xFFFF

    arms = landings or [(hop, [a0, a1], entry)]
    words = []
    for kk in range(n):
        if complex_:
            xi, xq = _q15v(seg[2 * kk]), _q15v(seg[2 * kk + 1])
        else:
            xi = (int(seg[kk]) & 0xFFFF) if raw else _q15v(seg[kk])
            xq = None
        for (l_hop, l_addrs, l_entry) in arms:
            vals = [xi, xq] if (xq is not None and len(l_addrs) > 1) else [xi]
            for v, a in zip(vals, l_addrs):
                words += [_w(l_hop, a), v]
            words.append(_j(l_hop, l_entry))
    return words


def _legacy_words(complex_, raw, n, seg, hop, a0, a1, entry):
    """The PRE-multi-arm generator, verbatim — the behaviour every existing
    single-arm design shipped against."""
    def _w(a):
        return (0x6 << 12) | ((hop & 0x1F) << 5) | (int(a) & 0x1F)

    def _j():
        return (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)

    def _q15v(x):
        return max(-32768, min(32767, int(round(float(x) * 32768.0)))) & 0xFFFF

    words = []
    if complex_:
        for kk in range(n):
            words += [_w(a0), _q15v(seg[2 * kk]),
                      _w(a1), _q15v(seg[2 * kk + 1]), _j()]
    else:
        for kk in range(n):
            xi = (int(seg[kk]) & 0xFFFF) if raw else _q15v(seg[kk])
            words += [_w(a0), xi, _j()]
    return words


def test_single_arm_injection_is_byte_identical_to_the_legacy_generator():
    """A single-arm stream — complex or real, raw or q15 — must produce the
    EXACT word stream it did before multi-arm support existed. Randomized over
    the parameter space rather than a couple of hand cases, because a
    regression here breaks every hosted example simultaneously and silently."""
    import random

    rng = random.Random(20260824)
    for trial in range(400):
        complex_ = rng.choice([True, False])
        raw = rng.choice([True, False])
        n = rng.randint(1, 12)
        hop, a0, a1, entry = (rng.randint(0, 31), rng.randint(0, 31),
                              rng.randint(0, 31), rng.randint(0, 31))
        width = 2 * n if complex_ else n
        seg = ([rng.randint(-32768, 32767) for _ in range(width)] if raw
               else [rng.uniform(-1.0, 1.0) for _ in range(width)])
        got = _bridge_words(complex_, raw, n, seg, hop, a0, a1, entry)
        want = _legacy_words(complex_, raw, n, seg, hop, a0, a1, entry)
        assert got == want, (
            f"trial {trial} (complex={complex_} raw={raw} n={n}) diverges from "
            f"the legacy single-arm word stream")


def test_multi_arm_complex_stream_delivers_the_pair_once_then_the_real_rail():
    """THE INGRESS PROTOCOL, asserted on the generator.

    A complex stream whose arms have different arity must emit, per sample:
    both rails to the 2-address landing followed by ONE JUMP (the pair is one
    delivery into the shared broker — writing it as two puts Im into the Re
    register and the block silently computes on Re alone), then Re alone to the
    1-address landing with its own JUMP. These are the landings the router
    resolves for the gru_classifier ingress."""
    pair, zcr = (26, [1, 2], 23), (29, [1], 25)
    words = _bridge_words(True, False, 1, [0.5, -0.25], 0, 0, 1, 0,
                          landings=[pair, zcr])

    def _w(h, a):
        return (0x6 << 12) | ((h & 0x1F) << 5) | (int(a) & 0x1F)

    def _j(h, e):
        return (0x7 << 12) | ((h & 0x1F) << 5) | (int(e) & 0x1F)

    i, q = 0x4000, 0xE000          # q15(0.5), q15(-0.25)
    assert words == [
        _w(26, 1), i, _w(26, 2), q, _j(26, 23),   # the pair: ONE trigger
        _w(29, 1), i, _j(29, 25),                 # the ZCR arm's own copy of Re
    ], f"multi-arm complex burst has the wrong shape: {words}"

    # the pair arm must fire EXACTLY ONE jump, not one per rail
    assert sum(1 for w in words if (w >> 12) == 0x7) == 2, (
        "expected exactly 2 JUMPs (one per arm); a JUMP per RAIL would "
        "double-fire the complex block")
