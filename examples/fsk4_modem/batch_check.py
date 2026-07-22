#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# M17 4FSK modem — BATCH (run-to-completion) headless BER checker.
#
# Drives the RX chain of the hosted 4FSK modem chip: an FM discriminator
# (QuadratureDemod), an RRC matched filter, M17 sync-word TIMING RECOVERY (a Gardner
# loop does NOT lock a 4-level FSK signal — real M17 receivers correlate the sync
# word), and a 4FSK slicer. The input is a framed M17 4FSK burst (preamble + LSF sync
# word + payload), FM-modulated and delivered as complex I/Q; the chip returns the
# recovered dibits (0..3), one per symbol — a real demodulator, end to end.
#
# Setup:
#   1. In placeKYT, import examples/fsk4_modem/fsk4_modem.grc and place+route it.
#   2. Simulation -> "Run as GNURadio Server"; note the printed port.
#   3. Run this with --port <PORT>.

import socket
import struct
import sys
from argparse import ArgumentParser

import numpy as np

# The stim module ships with gr-kyttar; import it the same way the .grc does.
sys.path.insert(0, __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "..", "gr-kyttar", "python"))
from kyttar import fsk4_demo_stim as stim  # noqa: E402

_HDR = struct.Struct(">I")


def _recv_exactly(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(conn):
    import json
    hlen = _HDR.unpack(_recv_exactly(conn, 4))[0]
    header = json.loads(_recv_exactly(conn, hlen).decode("utf-8"))
    n = int(header.get("n", 0))
    payload = (np.frombuffer(_recv_exactly(conn, n * 4), dtype="<f4")
               if n else None)
    return header, payload


def _send_message(conn, header, payload=None):
    import json
    header = dict(header)
    arr = None
    if payload is not None:
        arr = np.ascontiguousarray(payload, dtype="<f4")
        header["n"] = int(arr.size)
    else:
        header.setdefault("n", 0)
    hbytes = json.dumps(header).encode("utf-8")
    conn.sendall(_HDR.pack(len(hbytes)))
    conn.sendall(hbytes)
    if arr is not None and arr.size:
        conn.sendall(arr.tobytes())


def process_batch(conn, iq_interleaved):
    """Hand the whole interleaved-I/Q RX burst to placeKYT (stream 'rx'), get the
    recovered dibit stream back. ``raw=True`` returns the raw output words (the slicer
    emits the dibit 0..3 in the low bits; Q15 scaling would crush them)."""
    _send_message(conn, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "stream_id": "rx", "complex": True,
                         "raw": True, "pipelined": True},
                  np.asarray(iq_interleaved, dtype="<f4"))
    _reply, out = _recv_message(conn)
    if not _reply.get("ok"):
        raise RuntimeError(f"SimServer error: {_reply.get('error')}")
    return out if out is not None else np.array([], dtype=np.float32)


def _ber(rx_d, tx_d, guard=2, max_lag=4):
    best = (1.0, 0, 0)
    for lag in range(max_lag + 1):
        a = rx_d[lag:]
        m = min(len(a), len(tx_d))
        if m < guard + 40:
            continue
        e = sum(1 for k in range(guard, m) if a[k] != tx_d[k])
        if e / (m - guard) < best[0]:
            best = (e / (m - guard), e, lag)
    return best


def main():
    p = ArgumentParser()
    p.add_argument("--port", type=int, default=58950,
                   help="placeKYT GNURadio-server port")
    p.add_argument("--n", type=int, default=160,
                   help="number of 4FSK payload symbols in the burst")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()

    iq = np.asarray(stim.burst(args.n), dtype=np.complex64)
    tx_dibits = stim.rx_dibits(args.n)
    interleaved = np.empty(2 * len(iq), dtype=np.float32)
    interleaved[0::2] = iq.real
    interleaved[1::2] = iq.imag

    conn = socket.create_connection(("127.0.0.1", args.port))
    recovered = process_batch(conn, interleaved)
    conn.close()

    # The FSK4 slicer emits ONE recovered DIBIT (0..3) per symbol (like the QPSK
    # slicer), so read them directly — no b0/b1 reassembly.
    rx = [int(v) & 3 for v in recovered]
    ber, e, lag = _ber(rx, tx_dibits)
    print(f"Recovered {len(rx)} dibits from {len(iq)} samples "
          f"({args.n} payload symbols)")
    print(f"Symbol BER = {ber:.4f}  ({e} errors, lag {lag})", flush=True)
    if args.no_plot:
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(11, 6))
        ax[0].plot(iq.real, lw=0.8)
        ax[0].set_title("RX 4FSK FM burst (real part) into the chip")
        ax[0].grid(True)
        ax[1].step(range(len(rx)), rx, where="mid", color="tab:green")
        ax[1].set_title("Recovered dibits (chip out) — MF + sync-timing + 4FSK slicer")
        ax[1].set_ylim(-0.3, 3.3); ax[1].set_yticks([0, 1, 2, 3]); ax[1].grid(True)
        fig.tight_layout()
        fig.savefig("fsk4_modem_check.png")
        print("Wrote fsk4_modem_check.png")
    except Exception as exc:  # noqa: BLE001
        print(f"(plot skipped: {exc})")


if __name__ == "__main__":
    sys.exit(main())
