"""MIL-STD-188-110B 75-bps 2-chip RX demo (#162).

Builds the on-chip portion of a 110B receiver across TWO daisy-chained chips via
the auto-P&R toolchain: chip 0 filters (RRC matched filter → decimate 8→2 sps),
chip 1 recovers (CoherentRXBlock — Costas carrier + Gardner timing + slice). The
heavy deinterleave + Viterbi stages are FPGA-offloaded (won't fit on-chip), per the
HF-modem architecture. This proves the multi-chip auto-place-and-route flow on the
flagship modem design; the full-chain BER cross-check runs through the GNURadio
110B framework (the internal reference framework).
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
from engine.modem_110b_demo import build_110b_rx_2chip  # noqa: E402
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


def test_110b_2chip_rx_auto_pnrs_and_builds(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("110B RX", "kyttar_10x12")
    place_reports, route_reports = build_110b_rx_2chip(ctrl)

    # Two chips; the CoherentRX (full-width) is on its own chip.
    assert len(ctrl.project.chips) == 2
    btypes = {b.name: b.placement.chip for b in ctrl.project.blocks}
    assert any(b.type == "CoherentRXBlock" and b.placement.chip == 1
               for b in ctrl.project.blocks)
    assert any(b.type == "RRCPulseShaperBlock" and b.placement.chip == 0
               for b in ctrl.project.blocks)

    # auto-place + auto-route succeeded on both chips.
    assert all(p.ok for p in place_reports)
    assert all(r.ok for r in route_reports), \
        [(x.name, x.reason) for r in route_reports for x in r.failed]

    # The two chips are daisy-chained x16_out(0) → x16_in(1).
    ics = ctrl.project.inter_chip_connections
    assert any(ic.from_chip == 0 and ic.to_chip == 1 for ic in ics)

    # Builds clean across both chips.
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    # Both chips have programmed cells.
    assert res.chips[0].cell_count > 0 and res.chips[1].cell_count > 0


def test_110b_chip0_front_end_computes(qapp, catalog, chip_type):
    """The chip-0 filter front end (RRC matched filter → decimate 8→2 sps) COMPUTES:
    a real BPSK-rate burst in yields the decimated stream out (count = in/4). Proves
    the auto-P&R'd front-end chip runs, not just builds. (The chip-0→chip-1 complex
    I/Q handoff into the CoherentRX is a separate integration step — the CoherentRX
    needs both xi AND xq, which a single real inter-chip stream can't carry; the
    DSP itself is proven BER 0 single-chip in test_coherent_rx_block_build.)"""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("110B c0", "kyttar_10x12")
    build_110b_rx_2chip(ctrl)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok

    entry, _in = catalog.resolved_io("RRCPulseShaperBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))      # chip 0 only
    chip.set_port_entry_address("x16_in", entry)

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    n_in = 40
    out = []
    for k in range(n_in):
        s = 1.0 if (k // 8) % 2 == 0 else -1.0      # ~8-sps square BPSK
        chip.inject_data_physical([fq(s)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=5000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=20000)
        while chip.output_available("x16_out"):
            p = chip.read_port_i16("x16_out").tolist()
            out.append(p[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=2000)
    # decimate by 4 → ~n_in/4 outputs, and they are non-trivial (filtered).
    assert len(out) >= n_in // 4 - 1
    assert any(abs(v) > 0.05 for v in out), "front end produced only zeros"


_KYT = Path(__file__).parent / "data" / "demo" / "modem_110b_rx_2chip.kyt"


@pytest.mark.skipif(not _KYT.exists(), reason="demo .kyt absent")
def test_110b_demo_kyt_reloads_and_builds(qapp, catalog, chip_type):
    """The shipped 2-chip 110B RX demo project reloads (2 chips, RRC + decimator +
    CoherentRX) and rebuilds clean — a usable saved artifact, not just a generator."""
    ctrl = AppController(catalog=catalog)
    ctrl.open_project(str(_KYT))
    assert len(ctrl.project.chips) == 2
    types = sorted(b.type for b in ctrl.project.blocks)
    assert types == ["CoherentRXBlock", "DecimatorBlock", "RRCPulseShaperBlock"]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
