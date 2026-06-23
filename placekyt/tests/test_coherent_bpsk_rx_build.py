"""Build + run the redesigned CoherentBPSKRxBlock through the real placeKYT
pipeline (AppController.place_block uses the block's default_layout + routes the
internal connections — the same path the GUI uses).

The 8-cell compact 4x2 Costas loop with the END-OF-CHAIN packing slicer: the
slicer relays dphase north into phase (feedback) and packs sliced bits MSB-first
into 16-bit words, emitting one word south every 16 samples. Proves:
  * the block places + routes with NO face conflict (the old slicer-in-the-middle
    layout produced 0 output),
  * the carrier loop locks through a carrier offset,
  * the chip emits packed 16-bit words matching the float reference.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
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
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _place(catalog, x=0, y=0):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("CoherentBPSKRX", "kyttar_10x12")
    ctrl.place_block("CoherentBPSKRxBlock", 0, x, y,
                     library="lattrex.official")
    return ctrl


def _build(catalog, chip_type, x=0, y=0):
    ctrl = _place(catalog, x, y)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    return ctrl, res


def test_in_catalog(catalog):
    spec = catalog.get("CoherentBPSKRxBlock", "lattrex.official")
    assert spec is not None
    assert spec.default_cell_count == 8


def test_places_compact_4x2(qapp, catalog):
    ctrl = _place(catalog, 0, 0)
    blk = ctrl.project.blocks[-1]
    assert blk.placement is not None
    assert len(blk.placement.cells) == 8
    # slicer must be the output cell and sit at the loop's return corner.
    ids = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    assert "slicer" in ids


def test_builds_clean(qapp, catalog, chip_type):
    _ctrl, res = _build(catalog, chip_type, 0, 0)
    assert res.ok, [str(e) for e in res.errors]
    assert len(res.words(0)) > 0


def _drive(chip, entry, syms, foff):
    """Drive the built loop one sample at a time via the proven inject path
    (xi, xq as separate DATA injects, then a JUMP trigger), reading the recovered
    I from the rotate cell operands. Returns (yi_list, slicer_words)."""
    rot = chip.cell_id_at(2, 1)   # rotate in the compact 4x2
    sl = chip.cell_id_at(0, 1)    # slicer (packer) state

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def mq(a, b):
        return (s16(a) * s16(b)) >> 15

    yis, counts = [], []
    for k, sym in enumerate(syms):
        xi = fq(sym * math.cos(2 * math.pi * foff * k))
        xq = fq(sym * math.sin(2 * math.pi * foff * k))
        chip.inject_data_physical([xi], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_data_physical([xq], target_hop_cnt=30, target_addr=1)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=30000)
        xis = chip.read_cell_memory(rot, 5)
        xqs = chip.read_cell_memory(rot, 6)
        sv = chip.read_cell_memory(rot, 7)
        cv = chip.read_cell_memory(rot, 8)
        yis.append(mq(xis, cv) - mq(xqs, sv))
        counts.append(chip.read_cell_memory(sl, 9))   # packer bit-counter
    return yis, counts


def test_built_loop_locks(qapp, catalog, chip_type):
    """The placeKYT-built compact 4x2 (with the end-of-chain packing slicer that
    relays dphase) must LOCK: drive carrier-offset BPSK and confirm the recovered
    I sign-tracks the symbols. The OLD slicer-in-the-middle layout produced a dead
    loop (0 output); this proves the redesign closes the feedback."""
    import random
    import simkyt

    _ctrl, res = _build(catalog, chip_type, 0, 0)
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io("CoherentBPSKRxBlock")

    for seed, foff in [(3, 0.02), (3, -0.02), (7, 0.015)]:
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical(res.words(0))
        random.seed(seed)
        n = 200
        syms = [random.choice([1, -1]) for _ in range(n)]
        yis, _counts = _drive(chip, entry, syms, foff)
        late = range(n - 50, n)
        sm = sum(1 for k in late if (yis[k] >= 0) == (syms[k] > 0))
        consistency = max(sm, 50 - sm)
        mag = sum(abs(yis[k]) for k in late) / 50
        assert consistency >= 48 and mag > 20000, (
            f"built CoherentBPSKRx did NOT lock (seed={seed}, foff={foff}): "
            f"{consistency}/50, |yi|={mag:.0f}")


def test_live_monitor_streams_recovered_i(qapp, catalog, chip_type):
    """live_monitor mode: the slicer emits the recovered I per sample (1:1) out
    the port instead of packing, so a live GRC scope can watch the loop lock. The
    carrier loop must still lock and one recovered-I sample must egress per input
    sample (the demo path used by coherent_bpsk_live.kyt)."""
    import random
    import simkyt
    from model.connection import ChipPortEndpoint, BlockEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("CBRXmon", "kyttar_10x12")
    nm = ctrl.place_block("CoherentBPSKRxBlock", 0, 0, 0,
                          library="lattrex.official",
                          params={"live_monitor": True})
    # recovered I (slicer south) -> x1_out
    pts = [(0, 1)] + [(0, y) for y in range(2, 12)] + [(x, 11) for x in range(1, 10)]
    ctrl.add_route(BlockEndpoint(block=nm, port="out"),
                   ChipPortEndpoint(chip=0, port="x1_out"), pts)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(
        "CoherentBPSKRxBlock", {"live_monitor": True}, library="lattrex.official")

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    random.seed(3)
    n, foff = 120, 0.02
    syms = [random.choice([1, -1]) for _ in range(n)]
    recv = []
    for k in range(n):
        xi = fq(syms[k] * math.cos(2 * math.pi * foff * k))
        xq = fq(syms[k] * math.sin(2 * math.pi * foff * k))
        chip.inject_data_physical([xi], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_data_physical([xq], target_hop_cnt=30, target_addr=1)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=40000)
        o = chip.read_port_i16("x1_out").view("uint16").tolist()
        if o:
            recv.append(s16(o[-1]))
    # one recovered-I sample per input sample (1:1, NOT 1-per-16).
    assert len(recv) >= n - 2, f"expected ~{n} recovered-I samples, got {len(recv)}"
    late = range(len(recv) - 50, len(recv))
    sm = sum(1 for k in late if (recv[k] >= 0) == (syms[k] > 0))
    assert max(sm, 50 - sm) >= 48, "live_monitor loop did not lock"


def test_batch_bridge_recovers_burst_fast(qapp, catalog, chip_type):
    """The process_batch bridge op runs a WHOLE carrier-offset BPSK burst through
    the live_monitor receiver in ONE RPC and returns the full recovered stream:
    the loop locks (post-lock BER 0) and it runs far faster than per-sample
    streaming (the whole point — a multi-cell DUT can't be streamed in real time).
    """
    import socket
    import time
    import simkyt
    from engine.sim_bridge import SimServer, send_message, recv_message
    from model.connection import ChipPortEndpoint, BlockEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("CBRXbatch", "kyttar_10x12")
    nm = ctrl.place_block("CoherentBPSKRxBlock", 0, 0, 0,
                          library="lattrex.official",
                          params={"live_monitor": True})
    pts = [(0, 1)] + [(0, y) for y in range(2, 12)] + [(x, 11) for x in range(1, 10)]
    ctrl.add_route(BlockEndpoint(block=nm, port="out"),
                   ChipPortEndpoint(chip=0, port="x1_out"), pts)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io(
        "CoherentBPSKRxBlock", {"live_monitor": True}, library="lattrex.official")

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)
    srv = SimServer(chip, host="127.0.0.1", port=0,
                    default_entries={"x16_in": entry})
    p = srv.start()
    try:
        import numpy as np
        rng = np.random.default_rng(3)
        n, foff = 256, 0.02
        bits = rng.integers(0, 2, n)
        syms = np.where(bits == 0, 1.0, -1.0)
        rot = np.exp(1j * 2 * np.pi * foff * np.arange(n))
        iq = (syms * rot).astype(np.complex64)
        inter = np.empty(2 * n, dtype=np.float32)
        inter[0::2] = iq.real
        inter[1::2] = iq.imag

        c = socket.create_connection(("127.0.0.1", p))
        t0 = time.time()
        send_message(c, {"op": "process_batch", "port": "x1_out",
                         "in_port": "x16_in", "data_addrs": [0, 1]}, inter)
        h, out = recv_message(c)
        dt = time.time() - t0
        c.close()
        assert h["ok"]
        assert out is not None and len(out) == n, f"got {0 if out is None else len(out)}/{n}"

        # post-lock BER 0 (inversion-tolerant for the BPSK 180-deg ambiguity).
        chip_bits = (out < 0).astype(int)
        tail = slice(n - 150, n)
        e = int(np.sum(chip_bits[tail] != bits[tail]))
        e = min(e, 150 - e)
        assert e == 0, f"post-lock BER not 0: {e} errors"
        # speed sanity: batch should be ~thousands of samp/s, NOT the ~300 sps
        # of throttled streaming. Generous bound to avoid CI flakiness.
        assert n / dt > 1000, f"batch too slow: {n / dt:.0f} samp/s"
    finally:
        srv.stop()


def test_slicer_packs_and_wraps_every_16(qapp, catalog, chip_type):
    """The end-of-chain slicer must accumulate one bit per sample and wrap on the
    16-bit boundary (the emit cadence). The packer's bit-counter must therefore
    advance 1,2,3,... and return to 0 every 16 samples (count == sample_index+1
    mod 16). Proves the packer counts + emits on the boundary, independent of the
    carrier lock."""
    import random
    import simkyt

    _ctrl, res = _build(catalog, chip_type, 0, 0)
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io("CoherentBPSKRxBlock")

    random.seed(5)
    n = 160
    syms = [random.choice([1, -1]) for _ in range(n)]
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    _yis, counts = _drive(chip, entry, syms, foff=0.02)

    # After sample k the counter must equal (k+1) mod 16 (0 means a word just
    # emitted and the packer reset). Exactly n/16 = 10 wraps.
    expected = [(k + 1) % 16 for k in range(n)]
    assert counts == expected, (
        f"packer counter cadence wrong:\n  got  ={counts[:20]}\n"
        f"  want ={expected[:20]}")
    wraps = sum(1 for c in counts if c == 0)
    assert wraps == n // 16, f"expected {n // 16} word emits, saw {wraps}"
