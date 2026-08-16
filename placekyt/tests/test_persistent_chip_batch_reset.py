# SPDX-License-Identifier: GPL-3.0-or-later
"""Repeated persistent-chip RX Runs stay BER 0 — the packet-boundary reset guard.

A coherent RX modem hosted PERSISTENTLY on one SimServer used to CORRUPT on
repeated GRC "Run" presses: each Run is a fresh independent packet (new signal,
restart at symbol 0), but the chip's RX loops (Gardner timing, Costas carrier,
ComplexRRC-MF delay lines) still held the PREVIOUS packet's converged lock state,
so the new packet's first samples arrived into mis-locked loops and the bits
corrupted (measured: rep0 BER 0, rep1 ~54, rep2 ~66, rep3 ~84 — even the SAME
seed repeated degraded, so it was carried STATE, not data).

The fix is a DECLARATIVE per-batch reset spec: loop-memory StateVars are flagged
``reset_per_batch`` in the placement blocks; the build resolves them to concrete
``(x, y, addr, value)`` writes on ``ChipBuild.batch_reset_writes``; the SimServer
cold-starts each at the START of every process_batch (each RPC = one explicit
packet boundary). This test is the regression guard: it hosts the auto-P&R modem
on ONE SimServer and drives repeated 300-symbol RX packets, asserting BER 0 for
5 same-seed AND 6 different-seed runs.

Mirrors test_modem_grc_import_duplex_e2e's GUI-import → auto-P&R → build → host
path, all Qt-free.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_persistent_chip_batch_reset.py -x -q
"""

from __future__ import annotations

import os
import random
import socket

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests import modem_helpers as M  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.port_config import batch_reset_writes as resolve_reset_writes  # noqa: E402
from engine.port_config import stream_targets as resolve_stream_targets  # noqa: E402
from engine.sim_bridge import SimServer, recv_message, send_message  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402

GRC_MODEM = EXAMPLES_DIR / "bpsk_modem.grc"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC_MODEM.exists()),
    reason="chip yaml or modem .grc absent")

_NSYM = 300


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def built(qapp):
    """Import bpsk_modem.grc, auto-place + auto-P&R, build, and resolve BOTH the
    stream_targets and the per-batch reset writes from the placed/routed project —
    the exact Qt-free path SimController.start_gnuradio_server uses."""
    from ui.controller import AppController

    catalog = BlockCatalog.from_gr_kyttar()
    chip_type = load_chip_type(str(CT_PATH))

    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, f"import failed, unknown blocks: {res.unknown}"
    ctrl.auto_place(use_bus="always")
    ctrl.auto_pnr({"kyttar_10x12": chip_type}, use_bus="always")
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    targets = resolve_stream_targets(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                                     build_result=bres)
    reset_writes = resolve_reset_writes(bres, 0)
    return bres, targets, reset_writes


def test_batch_reset_writes_resolved(built):
    """The build resolves per-batch reset writes from the flagged loop-memory
    StateVars: a non-empty list of (x, y, addr, value), and EVERY value is a
    cold-start value (loop memory), NOT a coefficient — coefficients live at other
    registers and are never in the reset set."""
    _bres, _targets, rw = built
    assert rw, "no batch_reset_writes resolved — the reset spec didn't flow through"
    # The flagged loop-memory registers reset to their cold-start values:
    #   0 (delay lines, phase accumulators, integrators, carried samples),
    #   8192 (Gardner phase warm 0.5), 16384 (Gardner nominal half-period).
    vals = {v for (_x, _y, _a, v) in rw}
    assert vals <= {0, 8192, 16384}, f"unexpected reset values (coeff?): {vals}"
    # Each entry is a well-formed (x, y, addr, value) tuple.
    for (x, y, a, v) in rw:
        assert 0 <= a < 32 and 0 <= v < 0x10000, (x, y, a, v)


def _stimulus(seed, nsym=_NSYM):
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = M._tx_signal(bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    payload = np.empty(2 * len(iq), dtype=np.float32)
    payload[0::2] = iq.real
    payload[1::2] = iq.imag
    return payload, syms


def _rpc(c, payload):
    send_message(c, {"op": "process_batch", "port": "x16_out", "in_port": "x16_in",
                     "stream_id": "rx", "complex": True, "raw": True},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


def _ber(out, syms):
    rx = [int(round(v)) & 1 for v in (out if out is not None else [])]
    ref = [0 if s > 0 else 1 for s in syms]
    return M._ber_with_lag(rx, ref)


def _run_seeds(bres, targets, reset_writes, seeds):
    """Host the built modem on ONE persistent SimServer and drive one 300-sym RX
    packet per seed (same host, no rebuild between). Returns the per-seed BER."""
    import simkyt

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    srv = SimServer(chip, stream_targets=targets, batch_reset_writes=reset_writes)
    p = srv.start()
    out = []
    try:
        c = socket.socket()
        c.connect(("127.0.0.1", p))
        for seed in seeds:
            payload, syms = _stimulus(seed)
            _h, o = _rpc(c, payload)
            out.append((seed, _ber(o, syms)))
        c.close()
    finally:
        srv.stop()
    return out


def test_repeated_same_seed_persistent_stays_ber0(built):
    """5 repeated Runs of the SAME packet on a persistent chip all recover BER 0.

    This is the headline guard: WITHOUT the packet-boundary reset the SAME seed
    repeated degraded (rep0 0, then ~54/66/84…) because the loops carried the
    previous packet's lock. WITH the reset every repeat cold-starts and recovers
    identically."""
    bres, targets, rw = built
    results = _run_seeds(bres, targets, rw, [7] * 5)
    for seed, (e, m, lag) in results:
        assert m and e == 0, (
            f"repeated same-seed run corrupted: BER={e}/{m} (lag={lag}) — "
            f"the persistent chip carried loop state across packets")


def test_repeated_diff_seeds_persistent_stays_ber0(built):
    """6 different-seed packets on the SAME persistent chip each recover BER 0 —
    no packet inherits a prior packet's Costas/Gardner/matched-filter lock."""
    bres, targets, rw = built
    results = _run_seeds(bres, targets, rw, [1, 2, 3, 4, 5, 6])
    for seed, (e, m, lag) in results:
        assert m and e == 0, (
            f"different-seed run (seed={seed}) corrupted: BER={e}/{m} (lag={lag})")


def test_without_reset_corrupts(built):
    """Control: WITHOUT the reset writes the SAME persistent chip DOES corrupt on
    repeat (this is the bug the reset fixes). rep0 recovers BER 0; a later repeat
    degrades. Documents the mechanism so the guard above can't silently pass on a
    chip that never carried state in the first place."""
    bres, targets, _rw = built
    results = _run_seeds(bres, targets, None, [7] * 4)  # None ⇒ no reset
    (_s0, (e0, m0, _l0)) = results[0]
    assert m0 and e0 == 0, "control invalid: first run already corrupt"
    worst = max(e for _s, (e, _m, _l) in results)
    assert worst > 0, (
        "control invalid: repeated runs stayed BER 0 WITHOUT reset — the carried-"
        "state corruption this fix targets did not reproduce")
