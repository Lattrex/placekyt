"""MIL-STD-188-110B 75-bps 2-chip RX demo (#162).

A 110B receiver across TWO daisy-chained chips: chip 0 filters (RRC matched
filter → decimating FIR, 8→2 sps), chip 1 recovers (CoherentRXBlock — Costas
carrier + Gardner timing + slice). The heavy deinterleave + Viterbi stages are
FPGA-offloaded (won't fit on-chip). This reloads the shipped 2-chip demo project
and rebuilds it clean through the real open-project path.
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
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


_KYT = Path(__file__).parent / "data" / "demo" / "modem_110b_rx_2chip.kyt"


@pytest.mark.skipif(not _KYT.exists(), reason="demo .kyt absent")
def test_110b_demo_kyt_reloads_and_builds(qapp, catalog, chip_type):
    """The shipped 2-chip 110B RX demo project reloads (2 chips, RRC +
    decimating FIR + CoherentRX) and rebuilds clean — the real open-project path.
    (Decimation is a FIRFilterBlock parameter, matching GR fir_filter_fff(decim,
    taps); the standalone DecimatorBlock was removed.)"""
    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(_KYT))
    assert len(ctrl.project.chips) == 2
    types = sorted(b.type for b in ctrl.project.blocks)
    assert types == ["CoherentRXBlock", "FIRFilterBlock", "RRCPulseShaperBlock"]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
