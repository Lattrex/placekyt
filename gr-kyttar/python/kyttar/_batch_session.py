# SPDX-License-Identifier: GPL-3.0-or-later
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
import os
import socket
import struct
import threading
import time as _time

import numpy as np

_HDR = struct.Struct(">I")
_LOCK = threading.Lock()
_SESSIONS = {}   # (device_id, stream_id) -> BatchSession
_RENDEZVOUS = {}  # device_id -> DuplexRendezvous

# DEBUG TRACE (opt-in): set PLACEKYT_DEBUG=1 in the GR-python environment before a
# GRC Run to trace the client-side duplex rendezvous — every source's submit, the
# leader's collect window, the assembled batch, the dispatch, the reply, and each
# stream's result/timeout. This is where a duplex Run stalls when the sources do
# not all rendezvous into ONE RPC (a source arriving after the leader's collect
# window dispatched submits alone, then blocks on the 10 s result wait — the GRC
# flowgraph then hangs and returns "0 recovered"). Off by default (no noise).
# DISTINCT from the simulator's KYTTAR_DEBUG (the per-cell chip firehose) so these
# rendezvous markers stay readable during a real Run.
_DEBUG = os.environ.get("PLACEKYT_DEBUG", "") not in ("", "0", "no", "false")
_T0 = _time.time()


def _dbg(msg):
    if _DEBUG:
        import sys as _s
        _s.stderr.write(f"[kyttar.rv +{_time.time() - _T0:7.3f}s] {msg}\n")
        _s.stderr.flush()


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


_ENDPOINTS = {}  # device_id -> (host, port) of the last-dispatched placeKYT server


def _note_endpoint(device_id, host, port):
    """Remember where this device's server lives (set at every dispatch), so a
    mid-run GRC callback can push a LIVE param update to it (push_params_live)."""
    with _LOCK:
        _ENDPOINTS[str(device_id)] = (str(host), int(port))


def push_params_live(device_id, params):
    """Fire-and-forget ``set_grc_params`` to the device's last-known server — the
    LIVE half of a GRC slider callback. The server turns a registered tunable
    param into a coefficient WRITE on the RUNNING fabric immediately, without
    waiting for the next burst dispatch (which also carries the value, so a
    missed push self-heals). Runs on a daemon thread with a short timeout:
    never blocks the GR/Qt thread, never raises — best-effort by design."""
    with _LOCK:
        ep = _ENDPOINTS.get(str(device_id))
    if ep is None:
        return  # no dispatch yet this process — the next burst carries the value

    def _post(host=ep[0], port=ep[1], p={k: dict(v) for k, v in params.items()}):
        try:
            conn = socket.create_connection((host, port), timeout=1.0)
            try:
                _send_message(conn, {"op": "set_grc_params", "params": p})
                _recv_message(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — best-effort by design
            pass

    threading.Thread(target=_post, daemon=True).start()


def get_rendezvous(device_id):
    """One DuplexRendezvous per chip device — coordinates the TX/RX sources of a
    full-duplex flowgraph so they run INTERLEAVED on the shared input port in ONE
    process_batch_duplex RPC (not tx-whole-burst-then-rx, which put the two
    streams ~1.7M ns apart on the chip clock — see engine/sim_bridge duplex op)."""
    with _LOCK:
        r = _RENDEZVOUS.get(device_id)
        if r is None:
            r = DuplexRendezvous(device_id)
            _RENDEZVOUS[device_id] = r
        return r


class DuplexRendezvous:
    """Collects each source's burst for one Run, then dispatches ONE combined
    process_batch_duplex so the streams run interleaved. Each source calls
    :meth:`submit` with its stream_id + samples; the FIRST caller waits a short
    window for the others, then (as leader) dispatches all collected streams and
    stores each stream's recovered words for :meth:`take_result`.

    Keyed by device_id (shared across streams), distinct from the per-stream
    BatchSession (which still carries results to each sink)."""

    def __init__(self, device_id):
        self.device_id = device_id
        self._cv = threading.Condition()
        self._pending = {}       # stream_id -> submission dict (this Run)
        self._results = {}       # stream_id -> recovered words (np.float32)
        self._gen = 0            # bumped each dispatched Run
        self._taken = {}         # stream_id -> last gen that stream drained
        self._dispatching = False
        # SCHEDULE for this Run's duplex dispatch — see submit()/_dispatch_all.
        # "interleaved" (default) unless a source names a non-default value.
        self._schedule = "interleaved"
        # SATURATED drive for this Run — True if ANY source asked to be pipelined
        # (the honest full-speed drive; see _dispatch_all + sim_bridge duplex).
        self._pipelined = False
        # Per-sample event-budget override for this Run (0 = server default).
        # The MAX over the submitting sources wins; read-and-reset per Run.
        self._max_events_per = 0

    def submit(self, host, port, stream_id, samples, complex_, raw,
               collect_window=0.4, schedule="interleaved", pipelined=False,
               max_events_per=0):
        """Register this stream's burst for the current Run. The leader (first in)
        waits ``collect_window`` s for peers, then dispatches the combined duplex
        RPC. Returns this stream's recovered words.

        ``schedule`` ("interleaved"/"sequential") is the GRC-settable timing knob
        (kyttar_source's Duplex schedule dropdown). Both duplex sources carry it;
        whichever names a NON-default ("sequential") wins for the Run, so setting
        it on either source (or both) works. Reset to the default each new Run.

        The stream carries only its LOGICAL identity (stream_id); placeKYT resolves
        which chip/port/landing it maps to. A multi-chip design is handled entirely
        server-side (the server's stream_targets carry each stream's chip) — the
        rendezvous + dispatch here are identical for single- and multi-chip."""
        with self._cv:
            self._pending[str(stream_id)] = {
                "stream_id": str(stream_id), "samples": np.asarray(samples),
                "complex": bool(complex_), "raw": bool(raw),
            }
            _dbg(f"submit stream={stream_id!r} n={len(np.asarray(samples))} "
                 f"complex={bool(complex_)} raw={bool(raw)} "
                 f"pending_now={sorted(self._pending)} dispatching={self._dispatching}")
            # Non-default schedule wins (a source that leaves it at "interleaved"
            # must not clobber a peer that asked for "sequential").
            sched = str(schedule or "interleaved").lower()
            if sched != "interleaved":
                self._schedule = sched
            # Any source opting into saturation makes the whole Run saturated.
            if pipelined:
                self._pipelined = True
            # The largest requested per-sample event budget wins for the Run.
            if int(max_events_per) > 0:
                self._max_events_per = max(self._max_events_per,
                                           int(max_events_per))
            leader = not self._dispatching
            if leader:
                self._dispatching = True
        _dbg(f"stream={stream_id!r} leader={leader} collect_window={collect_window}")
        if leader:
            # Give peer sources a moment to submit, then dispatch everything.
            import time as _t
            _t.sleep(collect_window)
            with self._cv:
                _dbg(f"LEADER {stream_id!r} dispatching after {collect_window}s "
                     f"window; pending={sorted(self._pending)}")
            self._dispatch_all(host, port)
        # Wait for THIS run's results (leader has them; peers wait for the leader).
        with self._cv:
            g = self._taken.get(str(stream_id), 0)
            _waits = 0
            while self._gen <= g or str(stream_id) not in self._results:
                if not self._cv.wait(timeout=10.0):
                    _waits += 1
                    _dbg(f"stream={stream_id!r} RESULT WAIT TIMEOUT #{_waits} "
                         f"(gen={self._gen} taken_g={g} "
                         f"have_result={str(stream_id) in self._results} "
                         f"results_now={sorted(self._results)}) -- this is the "
                         f"hang: this stream never made it into a dispatched batch")
                    break
                g = self._taken.get(str(stream_id), 0)
            self._taken[str(stream_id)] = self._gen
            res = self._results.get(str(stream_id), np.array([], dtype=np.float32))
            _dbg(f"stream={stream_id!r} RETURN {len(res)} recovered words")
            return res

    def _dispatch_all(self, host, port):
        """Build + send ONE process_batch_duplex from all pending streams; split
        the reply per stream into ``self._results``. The server routes each stream
        to its chip (single- or multi-chip) from its own stream_targets — the
        client sends only stream_id + samples."""
        with self._cv:
            subs = list(self._pending.values())
            self._pending = {}
        _dbg(f"_dispatch_all: batching {len(subs)} stream(s): "
             f"{[s['stream_id'] for s in subs]}")
        # Build header stream list + concatenated payload (stream order preserved).
        streams_hdr = []
        parts = []
        for s in subs:
            arr = np.asarray(s["samples"])
            if s["complex"]:
                iqc = arr.astype(np.complex64)
                seg = np.empty(2 * len(iqc), dtype=np.float32)
                seg[0::2] = iqc.real
                seg[1::2] = iqc.imag
                n = len(iqc)
            else:
                seg = np.real(arr).astype(np.float32)
                n = len(seg)
            parts.append(seg)
            streams_hdr.append({"stream_id": s["stream_id"],
                                "complex": s["complex"], "raw": s["raw"],
                                "n_samples": int(n)})
        payload = (np.concatenate(parts).astype(np.float32) if parts
                   else np.array([], dtype=np.float32))
        # SCHEDULE (timing-analysis knob, no design change): how the duplex streams
        # are driven on the shared input port —
        #   "interleaved" (default): TX + RX round-robin sample-by-sample, so the two
        #     chains contend for the port and each throttles the other (the real
        #     full-duplex rate).
        #   "sequential"/"simplex": each stream's WHOLE burst runs before the next, so
        #     each direction is measured ALONE at its own compute-bound ceiling.
        # Carried from the kyttar_source "Duplex schedule" GRC dropdown (via submit),
        # NOT an env var — the user flips it in the flowgraph and re-Runs; the .kyt is
        # unchanged. Read-and-reset so it doesn't leak into a later Run.
        with self._cv:
            _sched = self._schedule
            _pipe = self._pipelined
            _mep = self._max_events_per
            self._schedule = "interleaved"
            self._pipelined = False
            self._max_events_per = 0
        header = {"op": "process_batch_duplex", "port": "x16_out",
                  "in_port": "x16_in", "streams": streams_hdr, "schedule": _sched,
                  "pipelined": bool(_pipe)}
        # Per-sample event budget: the server runs each injected sample with
        # run(max_events=<this>). Only sent when a source asked (0 = keep the
        # server default 40000); an older host ignores the field.
        if _mep > 0:
            header["max_events_per"] = int(_mep)
        # GRC-sync + LIVE tunables for the duplex path: markers register into the
        # device's default ('' stream) session — ship its snapshot exactly like
        # BatchSession.dispatch does for the single-stream path.
        try:
            collected = get_session(self.device_id).collected_params()
            if collected:
                header["grc_params"] = collected
        except Exception:  # noqa: BLE001 — advertising is best-effort
            pass
        _note_endpoint(self.device_id, host, port)  # enable live param pushes
        _dbg(f"_dispatch_all: connecting {host}:{port} schedule={_sched} "
             f"pipelined={_pipe} n_payload={payload.size}")
        conn = socket.create_connection((host, int(port)))
        try:
            _send_message(conn, header, payload)
            _dbg("_dispatch_all: sent process_batch_duplex; awaiting reply "
                 "(if it hangs HERE the SERVER is wedged on the chip drive)")
            reply, out = _recv_message(conn)
            _dbg(f"_dispatch_all: reply ok={reply.get('ok')} "
                 f"lengths={reply.get('lengths')} ids={reply.get('stream_ids')}")
        finally:
            conn.close()
        if not reply.get("ok"):
            raise RuntimeError(f"placeKYT SimServer error: {reply.get('error')}")
        # Split the concatenated reply back per stream by the reported lengths.
        lengths = list(reply.get("lengths") or [])
        ids = list(reply.get("stream_ids") or [s["stream_id"] for s in streams_hdr])
        out = (out if out is not None else np.array([], dtype=np.float32))
        results = {}
        off = 0
        for sid, ln in zip(ids, lengths):
            results[str(sid)] = np.asarray(out[off:off + ln], dtype=np.float32)
            off += ln
        with self._cv:
            self._results = results
            self._gen += 1
            self._dispatching = False
            self._cv.notify_all()


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


# --- server-mode selection + continuous streaming (hardware) -----------------
# The MODE is chosen by the SERVER, not the user: a hardware backend streams
# continuously (real silicon at USB speed), the simulator batches (too slow to
# stream). The blocks call query_mode() once at start() and pick their data flow
# accordingly — same .grc, no user-facing switch.

def query_mode(host, port, timeout=2.0):
    """Ask the placeKYT server which backend it hosts. Returns 'streaming'
    (hardware) or 'batch' (simulator). Falls back to 'batch' if the server is old
    or unreachable (safe default = today's behavior)."""
    try:
        conn = socket.create_connection((host, int(port)), timeout=timeout)
        try:
            _send_message(conn, {"op": "ping"})
            reply, _ = _recv_message(conn)
        finally:
            conn.close()
        return str(reply.get("mode", "batch"))
    except Exception:  # noqa: BLE001
        return "batch"


def stream_chunk(host, port, samples, *, in_port="x16_in", out_port="x16_out",
                 complex_=False, raw=False, jump_entry=None, timeout=5.0):
    """Push ONE chunk of samples through the chip and return the recovered words.

    Implemented as a small ``process_batch`` per chunk: on hardware the board runs
    at USB speed so a per-work-call batch IS real-time streaming, and reusing
    process_batch means the server resolves the design's hop/entry/data-addrs
    exactly (the same proven path as the sim batch), so the chunk lands on the
    right cell. Returns recovered words as a float32 array (may be empty)."""
    arr = np.ascontiguousarray(samples, dtype="<f4")
    header = {"op": "process_batch", "port": out_port, "in_port": in_port,
              "complex": bool(complex_), "raw": bool(raw)}
    if jump_entry is not None:
        header["jump_entry"] = int(jump_entry)
    conn = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        _send_message(conn, header, arr)
        _reply, out = _recv_message(conn)
    finally:
        conn.close()
    return out if out is not None else np.array([], dtype=np.float32)


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
        # RUN GENERATION: this session is process-global and OUTLIVES a single
        # flowgraph run (the source/sink blocks are re-instantiated each Run, but
        # the session persists in ``_SESSIONS``). ``done`` alone is NOT enough to
        # gate ``take_result``: after run N's sink drains the result, ``done`` stays
        # True, so run N+1's sink would immediately re-take the ALREADY-CONSUMED
        # (empty) result and emit nothing — a FLAT plot on every repeated Run whose
        # sink polls before the source's fresh dispatch (a dispatch-order race). We
        # therefore version each dispatch: the sink waits for a NEWER generation
        # than it last drained, so it always blocks for THIS run's fresh burst.
        self._seq = 0        # bumped on every dispatch (a new burst is available)
        self._taken_seq = 0  # the last generation take_result() has drained
        # GRC-sync: per-flowgraph block params advertised by the marker DSP blocks
        # in this same GR process (keyed by the placeKYT block NAME the importer
        # would assign — see ``register_params``). Sent alongside the batch so the
        # placeKYT host can flag a parameter drift from the placed design.
        self._params_lock = threading.Lock()
        self.grc_params = {}              # placeKYT block name -> params dict
        self._type_counts = {}            # placeKYT type -> instances advertised
        # STREAMING (hardware) mode: a continuous FIFO of recovered words the source
        # pushes each chunk and the sink drains, instead of the one-shot batch result.
        self._stream_q = np.array([], dtype=np.float32)

    def reset(self):
        with self._cv:
            self._result = None
            self.done = False
            # Leave _seq/_taken_seq intact: a reset marks "no result pending", not
            # "re-deliver the last one". A subsequent dispatch bumps _seq so the
            # sink still sees a fresh generation.
            self._cv.notify_all()

    def register_params(self, placekyt_type, params, explicit_name=None):
        """Advertise one DSP marker block's params for GRC↔placeKYT sync.

        ``explicit_name`` (the marker's ``block_name`` param): the placeKYT block
        NAME, verbatim — the ROBUST keying. Set it in the .grc whenever a design
        has several instances of one type: GR's codegen CONSTRUCTION order is NOT
        the .grc walk order (gain_hw constructs gain_blk_b before gain_blk_a), so
        the order-based fallback below can key two same-type blocks SWAPPED —
        harmless for the sync indicator (a no-match), but a LIVE-TUNED param
        would retune the WRONG cell. An explicit name has no such failure mode.

        Fallback (no explicit name): reconstruct the name with the SAME scheme
        ``engine/grc_import`` uses — first instance of a type gets
        ``_default_name(type)`` (``GainBlock`` → ``"gain"``), further instances
        ``<base>_2``, ``<base>_3``, … (the importer's ``_unique`` suffix), in GR
        construction order. Correct for the common single-instance-per-type
        design; a renamed/hand-built/reordered design simply won't match (no
        false sync, no crash). Returns the assigned name."""
        if explicit_name:
            name = str(explicit_name)
            with self._params_lock:
                if self.done:
                    self.grc_params.clear()
                    self._type_counts.clear()
                    self.done = False
                self.grc_params[name] = dict(params or {})
            return name
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

    def result_consumed(self):
        """True when the sink has drained the LATEST dispatched generation — the
        repeat-mode source's re-arm gate: it only accumulates + dispatches the
        next burst once the previous one has been taken, so a slow sink is never
        overrun (a new dispatch would overwrite the un-taken result)."""
        with self._cv:
            return self._seq <= self._taken_seq

    def update_param(self, name, key, value):
        """LIVE update of one advertised param (a GRC slider callback mid-run).

        The next burst dispatch ships the new value in its ``grc_params`` header;
        the placeKYT server turns a registered live-tunable param into a
        coefficient WRITE on the running fabric (no reflash). Unknown ``name``
        (e.g. before the first ``start``) is a silent no-op — advertising is
        best-effort telemetry, never on the data path."""
        with self._params_lock:
            p = self.grc_params.get(name)
            if p is not None:
                p[key] = value

    def collected_params(self):
        """A snapshot of the advertised {block name: params} for dispatch."""
        with self._params_lock:
            return {k: dict(v) for k, v in self.grc_params.items()}

    def dispatch(self, host, port, iq, in_port="x16_in", out_port="x16_out",
                 data_addrs=(0, 1), raw=True, complex=True, stream_id="",
                 pipelined=False, max_events_per=0):
        """Send the whole burst to the placeKYT SimServer in one process_batch RPC;
        store the recovered words for the sink.

        ``pipelined=True`` asks the server to drive the burst SATURATED (queue the
        whole word stream then run to completion) instead of per-sample-to-quiescence
        — the full-speed path. The chip design MUST tolerate back-to-back drive (a
        point-to-point-routed, saturation-safe receiver); a bus-congested layout
        would lock up. Absent/False ⇒ the per-sample path (an older host ignores the
        field). See engine.sim_bridge process_batch pipelined branch.

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
        # FULL-SPEED: drive the whole burst saturated (see docstring). Opt-in.
        if pipelined:
            header["pipelined"] = True
        # Per-sample event budget for the paced drive (0 = server default).
        if int(max_events_per) > 0:
            header["max_events_per"] = int(max_events_per)
        # GRC-sync: advertise the flowgraph's per-block params alongside the burst
        # (additive header field). The placeKYT SimServer routes a present
        # ``grc_params`` to ``on_grc_params`` → the out-of-sync indicator. Absent
        # ⇒ no callback (an older host ignores the field — backward compatible).
        collected = self.collected_params()
        if collected:
            header["grc_params"] = collected
        _note_endpoint(self.device_id, host, port)  # enable live param pushes
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
            self._seq += 1          # a NEW burst generation is available to drain
            self._cv.notify_all()
        return result

    def push_stream(self, words):
        """Streaming mode: the source appends a chunk of recovered words for the
        sink to drain. Non-blocking, unbounded FIFO (chunks are small)."""
        with self._cv:
            self._stream_q = np.concatenate(
                [self._stream_q, np.asarray(words, dtype=np.float32)])
            self._cv.notify_all()

    def take_stream(self, max_items):
        """Streaming mode: pop up to ``max_items`` recovered words the source has
        pushed. Returns a float32 array (possibly empty). Non-blocking — the sink
        polls each work() and emits whatever is ready."""
        with self._cv:
            if not len(self._stream_q):
                return np.array([], dtype=np.float32)
            n = min(int(max_items), len(self._stream_q))
            out = self._stream_q[:n]
            self._stream_q = self._stream_q[n:]
            return out

    def take_result(self, timeout=None):
        """Block until the source has dispatched a burst THIS run hasn't drained,
        then return the recovered words (once). Returns None on timeout.

        Gated on the dispatch GENERATION (``_seq``), not just ``done``: because the
        session is process-global and survives across Runs, a stale ``done=True``
        from the previous run must NOT satisfy this run's sink. The sink waits for
        ``_seq > _taken_seq`` — i.e. a dispatch NEWER than the last one it drained —
        so a repeated Run always blocks for its own fresh burst instead of
        re-taking the previous (already-consumed) result and plotting flat."""
        with self._cv:
            if self._seq <= self._taken_seq:
                self._cv.wait(timeout)
            if self._seq <= self._taken_seq:
                return None
            self._taken_seq = self._seq
            r = self._result
            self._result = np.array([], dtype=np.float32)
            return r
