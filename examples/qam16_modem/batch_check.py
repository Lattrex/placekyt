#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Coherent 16-QAM RX — BATCH (run-to-completion) through a chip in placeKYT.
#
# Drives the on-chip 16-QAM receiver: a decision-directed complex Costas loop for
# carrier recovery, then a 16-QAM hard-decision slicer. The input is a stream of
# GNU-Radio ``digital.constellation_16qam()`` symbols (4 bits/symbol) with a carrier
# frequency offset; the chip recovers one 4-bit symbol index (0..15) per symbol at
# BER 0 once the loop locks.
#
# 16-QAM is NON-constant-modulus, so the QPSK/BPSK Costas phase detectors fail — this
# runs a decision-directed loop (derotate, slice to the nearest grid point, form the
# error from the decision), the standard constellation_receiver_cb path. Like QPSK it
# keeps a 90-degree 4-fold phase ambiguity, so the BER check tries the 4 rotations.
#
# Batch model: a multi-cell async DUT can't be streamed per-sample in real time (it
# crawls), so the whole interleaved-I/Q burst is handed to placeKYT in ONE
# process_batch RPC and the decoded symbol stream comes back.
#
# Setup:
#   1. In placeKYT, open qam16_modem.kyt (Costas -> slicer; recovered symbols ->
#      x16_out).
#   2. Simulation -> "Run as GNURadio Server"; note the printed port.
#   3. Run this with --port <PORT>.  Plots: input constellation, recovered symbols,
#      and the running symbol BER.

import math
import random
import socket
import struct
import sys
from argparse import ArgumentParser

import numpy as np

_HDR = struct.Struct(">I")

# GNU Radio digital.constellation_16qam() points (index 0..15 -> (I, Q)), the exact
# golden constellation the on-chip mapper/slicer mirror. Units of {-1,-3}/sqrt(10).
_NORM = 1.0 / math.sqrt(10.0)
_LEVELS = [
    (+1, -1), (-1, -1), (+3, -3), (-3, -3),
    (-3, -1), (+3, -1), (-1, -3), (+1, -3),
    (-3, +3), (+3, +3), (-1, +1), (+1, +1),
    (+1, +3), (-1, +3), (+3, +1), (-3, +1),
]
_POINTS = [(i * _NORM, q * _NORM) for (i, q) in _LEVELS]


def _rot_sym(sym, r):
    """Rotate a 16-QAM symbol index by r*90 degrees (the carrier phase ambiguity):
    map its point through the rotation, return the nearest constellation index."""
    i, q = _POINTS[sym]
    for _ in range(r):
        i, q = -q, i
    best, bd = 0, 1e18
    for j, (pi, pj) in enumerate(_POINTS):
        d = (i - pi) ** 2 + (q - pj) ** 2
        if d < bd:
            bd, best = d, j
    return best


def _qam16_ber(rx, tx, max_lag=20, guard=40):
    """Best symbol BER over the 4 constellation rotations x a small lag. Returns
    (errors, symbols, rot, lag)."""
    best = (10 ** 9, 0, 0, 0)
    for r in range(4):
        for lag in range(0, max_lag + 1):
            a = [_rot_sym(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m < 80:
                continue
            e = sum(1 for k in range(guard, m) if a[k] != tx[k])
            if e < best[0]:
                best = (e, m - guard, r, lag)
    return best


# --- minimal SimServer wire client (verbatim from the QPSK/BPSK demos) ---------
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


def process_batch(conn, iq_interleaved, in_port="x16_in", out_port="x16_out"):
    """One RPC: hand the whole interleaved-I/Q burst to placeKYT, get the full
    recovered symbol stream back. ``raw=True`` returns the raw output WORDS (the
    slicer packs the 4-bit symbol in the low bits; Q15 scaling would crush it)."""
    _send_message(conn, {"op": "process_batch", "port": out_port,
                         "in_port": in_port, "data_addrs": [0, 1], "raw": True},
                  np.asarray(iq_interleaved, dtype="<f4"))
    _reply, out = _recv_message(conn)
    if not _reply.get("ok"):
        raise RuntimeError(f"SimServer error: {_reply.get('error')}")
    return out if out is not None else np.array([], dtype=np.float32)


def main():
    p = ArgumentParser()
    p.add_argument("--port", type=int, default=58950,
                   help="placeKYT GNURadio-server port")
    p.add_argument("--foff", type=float, default=0.002,
                   help="carrier offset (cycles/sample); the DD loop locks to ~+-0.003")
    p.add_argument("--n", type=int, default=400,
                   help="number of 16-QAM symbols in the burst")
    p.add_argument("--seed", type=int, default=5)
    p.add_argument("--no-plot", action="store_true",
                   help="print stats only, skip the matplotlib windows")
    args = p.parse_args()

    # --- generate the burst: random 16-QAM symbols + a carrier offset ---------
    random.seed(args.seed)
    tx = [random.randint(0, 15) for _ in range(args.n)]
    base = np.asarray([complex(*_POINTS[s]) for s in tx], dtype=np.complex64)
    k = np.arange(len(base))
    iq = (base * np.exp(1j * 2 * np.pi * args.foff * k)).astype(np.complex64)
    interleaved = np.empty(2 * len(iq), dtype=np.float32)
    interleaved[0::2] = iq.real
    interleaved[1::2] = iq.imag

    # --- one batch through the chip -----------------------------------------
    import time
    conn = socket.create_connection(("127.0.0.1", args.port))
    t0 = time.time()
    recovered = process_batch(conn, interleaved)
    dt = time.time() - t0
    conn.close()
    print(f"Processed {len(iq)} symbols in {dt:.3f}s ({len(iq) / dt:.0f} sym/s) "
          f"-> {len(recovered)} decoded symbols", flush=True)

    # recovered are 0..15 decoded 16-QAM symbol indices (the slicer packs 4 bits).
    rx = [int(round(v)) & 0xF for v in recovered]
    e, mm, rot, lag = _qam16_ber(rx, tx)
    ber = (e / mm) if mm else 1.0
    print(f"Symbol BER = {ber:.4f}  ({e} errors / {mm} symbols, "
          f"best rot={rot}*90deg, lag={lag})", flush=True)
    if args.no_plot:
        return
    print("Opening plots (close the window to exit)...", flush=True)

    try:
        import matplotlib
        for _bk in ("QtAgg", "Qt5Agg"):
            try:
                matplotlib.use(_bk)
                break
            except Exception:  # noqa: BLE001
                continue
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        print(f"(matplotlib unavailable: {exc}; skipping plots)")
        return

    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0, 0].plot(iq.real, iq.imag, ".", ms=3, alpha=0.4)
    ax[0, 0].set_title("Input constellation (16-QAM, carrier offset, into the chip)")
    ax[0, 0].set_aspect("equal"); ax[0, 0].grid(True)
    ax[0, 1].plot(iq.real, lw=0.8)
    ax[0, 1].set_title("Input I")
    ax[0, 1].grid(True)
    ax[1, 0].step(range(len(rx)), rx, where="mid", color="tab:green", lw=1.0)
    ax[1, 0].set_title("Recovered symbols (chip out) — DD Costas + 16-QAM slicer")
    ax[1, 0].set_ylim(-0.5, 15.5); ax[1, 0].grid(True)
    a = [_rot_sym(x, rot) for x in rx[lag:]]
    ref = tx[: len(a)]
    m = min(len(a), len(ref))
    err = np.array([1 if a[i] != ref[i] else 0 for i in range(m)])
    run_ber = np.cumsum(err) / np.arange(1, m + 1)
    ax[1, 1].plot(run_ber, color="tab:red", lw=1.0)
    ax[1, 1].set_title("Running symbol BER (post-alignment) — converges to 0")
    ax[1, 1].set_ylim(-0.02, 1.0); ax[1, 1].grid(True)
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    sys.exit(main())
