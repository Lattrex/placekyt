"""Live full-duplex modem over the bridge, demuxed by stream_id (§ shared-port).

ONE built duplex bitstream (engine.bpsk_modem_demo) hosts BOTH a coherent BPSK RX
chain AND a production BPSK TX chain, sharing ONE input port (x16_in) and ONE
output port (x16_out). This test drives it the way two GR sources↔sinks would —
via TWO separate ``process_batch`` RPCs over a REAL socket, one per stream:

  * stream 'rx': the RRC BPSK I/Q burst (carrier+timing offset) → recovered bits;
  * stream 'tx': the TX bit burst → passband samples.

The SERVER is the source of truth for placement: it carries a ``stream_targets``
map (the same shape engine.port_config.stream_targets produces) keyed by
stream_id, so each RPC names only its stream and the server resolves the right
entry/hop/data-registers and demuxes the recovered words by out_tag. Neither
client knows any placement-dependent value.

GATES:
  * RX stream recovers bits at BER 0 (lag-aligned, inversion-tolerant);
  * TX stream returns the passband (non-empty, value-close to the demo's TX ref).

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_live_duplex_stream_id.py -x -q
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
from engine.sim_bridge import SimServer, recv_message, send_message  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def built(qapp):
    """The ONE co-resident duplex bitstream (both chains, ONE in + ONE out port)."""
    return M.build_modem()


def _stream_targets(built):
    """The {stream_id -> {entry_addr, hop_count, data_addrs, in_port, out_tag}} the
    server would resolve from the placed duplex project (port_config.stream_targets
    shape). Built here from build_modem's resolved per-direction params so the test
    is self-contained — the RX matched filter takes I/Q (R0/R1), the TX mapper takes
    one bit operand; each chain's output net is tagged distinctly."""
    rx, tx = built["rx"], built["tx"]
    return {
        "rx": {"entry_addr": int(rx["entry"]), "hop_count": int(rx["hop"]),
               "data_addrs": [0, 1], "in_port": "x16_in", "out_tag": M.RX_TAG},
        "tx": {"entry_addr": int(tx["entry"]), "hop_count": int(tx["hop"]),
               "data_addrs": [int(tx["da"])], "in_port": "x16_in",
               "out_tag": M.TX_TAG},
    }


def _client(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    return c


def _batch(c, *, stream_id, payload, complex_, raw):
    """One process_batch RPC for a stream; returns (reply_header, out_array)."""
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "stream_id": stream_id,
                     "complex": bool(complex_), "raw": bool(raw)},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


def test_live_duplex_demux_by_stream_id(built):
    import simkyt

    targets = _stream_targets(built)

    # --- RX stimulus: RRC BPSK I/Q burst, carrier + timing offset --------------
    random.seed(5)
    rx_bits = [random.randint(0, 1) for _ in range(120)]
    sig, syms = M._tx_signal(rx_bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    rx_payload = np.empty(2 * len(iq), dtype=np.float32)
    rx_payload[0::2] = iq.real
    rx_payload[1::2] = iq.imag

    # --- TX stimulus: a bit burst ---------------------------------------------
    tx_bits = [0, 1, 1, 0, 1, 0, 0, 1]
    tx_payload = np.asarray(tx_bits, dtype=np.float32)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(built["bres"].words(0))

    srv = SimServer(chip, stream_targets=targets)
    p = srv.start()
    try:
        c = _client(p)

        # TWO process_batch RPCs over the real socket, one per stream. The RX is a
        # complex (I/Q) bit-packing receiver → raw words (bit in the LSB); the TX
        # is a real bit burst → raw passband words.
        rx_h, rx_out = _batch(c, stream_id="rx", payload=rx_payload,
                              complex_=True, raw=True)
        tx_h, tx_out = _batch(c, stream_id="tx", payload=tx_payload,
                              complex_=False, raw=True)
        c.close()
    finally:
        srv.stop()

    # The server echoes the resolved out_tag (additive reply confirmation).
    assert rx_h["ok"] and rx_h.get("out_tag") == M.RX_TAG, rx_h
    assert tx_h["ok"] and tx_h.get("out_tag") == M.TX_TAG, tx_h

    # --- RX gate: recovered bits at BER 0 (lag-aligned, inversion-tolerant) ----
    rx = [int(round(v)) & 1 for v in (rx_out if rx_out is not None else [])]
    rx_ref = [0 if s > 0 else 1 for s in syms]
    e, m, lag = M._ber_with_lag(rx, rx_ref)
    assert m and e == 0, f"RX stream BER={e}/{m} (lag={lag}); {len(rx)} bits"

    # --- TX gate: non-empty passband, value-close to the demo's TX reference ---
    got = [M._s16(int(v) & 0xFFFF) for v in (tx_out if tx_out is not None else [])]
    assert got, "TX stream returned no passband samples"
    ref = M._tx_reference(tx_bits)
    n = min(len(got), len(ref))
    assert n > 0, "no overlap between TX got and reference"
    maxd = max(abs(got[i] - ref[i]) for i in range(n))
    assert maxd <= M.TX_TOL, \
        f"TX stream max_abs_diff={maxd} (tol {M.TX_TOL}); got={got[:8]} ref={ref[:8]}"


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    b = M.build_modem()
    test_live_duplex_demux_by_stream_id(b)
    print("live duplex demux by stream_id: PASS")
