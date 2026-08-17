# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression pins for the MIXED complex fan-out shape + per-port JUMP entries.

The converter_flavors live run used to DEADLOCK (0 egress) on every auto-P&R
layout, from two independent engine defects this file pins DETERMINISTICALLY
(hand-placed geometry — no auto-P&R layout randomness):

1. MIXED fan-out (one rail ABUTTED + one rail BROKERED from a complex source):
   the exit cell has ONE output face and the INV-17 hop-steered fan-out form
   sequences every arm down that single face — an abutted arm whose consumer
   sits on a DIFFERENT face than the corridor's first hop makes the routed
   arm's @hop words land in the abutted consumer instead (its trigger JUMP was
   swallowed there too, so the routed rail silently dropped and the downstream
   starved). The maze router now mirrors the bus router's abutment fast-path
   rule: a mixed fan-out exit cell keeps EVERY arm fully routed (the routed
   fan-out form is the proven one — all arms ride one corridor and peel off at
   their own brokers).

2. PER-PORT JUMP ENTRY (the DualFloatToComplex ``got_i``/``got_q`` pair): a
   multi-entry rendezvous cell runs DIFFERENT code per input port, but every
   producer used to resolve the block's single default entry — so the ``q``
   arm's delivery JUMPed ``got_i``, ``got_q`` never ran, and the rendezvous
   never emitted (the sim deadlocks with the whole chain backed up). Input
   ports now declare their entry (``Port.entry`` → PortMap → build/broker).

Run::

    cd <repo root>
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_mixed_fanout_rails.py -q
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"


def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint
    return (BlockCatalog, load_chip_type, AppController,
            BlockEndpoint, ChipPortEndpoint)


def _port_entries(cat):
    """(entry_i, entry_q) resolved from the DualFloatToComplex port map."""
    pm = cat.port_map("DualFloatToComplexBlock", library=LIB)
    e = {p.name: p.entry for p in pm.ports if p.direction == "in"}
    return e.get("i"), e.get("q")


def test_dual_ports_declare_distinct_entries():
    """The rendezvous block's two input ports resolve to DISTINCT JUMP entries
    (got_i vs got_q). Pre-fix both resolved to the block's single default entry."""
    BlockCatalog, *_ = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ei, eq = _port_entries(cat)
    assert ei is not None and eq is not None
    assert ei != eq, (
        f"dual i/q ports must resolve DISTINCT entries (got_i vs got_q), both "
        f"resolved {ei} — every producer would trigger got_i and the rendezvous "
        f"never emits")


def test_brokered_q_delivery_jumps_got_q():
    """A ROUTED (brokered) net into ``dual.q`` must deliver with a JUMP at the
    got_q entry — not the block default (got_i). Hand-placed: one gain ABUTS
    ``i`` (@1 handoff, got_i) and one gain 3 cells away drives ``q`` through a
    broker. Pre-fix the built fabric carried NO JUMP to got_q anywhere."""
    import simkyt
    (BlockCatalog, load_chip_type, AppController, BE, CPE) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ei, eq = _port_entries(cat)
    assert ei is not None and eq is not None and ei != eq

    ctrl = AppController(catalog=cat)
    ctrl.new_project("mixq", ctk)
    d = ctrl.place_block("DualFloatToComplexBlock", 0, 5, 5, library=LIB, params={})
    gi = ctrl.place_block("GainBlock", 0, 4, 5, library=LIB)   # abuts dual.i (west)
    gq = ctrl.place_block("GainBlock", 0, 5, 8, library=LIB)   # 3 away → brokered
    ctrl.add_logical_connection(BE(block=gi, port="out"), BE(block=d, port="i"),
                                name="ni")
    ctrl.add_logical_connection(BE(block=gq, port="out"), BE(block=d, port="q"),
                                name="nq")
    ctrl.add_logical_connection(BE(block=d, port="yi"),
                                CPE(chip=0, port="x16_out"), name="no")
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    assert rep.ok, "the hand-placed dual fan-in must route"
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    # Every JUMP entry the built fabric fires (outside the dual's own cell).
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.chips[0].words)
    blk = ctrl.project.block(d)
    dual_cell = (blk.placement.cells[0].x, blk.placement.cells[0].y)
    jump_entries: set[int] = set()
    for cid in range(120):
        x, y = cid % 10, cid // 10
        if (x, y) == dual_cell:
            continue
        mem = [chip.read_cell_memory(cid, a) for a in range(32)]
        if not any(mem):
            continue
        dis = simkyt.Program.from_words("c", mem, 0).disassemble()
        for m in re.finditer(r"Jump \{ hop_cnt: \d+, dest: (\d+) \}", dis):
            jump_entries.add(int(m.group(1)))
    assert eq in jump_entries, (
        f"no built cell JUMPs the dual's got_q entry ({eq}) — the q arm's "
        f"delivery triggers got_i instead and the rendezvous never emits; "
        f"entries fired: {sorted(jump_entries)}")
    assert ei in jump_entries, (
        f"the abutted i arm must trigger got_i ({ei}); entries fired: "
        f"{sorted(jump_entries)}")


def test_maze_mixed_fanout_keeps_routed():
    """The maze router must NOT classify one arm of a fan-out as an abutment
    while a sibling arm from the SAME exit cell routes a corridor (the mixed
    shape cannot be expressed by the single-face hop-steered fan-out form).
    Hand-placed mixer with one gain abutting its exit cell and one gain far:
    pre-fix the adjacent arm came back ``abutment=True``; now BOTH arms route."""
    (BlockCatalog, load_chip_type, AppController, BE, _CPE) = _engine()
    from engine.maze_router import route_all_maze
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("mixfan", ctk)
    mx = ctrl.place_block("ComplexMixerBlock", 0, 1, 1, library=LIB)
    # The mixer's exit cell (both yi/yq leave here) from its port map offsets.
    pm = cat.port_map("ComplexMixerBlock", library=LIB)
    out = next(p for p in pm.ports if p.direction == "out")
    ex = (1 + out.dx, 1 + out.dy)
    ga = ctrl.place_block("GainBlock", 0, ex[0] + 1, ex[1], library=LIB)  # abuts
    gb = ctrl.place_block("GainBlock", 0, ex[0] + 4, ex[1], library=LIB)  # far
    ctrl.add_logical_connection(BE(block=mx, port="yi"),
                                BE(block=gb, port="sample"), name="n_yi")
    ctrl.add_logical_connection(BE(block=mx, port="yq"),
                                BE(block=ga, port="sample"), name="n_yq")

    def port_cells(block_type, library, params=None):
        p = cat.port_map(block_type, params=params, library=library)
        return {q.name: (q.cell_id, q.direction) for q in p.ports}

    def port_maps(block_type, library, params=None):
        return cat.port_map(block_type, params=params, library=library)

    rep = route_all_maze(ctrl.project, {ctk: ct}, port_cells, port_maps)
    by_name = {r.name: r for r in rep.results}
    assert by_name["n_yi"].ok and by_name["n_yq"].ok, (
        "both fan-out arms must route: " + str(
            {r.name: r.reason for r in rep.results if not r.ok}))
    assert not getattr(by_name["n_yq"], "abutment", False), (
        "mixed fan-out: the adjacent arm must keep the fully-routed path, not "
        "an abutment — the exit cell's single output face cannot serve an "
        "abutted arm and a routed sibling")
    assert not getattr(by_name["n_yi"], "abutment", False)
