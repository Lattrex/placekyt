# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless TMR-pipeline demo — triple-modular redundancy END TO END on one array.

Two independent streams on the SAME 10x12 chip, one run:

  tmr  : ramp bytes -> StreamSplitter (3-way fan-out)
         -> 3x (StreamSplitter identity worker -> AddConst fault injector 0/f/0)
         -> TMRVoter -> x16_out as 2-word [value, status] packets
  solo : ramp bytes -> GainBlock (0.5x) -> x16_out (an ordinary single-path chain)

The fault injection is DEPTH-NEUTRAL: an AddConst sits on ALL THREE arms
(constants 0, f, 0), so toggling f changes path B's VALUE by exactly one word
LSB without changing any arm's pipeline depth.

GOLDEN: the voter has no stock GNU Radio counterpart; its pinned specification
is ``TMRVoterBlock.vote`` (verified word-for-word against the chip by
verification/tests/test_tmr_voter.py). Healthy (f=0): every packet is
[ramp byte, 0]. Fault (f=1 LSB on path B): every packet is [ramp byte, 2] —
the value is STILL the correct ramp byte (TMR corrects the fault) and the
status names path B on every sample. The solo stream is 0.5x the ramp
throughout, same array, same run.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/tmr_pipeline/tmr_pipeline_demo.py
"""
from __future__ import annotations

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
GRC_PATH = HERE / "tmr_pipeline.grc"
KYT_PATH = HERE / "tmr_pipeline.kyt"

RAMP = list(range(256))                # the stimulus: one byte per 16-bit word
FAULT_LSB = 1.0 / 32768.0              # one word LSB in q15 signal units


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


# The shipped HAND placement (this example is hand-placed, like the modem
# examples — the generic auto-P&R pack cannot give the voter's rendezvous cell
# its three distinct arm faces). Geometry: the voter runs EAST from its
# rendezvous at (4,5); the three fault injectors deliver the arms from the
# WEST (2,5), NORTH (4,3) and SOUTH (4,7) sides, each fed by its identity
# worker; the splitter sits near the x16_in corner and the solo gain on the
# bottom row beside the x16_out corridor.
PLACEMENT = {
    "streamsplitter": (2, 1),      # 3-way fan-out
    "streamsplitter_2": (1, 5),    # worker A
    "streamsplitter_3": (4, 2),    # worker B
    "streamsplitter_4": (2, 8),    # worker C
    "addconst": (2, 5),            # injector A (const 0)
    "addconst_2": (4, 3),          # injector B (const f)
    "addconst_3": (4, 7),          # injector C (const 0)
    "tmrvoter": (4, 5),            # rendezvous..emit = (4,5)..(7,5)
    "gain": (7, 1),                # the solo stream's one block
}


def place_route_build(placement: dict | None = None):
    """import the .grc -> HAND-place per ``placement`` -> route -> build."""
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    # Shift each block's provisional cells so its anchor cell lands at the
    # placement position — a plain coordinate shift on the model (NOT
    # MoveBlockToChipCommand, which also removes the block's nets; they are
    # still logical/unrouted here and must survive for the router).
    for name, (ax, ay) in (placement or PLACEMENT).items():
        blk = res.project.block(name)
        pl = blk.placement
        dx, dy = ax - pl.cells[0].x, ay - pl.cells[0].y
        for c in pl.cells:
            c.x, c.y = c.x + dx, c.y + dy
    rep = ctrl.auto_route_all({ctk: ct}, use_bus="always")
    if not rep.ok:
        bad = [(r.name, r.reason) for r in rep.results if not r.ok]
        raise RuntimeError(f"route failed: {bad}")
    bres = ctrl.build()
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct


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
    """(input landings by stream_id, out_tag -> stream_id) for the two streams."""
    from model.connection import ChipPortEndpoint

    landings = bres.chips[0].input_landings
    by_sid, tag_to_sid = {}, {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint) and c.source.port == "x16_in"
                and getattr(c, "stream_id", None) and c.name in landings):
            by_sid[c.stream_id] = landings[c.name]
        if (isinstance(c.target, ChipPortEndpoint)
                and c.target.port == "x16_out"
                and getattr(c, "out_tag", None) is not None):
            tag_to_sid[c.out_tag] = (
                "tmr" if c.source.block.startswith("tmrvoter") else "solo")
    if set(by_sid) != {"tmr", "solo"} or set(tag_to_sid.values()) != {"tmr",
                                                                      "solo"}:
        raise RuntimeError(f"stream map incomplete: in={list(by_sid)}, "
                           f"out={sorted(set(tag_to_sid.values()))}")
    return by_sid, tag_to_sid


def run_streams(project, bres, payload=RAMP):
    """Drive BOTH streams per-sample on real simKYT; return
    (tmr packet words, solo words, per-sample settle stop_reasons).

    INV-56: ``stop_reason`` is read for EVERY sample, at the point the chip has
    settled (the idle threshold). Mid-drain a bounded ``run(max_events=...)``
    legitimately reports ``"EventLimit"``; the SETTLED reason must be
    ``"QueueEmpty"`` (ran to quiescence) — ``"Deadlock"`` anywhere means the
    chip wedged and the missing words will never arrive."""
    import simkyt

    by_sid, tag_to_sid = stream_map(project, bres)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    out = {"tmr": [], "solo": []}
    settle_reasons = []
    for b in payload:
        words = []
        for sid in ("tmr", "solo"):
            lin = by_sid[sid]
            words += [_wr(lin["hop"], lin["data_addrs"][0]), int(b) & 0xFFFF,
                      _jp(lin["hop"], lin["entry"])]
        chip.queue_words_physical("x16_in", words)
        idle = 0
        reason = None
        for _ in range(120000):
            res = chip.run(max_events=128)
            reason = res.get("stop_reason")
            if reason == "Deadlock":
                break
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for v, d, _t in got:
                    sid = tag_to_sid.get(int(d))
                    if sid:
                        out[sid].append(int(v) & 0xFFFF)
            else:
                idle += 1
            if idle > 300:
                break
        settle_reasons.append(reason)
    return out["tmr"], out["solo"], settle_reasons


def tmr_golden(payload, fault_lsbs_on_b=0):
    """The pinned voter specification applied to the three (identity) arms."""
    from gr_kyttar.placement.blocks import TMRVoterBlock

    a = [int(v) & 0xFFFF for v in payload]
    b = [(int(v) + int(fault_lsbs_on_b)) & 0xFFFF for v in payload]
    return TMRVoterBlock.process_reference_words(a, b, a)


def solo_golden(payload, gain=0.5):
    """The solo chain's bit-exact expectation: the on-chip Q15 multiply
    ``(word * q15(gain)) >> 15`` (floor), measured on the built chip."""
    q = int(round(float(gain) * 32768.0))
    return [((int(v) & 0xFFFF) * q) >> 15 for v in payload]


def main():
    print("1. load the shipped tmr_pipeline.kyt -> build ...")
    project, bres, cat, ct = load_and_build()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} placed blocks")
    inj_b = next(b for b in project.blocks
                 if b.type == "AddConstBlock" and b.params.get("const", 0))
    f_lsbs = int(round(float(inj_b.params["const"]) * 32768.0))
    print(f"2. drive the 256-byte ramp through BOTH streams per-sample "
          f"(path-B fault = {f_lsbs} LSB) ...")
    tmr, solo, reasons = run_streams(project, bres)
    exp_tmr = tmr_golden(RAMP, f_lsbs)
    exp_solo = solo_golden(RAMP)
    vals, stats = tmr[0::2], tmr[1::2]
    print(f"   tmr: {len(tmr) // 2} packets; values == ramp: "
          f"{vals == RAMP}; statuses: {sorted(set(stats))}")
    print(f"   solo: {len(solo)} words; == 0.5x ramp: {solo == exp_solo}")
    print(f"   settle stop_reasons: {sorted(set(map(str, reasons)))}")
    ok = (tmr == exp_tmr) and (solo == exp_solo) and (
        set(reasons) == {"QueueEmpty"})
    print("RESULT:", "EXACT — voter packets and single-path stream both match "
          "the pinned goldens" if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
