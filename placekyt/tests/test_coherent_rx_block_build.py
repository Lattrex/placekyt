"""Build + run the full coherent BPSK receiver ``CoherentRXBlock`` through the
real placeKYT pipeline (catalog -> place -> BuildEngine -> simkyt), end to end.

``CoherentRXBlock`` packages the proven single-bitstream coherent receiver
(``proto_dual_loop_stage2.py``): Costas carrier recovery -> recovered-I (yi)
handoff -> Gardner timing recovery -> on-chip BPSK slice -> recovered BITS. Unlike
the older carrier-only ``CoherentBPSKRxBlock`` (recovered-I, 1 value/sample, no
timing recovery), this block does FULL receive — input is an RRC-shaped 2 sps
passband-derotated stream with a carrier AND timing offset; output is 1 recovered
bit per symbol.

These tests pin the load-bearing claim for PnR Phase 1 / #233: the placeKYT-BUILT
bitstream recovers BER 0 across the carrier+timing offset sweep, matching the
proto. The 2x-output bug (the Gardner mid-strobe path mis-routed to the loop
filter) is the reason the ``"__terminate__"`` router sentinel exists; the
``test_one_bit_per_symbol`` case guards against its regression.
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
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(),
                                reason="chip yaml absent")

BLOCK = "CoherentRXBlock"
LIB = "lattrex.official"


# --- self-contained BPSK RRC transmitter (mirrors proto_gardner.tx_signal) -----

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


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0):
    """BPSK -> RRC pulse-shape at `sps` samples/sym with a fractional timing
    offset. Returns (samples, syms)."""
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
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    return out, syms


def _ber_with_lag(rx, tx, max_lag=20, min_overlap=40):
    """BER with the best symbol-lag alignment, BPSK 180-deg inversion tolerant
    (the coherent loop has a sign ambiguity). Returns (errors, overlap)."""
    best = (10 ** 9, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)  # inversion tolerant
        if e < best[0]:
            best = (e, m)
    return best


# --- fixtures ------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


@pytest.fixture(scope="module")
def built(qapp, catalog, chip_type):
    """Place CoherentRXBlock at the origin (it spans the full fabric width) and
    build it once; reused by the recovery tests."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("CoherentRX", "kyttar_10x12")
    ctrl.place_block(BLOCK, 0, 0, 0, library=LIB)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(BLOCK, library=LIB)
    return res, entry


# --- tests ---------------------------------------------------------------------

def test_in_catalog(catalog):
    spec = catalog.get(BLOCK, LIB)
    assert spec is not None
    assert catalog.cell_count(BLOCK, library=LIB) >= 12


def test_builds_clean(built):
    res, _entry = built
    assert res.ok


def _run(res, entry, foff, toff, seed=5, nbits=120):
    """Stream an RRC BPSK burst (carrier offset foff, timing offset toff) through
    the built chip and return (ber, nbits_out)."""
    import simkyt

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nbits)]
    sig, syms = _tx_signal(bits, timing_offset=toff)
    rx = []
    for n, s in enumerate(sig):
        ph = 2 * math.pi * foff * n
        chip.inject_data_physical([fq(s * math.cos(ph))],
                                  target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_data_physical([fq(s * math.sin(ph))],
                                  target_hop_cnt=30, target_addr=1)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=40000)
        while chip.output_available("x16_out"):
            p = chip.read_port_i16("x16_out").view("uint16").tolist()
            rx.append(int(p[-1]) & 1)
            chip.release_output_ack("x16_out")
            chip.run(max_events=2000)
    tx = [0 if s > 0 else 1 for s in syms]
    e, m = _ber_with_lag(rx, tx)
    return (e / m if m else 1.0), len(rx)


@pytest.mark.parametrize("foff,toff", [(0.0, 0.4), (0.01, 0.5), (-0.008, 0.45)])
def test_recovers_ber0(built, foff, toff):
    """The placeKYT-built receiver recovers BER 0 across carrier+timing offsets —
    the proto's exact result, reproduced through the full build path."""
    res, entry = built
    ber, n = _run(res, entry, foff, toff)
    assert n >= 80, f"too few recovered bits ({n})"
    assert ber == 0.0, f"foff={foff} toff={toff}: BER={ber:.4f} (expected 0)"


def test_auto_pnr_recovers_ber0(qapp, catalog, chip_type):
    """Single-block build smoke test (NOT the flagship).

    Places the PRE-FUSED ``CoherentRXBlock`` (Costas + Gardner + slice already
    hand-composed inside ONE block) and auto-routes its ONE output net, then
    confirms it still recovers BER 0. This proves the build backend + single-block
    input-port anchor — it does NOT exercise the auto-placer/router on a real
    MULTI-block design (the receiver's hard part lives inside the hand-built block,
    untouched by auto-P&R). The REAL flagship — RRC + ComplexCostasLoop + Gardner +
    Slicer as SEPARATE blocks, auto-placed + bus/broker-routed to BER 0 — is the
    active bus/broker work; see ``test_coherent_rx_grc_autopnr.py`` (Stage 5) and
    the KNOWN GAP note in the implementation-status notes."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("autopnr_crx", "kyttar_10x12")
    b = ctrl.place_block(BLOCK, 0, 0, 0, library=LIB)
    ctrl.add_logical_connection(
        BlockEndpoint(block=b, port="bit"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="bit_out")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(BLOCK, library=LIB)
    ber, n = _run(res, entry, foff=0.008, toff=0.45)
    assert n >= 80, f"too few recovered bits ({n})"
    assert ber == 0.0, f"auto-P&R CoherentRX BER={ber:.4f} (expected 0)"


def test_batch_bridge_recovers_bits_fast(built):
    """The GNURadio↔placeKYT `process_batch` bridge runs a WHOLE RRC BPSK burst
    (carrier + timing offset) through the receiver in ONE RPC and returns the full
    decoded-bit stream: BER 0, and far faster than per-sample streaming. Uses
    `raw=True` so the bit (packed in the output word's LSB) survives — Q15 scaling
    would crush it to 0. This is the real modem-validation path for #233."""
    import socket
    import time
    import simkyt
    from engine.sim_bridge import SimServer, send_message, recv_message

    res, entry = built
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)
    srv = SimServer(chip, host="127.0.0.1", port=0,
                    default_entries={"x16_in": entry})
    p = srv.start()
    try:
        import numpy as np
        random.seed(5)
        nbits, foff, toff = 160, 0.008, 0.45
        bits = [random.randint(0, 1) for _ in range(nbits)]
        sig, syms = _tx_signal(bits, timing_offset=toff)
        k = np.arange(len(sig))
        rot = np.exp(1j * 2 * np.pi * foff * k)
        iq = (np.asarray(sig) * rot).astype(np.complex64)
        inter = np.empty(2 * len(sig), dtype=np.float32)
        inter[0::2] = iq.real
        inter[1::2] = iq.imag

        c = socket.create_connection(("127.0.0.1", p))
        t0 = time.time()
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "data_addrs": [0, 1],
                         "raw": True}, inter)
        h, out = recv_message(c)
        dt = time.time() - t0
        c.close()
        assert h["ok"]
        assert out is not None and len(out) >= 80, \
            f"got {0 if out is None else len(out)} bits"

        rx = [int(round(float(v))) & 1 for v in out]
        tx = [0 if s > 0 else 1 for s in syms]
        e, m = _ber_with_lag(rx, tx)
        assert m and e == 0, f"batch BER not 0: {e}/{m}"
        # batch must be fast (thousands of samp/s, not the ~300 sps of streaming)
        assert len(sig) / dt > 1000, f"batch too slow: {len(sig) / dt:.0f} samp/s"
    finally:
        srv.stop()


def test_one_bit_per_symbol(built):
    """REGRESSION for the 2x-output bug: the Gardner mid-strobe path must
    self-terminate (the ``__terminate__`` router sentinel), so the slicer emits
    exactly ONE bit per SYMBOL, not one per SAMPLE. A 120-symbol 2-sps burst must
    yield ~120 bits (not ~240)."""
    res, entry = built
    _ber, n = _run(res, entry, 0.0, 0.4, nbits=120)
    # 120 symbols -> ~120 recovered bits (allow loop warm-up slack). A per-sample
    # (2x) emit would be ~240 — the bound rules that out decisively.
    assert 90 <= n <= 150, f"expected ~120 bits/symbol, got {n} (2x => ~240)"
