#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# FULL coherent BPSK RX — BATCH (run-to-completion) through a chip in placeKYT.
#
# This drives the COMPLETE on-chip coherent receiver: Costas carrier recovery ->
# recovered-I (yi) handoff -> Gardner TIMING recovery -> on-chip BPSK slice ->
# recovered BITS. The input is an RRC pulse-shaped 2-samples/symbol stream with
# BOTH a carrier offset AND a fractional timing offset, and the chip output is one
# decoded BIT per symbol — a real demodulator, end to end.
#
# RECOMMENDED host: coherent_bpsk_rx_autopnr.kyt — the REAL receiver built from
# THREE SEPARATE catalog blocks (ComplexCostasLoop -> Gardner -> BPSKSlicer),
# auto-placed + bus/broker/crossover-routed by placeKYT (NOT a fused block, NOT
# hand-placed). Recovers BER 0 through the live bridge. (The older
# coherent_rx_block_demo.kyt — the pre-fused single CoherentRXBlock — also works.)
#
# Batch model: a multi-cell async DUT can't be streamed per-sample in real time
# (it crawls), so GNURadio generates a finite burst, hands the WHOLE interleaved
# I/Q burst to placeKYT in ONE process_batch RPC, and gets the decoded bit stream
# back. Runs in a fraction of a second; scales by COMPUTE, never STALLS.
#
# Setup:
#   1. In placeKYT, open coherent_bpsk_rx_autopnr.kyt (the real 3-block RX;
#      recovered bits -> x16_out).
#   2. Simulation -> "Run as GNURadio Server"; note the printed port.
#   3. Run this with --port <PORT>.  Plots: input I (RRC, timing+carrier offset),
#      the recovered bit stream, and the running BER.

import math
import random
import socket
import struct
import sys
from argparse import ArgumentParser

import numpy as np

_HDR = struct.Struct(">I")


# --- self-contained BPSK RRC transmitter (carrier + timing offset) ------------
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
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        inv = e > m - e
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m, lag)
    return best  # (errors, overlap, lag)


# --- minimal SimServer wire client (no GNURadio import needed) ----------------
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
    recovered (decoded-bit) stream back. ``raw=True`` returns the raw output WORDS
    (the slicer packs the decoded bit in the LSB; Q15 scaling would crush it)."""
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
                   help="number of BPSK symbols in the burst")
    p.add_argument("--seed", type=int, default=5)
    p.add_argument("--no-plot", action="store_true",
                   help="print stats only, skip the matplotlib windows")
    args = p.parse_args()

    # --- generate the burst: random BPSK -> RRC 2sps + timing + carrier -------
    random.seed(args.seed)
    bits = [random.randint(0, 1) for _ in range(args.n)]
    sig, syms = _tx_signal(bits, timing_offset=args.toff)   # RRC 2 sps, timing
    k = np.arange(len(sig))
    rot = np.exp(1j * 2 * np.pi * args.foff * k)            # carrier offset
    iq = (np.asarray(sig) * rot).astype(np.complex64)
    interleaved = np.empty(2 * len(sig), dtype=np.float32)
    interleaved[0::2] = iq.real
    interleaved[1::2] = iq.imag

    # --- one batch through the chip -----------------------------------------
    import time
    conn = socket.create_connection(("127.0.0.1", args.port))
    t0 = time.time()
    recovered = process_batch(conn, interleaved)
    dt = time.time() - t0
    conn.close()
    nsamp = len(sig)
    print(f"Processed {nsamp} samples ({args.n} symbols, 2 sps) in {dt:.3f}s "
          f"({nsamp / dt:.0f} samp/s) -> {len(recovered)} decoded bits",
          flush=True)

    # recovered are 0/1 decoded bits (the slicer packs the LSB).
    rx = [int(round(v)) & 1 for v in recovered]
    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    ber = (e / m) if m else 1.0
    print(f"BER = {ber:.4f}  ({e} errors / {m} symbols, best lag={lag})",
          flush=True)
    if args.no_plot:
        return
    print("Opening plots (close the window to exit)...", flush=True)

    try:
        import matplotlib
        # Pick a GUI backend that matches the installed Qt binding: QtAgg works
        # with PySide6/PyQt6 (and PyQt5); fall back to the legacy Qt5Agg, then to
        # any interactive default. (Qt5Agg alone fails on a PySide6-only env.)
        for _bk in ("QtAgg", "Qt5Agg"):
            try:
                matplotlib.use(_bk)
                break
            except Exception:  # noqa: BLE001 — try the next backend
                continue
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        print(f"(matplotlib unavailable: {exc}; skipping plots)")
        return

    fig, ax = plt.subplots(3, 1, figsize=(9, 8))
    ax[0].plot(iq.real, lw=0.8)
    ax[0].set_title("Input I (RRC BPSK, carrier+timing offset, into the chip)")
    ax[0].grid(True)
    ax[1].step(range(len(rx)), rx, where="mid", color="tab:green", lw=1.0)
    ax[1].set_title("Recovered bits (chip out) — full Costas + Gardner RX")
    ax[1].set_ylim(-0.3, 1.3); ax[1].grid(True)
    # running BER over the aligned tail
    a = rx[lag:]
    ref = tx[: len(a)]
    mm = min(len(a), len(ref))
    err = np.array([1 if a[i] != ref[i] else 0 for i in range(mm)])
    # inversion-tolerant: if the majority disagree, the loop locked inverted
    if err.sum() > mm - err.sum():
        err = 1 - err
    run_ber = np.cumsum(err) / np.arange(1, mm + 1)
    ax[2].plot(run_ber, color="tab:red", lw=1.0)
    ax[2].set_title("Running BER (post-alignment) — converges to 0 after lock")
    ax[2].set_ylim(-0.02, 0.55); ax[2].grid(True)
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    sys.exit(main())
