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
    ctrl, name = _place_costas(catalog)
    blk = ctrl.project.block(name)
    assert blk is not None and blk.placement is not None
    # 7 programmed cells in the serpentine fold.
    assert len(blk.placement.cells) == 7
    # 1 FACE-only transit cell (the corner) forms the dphase feedback return.
    transit = getattr(blk.placement, "transit", None) or \
        getattr(blk.placement, "transit_cells", [])
    assert len(transit) == 1, f"expected 1 transit cell, got {len(transit)}"


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
