# SPDX-License-Identifier: GPL-3.0-or-later
"""ConjugateBlock CHAIN-level gate (AGENTS.md §5b — per-block tests are not
whole-chain tests).

History: the 2026-08-09 channel_selector round shipped WITHOUT its planned
Conjugate stage — a single-cell complex-in→complex-out block mis-delivered its
rails under the auto-router's handoff (lessons_log 2026-08-09). The complex
abutment/handoff engine fixes that landed with the transceiver work resolved
it; this gate pins BOTH placement topologies so a regression cannot ship
silently again:

  * ABUTMENT — auto-placed row, conj face-adjacent to its consumer;
  * ROUTED   — conj displaced off the row so both its input and output
    complex pairs traverse multi-cell routes (the broker path).

Chain: x16_in → FloatToComplex → MultiplyConstComplex(0.6+0.35j) → Conjugate
→ ComplexToImag → x16_out, compared BIT-EXACTLY against the blocks' own
verified Q15 references composed (rot then conjugate-negate; c2i selects im).
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

SIG = [0.25 * math.sin(2 * math.pi * 860 * t / 32000) for t in range(24)]


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _mini_grc(tmp_path: Path) -> Path:
    """Derive the minimal rot→conj→c2i .grc from the shipped channel_selector
    flowgraph (same source/sink plumbing, mid-chain replaced)."""
    doc = yaml.safe_load(_SRC_GRC.read_text())
    keep = {"imports", "samp_rate", "sig", "burst_len", "rf_vec", "rf_in",
            "f2c", "qzero", "rot", "c2i", "rf_out", "scope"}
    doc["blocks"] = [b for b in doc["blocks"]
                     if b["id"] in ("import", "options") or b["name"] in keep]
    doc["blocks"].append({
        "name": "conj", "id": "kyttar_conjugate",
        "parameters": {"affinity": "", "alias": "", "comment": "",
                       "maxoutbuf": "0", "minoutbuf": "0"},
        "states": {"bus_sink": False, "bus_source": False,
                   "bus_structure": None, "coordinate": [600, 300],
                   "rotation": 0, "state": "enabled"}})
    doc["connections"] = [
        ["rf_vec", "0", "rf_in", "0"], ["rf_in", "0", "f2c", "0"],
        ["qzero", "0", "f2c", "1"], ["f2c", "0", "rot", "0"],
        ["rot", "0", "conj", "0"], ["conj", "0", "c2i", "0"],
        ["c2i", "0", "rf_out", "0"], ["rf_out", "0", "scope", "0"]]
    out = tmp_path / "conj_chain.grc"
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


def _build(tmp_path: Path, displace: bool):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from model.connection import AUTO_ROUTE
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(_mini_grc(tmp_path)), cat)
    assert res.ok, res.unknown
    proj = res.project
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = proj
    rep = ctrl.auto_pnr({proj.chip_type: ct})
    assert rep.ok, getattr(rep, "reason", "")
    if displace:
        for b in proj.blocks:
            if b.type == "ConjugateBlock":
                for c in b.placement.cells:
                    c.x, c.y = 6, 4
        for c in proj.connections:
            c.route = AUTO_ROUTE
        assert ctrl.auto_route_all({proj.chip_type: ct})
        conj_conns = [c for c in proj.connections
                      if getattr(c.source, "block", None) == "conjugate"
                      or getattr(c.target, "block", None) == "conjugate"]
        assert all(isinstance(c.route, list) and len(c.route) > 2
                   for c in conj_conns), "displacement did not force routing"
    bres = BuildEngine(cat, CHIP_YAML).build(proj, {proj.chip_type: ct})
    assert bres.ok, [str(e) for e in bres.errors[:3]]
    return proj, bres, cat


def _run(bres) -> list:
    import simkyt

    lin = next(iter(bres.chips[0].input_landings.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    out = []
    for v in SIG:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), _q15(v),
            _jp(lin["hop"], lin["entry"])])
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(_s16(w) for w, _d, _t in got)
            else:
                idle += 1
            if idle > 200:
                break
    return out


def _expected(cat) -> list:
    rotb = cat.instantiate("MultiplyConstComplex", "r", {"re": 0.6, "im": 0.35})
    conjb = cat.instantiate("ConjugateBlock", "c", {})
    rr = rotb.process_reference([[_q15(v), 0] for v in SIG])
    cr = conjb.process_reference_q15([a for a, _b in rr], [b for _a, b in rr])
    return [_s16(b) for _a, b in cr]


@pytest.mark.parametrize("displace", [False, True],
                         ids=["abutment", "routed"])
def test_conjugate_chain_bit_exact(tmp_path, displace):
    proj, bres, cat = _build(tmp_path, displace)
    out = _run(bres)
    exp = _expected(cat)
    assert len(out) == len(SIG)
    assert out == exp, f"chain mis-delivery: {out[:8]} vs {exp[:8]}"


def test_mutation_unnegated_im_FAILS(tmp_path):
    """The gate must DETECT a conjugate that passes im through unnegated (the
    classic mis-delivery symptom: the wrong rail reaching the c2i im input)."""
    proj, bres, cat = _build(tmp_path, False)
    out = _run(bres)
    rotb = cat.instantiate("MultiplyConstComplex", "r", {"re": 0.6, "im": 0.35})
    passthru = [_s16(b) for _a, b in
                rotb.process_reference([[_q15(v), 0] for v in SIG])]
    assert out != passthru, "gate blind to an unnegated im rail"
