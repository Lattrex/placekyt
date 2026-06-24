"""MIL-STD-188-110B 75-bps modem RX — 2-chip placeKYT design (#162).

Builds the on-chip portion of a 110B 75-bps receiver as a TWO-CHIP placeKYT
project, laid out + wired by the auto-P&R toolchain. The architecture follows the
HF-modem plan (see the architecture notes / the HF-modem memory): the heavy storage
stages — the 40×72 block deinterleaver and the K=7 Viterbi — are FPGA-offloaded
(2880 symbols won't fit on-chip), so the chip does the front-end carrier/timing
recovery + slice and a back-end descramble, handing coded/decoded bits to the FPGA
over the x1 ports. This demo covers the ON-CHIP stages:

  Chip 0 (filter):     x16_in → RRC matched filter (α=0.35) → decimate (8→2 sps)
                       → x16_out (2-sps complex symbols to chip 1)
  Chip 1 (recover):    x16_in → CoherentRXBlock (Costas carrier + Gardner timing +
                       slice) → coded bits → x16_out (to FPGA deinterleave+Viterbi)

The CoherentRXBlock spans the full fabric width (10 cells), so it gets its OWN
chip — it cannot share a row with the RRC + decimator. This 2-chip split mirrors
the real cell budget (the design doc's ~200-cell, multi-chip RX).

The CoherentRXBlock is the proven BER-0 coherent receiver (#233). Each chip's
blocks are placed + routed by ``auto_place`` + ``auto_route_all``; the two chips
are daisy-chained x16_out(0) → x16_in(1). The full-chain BER cross-check against a
real 110B waveform runs through the GNURadio framework
(the internal reference framework); this generator owns the
placeKYT build.
"""

from __future__ import annotations

from model.connection import BlockEndpoint, ChipPortEndpoint


# 110B 75-bps on-chip RX parameters (the subset that maps to placeKYT blocks).
_RRC_ALPHA = 0.35
_RRC_SPAN = 8
_DECIM = 4               # 8 sps (110B) → 2 sps (CoherentRX Gardner expects 2 sps)


def build_110b_rx_2chip(controller, *, library: str = "lattrex.official"):
    """Construct the 2-chip 110B RX in ``controller``'s project and auto-P&R it.

    Returns ``(place_reports, route_reports)`` — one entry per chip — so the caller
    can assert every block placed + every net routed. The project is left ready to
    build (``controller.build()``).
    """
    # --- Chip 0: filter front-end (RRC MF → decimate 8→2 sps) ----------------
    rrc = controller.place_block(
        "RRCPulseShaperBlock", 0, 0, 0, library=library,
        params={"alpha": _RRC_ALPHA, "span": _RRC_SPAN})
    # Anti-alias lowpass for the 8→2 sps decimation: a short moving-average
    # (1/M each tap) is enough to suppress the imaging for this demo.
    dec = controller.place_block(
        "DecimatorBlock", 0, 3, 3, library=library,
        params={"decimation": _DECIM,
                "coefficients": [1.0 / _DECIM] * _DECIM})
    controller.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=rrc, port="sample"), name="c0_in_rrc")
    controller.add_logical_connection(
        BlockEndpoint(block=rrc, port="out"),
        BlockEndpoint(block=dec, port="sample"), name="c0_rrc_dec")
    controller.add_logical_connection(
        BlockEndpoint(block=dec, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="c0_dec_out")

    # --- Chip 1: carrier/timing recovery + slice (CoherentRXBlock) -----------
    # Full-fabric-width — gets its own chip. Recovers the coded bits.
    controller.add_chip("RX recover")
    crx = controller.place_block("CoherentRXBlock", 1, 0, 0, library=library)
    controller.add_logical_connection(
        ChipPortEndpoint(chip=1, port="x16_in"),
        BlockEndpoint(block=crx, port="xi"), name="c1_in_crx")
    controller.add_logical_connection(
        BlockEndpoint(block=crx, port="bit"),
        ChipPortEndpoint(chip=1, port="x16_out"), name="c1_crx_out")

    # Daisy-chain the two chips: filtered 2-sps symbols → carrier/timing recovery.
    controller.add_inter_chip(0, "x16_out", 1, "x16_in")

    # --- auto-P&R each chip --------------------------------------------------
    place_reports = [controller.auto_place(0), controller.auto_place(1)]
    route_reports = [controller.auto_route_all(auto_orient=True)]
    return place_reports, route_reports


def _first_in(controller, btype, library):
    pm = controller.catalog.port_map(btype, library=library)
    ins = [p.name for p in pm.ports if p.direction == "in"]
    return ins[0] if ins else "sample"


def _first_out(controller, btype, library):
    pm = controller.catalog.port_map(btype, library=library)
    outs = [p.name for p in pm.ports if p.direction == "out"]
    return outs[0] if outs else "out"
