#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# FULL coherent QPSK RX — BATCH (run-to-completion) through a chip in placeKYT.
#
# This drives the COMPLETE on-chip coherent QPSK receiver: an RRC matched filter,
# an order-4 (QPSK) Costas loop for carrier recovery, M&M decision-directed timing
# recovery, and a QPSK hard-decision slicer. The input is an RRC pulse-shaped QPSK
# stream (2 samples/symbol) with BOTH a carrier offset AND a fractional timing
# offset; the chip output is one decoded 2-bit symbol (0..3) per symbol — a real
# demodulator, end to end.
#
# Batch model: a multi-cell async DUT can't be streamed per-sample in real time (it
# crawls), so the whole interleaved I/Q burst is handed to placeKYT in ONE
# process_batch RPC and the decoded symbol stream comes back.
#
# Setup:
#   1. In placeKYT, open qpsk_modem.kyt (the real 4-block RX; recovered symbols ->
#      x16_out).
#   2. Simulation -> "Run as GNURadio Server"; note the printed port.
#   3. Run this with --port <PORT>.  Plots: input I (RRC), the input constellation,
#      the recovered symbols, and the running symbol BER.

import math
import random
import socket
import struct
import sys
from argparse import ArgumentParser

import numpy as np

_HDR = struct.Struct(">I")


# --- self-contained QPSK RRC transmitter (carrier + timing offset) ------------
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


def _shape(syms, taps, sps):
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    out = []
    for n in range(len(up)):
        acc = 0.0
        for k in range(len(taps)):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        out.append(acc)
    return out


def _timing_shift(sh, toff):
    out = []
    for n in range(len(sh) - 1):
        i = n + int(math.floor(toff))
        frac = toff - math.floor(toff)
        out.append(sh[i] * (1 - frac) + sh[i + 1] * frac
                   if 0 <= i < len(sh) - 1 else sh[n])
    return out


def _tx_signal(symbols, sps=2, beta=0.35, span=8, toff=0.0, amp=0.7):
    """(bi, bq) symbol pairs -> peak-normalised complex RRC I/Q + the symbol
    indices (GR constellation_qpsk map)."""
    si = [(1 if bi == 0 else -1) / math.sqrt(2) for bi, _ in symbols]
    sq = [(1 if bq == 0 else -1) / math.sqrt(2) for _, bq in symbols]
    taps = _make_rrc(beta, sps, span)
    xi = _timing_shift(_shape(si, taps, sps), toff)
    xq = _timing_shift(_shape(sq, taps, sps), toff)
    pk = max(max(abs(a) for a in xi), max(abs(b) for b in xq)) or 1.0
    xi = [amp * a / pk for a in xi]
    xq = [amp * b / pk for b in xq]
    tx = [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in symbols]
    return xi, xq, tx


def _rot(sym, r):
    """Rotate a QPSK symbol index by r*90 deg (the carrier phase ambiguity)."""
    i = 1 if sym & 1 else -1
    q = 1 if sym & 2 else -1
    for _ in range(r):
        i, q = -q, i
    return (2 if q >= 0 else 0) | (1 if i >= 0 else 0)


def _qpsk_ber(rx, tx, max_lag=20, guard=20):
    """Best symbol BER over the 4 constellation rotations x a small lag. Returns
    (errors, symbols, rot, lag)."""
    best = (10 ** 9, 0, 0, 0)
    for r in range(4):
        for lag in range(0, max_lag + 1):
            a = [_rot(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m < 60:
                continue
            e = sum(1 for k in range(guard, m) if a[k] != tx[k])
            if e < best[0]:
                best = (e, m - guard, r, lag)
    return best


# --- minimal SimServer wire client (verbatim from the coherent BPSK demo) -----
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
    slicer packs the 2-bit symbol in the low bits; Q15 scaling would crush it)."""
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
    p.add_argument("--foff", type=float, default=0.008,
                   help="carrier offset (cycles/sample); locks to ~+-0.01")
    p.add_argument("--toff", type=float, default=0.45,
                   help="fractional symbol-timing offset (samples)")
    p.add_argument("--n", type=int, default=160,
                   help="number of QPSK symbols in the burst")
    p.add_argument("--seed", type=int, default=5)
    p.add_argument("--no-plot", action="store_true",
                   help="print stats only, skip the matplotlib windows")
    args = p.parse_args()

    # --- generate the burst: random QPSK -> RRC 2sps + timing + carrier -------
    random.seed(args.seed)
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(args.n)]
    xi, xq, tx = _tx_signal(symbols, toff=args.toff)
    k = np.arange(len(xi))
    base = np.asarray(xi) + 1j * np.asarray(xq)
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
    nsamp = len(iq)
    print(f"Processed {nsamp} samples ({args.n} symbols, 2 sps) in {dt:.3f}s "
          f"({nsamp / dt:.0f} samp/s) -> {len(recovered)} decoded symbols",
          flush=True)

    # recovered are 0..3 decoded QPSK symbol indices (the slicer packs 2 bits).
    rx = [int(round(v)) & 0x3 for v in recovered]
    e, m, rot, lag = _qpsk_ber(rx, tx)
    ber = (e / m) if m else 1.0
    print(f"Symbol BER = {ber:.4f}  ({e} errors / {m} symbols, "
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
    ax[0, 0].plot(iq.real, lw=0.8)
    ax[0, 0].set_title("Input I (RRC QPSK, carrier+timing offset, into the chip)")
    ax[0, 0].grid(True)
    ax[0, 1].plot(iq.real, iq.imag, ".", ms=2, alpha=0.4)
    ax[0, 1].set_title("Input constellation (RRC QPSK)")
    ax[0, 1].set_aspect("equal"); ax[0, 1].grid(True)
    ax[1, 0].step(range(len(rx)), rx, where="mid", color="tab:green", lw=1.0)
    ax[1, 0].set_title("Recovered symbols (chip out) — MF+Costas4+M&M+QPSK slicer")
    ax[1, 0].set_ylim(-0.3, 3.3); ax[1, 0].set_yticks([0, 1, 2, 3])
    ax[1, 0].grid(True)
    a = [_rot(x, rot) for x in rx[lag:]]
    ref = tx[: len(a)]
    mm = min(len(a), len(ref))
    err = np.array([1 if a[i] != ref[i] else 0 for i in range(mm)])
    run_ber = np.cumsum(err) / np.arange(1, mm + 1)
    ax[1, 1].plot(run_ber, color="tab:red", lw=1.0)
    ax[1, 1].set_title("Running symbol BER (post-alignment) — converges to 0")
    ax[1, 1].set_ylim(-0.02, 0.8); ax[1, 1].grid(True)
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    sys.exit(main())
