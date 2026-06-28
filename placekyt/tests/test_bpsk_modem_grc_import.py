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


def test_autoplace_strategy_aware_off_port(catalog):
    """STRATEGY-AWARE multi-filament auto-place: x16_in feeds TWO filaments (the TX
    mapper chain AND the RX matched-filter chain), so the placer must NOT anchor any
    block on the input port (it stays a free bus tap) and must lay each filament as
    its own coherent region. Asserts:
      * import + place succeed, all 8 blocks placed;
      * NO block sits on the chip INPUT port cell (the multi-filament off-port rule);
      * the RRC pulse shaper is FOLDED (~2 rows, not a flat 1x7 line);
      * the TX and RX filaments are vertically SEPARABLE (their cell rows don't
        interleave) — a coherent, not jumbled, layout.
    """
    from ui.controller import AppController
    from engine.io.chip_type_io import load_chip_type

    chip_type = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    assert len(ctrl.project.blocks) == 8

    ctrl.auto_place(0, use_bus="always")
    for b in ctrl.project.blocks:
        assert b.placement is not None and b.placement.cells, \
            f"{b.name} ({b.type}) was not placed"

    # The chip INPUT port cell.
    port = chip_type.port("x16_in")
    port_cell = (port.cell_x, port.cell_y)
    # NO block may cover the input port cell (multi-filament: the port stays a free
    # bus tap so the bus can reach each filament).
    for b in ctrl.project.blocks:
        cells = {(c.x, c.y) for c in b.placement.cells}
        assert port_cell not in cells, \
            f"{b.type} sits ON the input port {port_cell} (multi-filament must be off-port)"

    # The RRC pulse shaper is FOLDED: ~2 rows, not a flat 1x7 line.
    rrc = next(b for b in ctrl.project.blocks if b.type == "RRCPulseShaperBlock")
    rys = {c.y for c in rrc.placement.cells}
    rxs = {c.x for c in rrc.placement.cells}
    assert len(rys) <= 2 and len(rxs) <= 4, \
        f"RRC not folded: spans rows {sorted(rys)}, cols {sorted(rxs)}"

    # The two filaments occupy SEPARABLE row bands (TX: mapper/up/rrc/upc ; RX:
    # MF/costas/gardner/slicer). The RX-only blocks (Costas, Gardner, Slicer) sit in a
    # band BELOW the TX-only blocks (Upsampler, RRC, IQUpconvert) — a coherent split,
    # not an interleaved jumble. (The two heads, mapper + MF, share the port row.)
    def rows(type_name):
        b = next(x for x in ctrl.project.blocks if x.type == type_name)
        return {c.y for c in b.placement.cells}
    tx_rows = rows("UpsamplerBlock") | rows("RRCPulseShaperBlock") | rows("IQUpconvertBlock")
    rx_rows = rows("ComplexCostasLoopBlock") | rows("GardnerTimingRecovery") \
        | rows("BPSKSlicerBlock")
    assert min(rx_rows) > max(tx_rows), \
        f"filaments interleave: TX rows {sorted(tx_rows)}, RX rows {sorted(rx_rows)}"


def test_autoplace_then_route_status(catalog):
    """The strategy-aware multi-filament auto-place + bus auto-route routes ALL 12 of
    the modem's logical nets over the shared bus. The off-port multi-filament placement
    (the port stays a free tap, each filament in its own coherent region) plus the
    folded RRC and the egress-corridor reservation let the bus fan x16_in out to BOTH
    filaments AND egress BOTH back to x16_out — the full-duplex shared-port design,
    fully routed (no block on a port cell). This is the GUI deliverable: importing
    bpsk_modem.grc → full place-and-route yields a coherent, completely-routed layout.
    """
    from ui.controller import AppController
    from engine.io.chip_type_io import load_chip_type

    chip_type = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    assert len(ctrl.project.blocks) == 8

    ctrl.auto_place(0, use_bus="always")
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    routed = sorted(r.name for r in rep.routed)
    failed = [(r.name, r.reason) for r in rep.failed]
    print(f"\n[bpsk_modem auto-route] ok={rep.ok} "
          f"routed={len(routed)} failed={len(failed)}")
    if failed:
        print("  failed nets:", failed)
    # ALL 12 nets route — the coherent, fully-routed multi-filament modem layout.
    assert rep.ok and len(routed) == 12, \
        f"routed {len(routed)}/12, failed: {failed}"


def test_gui_import_path_routes_all_12(catalog):
    """The ACTUAL GUI import path (File→Import GNURadio Flowgraph, full P&R) must
    route all 12 nets. The GUI runs auto_place(use_bus) then auto_route_all with
    auto_orient = (use_bus != "always") — for bus mode the strategy-aware placer
    already oriented everything, and the flow-orient re-pass would re-rotate a
    block and strand a broker tap (the net10 "no free broker cell" regression).
    This mirrors ui.main_window._import_grc exactly so a future change there can't
    silently leave the modem 11/12."""
    from ui.controller import AppController
    from engine.io.chip_type_io import load_chip_type

    chip_type = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    use_bus = "always"                       # the GUI default route strategy
    ctrl.auto_place(use_bus=use_bus)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type},
                              use_bus=use_bus,
                              auto_orient=(use_bus != "always"))
    failed = [(r.name, r.reason) for r in rep.failed]
    assert rep.ok and len(rep.routed) == 12, \
        f"GUI import path routed {len(rep.routed)}/12, failed: {failed}"
