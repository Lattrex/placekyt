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
_SESSIONS = {}   # (device_id, stream_id) -> BatchSession


def _default_block_name(placekyt_type):
    """Mirror placeKYT ``ui.controller._default_name`` / ``grc_import`` naming:
    a block TYPE → the default instance NAME (``GainBlock`` → ``"gain"``). Kept in
    sync by hand because this module imports with only socket + numpy (no placeKYT
    on the GR side). Must stay identical to the importer's ``_default_name``."""
    t = str(placekyt_type or "")
    base = t[:-5] if t.endswith("Block") else t
    return base.lower() or "block"


def get_session(device_id, stream_id=""):
    """One shared source↔sink batch session, keyed by ``(device_id, stream_id)``.

    SHARED-INPUT-PORT DUPLEX: two source↔sink pairs that share ONE chip device
    (the full-duplex modem: a TX pair and an RX pair) get SEPARATE sessions by
    naming distinct ``stream_id``s ("tx"/"rx"), so each sink takes only ITS
    stream's recovered words. The default empty ``stream_id`` preserves today's
    single-stream session (one source, one sink, no stream_id)."""
    with _LOCK:
        key = (device_id, str(stream_id or ""))
        s = _SESSIONS.get(key)
        if s is None:
            s = BatchSession(device_id)
            _SESSIONS[key] = s
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
        # GRC-sync: per-flowgraph block params advertised by the marker DSP blocks
        # in this same GR process (keyed by the placeKYT block NAME the importer
        # would assign — see ``register_params``). Sent alongside the batch so the
        # placeKYT host can flag a parameter drift from the placed design.
        self._params_lock = threading.Lock()
        self.grc_params = {}              # placeKYT block name -> params dict
        self._type_counts = {}            # placeKYT type -> instances advertised

    def reset(self):
        with self._cv:
            self._result = None
            self.done = False
            self._cv.notify_all()

    def register_params(self, placekyt_type, params):
        """Advertise one DSP marker block's params for GRC↔placeKYT sync.

        The marker knows its placeKYT TYPE (e.g. ``"GainBlock"``) and its current
        params; it does NOT know the placeKYT block NAME the importer assigned. We
        reconstruct that name with the SAME scheme ``engine/grc_import`` uses: the
        first instance of a type gets ``_default_name(type)`` (``GainBlock`` →
        ``"gain"``), and further instances get ``<base>_2``, ``<base>_3``, … (the
        importer's ``_unique`` suffix). Markers register in GR construction order,
        which mirrors the .grc block order the importer walks, so the names line up
        for the common single-instance-per-type demo. Returns the assigned name.

        NOTE/LIMITATION: this name reconstruction is correct when the placed design
        was IMPORTED from this flowgraph (so names follow the importer scheme) and
        the per-type instance ORDER matches. A user who manually RENAMED a block in
        placeKYT, or hand-built/reordered the design, can desync the keying for
        that block; the diff then simply won't match that block (no false sync, no
        crash). Robust per-instance keying needs the GRC instance id, which a
        ``gr.sync_block`` does not expose to its own Python instance."""
        base = _default_block_name(placekyt_type)
        with self._params_lock:
            # New-run boundary: the previous burst already dispatched (``done``),
            # so the first registration of the NEW run starts a fresh advertisement
            # map. This keeps the per-type counter from growing unboundedly across
            # repeated flowgraph runs in one long-lived GR process (markers
            # re-register every run via ``start``).
            if self.done:
                self.grc_params.clear()
                self._type_counts.clear()
                self.done = False
            n = self._type_counts.get(base, 0)
            self._type_counts[base] = n + 1
            name = base if n == 0 else f"{base}_{n + 1}"
            self.grc_params[name] = dict(params or {})
        return name

    def collected_params(self):
        """A snapshot of the advertised {block name: params} for dispatch."""
        with self._params_lock:
            return {k: dict(v) for k, v in self.grc_params.items()}

    def dispatch(self, host, port, iq, in_port="x16_in", out_port="x16_out",
                 data_addrs=(0, 1), raw=True, complex=True, stream_id=""):
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
        header = {"op": "process_batch", "port": out_port,
                  "in_port": in_port, "complex": bool(complex),
                  "data_addrs": list(data_addrs), "raw": bool(raw)}
        # SHARED-INPUT-PORT DUPLEX: name this burst's stream so the placeKYT
        # server resolves it to the right block's entry/hop/data-addrs and demuxes
        # its recovered words by out_tag (engine.port_config.stream_targets). Empty
        # ⇒ the single-stream path (server uses the port's default entry/hop).
        if stream_id:
            header["stream_id"] = str(stream_id)
        # GRC-sync: advertise the flowgraph's per-block params alongside the burst
        # (additive header field). The placeKYT SimServer routes a present
        # ``grc_params`` to ``on_grc_params`` → the out-of-sync indicator. Absent
        # ⇒ no callback (an older host ignores the field — backward compatible).
        collected = self.collected_params()
        if collected:
            header["grc_params"] = collected
        conn = socket.create_connection((host, int(port)))
        try:
            _send_message(conn, header, payload)
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
