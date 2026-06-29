#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Kyttar Chip (batch) — a GNURadio block that runs a WHOLE burst through ONE
# chain hosted on a placeKYT chip in a single process_batch RPC and EMITS the
# chain's output stream mid-flowgraph.
#
# This is the rate-changing generalization of rx_batch. A batch DSP chain is
# DECIMATING / rate-changing (a coherent BPSK RX turns 239 I/Q samples into 119
# bits; the TX turns 64 bits into 256 passband words). A gr.sync_block cannot
# carry that — its 1:1 in/out-rate contract truncates the recovered stream to the
# input length, so the words never reach a downstream sink/plot (the headless-vs-
# live disconnect: a direct RPC read sees the words, a sync-block pipeline drops
# them). So this is a gr.basic_block: it consumes the whole input burst, sends it
# in one RPC, and produces the FULL recovered stream regardless of input length.
#
# It is the loopback transceiver vehicle: instantiate it twice against ONE hosted
# chip that has BOTH a TX and an RX chain placed on it —
#   TX stage: stream_id='tx', in=real bits  -> out=real passband
#   RX stage: stream_id='rx', in=complex I/Q -> out=real recovered bits
# — and close the loop in GRC (TX passband -> [channel] -> RX). The ``stream_id``
# tells placeKYT's SimServer which chain to drive (it resolves that chain's broker
# landing + output tag from the hosted build), so two chains share one chip.

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


class chip_batch(gr.basic_block):
    """Run a burst through ONE chain on a placeKYT-hosted chip in one batch RPC
    and emit the chain's recovered stream (rate-changing).

    Parameters:
      stream_id  — which placed chain to drive (e.g. 'tx' / 'rx'); the placeKYT
                   server resolves THIS chain's broker landing + output tag from
                   the hosted build, so two chains can share one chip. Empty ⇒ the
                   server's single-stream default port config.
      in_kind    — 'complex' (interleaved I/Q burst, two operands/sample) or
                   'real' (one operand/sample, e.g. a bit/symbol stream).
      out_kind   — 'real' (default) or 'complex'; the GR output stream dtype.
      raw        — True returns the raw int16 output WORDS as float (exact for the
                   small integers a packer/slicer emits; Q15 scaling would crush a
                   bit-in-LSB to ~0). True for a bit-recovering RX; False for a
                   value path (a passband / filtered sample) the sink rescales.
      burst_len  — if >0, dispatch as soon as this many INPUT samples accumulate
                   (lets a finite GRC vector source flush without an explicit EOF);
                   else dispatch at stop().
      host, port — the placeKYT "Run as GNURadio Server" endpoint.
    """

    def __init__(self, host="127.0.0.1", port=58950, stream_id="",
                 in_kind="complex", out_kind="real", in_port="x16_in",
                 out_port="x16_out", data_addr0=0, data_addr1=1, raw=True,
                 burst_len=0):
        in_dtype = np.complex64 if str(in_kind) == "complex" else np.float32
        out_dtype = np.complex64 if str(out_kind) == "complex" else np.float32
        gr.basic_block.__init__(
            self, name="kyttar_chip_batch",
            in_sig=[in_dtype], out_sig=[out_dtype])
        self._host = str(host)
        self._port = int(port)
        self._stream_id = str(stream_id or "")
        self._complex_in = (str(in_kind) == "complex")
        self._out_complex = (str(out_kind) == "complex")
        self._in_port = str(in_port)
        self._out_port = str(out_port)
        self._addrs = [int(data_addr0), int(data_addr1)]
        self._raw = bool(raw)
        self._burst_len = int(burst_len)
        self._inbuf = []           # accumulated input samples
        self._outq = np.array([], dtype=out_dtype)   # decoded, awaiting emit
        self._out_dtype = out_dtype
        self._sent = False

    # -- batch dispatch -------------------------------------------------------
    def _payload(self, x):
        """Build the float32 payload: interleaved I/Q for a complex burst (two
        operands/sample), or one real operand/sample otherwise."""
        if self._complex_in:
            iq = np.asarray(x, dtype=np.complex64)
            out = np.empty(2 * len(iq), dtype=np.float32)
            out[0::2] = iq.real
            out[1::2] = iq.imag
            return out
        return np.real(np.asarray(x)).astype(np.float32)

    def _dispatch(self):
        if self._sent or not self._inbuf:
            return
        payload = self._payload(self._inbuf)
        header = {"op": "process_batch", "port": self._out_port,
                  "in_port": self._in_port, "complex": self._complex_in,
                  "data_addrs": self._addrs, "raw": self._raw}
        if self._stream_id:
            header["stream_id"] = self._stream_id
        conn = socket.create_connection((self._host, self._port))
        try:
            _send_message(conn, header, payload)
            reply, out = _recv_message(conn)
        finally:
            conn.close()
        if not reply.get("ok"):
            raise RuntimeError(f"placeKYT SimServer error: {reply.get('error')}")
        words = (out if out is not None
                 else np.array([], dtype=np.float32)).astype(np.float32)
        # A complex output stream carries the words on the real rail (the chip
        # emits scalar words; an explicit I/Q recovery would tag two rails).
        self._outq = (words.astype(self._out_dtype) if not self._out_complex
                      else words.astype(np.complex64))
        self._sent = True
        n_in = (len(self._inbuf))
        print(f"[kyttar.chip_batch] stream={self._stream_id!r} burst of {n_in} "
              f"samples -> {len(self._outq)} decoded values (one RPC)", flush=True)

    def general_work(self, input_items, output_items):
        x = input_items[0]
        out = output_items[0]
        nin = len(x)
        if not self._sent and nin:
            self._inbuf.extend(np.asarray(x).tolist())
            if self._burst_len > 0 and len(self._inbuf) >= self._burst_len:
                self._dispatch()
        if nin:
            self.consume(0, nin)
        # Drain the decoded stream to the output (rate-changing: produce only what
        # we have, independent of how many inputs we consumed).
        n = 0
        if self._sent and len(self._outq):
            n = min(len(out), len(self._outq))
            out[:n] = self._outq[:n]
            self._outq = self._outq[n:]
        # Done once dispatched AND fully drained: end the stream so the flowgraph
        # terminates instead of spinning.
        if self._sent and not len(self._outq) and n == 0:
            return -1  # gr.WORK_DONE
        return n

    def stop(self):
        try:
            if not self._sent and self._inbuf:
                self._dispatch()
        except Exception as exc:  # noqa: BLE001
            print(f"[kyttar.chip_batch] dispatch on stop failed: {exc}",
                  flush=True)
        return True
