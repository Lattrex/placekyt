"""Shared batch session between kyttar_source and kyttar_sink in SERVER mode.

In the GRC-first demo flowgraph the chain is:

    vector_source -> kyttar_source -> [real DSP blocks] -> kyttar_sink -> time_sink

The real DSP blocks are pass-through MARKERS in the GR graph (they exist so the
flowgraph IMPORTS into placeKYT as real placeable blocks); the actual DSP runs on
the placeKYT-hosted chip. In server-batch mode the source accumulates the whole
complex burst and hands it to the placeKYT SimServer in ONE process_batch RPC; the
sink drains the recovered words and emits them to the downstream GUI sink.

Source and sink live in the same GR process but are separate blocks, so they
coordinate through a process-global session keyed by device_id. This is a tiny,
self-contained channel — no registry/device machinery, no per-sample socket I/O.

The wire protocol is the placeKYT SimServer's (engine/sim_bridge.py): a 4-byte
big-endian header length, a JSON header, then little-endian float32 payload. It is
duplicated here so this module imports with only socket + numpy (no GNURadio, no
placeKYT) — a headless test can drive it directly.
"""

import json
import socket
import struct
import threading

import numpy as np

_HDR = struct.Struct(">I")
_LOCK = threading.Lock()
_SESSIONS = {}   # device_id -> BatchSession


def get_session(device_id):
    with _LOCK:
        s = _SESSIONS.get(device_id)
        if s is None:
            s = BatchSession(device_id)
            _SESSIONS[device_id] = s
        return s


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


class BatchSession:
    """One source↔sink batch handshake for a device_id.

    The source calls :meth:`dispatch` once it has the whole burst; the sink calls
    :meth:`take_result` to drain the recovered words. ``done`` flips True after a
    successful dispatch so the sink knows to stop waiting.
    """

    def __init__(self, device_id):
        self.device_id = device_id
        self._cv = threading.Condition()
        self._result = None
        self.done = False

    def reset(self):
        with self._cv:
            self._result = None
            self.done = False
            self._cv.notify_all()

    def dispatch(self, host, port, iq, in_port="x16_in", out_port="x16_out",
                 data_addrs=(0, 1), raw=True, complex=True):
        """Send the whole burst to the placeKYT SimServer in one process_batch RPC;
        store the recovered words for the sink.

        ``complex=True``  → INTERLEAVED I/Q: payload is [xi0, xq0, xi1, xq1, ...],
        TWO operands per sample (the I/Q receiver path); process_batch injects xi
        and xq to two data addresses. ``complex=False`` → a REAL burst: payload is
        [x0, x1, ...], ONE operand per sample; process_batch injects ONLY xi.

        The real path is REQUIRED for single-input float blocks (e.g. a gain):
        injecting a phantom xq=0 into the second data address would clobber that
        block's state — a gain keeps its coefficient in R1, which is the second
        data address, so the phantom imag zeros the gain and all output goes 0."""
        arr = np.asarray(iq)
        if complex:
            iqc = arr.astype(np.complex64)
            payload = np.empty(2 * len(iqc), dtype=np.float32)
            payload[0::2] = iqc.real
            payload[1::2] = iqc.imag
        else:
            # Real burst: one operand per sample, no phantom imaginary part.
            payload = np.real(arr).astype(np.float32)
        conn = socket.create_connection((host, int(port)))
        try:
            _send_message(conn, {"op": "process_batch", "port": out_port,
                                 "in_port": in_port, "complex": bool(complex),
                                 "data_addrs": list(data_addrs), "raw": bool(raw)},
                          payload)
            reply, out = _recv_message(conn)
        finally:
            conn.close()
        if not reply.get("ok"):
            raise RuntimeError(f"placeKYT SimServer error: {reply.get('error')}")
        result = (out if out is not None
                  else np.array([], dtype=np.float32)).astype(np.float32)
        with self._cv:
            self._result = result
            self.done = True
            self._cv.notify_all()
        return result

    def take_result(self, timeout=None):
        """Block until the source has dispatched, then return the recovered words
        (and clear them so they're emitted once). Returns None on timeout."""
        with self._cv:
            if not self.done:
                self._cv.wait(timeout)
            if not self.done:
                return None
            r = self._result
            self._result = np.array([], dtype=np.float32)
            return r
