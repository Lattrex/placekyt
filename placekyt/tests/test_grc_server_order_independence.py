"""FOOLPROOF: any order of {import, place, route, start-server, Run, Reset} must
give the SAME correct output (RX BER 0).

The user's hard requirement: "I should be able to run it any one of those
directions ... and I should get the same results out." The failure mode was a
run that only worked with one exact click sequence (open → server → import →
Run); deviating gave a flat/empty run.

Unlike ``test_modem_grc_import_duplex_e2e`` (which hosts a hand-built
``SimServer`` in the CANONICAL order only), this test drives real
``process_batch`` RPCs through the ACTUAL ``SimController.start_gnuradio_server``
+ ``_rebuild_if_dirty_threadsafe`` path — the code the GUI runs — under each
distinct ordering, and asserts BER 0 for ALL of them:

  ORD-A  canonical:     import → place → route → START server → Run
  ORD-B  server-first:  START server → import → place → route → Run
                        (the "flat run" hazard: server captured an EMPTY
                        stream_targets; the pre-batch dirty-rebuild MUST
                        re-resolve them or the batch injects at the fallback and
                        emits 0 words)
  ORD-C  repeated Run:  ORD-A, then Run AGAIN with no edit (persistent chip;
                        per-batch loop-memory reset must recover a fresh packet)
  ORD-D  reset then Run: ORD-A → 'reset' RPC (rehost) → Run

Each ordering opens a REAL socket to the SimController-hosted server, drives the
RX stream through one ``process_batch``, and checks the recovered bits are the
transmitted bits (lag-aligned, inversion-tolerant) at BER 0. A regression in ANY
ordering fails here — that is the foolproof gate.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_grc_server_order_independence.py -x -q
"""

from __future__ import annotations

import os
import random
import socket

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine import bpsk_modem_demo as M  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.sim_bridge import recv_message, send_message  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402

GRC_MODEM = EXAMPLES_DIR / "bpsk_modem.grc"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC_MODEM.exists()),
    reason="chip yaml or modem .grc absent")

# A fixed test port distinct from the demo's 58950 so this never clashes a real
# GUI session running on the same machine.
_TEST_PORT = 58952


# ---- socket client + one-RPC helper (shared shape with the duplex e2e) --------
def _client(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    return c


def _batch(c, *, stream_id, payload, complex_, raw):
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "stream_id": stream_id,
                     "complex": bool(complex_), "raw": bool(raw)},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


def _reset(c):
    send_message(c, {"op": "reset"}, np.asarray([], dtype=np.float32))
    return recv_message(c)


def _rx_stimulus(seed=5, n=120):
    """The demo's RRC BPSK I/Q burst (carrier + timing offset) + its bit ref."""
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(n)]
    sig, syms = M._tx_signal(bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    payload = np.empty(2 * len(iq), dtype=np.float32)
    payload[0::2] = iq.real
    payload[1::2] = iq.imag
    ref = [0 if s > 0 else 1 for s in syms]
    return payload, ref


def _drive_rx_ber(port):
    """One RX process_batch over a real socket → (errors, matched, lag)."""
    payload, ref = _rx_stimulus()
    c = _client(port)
    try:
        _h, out = _batch(c, stream_id="rx", payload=payload, complex_=True, raw=True)
    finally:
        c.close()
    rx = [int(round(v)) & 1 for v in (out if out is not None else [])]
    return M._ber_with_lag(rx, ref)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _fresh_ctrl():
    """A fresh AppController + SimController with NOTHING imported yet."""
    from ui.controller import AppController
    from ui.sim_controller import SimController

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    sim = SimController(ctrl)
    return ctrl, sim


def _import_place_route(ctrl):
    """Import the modem .grc and auto-P&R it (the GUI deliverable path)."""
    chip_type = load_chip_type(str(CT_PATH))
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, f"import failed: {res.unknown}"
    ctrl.auto_place(use_bus="always")
    rep = ctrl.auto_pnr({"kyttar_10x12": chip_type}, use_bus="always")
    assert rep.ok, f"auto-P&R failed: {[(r.name, r.reason) for r in rep.failed]}"


# ---------------------------------------------------------------------------
# ORD-A — canonical: import → place → route → start server → Run.
# ---------------------------------------------------------------------------
def test_ordA_canonical_ber0(qapp):
    ctrl, sim = _fresh_ctrl()
    _import_place_route(ctrl)
    port = sim.start_gnuradio_server(port=_TEST_PORT)
    assert port == _TEST_PORT
    try:
        e, m, lag = _drive_rx_ber(port)
        assert m and e == 0, f"ORD-A canonical BER={e}/{m} (lag={lag})"
    finally:
        sim.stop_gnuradio_server()


# ---------------------------------------------------------------------------
# ORD-B — server-first: start server → import → place → route → Run.
# The server binds BEFORE any design exists, so it starts with an EMPTY
# stream_targets. The first batch's pre-run _rebuild_if_dirty_threadsafe MUST
# rebuild the now-routed chip AND re-resolve stream_targets into the running
# server — else the batch injects at the entry=0/hop=30 fallback and emits 0
# words (the reported "turn the server on, THEN import → flat run" bug).
# ---------------------------------------------------------------------------
def test_ordB_server_first_ber0(qapp):
    ctrl, sim = _fresh_ctrl()
    # Server ON first, on an EMPTY project (no blocks) — mirrors the user
    # enabling the server before importing.
    port = sim.start_gnuradio_server(port=_TEST_PORT)
    assert port == _TEST_PORT
    try:
        srv = sim._gr_server
        # Empty project → no stream targets captured at start.
        assert set(srv._stream_targets) == set(), srv._stream_targets
        # NOW import + place + route (design_version bumps past the hosted one).
        _import_place_route(ctrl)
        # The first batch triggers the pre-run dirty-rebuild + re-resolve.
        e, m, lag = _drive_rx_ber(port)
        assert set(srv._stream_targets) == {"rx", "tx"}, \
            f"stream_targets not re-resolved: {srv._stream_targets}"
        assert m and e == 0, f"ORD-B server-first BER={e}/{m} (lag={lag})"
    finally:
        sim.stop_gnuradio_server()


# ---------------------------------------------------------------------------
# ORD-C — repeated Run with no edit: the persistent hosted chip must recover a
# FRESH packet each Run (per-batch loop-memory reset), AND the fast dirty-check
# path (design unchanged) must not regress the output.
# ---------------------------------------------------------------------------
def test_ordC_repeated_run_ber0(qapp):
    ctrl, sim = _fresh_ctrl()
    _import_place_route(ctrl)
    port = sim.start_gnuradio_server(port=_TEST_PORT)
    assert port == _TEST_PORT
    try:
        for i in range(3):
            e, m, lag = _drive_rx_ber(port)
            assert m and e == 0, f"ORD-C run #{i + 1} BER={e}/{m} (lag={lag})"
    finally:
        sim.stop_gnuradio_server()


# ---------------------------------------------------------------------------
# ORD-D — reset then Run: a 'reset' RPC rehosts a fresh chip; the next Run must
# still recover BER 0 (the rehost path re-configures the input port + keeps
# stream_targets, and the fresh chip runs the same design).
# ---------------------------------------------------------------------------
def test_ordD_reset_then_run_ber0(qapp):
    ctrl, sim = _fresh_ctrl()
    _import_place_route(ctrl)
    port = sim.start_gnuradio_server(port=_TEST_PORT)
    assert port == _TEST_PORT
    try:
        # First Run establishes the baseline.
        e, m, lag = _drive_rx_ber(port)
        assert m and e == 0, f"ORD-D pre-reset BER={e}/{m} (lag={lag})"
        # Reset RPC (the client requesting a fresh run), then Run again.
        c = _client(port)
        try:
            rh, _ = _reset(c)
            assert rh.get("ok"), f"reset RPC failed: {rh}"
        finally:
            c.close()
        e, m, lag = _drive_rx_ber(port)
        assert m and e == 0, f"ORD-D post-reset BER={e}/{m} (lag={lag})"
    finally:
        sim.stop_gnuradio_server()
