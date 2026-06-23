"""A hand-drawn route that ends ON the target block's input cell still builds + runs.

The user rerouted Costas->Gardner by drawing a path that terminated ON the Gardner
input cell (3,3) — legal to draw, but it produced NO output. Root cause: the auto-router
ends a block->block route at the BROKER cell ABUTTING the input (the input is reached by
the broker's WRITE@1+JUMP@1); a route ending ON the input cell made the source-exit hop
overshoot and the final transit cell relay into ITSELF, so nothing reached the target.

The build now derives the PHYSICAL route (engine.build._phys_route_pts: strip a trailing
target-input-cell waypoint to the abutting broker) everywhere it consumes a route —
faces/hops (_apply_routes), broker/crossover planning (bus_router._phys_pts), AND the
broker source re-point (_apply_brokers). So a hand-drawn route that stops ON the cell
behaves identically to the auto-router's stop-one-short route.

``reroute_ends_on_target_cell.kyt`` is the user's actual saved broken project (the
Costas->Gardner route ends on the Gardner input cell). It must now recover BER 0.
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
FIXTURE = Path(__file__).parent / "data" / "demo" / "reroute_ends_on_target_cell.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and FIXTURE.exists()),
    reason="chip yaml / fixture absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# --- the production RRC BPSK burst (copied from test_production_rx_mf_ber) ---
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


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0, amp=0.9):
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
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac
                   if 0 <= i < len(shaped) - 1 else shaped[n])
    pk = max(abs(v) for v in out) or 1.0
    return [amp * v / pk for v in out], syms


def _ber_with_lag(rx, tx, max_lag=24, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, c = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(c))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != c[i])
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def test_route_ending_on_target_cell_recovers_ber0(qapp):
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    proj = load_project(str(FIXTURE))

    # Sanity: the Costas->Gardner route really does end ON the Gardner input cell
    # (the condition that used to produce no output).
    cg = next(c for c in proj.connections
              if isinstance(c.source, BlockEndpoint)
              and isinstance(c.target, BlockEndpoint)
              and "costas" in c.source.block.lower()
              and "gardner" in c.target.block.lower())
    g = next(b for b in proj.blocks if b.type == "GardnerTimingRecovery")
    pm = cat.port_map("GardnerTimingRecovery")
    incell_id = next(p.cell_id for p in pm.ports if p.direction == "in")
    gc = g.placement.cell(incell_id)
    assert (cg.route[-1].x, cg.route[-1].y) == (gc.x, gc.y), \
        "fixture must have the route ending ON the Gardner input cell"

    bres = BuildEngine(cat, str(CT_PATH)).build(proj, {"kyttar_10x12": ct})
    assert bres.ok, [str(e) for e in bres.errors]

    entry, _ = cat.resolved_io("ComplexRRCMatchedFilterBlock")
    import simkyt
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    random.seed(5)
    bits = [random.randint(0, 1) for _ in range(160)]
    sig, syms = _tx_signal(bits, timing_offset=0.45, amp=0.9)
    k = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * k)).astype(np.complex64)
    rx = []
    for n in range(len(sig)):
        chip.inject_data_physical([_fq(float(iq[n].real))], target_hop_cnt=30,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(iq[n].imag))], target_hop_cnt=30,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=90000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(w[-1]) & 1)
            chip.release_output_ack("x16_out")
            chip.run(max_events=4000)
    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    assert m and e == 0, \
        f"route-ends-on-target-cell still broken: BER {e}/{m}, {len(rx)} bits"


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    test_route_ending_on_target_cell_recovers_ber0(app)
    print("PASS — hand-drawn route ending on the target cell recovers BER 0")
