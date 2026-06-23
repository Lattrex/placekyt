"""Gate-1 on-chip lock check for the DUAL-FACE GardnerTimingRecovery.

Builds, through the REAL placeKYT pipeline, a small chain that drives the Gardner
block's NEW dual-face ``loop_filter`` (in-program FACE flips: `out` egresses
outward, `period_fb` returns to the resampler) and confirms it STILL recovers BPSK
symbol timing at BER 0 over a fractional timing offset.

A leading Gain(1.0) on x16_in forwards each derotated sample into the Gardner
resampler (and triggers it) so the resampler is NOT pinned on the x16_in port cell
(whose NORTH input face would otherwise contend with the period feedback's return).
The Gardner's recovered centers feed a BPSK slicer; the recovered bits leave on
x16_out. This is the standalone Gardner half of the coherent RX — the carrier is
removed in the float reference, exactly as proto_coherent_rx_full does.
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from commands import SetConnectionRouteCommand  # noqa: E402
from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
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


def _make_rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _tx_real(bits, sps=2, beta=0.35, span=6, toff=0.45):
    """Real 2-sps RRC BPSK with a fractional timing offset (no carrier)."""
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(toff))
        frac = toff - math.floor(toff)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    return out, syms


def _ber_with_lag(rx, tx, max_lag=20, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def test_gardner_dualface_recovers_timing_ber0(qapp, catalog, chip_type):
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("g", "kyttar_10x12")
    # Gain(1.0) on x16_in forwards samples into the Gardner resampler (off-port).
    gain = ctrl.place_block("GainBlock", 0, 0, 0, params={"gain": 1.0},
                            library="lattrex.official")
    # Gardner: resampler(2,0) ted(3,0) loop_filter(3,1) transit(2,1).
    ctrl.place_block("GardnerTimingRecovery", 0, 2, 0, library="lattrex.official")
    gar = ctrl.project.blocks[-1].name
    # Slicer south of loop_filter's `out` (3,1)->(3,2). out_mode='bit' (one word
    # per bit) for this per-bit BER check; the block default is now 'word' (packed).
    ctrl.place_block("BPSKSlicerBlock", 0, 3, 2, library="lattrex.official",
                     params={"out_mode": "bit"})
    sli = ctrl.project.blocks[-1].name

    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=gain, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=gain, port="out"),
                                BlockEndpoint(block=gar, port="xi"), name="g2r")
    ctrl.add_logical_connection(BlockEndpoint(block=gar, port="out"),
                                BlockEndpoint(block=sli, port="llr"), name="r2s")
    ctrl.add_logical_connection(BlockEndpoint(block=sli, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")

    g = ctrl.project.block(gar)
    rs = g.placement.cell("resampler")
    lf = g.placement.cell("loop_filter")
    sl = ctrl.project.block(sli).placement.cells[0]
    # gain(0,0) -> resampler(2,0): abut east via (1,0).
    SetConnectionRouteCommand(ctrl.project, "g2r",
                              [(0, 0), (1, 0), (rs.x, rs.y)]).execute()
    # loop_filter out SOUTH -> slicer.
    SetConnectionRouteCommand(ctrl.project, "r2s",
                              [(lf.x, lf.y), (sl.x, sl.y)]).execute()
    # slicer -> x16_out(9,0): east along row then up.
    xout = next(p for p in chip_type.ports if p.name == "x16_out")
    route = [(sl.x, sl.y)]
    for xx in range(sl.x + 1, xout.cell_x + 1):
        route.append((xx, sl.y))
    for y2 in range(sl.y - 1, xout.cell_y - 1, -1):
        route.append((xout.cell_x, y2))
    SetConnectionRouteCommand(ctrl.project, "out", route).execute()

    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    g_entry, _ = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", g_entry)

    random.seed(7)
    nsym = 80
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = _tx_real(bits)

    rx = []
    for v in sig:
        chip.inject_data_physical([_fq(v)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=g_entry)
        chip.run(max_events=60000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(w[-1]) & 1)
            chip.release_output_ack("x16_out")
            chip.run(max_events=3000)

    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    ber = (e / m) if m else 1.0
    # The Gardner decimates ~2:1 (one center per symbol) and recovers the bits.
    assert 0.4 * nsym <= len(rx) <= 1.1 * nsym, \
        f"expected ~{nsym} symbol decisions, got {len(rx)}"
    assert m and e == 0, f"BER={ber:.4f} ({e}/{m}, lag={lag}); {len(rx)} bits"
