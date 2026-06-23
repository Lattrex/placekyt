#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Kyttar RX (batch) — a GNURadio block that runs a WHOLE complex burst through
# a placeKYT-hosted chip in ONE process_batch RPC and emits the decoded stream.
#
# This is the GRC-native realization of the proven BATCH bridge model. A multi-cell
# async DUT (a coherent BPSK receiver and up) cannot be streamed per-sample in real
# time — the per-sample socket round-trip CRAWLS. Instead this block collects the
# complex input, hands the entire interleaved-I/Q burst to placeKYT's "Run as
# GNURadio Server" in a single RPC, and outputs the decoded bits. Execute in GRC =>
# one RPC => waveforms in a fraction of a second.
#
# Wire protocol is the placeKYT SimServer's (engine/sim_bridge.py): a 4-byte
# big-endian header length, a JSON header, then little-endian float32 payload.
# This block speaks it directly so it has NO dependency on placeKYT internals — it
# only needs the host/port the GUI prints when you click "Run as GNURadio Server".

import json
import socket
import struct

import numpy as np
from gnuradio import gr

_HDR = struct.Struct(">I")


def _recv_exactly(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("placeKYT server closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(conn):
    hlen = _HDR.unpack(_recv_exactly(conn, 4))[0]
    header = json.loads(_recv_exactly(conn, hlen).decode("utf-8"))
    n = int(header.get("n", 0))
    payload = (np.frombuffer(_recv_exactly(conn, n * 4), dtype="<f4")
               if n else None)
    return header, payload


def _send_message(conn, header, payload=None):
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


class rx_batch(gr.sync_block):
    """Run a complex burst through a placeKYT-hosted chip in one batch RPC.

    Input: complex64 (the I/Q burst — e.g. an RRC BPSK signal with carrier+timing
    offset). Output: float32 decoded stream (the chip's recovered words; for a
    bit-packing slicer, the decoded bit is in each word's LSB — set ``raw=True``).

    The block buffers the whole input, and once GNURadio signals end-of-input (or a
    fixed ``burst_len`` is reached) it sends the interleaved I/Q to the placeKYT
    server's ``process_batch`` op and emits the recovered stream. One RPC per burst.

    Parameters:
      host, port   — the placeKYT "Run as GNURadio Server" endpoint (the GUI prints
                     the port when you start the server).
      in_port      — chip input port the burst enters (default x16_in).
      out_port     — chip output port the decoded stream leaves (default x16_out).
      data_addrs   — [a0, a1]: the I and Q landing registers at the input cell.
      raw          — True returns the raw int16 output WORDS as float (exact for the
                     small integers a packer/slicer emits; Q15 scaling would crush a
                     bit-in-LSB to ~0). True is correct for the coherent BPSK RX.
      burst_len    — if >0, send as soon as this many complex samples accumulate
                     (lets a finite GRC vector source flush without an explicit EOF).
    """

    def __init__(self, host="127.0.0.1", port=58950, in_port="x16_in",
                 out_port="x16_out", data_addr0=0, data_addr1=1, raw=True,
                 burst_len=0):
        # A BASIC block (not sync): it consumes N complex input samples and
        # produces a DIFFERENT number of float outputs (e.g. 319 samples -> 159
        # decoded bits). A sync_block's 1:1 in/out-rate contract does not hold for
        # a decimating receiver, so we manage consume/produce explicitly.
        gr.basic_block.__init__(
            self, name="kyttar_rx_batch",
            in_sig=[np.complex64], out_sig=[np.float32])
        self._host = str(host)
        self._port = int(port)
        self._in_port = str(in_port)
        self._out_port = str(out_port)
        self._addrs = [int(data_addr0), int(data_addr1)]
        self._raw = bool(raw)
        self._burst_len = int(burst_len)
        self._inbuf = []           # accumulated complex input
        self._outq = np.array([], dtype=np.float32)  # decoded, awaiting emit
        self._sent = False         # burst already dispatched?

    # -- batch dispatch -------------------------------------------------------
    def _interleave(self, iq):
        out = np.empty(2 * len(iq), dtype=np.float32)
        out[0::2] = np.real(iq).astype(np.float32)
        out[1::2] = np.imag(iq).astype(np.float32)
        return out

    def _dispatch(self):
        """Send the accumulated burst to the placeKYT server in one RPC and stage
        the decoded stream for output."""
        if self._sent or not self._inbuf:
            return
        iq = np.asarray(self._inbuf, dtype=np.complex64)
        interleaved = self._interleave(iq)
        conn = socket.create_connection((self._host, self._port))
        try:
            _send_message(conn, {"op": "process_batch", "port": self._out_port,
                                 "in_port": self._in_port,
                                 "data_addrs": self._addrs, "raw": self._raw},
                          interleaved)
            reply, out = _recv_message(conn)
        finally:
            conn.close()
        if not reply.get("ok"):
            raise RuntimeError(f"placeKYT SimServer error: {reply.get('error')}")
        self._outq = (out if out is not None
                      else np.array([], dtype=np.float32)).astype(np.float32)
        self._sent = True
        print(f"[kyttar.rx_batch] burst of {len(iq)} samples -> "
              f"{len(self._outq)} decoded values (one process_batch RPC)",
              flush=True)

    def general_work(self, input_items, output_items):
        x = input_items[0]
        out = output_items[0]
        # Consume ALL available input (a basic_block must consume explicitly).
        # Accumulate the burst; dispatch when burst_len is reached (burst_len>0)
        # or at stop() (burst_len==0). The decimating rate is handled by
        # producing only as many decoded samples as we have, independent of how
        # many inputs we consumed — exactly what sync_block could NOT do.
        nin = len(x)
        if not self._sent and nin:
            self._inbuf.extend(np.asarray(x, dtype=np.complex64).tolist())
            if self._burst_len > 0 and len(self._inbuf) >= self._burst_len:
                self._dispatch()
        if nin:
            self.consume(0, nin)
        # Drain the decoded stream to the output.
        n = 0
        if self._sent and len(self._outq):
            n = min(len(out), len(self._outq))
            out[:n] = self._outq[:n]
            self._outq = self._outq[n:]
        # Done once dispatched AND drained: signal end-of-stream so the flowgraph
        # terminates instead of spinning. Only when nothing was produced this call.
        if self._sent and not len(self._outq) and n == 0:
            return -1  # gr.WORK_DONE
        return n

    def stop(self):
        # End of the flowgraph: flush any burst that never hit burst_len (e.g. a
        # head/throttle source that ran to completion without a length trigger).
        try:
            if not self._sent and self._inbuf:
                self._dispatch()
        except Exception as exc:  # noqa: BLE001
            print(f"[kyttar.rx_batch] dispatch on stop failed: {exc}",
                  flush=True)
        return True
