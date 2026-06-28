"""Full-duplex BPSK modem GRC import test (#334 — the GRC-first front end).

A GNU Radio .grc flowgraph of the 8 modem DSP blocks (TX: PSK symbol mapper ->
upsampler -> RRC pulse shaper -> I/Q upconvert; RX: complex RRC matched filter ->
complex Costas loop -> Gardner timing recovery -> BPSK slicer) imports CLEANLY
into placeKYT: every kyttar_* block resolves to a catalog type, both source/sink
pairs collapse onto the shared x16_in / x16_out duplex ports, and the 12 logical
nets are recovered. This is the IMPORT gate; BER is proven headless by
engine.bpsk_modem_demo / test_bpsk_modem on the built bitstream.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.grc_import import import_grc  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402

GRC_MODEM = EXAMPLES_DIR / "bpsk_modem.grc"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC_MODEM.exists()),
    reason="chip yaml or modem .grc absent")

# The 8 modem DSP block TYPES the importer must produce.
EXPECTED_TYPES = {
    "PSKSymbolMapperBlock",
    "UpsamplerBlock",
    "RRCPulseShaperBlock",
    "IQUpconvertBlock",
    "ComplexRRCMatchedFilterBlock",
    "ComplexCostasLoopBlock",
    "GardnerTimingRecovery",
    "BPSKSlicerBlock",
}


@pytest.fixture(scope="module")
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(_qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def result(catalog):
    return import_grc(GRC_MODEM, catalog, chip_type="kyttar_10x12")


def test_import_ok(result):
    """No unknown kyttar_* blocks — every DSP block resolved to a catalog type."""
    assert result.ok, f"unknown blocks: {result.unknown}"
    assert not result.unknown


def test_all_eight_block_types_present(result):
    """All 8 modem block TYPES are placed (and only those)."""
    types = {b.type for b in result.project.blocks}
    assert types == EXPECTED_TYPES, (
        f"missing: {EXPECTED_TYPES - types}, extra: {types - EXPECTED_TYPES}")
    # 8 distinct DSP blocks (one instance each).
    assert len(result.project.blocks) == 8


def _name_of(result, type_name):
    """The placeKYT block instance NAME for a given type (one instance each)."""
    matches = [b.name for b in result.project.blocks if b.type == type_name]
    assert len(matches) == 1, f"expected 1 {type_name}, got {matches}"
    return matches[0]


def test_twelve_logical_nets(result):
    """Exactly 12 logical nets, matching the modem topology with the shared
    x16_in / x16_out duplex ports and the MF I/Q fan-out (xi AND xq)."""
    mapper = _name_of(result, "PSKSymbolMapperBlock")
    up = _name_of(result, "UpsamplerBlock")
    rrc = _name_of(result, "RRCPulseShaperBlock")
    upc = _name_of(result, "IQUpconvertBlock")
    mf = _name_of(result, "ComplexRRCMatchedFilterBlock")
    costas = _name_of(result, "ComplexCostasLoopBlock")
    gardner = _name_of(result, "GardnerTimingRecovery")
    slicer = _name_of(result, "BPSKSlicerBlock")

    def src(ep):
        b = getattr(ep, "block", None)
        return ("blk", b, ep.port) if b is not None else ("chip", ep.port)

    nets = {(src(c.source), src(c.target)) for c in result.project.connections}
    assert len(result.project.connections) == 12, (
        f"expected 12 nets, got {len(result.project.connections)}")

    IN = ("chip", "x16_in")
    OUT = ("chip", "x16_out")

    # TX chain: x16_in -> mapper -> up -> rrc -> upc -> x16_out
    assert (IN, ("blk", mapper, "sample")) in nets
    assert (("blk", mapper, "out_i"), ("blk", up, "x")) in nets
    assert (("blk", up, "out"), ("blk", rrc, "sample")) in nets
    assert (("blk", rrc, "out"), ("blk", upc, "xi")) in nets
    assert (("blk", upc, "out"), OUT) in nets

    # RX chain: x16_in -> MF (xi AND xq) -> costas (xi AND xq) -> gardner -> slicer -> x16_out
    assert (IN, ("blk", mf, "xi")) in nets
    assert (IN, ("blk", mf, "xq")) in nets
    assert (("blk", mf, "yi"), ("blk", costas, "xi")) in nets
    assert (("blk", mf, "yq"), ("blk", costas, "xq")) in nets
    assert (("blk", costas, "yi_tap"), ("blk", gardner, "xi")) in nets
    assert (("blk", gardner, "out"), ("blk", slicer, "llr")) in nets
    assert (("blk", slicer, "out"), OUT) in nets


def test_shared_duplex_ports(result):
    """Both TX and RX source/sink pairs collapse onto the SAME chip ports
    (the shared-port full-duplex): x16_in appears as the source of >=2 nets
    (mapper + MF), x16_out as the target of >=2 nets (upc + slicer)."""
    from_in = [c for c in result.project.connections
               if getattr(c.source, "port", None) == "x16_in"
               and getattr(c.source, "block", None) is None]
    to_out = [c for c in result.project.connections
              if getattr(c.target, "port", None) == "x16_out"
              and getattr(c.target, "block", None) is None]
    # x16_in -> mapper, x16_in -> mf:xi, x16_in -> mf:xq  (3 fan-out nets)
    assert len(from_in) == 3, [(_t(c)) for c in from_in]
    # upc -> x16_out, slicer -> x16_out  (2 fan-in nets)
    assert len(to_out) == 2, [(_t(c)) for c in to_out]


def _t(c):
    return (getattr(c.source, "block", None) or getattr(c.source, "port", None),
            getattr(c.target, "block", None) or getattr(c.target, "port", None))


def test_autoplace_then_route_status(catalog):
    """BONUS (non-gating): import via the controller, auto-place all 8 blocks, and
    ATTEMPT a bus auto-route. Auto-route is EXPECTED to be partial — the bus router
    cannot fan ONE input port (x16_in) out to two parallel input nets (mapper AND
    the MF I/Q pair), so the headless modem (engine.bpsk_modem_demo) direct-injects
    each chain by per-burst hop instead. This test asserts only that import + place
    succeed and records the route outcome; BER is proven by test_bpsk_modem.
    """
    from ui.controller import AppController
    from engine.io.chip_type_io import load_chip_type

    chip_type = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    assert len(ctrl.project.blocks) == 8

    ctrl.auto_place(0)
    # Every block got real cell placements.
    for b in ctrl.project.blocks:
        assert b.placement is not None and b.placement.cells, \
            f"{b.name} ({b.type}) was not placed"

    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    routed = sorted(r.name for r in rep.routed)
    failed = [(r.name, r.reason) for r in rep.failed]
    # Informational — NOT an assertion on rep.ok (shared-port fan-out is a known
    # router limit; the modem uses direct-inject, see test_bpsk_modem).
    print(f"\n[bpsk_modem auto-route] ok={rep.ok} "
          f"routed={len(routed)} failed={len(failed)}")
    if failed:
        print("  failed nets:", failed)
