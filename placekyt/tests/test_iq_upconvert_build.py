"""Build the IQUpconvertBlock through the real placeKYT pipeline + run it.

The I/Q passband upconverter (s = I*cos - Q*sin, free-running NCO) is a 6-cell
feed-forward block. This proves it is catalog-discovered, places, builds to a
valid bitstream, and the BUILT bitstream produces the correct passband signal
(matches an ideal continuous I*cos - Q*sin within table quantization).
"""

from __future__ import annotations

import math
import os
import random
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

# freq_word 4096 @ 32 kHz == 2000 Hz carrier (the block now takes Hz, not freq_word)
SAMPLE_RATE = 32000.0
FREQUENCY = 2000.0


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _place(catalog, x=0, y=0):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("IQUp", "kyttar_10x12")
    ctrl.place_block("IQUpconvertBlock", 0, x, y,
                     library="lattrex.official", params={"sample_rate": SAMPLE_RATE, "frequency": FREQUENCY})
    return ctrl


def test_in_catalog(catalog):
    spec = catalog.get("IQUpconvertBlock", "lattrex.official")
    assert spec is not None
    assert spec.default_cell_count == 6
    assert len(spec.input_registers) == 2  # complex I/Q input


def test_builds_to_bitstream(qapp, catalog, chip_type):
    ctrl = _place(catalog)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    assert len(res.words(0)) > 0


def test_built_bitstream_upconverts(qapp, catalog, chip_type):
    """The BUILT bitstream must produce s = I*cos - Q*sin: high correlation +
    <=2-LSB error vs an ideal continuous passband. Anchored at (0,0) so the
    phase landing cell is reachable from x16_in."""
    import simkyt

    ctrl = _place(catalog, 0, 0)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _ = catalog.resolved_io(
        "IQUpconvertBlock", {"sample_rate": SAMPLE_RATE, "frequency": FREQUENCY}, library="lattrex.official")

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    # The block's exit-cell output is @1-abutment (its real consumer is a
    # downstream block); standalone there is no port route, so read the computed
    # passband sample from the upmix cell's 'acc' state register (R9).
    upmix = chip.cell_id_at(5, 0)
    random.seed(7)
    n = 64
    iq = [(random.uniform(-0.7, 0.7), random.uniform(-0.7, 0.7))
          for _ in range(n)]
    chip_out, ideal = [], []
    ph = 0.0
    for (i, q) in iq:
        chip.inject_data_physical([fq(i)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_data_physical([fq(q)], target_hop_cnt=30, target_addr=1)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=20000)
        chip_out.append(s16(chip.read_cell_memory(upmix, 9)))
        ph += round(FREQUENCY / SAMPLE_RATE * 65536) / 65536 * 2 * math.pi
        ideal.append((i * math.cos(ph) - q * math.sin(ph)) * 32768)

    max_err = max(abs(chip_out[k] - ideal[k]) for k in range(n))
    num = sum(chip_out[k] * ideal[k] for k in range(n))
    den = math.sqrt(sum(c * c for c in chip_out) * sum(v * v for v in ideal))
    corr = num / den
    assert corr > 0.999, f"correlation {corr:.4f} too low"
    assert max_err <= 3, f"max error {max_err} LSB too high"
