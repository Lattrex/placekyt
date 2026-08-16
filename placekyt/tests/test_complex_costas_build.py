# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the ComplexCostasLoopBlock through the real placeKYT pipeline.

This is Gate 1 for #216: prove the 7-cell complex Costas block (with its row-1
dphase FEEDBACK return path) places via its ``default_layout`` and routes to a
valid bitstream through the BuildEngine. The lock behaviour is verified
separately against simkyt in the verification harness; here we only assert
the block is placeable + routable + loads into the simulator.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _place_costas(catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Costas", "kyttar_10x12")
    # Anchor at (1, 1): the COMPACT 4x2 serpentine fold occupies (1,1)..(4,2)
    # (7 program cells + 1 corner transit) — all inside the 10x12 array.
    name = ctrl.place_block(
        "ComplexCostasLoopBlock", 0, 1, 1, library="lattrex.official")
    return ctrl, name


def test_costas_in_catalog(catalog):
    spec = catalog.get("ComplexCostasLoopBlock", "lattrex.official")
    assert spec is not None
    assert spec.default_cell_count == 7
    # Complex input: two input registers (xi, xq).
    assert len(spec.input_registers) == 2


def test_costas_places_with_transit_feedback(qapp, catalog):
    from model.placement import is_transit_cell
    ctrl, name = _place_costas(catalog)
    blk = ctrl.project.block(name)
    assert blk is not None and blk.placement is not None
    # Internal transit_* cells are first-class cells in ``cells`` now: 7 program
    # cells in the serpentine fold + 1 FACE-only corner feedback-return cell.
    program = [c for c in blk.placement.cells if not is_transit_cell(c)]
    transit = blk.placement.transit_cells
    assert len(program) == 7
    assert len(transit) == 1, f"expected 1 transit cell, got {len(transit)}"
    assert len(blk.placement.cells) == 8


def test_costas_builds_to_bitstream(qapp, catalog, chip_type):
    ctrl, _name = _place_costas(catalog)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    assert 0 in res.chips
    assert len(res.words(0)) > 0


def test_costas_bitstream_loads_into_simkyt(qapp, catalog, chip_type):
    import simkyt

    ctrl, _name = _place_costas(catalog)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))


def test_costas_feedback_hop_resolved(qapp, catalog, chip_type):
    """The dphase feedback (pd_pi -> phase) must route the transit return path,
    NOT @1-abutment. With the COMPACT fold at (0,0): forward serpentine
    (0,0)(1,0)(2,0)(3,0)->(3,1)(2,1)(1,1)=pd_pi, and one corner transit (0,1)
    north -> phase. pd_pi.dphase is @2 to the phase cell's dphase register (R2):
    pd_pi(1,1,W) -> (0,1,N) -> phase(0,0). (#217 build-level internal feedback;
    the short return is the whole point of the fold.)"""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Costas", "kyttar_10x12")
    ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0,
                     library="lattrex.official")
    BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    prog = ctrl.cell_program(0, 1, 1)  # pd_pi at (1,1) in the fold
    fb = [i for i in prog["instructions"]
          if i["kind"] == "WRITE" and i.get("field") == 2]
    assert fb, "pd_pi has no dphase WRITE to R2"
    assert fb[0]["hop"] == 2, f"feedback hop {fb[0]['hop']} != 2 (@1-defaulted?)"


def test_costas_built_bitstream_locks(qapp, catalog, chip_type):
    """The placeKYT-BUILT bitstream (not the hand-resolved proto) must LOCK: the
    internal feedback closes through the transit return path. Anchor at (0,0) so
    the phase landing cell is reachable from x16_in; drive a freq-offset BPSK
    signal and confirm the recovered I sign-matches the symbols (lock)."""
    import math
    import random
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Costas", "kyttar_10x12")
    ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0,
                     library="lattrex.official")
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(
        "ComplexCostasLoopBlock", {}, library="lattrex.official")

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def mq(a, b):
        return (s16(a) * s16(b)) >> 15

    def lock_consistency(seed, foff, n=200):
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical(res.words(0))
        rot = chip.cell_id_at(2, 1)  # rotate cell (fold pos) holds yi operands
        random.seed(seed)
        syms = [random.choice([1, -1]) for _ in range(n)]
        yis = []
        for k in range(n):
            xi = fq(syms[k] * math.cos(2 * math.pi * foff * k))
            xq = fq(syms[k] * math.sin(2 * math.pi * foff * k))
            chip.inject_data_physical([xi], target_hop_cnt=30, target_addr=0)
            chip.run(max_events=3000)
            chip.inject_data_physical([xq], target_hop_cnt=30, target_addr=1)
            chip.run(max_events=3000)
            chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
            chip.run(max_events=30000)
            xis = chip.read_cell_memory(rot, 5)
            xqs = chip.read_cell_memory(rot, 6)
            sv = chip.read_cell_memory(rot, 7)
            cv = chip.read_cell_memory(rot, 8)
            yis.append(mq(xis, cv) - mq(xqs, sv))
        late = range(n - 50, n)
        sm = sum(1 for k in late if (yis[k] >= 0) == (syms[k] > 0))
        mag = sum(abs(yis[k]) for k in late) / 50
        return max(sm, 50 - sm), mag

    # Both frequency-offset signs, a few seeds — the built loop must lock.
    for seed, foff in [(3, 0.02), (3, -0.02), (7, 0.015), (5, 0.025)]:
        consistency, mag = lock_consistency(seed, foff)
        assert consistency >= 48 and mag > 20000, (
            f"built Costas did NOT lock (seed={seed}, foff={foff}): "
            f"{consistency}/50, |yi|={mag:.0f}")


def test_costas_order4_in_catalog(catalog):
    """The order param exposes the QPSK (order=4) variant: 8 cells (the extra
    ``qpd`` 2-term phase-detector cell), same 2 complex input registers."""
    spec = catalog.get("ComplexCostasLoopBlock", "lattrex.official")
    assert spec is not None
    from gr_kyttar.placement import ComplexCostasLoopBlock
    b2 = ComplexCostasLoopBlock("b2", order=2)
    b4 = ComplexCostasLoopBlock("b4", order=4)
    assert b2.cell_count == 7 and b4.cell_count == 8
    assert list(b4.build_cell_programs().keys()) == [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
        "rotate", "qpd", "pd_pi"]
    with pytest.raises(ValueError):
        ComplexCostasLoopBlock("bad", order=3)


def test_costas_order4_builds_and_routes(qapp, catalog, chip_type):
    """The QPSK (order=4) Costas places, routes I/Q in + recovered out, and
    builds to a bitstream through the real placeKYT pipeline."""
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Costas4", "kyttar_10x12")
    blk = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0,
                           library="lattrex.official", params={"order": 4})
    pm = catalog.port_map("ComplexCostasLoopBlock", {"order": 4},
                          library="lattrex.official")
    outp = [p.name for p in pm.ports if p.direction == "out"][0]
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xi"), name="i")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xq"), name="q")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port=outp),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type})
    assert rep.ok, [r for r in rep.results if not r.ok]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    assert len(res.words(0)) > 0


def test_costas_order4_built_bitstream_locks_qpsk(qapp, catalog, chip_type):
    """The placeKYT-BUILT order-4 bitstream must LOCK a QPSK carrier: drive a
    freq-offset QPSK signal and confirm the recovered I settles onto the
    constant-modulus ±45deg grid (|yi| ~ 0.707*32767). This exercises the qpd
    2-term phase detector + the qpd->pd_pi trigger handoff (the fix that made
    order-4 fire on-chip) end to end through the real build pipeline."""
    import math
    import random
    import simkyt
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Costas4", "kyttar_10x12")
    blk = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0,
                           library="lattrex.official", params={"order": 4})
    pm = catalog.port_map("ComplexCostasLoopBlock", {"order": 4},
                          library="lattrex.official")
    outp = [p.name for p in pm.ports if p.direction == "out"][0]
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xi"), name="i")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xq"), name="q")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port=outp),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type})
    assert rep.ok, [r for r in rep.results if not r.ok]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, ins = catalog.resolved_io(
        "ComplexCostasLoopBlock", {"order": 4}, library="lattrex.official")
    a0, a1 = int(ins[0]), int(ins[1])

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def run_qpsk(seed, foff, n=200):
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical(res.words(0))
        chip.set_port_entry_address("x16_in", entry)
        random.seed(seed)
        for k in range(n):
            i = (1.0 if random.randint(0, 1) == 0 else -1.0) / math.sqrt(2)
            q = (1.0 if random.randint(0, 1) == 0 else -1.0) / math.sqrt(2)
            c = math.cos(2 * math.pi * foff * k); sn = math.sin(2 * math.pi * foff * k)
            xi, xq = i * c - q * sn, i * sn + q * c
            chip.inject_data_physical([fq(xi)], target_hop_cnt=30, target_addr=a0)
            chip.run(max_events=6000)
            chip.inject_data_physical([fq(xq)], target_hop_cnt=30, target_addr=a1)
            chip.run(max_events=6000)
            chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
            chip.run(max_events=200000)
        out = [s16(int(v)) for v, d, t in chip.read_port_words_timed("x16_out")]
        return out

    # A locked QPSK carrier puts the recovered I on the constant-modulus grid:
    # |yi| settles near 0.707*32767 ~ 23170. Check the late-window mean magnitude.
    for seed, foff in [(3, 0.01), (7, -0.01), (5, 0.008)]:
        out = run_qpsk(seed, foff)
        assert len(out) >= 150, f"only {len(out)} output words (loop stalled?)"
        late = out[-80:]
        mag = sum(abs(v) for v in late) / len(late)
        assert 19000 < mag < 27000, (
            f"order-4 did NOT lock to the QPSK grid (seed={seed}, foff={foff}): "
            f"late mean|yi|={mag:.0f} (expect ~23170)")
