"""Build the QAM16ComplexCostasLoopBlock through the real placeKYT pipeline.

Gate for #225: prove the 9-cell decision-directed 16-QAM Costas block (with its
row-1 dphase FEEDBACK return path) places via its ``default_layout`` and routes to
a valid bitstream through the BuildEngine. The lock/track behaviour is verified
separately on-chip in the internal reference implementation (the
incremental-error pipeline recovers symbols, SER<=0.03); here we assert the block
is placeable + routable + loads into simkyt, and that the dphase feedback
routes the full transit return path (NOT @1-abutment).
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

_BLOCK = "QAM16ComplexCostasLoopBlock"
_LIB = "lattrex.official"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _place_qam16(catalog, x=0, y=0):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("QAM16Costas", "kyttar_10x12")
    name = ctrl.place_block(_BLOCK, 0, x, y, library=_LIB)
    return ctrl, name


def test_qam16_costas_in_catalog(catalog):
    spec = catalog.get(_BLOCK, _LIB)
    assert spec is not None
    assert spec.default_cell_count == 9
    # Complex input: two input registers (xi, xq).
    assert len(spec.input_registers) == 2


def test_qam16_costas_places_with_transit_feedback(qapp, catalog):
    ctrl, name = _place_qam16(catalog, x=1, y=1)
    blk = ctrl.project.block(name)
    assert blk is not None and blk.placement is not None
    # 9 programmed cells on the forward row.
    assert len(blk.placement.cells) == 9
    # 9 FACE-only transit cells form the dphase feedback return path.
    transit = getattr(blk.placement, "transit", None) or \
        getattr(blk.placement, "transit_cells", [])
    assert len(transit) == 9, f"expected 9 transit cells, got {len(transit)}"


def test_qam16_costas_builds_to_bitstream(qapp, catalog, chip_type):
    ctrl, _name = _place_qam16(catalog)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    assert 0 in res.chips
    assert len(res.words(0)) > 0


def test_qam16_costas_bitstream_loads_into_simkyt(qapp, catalog, chip_type):
    import simkyt

    ctrl, _name = _place_qam16(catalog)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))


def test_qam16_costas_feedback_hop_resolved(qapp, catalog, chip_type):
    """The dphase feedback (pi -> phase) must route the full transit return path,
    NOT @1-abutment. With the block at (0,0): forward cells (0,0)..(8,0), transit
    return (8,1)..(1,1) west + (0,1) north -> the pi cell's dphase WRITE is @10 to
    the phase cell's dphase register (R2). (#217 build-level internal feedback.)"""
    ctrl, _name = _place_qam16(catalog, x=0, y=0)
    BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    prog = ctrl.cell_program(0, 8, 0)  # pi cell at (8,0)
    fb = [i for i in prog["instructions"]
          if i["kind"] == "WRITE" and i.get("field") == 2]
    assert fb, "pi cell has no dphase WRITE to R2"
    assert fb[0]["hop"] == 10, f"feedback hop {fb[0]['hop']} != 10 (@1-defaulted?)"


def test_qam16_costas_built_bitstream_recovers_symbols(qapp, catalog, chip_type):
    """The placeKYT-BUILT bitstream (not the hand-resolved proto) must RECOVER
    16-QAM symbols: the incremental-error DD loop closes through the transit
    return path. Anchor at (0,0); drive a carrier-offset 16-QAM signal and confirm
    the derotated (yi,yq) slice to the transmitted Gray 4-PAM levels (up to the
    90-deg ambiguity)."""
    import math
    import random
    import simkyt

    ctrl, _name = _place_qam16(catalog, x=0, y=0)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(_BLOCK, {}, library=_LIB)

    norm = 1.0 / math.sqrt(10.0)
    gray = {0b00: -3, 0b01: -1, 0b11: +1, 0b10: +3}
    pam_q15 = {lvl: int(round(max(-1, min(0.999, lvl * norm)) * 32768)) & 0xFFFF
               for lvl in (-3, -1, 1, 3)}
    thr = int(round(2 * norm * 32768)) & 0xFFFF

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def mq(a, b):
        return (s16(a) * s16(b)) >> 15

    def level_of(y):
        yv = s16(y)
        return min((-3, -1, 1, 3),
                   key=lambda L: abs(yv - s16(pam_q15[L])))

    def qam16_tx(bits, foff):
        iq, levels = [], []
        quads = [bits[i:i + 4] for i in range(0, len(bits) - 3, 4)]
        for n, q in enumerate(quads):
            li = gray[(q[0] << 1) | q[1]]
            lq = gray[(q[2] << 1) | q[3]]
            levels.append((li, lq))
            i = li * norm
            qv = lq * norm
            ph = 2 * math.pi * foff * n
            iq.append((fq(i * math.cos(ph) - qv * math.sin(ph)),
                       fq(i * math.sin(ph) + qv * math.cos(ph))))
        return iq, levels

    def ser_with_rotation(rx, tx, settle):
        def rot(levels, k):
            out = []
            for (li, lq) in levels:
                a, b = li, lq
                for _ in range(k):
                    a, b = -b, a
                out.append((a, b))
            return out
        n = min(len(rx), len(tx))
        late = list(range(settle, n))
        best = min(sum(1 for j in late if rot(rx, k)[j] != tx[j])
                   for k in range(4))
        return best / max(1, len(late))

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    rot = chip.cell_id_at(5, 0)  # rotate cell holds yi/yq operands

    random.seed(11)
    bits = [random.randint(0, 1) for _ in range(1200)]
    all_ok = True
    for foff in [0.0, 0.003, -0.003]:
        iq, tx_levels = qam16_tx(bits, foff)
        rx_levels = []
        for (xi, xq) in iq:
            chip.inject_data_physical([xi], target_hop_cnt=30, target_addr=0)
            chip.run(max_events=3000)
            chip.inject_data_physical([xq], target_hop_cnt=30, target_addr=1)
            chip.run(max_events=3000)
            chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
            chip.run(max_events=40000)
            xis = chip.read_cell_memory(rot, 5)
            xqs = chip.read_cell_memory(rot, 6)
            sv = chip.read_cell_memory(rot, 7)
            cv = chip.read_cell_memory(rot, 8)
            yi = (mq(xis, cv) - mq(xqs, sv)) & 0xFFFF
            yq = (mq(xis, sv) + mq(xqs, cv)) & 0xFFFF
            rx_levels.append((level_of(yi), level_of(yq)))
        ser = ser_with_rotation(rx_levels, tx_levels, settle=120)
        all_ok &= ser <= 0.05
        assert ser <= 0.05, f"built QAM16 DD foff={foff}: SER={ser:.3f}"
    assert all_ok
