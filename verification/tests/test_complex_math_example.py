# SPDX-License-Identifier: GPL-3.0-or-later
"""complex_math example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

Three two-complex-stream arithmetic blocks (AddCC, SubCC, MultiplyCC) on ONE
chip, each fed its own ingress stream pair of Q15-snapped analytic tones
(f_a = 10/256, f_b = 17/256 cyc/sample, amp 0.45). Verified EXACT
(bit-for-bit) against each block's own ``process_reference_q15``; the demo's
mixer claim ("multiplying analytic tones adds their frequencies") is
asserted bin-sharp with INV-4 controls (a separable no-cross-term fake and a
conjugated-second-stream correlator both MISMATCH the chip product — the
exactness gate genuinely sees the cross terms); and the live-GR cross-check
holds add/sub EXACT and the product within its derived 3-LSB floor on the
snapped stimulus.

This example is also the pin for two fresh engine contracts:
  * per-stream I/Q pair delivery for 4-register two-pair blocks (the broker
    relay group + stream_targets data_addrs slice — stream b lands on bi/bq,
    never clobbering ai/aq);
  * deterministic out_tag ownership (the block's FIRST input's stream owns
    the chain's complex two-tag egress; the partner resolves None).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "complex_math"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from complex_math_demo import (  # noqa: E402
    BLOCK_OF, CHIP_YAML, GR_PYTHON, KYT_PATH, PAIRS, _s16, dominant_bin,
    import_and_pnr, references, run_streams, stim, stream_cfgs)

pytestmark = pytest.mark.skipif(
    not Path(GR_PYTHON).exists(), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


@pytest.fixture(scope="module")
def recovered(built):
    project, bres, cat, ctrl = built
    return run_streams(project, bres, cat, ctrl)


def test_import_pnr_build_ok(built):
    project, bres, cat, ctrl = built
    assert bres.ok
    assert sorted(b.type for b in project.blocks) == \
        ["AddCCBlock", "MultiplyCCBlock", "SubCCBlock"]
    from model.connection import ChipPortEndpoint
    sids = {c.stream_id for c in project.connections
            if isinstance(c.source, ChipPortEndpoint)
            and getattr(c, "stream_id", None)}
    assert sids == set(PAIRS) | set(PAIRS.values())


def test_out_tag_ownership_deterministic(built):
    """The engine contract this example pins: each chain's out_tag belongs to
    the FIRST input's stream (complex two-tag egress), the partner stream
    resolves out_tag=None — so the duplex demux never depends on client
    thread order. (stream_cfgs raises on any violation.)"""
    project, bres, cat, ctrl = built
    cfgs = stream_cfgs(project, bres, cat, ctrl)
    # and each stream's injection delivers exactly ITS I/Q pair
    for sid, cfg in cfgs.items():
        assert len(cfg["data_addrs"]) == 2, (sid, cfg)


def test_all_three_streams_bit_exact(built, recovered):
    refs = references()
    for name in ("sum", "diff", "prod"):
        assert len(recovered[name]) == len(refs[name]), name
        assert recovered[name] == refs[name], (
            f"{name} ({BLOCK_OF[name]}) diverges from its "
            f"process_reference_q15")


def test_mixer_bin_and_inv4_controls(recovered):
    """The story's claim, with teeth: the chip product's dominant DFT bin is
    exactly f_a+f_b (not f_a, not f_b), AND the exactness gate would catch a
    wrong product: a separable fake (yi=ai*bi, yq=aq*bq — no cross terms)
    and a conjugated-b correlator both MISMATCH the chip stream."""
    import numpy as np
    from gr_kyttar.placement.blocks._base import float_to_q15

    k = dominant_bin(recovered["prod"])
    assert k == stim.BIN_A + stim.BIN_B
    assert k not in (stim.BIN_A, stim.BIN_B)

    a = stim.tone_a()
    b = stim.tone_b()
    ai = np.array([float_to_q15(z.real) for z in a], dtype=np.int64)
    aq = np.array([float_to_q15(z.imag) for z in a], dtype=np.int64)
    bi = np.array([float_to_q15(z.real) for z in b], dtype=np.int64)
    bq = np.array([float_to_q15(z.imag) for z in b], dtype=np.int64)

    def q15mul(p, q):
        return (p * q) >> 15

    got_i = np.array(recovered["prod"][0::2])
    got_q = np.array(recovered["prod"][1::2])
    # separable fake: no cross terms
    fake_i, fake_q = q15mul(ai, bi), q15mul(aq, bq)
    assert not (np.array_equal(got_i, fake_i)
                and np.array_equal(got_q, fake_q)), \
        "gate blind to dropped cross terms"
    # correlator fake: b conjugated
    corr_i = q15mul(ai, bi) + q15mul(aq, bq)
    corr_q = q15mul(aq, bi) - q15mul(ai, bq)
    assert not (np.array_equal(got_i, corr_i)
                and np.array_equal(got_q, corr_q)), \
        "gate blind to a conjugated second stream"


def test_gr_cross_check(recovered):
    """Live-GR add_cc / sub_cc / multiply_cc on the SAME Q15-snapped tones:
    add/sub are EXACT after Q15 rounding (in-range integer sums are exactly
    representable), the product within the MultiplyCC gate's derived 3-LSB
    floor (two truncating MULQs per rail on a snapped stimulus)."""
    script = r"""
import json, sys
import numpy as np
from gnuradio import gr, blocks
d = json.load(sys.stdin)
a = [complex(p, q) for p, q in d["a"]]
b = [complex(p, q) for p, q in d["b"]]
out = {}
for name, cls in (("sum", blocks.add_cc), ("diff", blocks.sub_cc),
                  ("prod", blocks.multiply_cc)):
    tb = gr.top_block()
    sa = blocks.vector_source_c(a, False)
    sb = blocks.vector_source_c(b, False)
    op = cls(1)
    sk = blocks.vector_sink_c()
    tb.connect(sa, (op, 0)); tb.connect(sb, (op, 1)); tb.connect(op, sk)
    tb.run()
    out[name] = [[float(v.real), float(v.imag)] for v in sk.data()]
print(json.dumps(out))
"""
    a = stim.tone_a()
    b = stim.tone_b()
    payload = {"a": [[z.real, z.imag] for z in a],
               "b": [[z.real, z.imag] for z in b]}
    r = subprocess.run([GR_PYTHON, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-500:]
    gold = json.loads(r.stdout.strip().splitlines()[-1])
    for name, tol in (("sum", 0), ("diff", 0), ("prod", 3)):
        gi = [int(round(p * 32768.0)) for p, _ in gold[name]]
        gq = [int(round(q * 32768.0)) for _, q in gold[name]]
        ci = recovered[name][0::2]
        cq = recovered[name][1::2]
        n = min(len(gi), len(ci))
        assert n == stim.N
        worst = max(max(abs(ci[i] - gi[i]), abs(cq[i] - gq[i]))
                    for i in range(n))
        assert worst <= tol, f"{name}: worst |err| {worst} > {tol} LSB vs GR"


def test_shipped_kyt_runs_end_to_end():
    """Drive the SHIPPED .kyt (not a reconstruction): build + run + the same
    bit-exact and mixer verdicts."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project

    project = load_project(KYT_PATH)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    got = run_streams(project, bres, cat)
    refs = references()
    for name in ("sum", "diff", "prod"):
        assert got[name] == refs[name], name
    assert dominant_bin(got["prod"]) == stim.BIN_A + stim.BIN_B


def test_arrival_order_any(built):
    """The counting join pairs the two packets in ANY arrival order: driving
    every a-packet BEFORE its b-packet (the reverse of the demo's order)
    recovers the identical bit-exact streams."""
    project, bres, cat, ctrl = built
    a = stim.tone_a()[:64]
    b = stim.tone_b()[:64]
    got = run_streams(project, bres, cat, ctrl, a=a, b=b)
    # run_streams drives b-first; drive a-first via swapped vectors on the
    # partner streams is NOT equivalent (different math), so instead re-run
    # with the same vectors but reversed per-sample order by monkeypatching
    # is overkill — assert against references directly, which the b-first
    # order already proved; here we prove a SECOND order (a-first) matches.
    import simkyt
    cfgs = stream_cfgs(project, bres, cat, ctrl)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    by_tag = {}
    for owner in PAIRS:
        t = int(cfgs[owner]["out_tag"])
        by_tag[t] = owner
        by_tag[t + 1] = owner
    out = {owner: [] for owner in PAIRS}

    def drain():
        gotw = chip.read_port_words_timed("x16_out")
        for v, d, _t in gotw:
            sid = by_tag.get(int(d))
            if sid in out:
                out[sid].append(_s16(int(v)))
        return bool(gotw)

    def _wr(h, d):
        return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)

    def _jp(h, e):
        return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)

    def _q15(f):
        return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF

    order = [("sum", a), ("b_add", b), ("diff", a), ("b_sub", b),
             ("prod", a), ("b_mul", b)]           # a-first this time
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
    for name in ("sum", "diff", "prod"):
        assert out[name] == got[name], \
            f"{name}: arrival order changed the result"
