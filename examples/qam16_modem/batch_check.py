#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Coherent 16-QAM RX — BATCH (run-to-completion) headless BER checker.
#
# Drives the RX chain of the hosted 16-QAM modem chip: a complex RRC matched
# filter, a complex gain stage (restores the 0.949 outer constellation level the
# decision-directed loops need), Mueller & Muller decision-directed symbol-timing
# recovery (the correct TED for multilevel QAM — raw Gardner leaves ~3% jitter on
# 16-QAM's 4-level axes), a QAM16 decision-directed Costas (carrier phase), and a
# 16-QAM slicer. The input is a random 16-QAM symbol stream, upsampled sps=2 and
# RRC-shaped, delivered as complex I/Q; the chip returns the recovered symbol
# indices (0..15), one per symbol — a real demodulator, end to end. No carrier
# frequency offset: the hosted TX and RX share one clock (foff = 0).
#
# Setup:
#   1. Open examples/qam16_modem/qam16_modem.kyt in placeKYT (File -> Open). This
#      is a dense design the auto-router can't fully route from a fresh .grc
#      import, so open the pre-placed .kyt directly -- do NOT import the .grc.
#   2. Simulation -> "Run as GNURadio Server"; note the printed port.
#   3. Run this with --port <PORT>.

import math
import socket
import struct
import sys
from argparse import ArgumentParser

import numpy as np

# The stim module ships with gr-kyttar; import it the same way the .grc does.
sys.path.insert(0, __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "..", "gr-kyttar", "python"))
from kyttar import qam16_demo_stim as stim  # noqa: E402

_HDR = struct.Struct(">I")

_NORM = 1.0 / math.sqrt(10.0)
_LEVELS = [(+1, -1), (-1, -1), (+3, -3), (-3, -3), (-3, -1), (+3, -1), (-1, -3),
           (+1, -3), (-3, +3), (+3, +3), (-1, +1), (+1, +1), (+1, +3), (-1, +3),
           (+3, +1), (-3, +1)]
_POINTS = [(i * _NORM, q * _NORM) for (i, q) in _LEVELS]


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
    recovered symbol stream back. ``raw=True`` returns the raw output words (the
    slicer emits the symbol index 0..15 in the low bits)."""
    _send_message(conn, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "stream_id": "rx", "complex": True,
                         "raw": True, "pipelined": False},
                  np.asarray(iq_interleaved, dtype="<f4"))
    _reply, out = _recv_message(conn)
    if not _reply.get("ok"):
        raise RuntimeError(f"SimServer error: {_reply.get('error')}")
    return out if out is not None else np.array([], dtype=np.float32)


def _rot_sym(sym, r):
    i, q = _POINTS[sym]
    for _ in range(r):
        i, q = -q, i
    return min(range(16),
              key=lambda j: (i - _POINTS[j][0]) ** 2 + (q - _POINTS[j][1]) ** 2)


def _ber(rx, tx, guard=60, max_lag=25):
    """Best 16-QAM symbol BER over the 4 constellation rotations x a small lag
    (the decision-directed carrier loop keeps a 90-degree ambiguity)."""
    best = (1.0, 0, 0)
    for r in range(4):
        for lag in range(max_lag + 1):
            a = [_rot_sym(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m - guard < 80:
                continue
            e = sum(1 for k in range(guard, m) if a[k] != tx[k])
            if e / (m - guard) < best[0]:
                best = (e / (m - guard), r, lag)
    return best


def main():
    p = ArgumentParser()
    p.add_argument("--port", type=int, default=58950,
                   help="placeKYT GNURadio-server port")
    p.add_argument("--n", type=int, default=400,
                   help="number of 16-QAM symbols in the burst")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()

    iq = np.asarray(stim.burst(args.n), dtype=np.complex64)
    tx_syms = stim.rx_syms(args.n)
    interleaved = np.empty(2 * len(iq), dtype=np.float32)
    interleaved[0::2] = iq.real
    interleaved[1::2] = iq.imag

    conn = socket.create_connection(("127.0.0.1", args.port))
    recovered = process_batch(conn, interleaved)
    conn.close()

    rx = [int(v) & 0xF for v in recovered]
    ber, rot, lag = _ber(rx, tx_syms)
    print(f"Recovered {len(rx)} symbols from {len(iq)} samples "
          f"({args.n} payload symbols)")
    print(f"Symbol BER = {ber:.4f}  (rotation {rot}, lag {lag})", flush=True)
    if args.no_plot:
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(11, 6))
        ax[0].plot(iq.real, lw=0.8)
        ax[0].set_title("RX 16-QAM RRC burst (real part) into the chip")
        ax[0].grid(True)
        aligned = [_rot_sym(x, rot) for x in rx[lag:]]
        ax[1].step(range(len(aligned)), aligned, where="mid", color="tab:green")
        ax[1].set_title("Recovered 16-QAM symbols (chip out) — MF + gain + M&M "
                        "+ Costas + slicer")
        ax[1].set_ylim(-0.5, 15.5); ax[1].grid(True)
        fig.tight_layout()
        fig.savefig("qam16_modem_check.png")
        print("Wrote qam16_modem_check.png")
    except Exception as exc:  # noqa: BLE001
        print(f"(plot skipped: {exc})")


if __name__ == "__main__":
    sys.exit(main())
