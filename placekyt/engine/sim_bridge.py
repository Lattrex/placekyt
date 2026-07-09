"""Live GNURadio ↔ placeKYT chip bridge — server side (Qt-free).

placeKYT OWNS the running ``simkyt.Chip`` (with its live debug views); a
GNURadio flowgraph streams samples to/from it over a localhost TCP socket. This
module is the SERVER: it wraps a chip and serves the tiny port API the GNURadio
source/sink blocks call — ``write_port`` / ``output_available`` /
``run_until_output`` / ``read_port`` — over the wire.

The matching client (``ChipProxy``) lives in the GNURadio OOT module
(`gr-kyttar/python/kyttar/placekyt_sim_client.py`); the two processes run in
different Python envs and can't import each other, so the WIRE PROTOCOL below is
duplicated verbatim on both sides. Keep them in sync.

Wire protocol (one request → one reply, synchronous):
  Each message = 4-byte big-endian header length H, then H bytes of UTF-8 JSON,
  then (optional) a raw little-endian float32 payload whose element count is in
  the JSON ``n``. Request JSON: ``{"op": <str>, ...args, "n": <payload len>}``.
  Reply JSON: ``{"ok": bool, "error": <str?>, ...result, "n": <payload len>}``.

Ops: ``write_port`` (payload=samples), ``output_available``, ``run_until_output``,
``read_port`` (reply payload=samples), ``ping``.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time

import numpy as np

_HDR = struct.Struct(">I")  # 4-byte big-endian frame-header length


class BatchAborted(Exception):
    """Raised by a BatchDebugHooks.after_sample to abort an in-progress batch
    (the user pressed Stop while paused at a breakpoint). process_batch catches
    it and returns the samples produced so far with ``aborted: True``, rather
    than running the rest of the burst."""


def _q15_to_float(v: int) -> float:
    """uint16 Q15 → float in [-1, 1). Interprets bit 15 as the sign."""
    s = v - 0x10000 if (v & 0x8000) else v
    return s / 32768.0


def _float_to_q15(f: float) -> int:
    """float in [-1, 1) → uint16 Q15 (clipped). Inverse of _q15_to_float."""
    f = max(-1.0, min(0.999, float(f)))
    return int(round(f * 32768)) & 0xFFFF


def _float_to_raw_i16(f: float) -> int:
    """float → uint16 by rounding to the nearest INTEGER (clamped to int16),
    NOT Q15-scaled. Used to inject a `raw` REAL stream's integer-valued operands
    (a 0/1 TX bit) as their own value — bit 1 → 0x0001 — so the TX-INPUT trace
    reads on the same scale as the raw RX-OUTPUT bit trace. A NON-raw real stream
    (fractional analog samples, e.g. a FIR) and the complex I/Q path both keep
    Q15, where a sample is a fractional signal value."""
    v = int(round(float(f)))
    v = max(-32768, min(32767, v))
    return v & 0xFFFF


def _recv_exactly(conn: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise ConnectionError on early EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def recv_message(conn: socket.socket):
    """Receive one ``(header_dict, payload_float32_or_None)`` message."""
    hlen = _HDR.unpack(_recv_exactly(conn, 4))[0]
    header = json.loads(_recv_exactly(conn, hlen).decode("utf-8"))
    n = int(header.get("n", 0))
    payload = None
    if n:
        raw = _recv_exactly(conn, n * 4)
        payload = np.frombuffer(raw, dtype="<f4")
    return header, payload


def send_message(conn: socket.socket, header: dict,
                 payload: np.ndarray | None = None) -> None:
    """Send one ``(header, optional float32 payload)`` message."""
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


class SimServer:
    """Serves a chip's port API over a localhost TCP socket.

    ``chip`` is a ``simkyt.Chip`` (already programmed + ports configured).
    ``on_activity`` (optional) is called after each ``run_until_output`` so the
    host can refresh its debug views from the (now-advanced) chip + trace.
    Single client at a time (the flowgraph). Runs its accept/serve loop on a
    background thread; ``start`` returns the bound port immediately.
    """

    def __init__(self, chip, *, host: str = "127.0.0.1", port: int = 0,
                 on_activity=None, on_reset=None, on_before_batch=None,
                 default_entries=None, on_grc_params=None, debug_hooks=None,
                 default_hops=None, stream_targets=None,
                 batch_reset_writes=None, on_new_run=None):
        self._chip = chip
        # Optional: called at the start of a new GRC "Run" — detected by STREAM
        # CYCLING, not by socket connection. Evidence (WAVE2 log) showed each GRC
        # Run opens a SEPARATE connection PER STREAM (tx on one socket, rx on
        # another), so a per-connection signal fired twice per Run and its async
        # trace-reset raced the per-batch refreshes (wiping a finished Run). The
        # robust invariant: within ONE Run each stream_id appears exactly ONCE
        # (tx once, rx once); when a stream_id we've ALREADY seen this Run arrives
        # again, that batch begins a NEW Run. We fire on_new_run then, so the host
        # resets the trace exactly once per Run and the Run's streams accumulate.
        self._on_new_run = on_new_run
        self._run_seen_streams: set = set()   # stream_ids seen in the current Run
        self._host = host
        self._req_port = port
        self._on_activity = on_activity
        # Optional: a BatchDebugHooks (thread-safe, Qt-free) that makes the GUI
        # debug controls first-class DURING a GRC batch run. The process_batch
        # per-sample loop consults it after every sample: honor a breakpoint
        # (pause + report which sample), block while paused, single-step, and
        # apply the playback speed delay — so breakpoints / speed / step work
        # even though the burst runs server-side here rather than in the GUI's
        # local SimController loop. None ⇒ the loop runs flat out as before.
        self._debug_hooks = debug_hooks
        # Optional: called at the TOP of each process_batch, BEFORE the burst is
        # run. The host rebuilds the hosted chip from the CURRENT project if the
        # design was edited since the last build (placement/route/connection
        # change), re-points the server at it (set_chip), and returns
        # (rebuilt_chip_or_None, error_or_None). A non-None error (e.g. a DRC
        # failure on the edited design) ABORTS the batch with that error instead
        # of silently running a STALE chip. This is what makes a GRC Execute
        # always reflect the current placeKYT design — not the build that was
        # hosted when "Run as GNURadio Server" was first clicked.
        self._on_before_batch = on_before_batch
        # Per-input-port default JUMP entry address (from the build's resolved
        # interface, e.g. the Costas/receiver phase cell's entry=17). Used when a
        # client injects WITHOUT specifying jump_entry, so a block whose entry is
        # not 0 works over the bridge without the GRC having to know the entry.
        self._default_entries: dict[str, int] = dict(default_entries or {})
        # Per-input-port injection HOP (the raw 5-bit hop field = 31 - distance
        # from the port cell to the block's landing cell). This is PLACEMENT-
        # DEPENDENT (INV-1): a block 1 hop from the port needs 30, a block placed
        # deeper needs less. process_batch MUST use this, not a hardcoded 30 —
        # otherwise the WRITE/JUMP is consumed at the wrong cell and the block
        # never executes (empty output). Absent ⇒ fall back to 30 (the 1-hop
        # case, e.g. a block auto-placed on the port edge).
        self._default_hops: dict[str, int] = dict(default_hops or {})
        # Per-STREAM injection targets for the shared-input-port duplex case
        # (engine.port_config.stream_targets): {stream_id -> {entry_addr,
        # hop_count, data_addrs, in_port, out_tag}}. When a process_batch header
        # names a ``stream_id`` present here, the burst is injected at THAT
        # stream's block entry/hop/data-registers and its output is tagged with
        # out_tag (so the matching sink demuxes its own words). Absent / no
        # stream_id ⇒ the single-stream default_entries/default_hops path. This is
        # how two GR sources sharing x16_in (TX mapper + RX matched filter) each
        # reach the right block without the GR source knowing any placement value.
        self._stream_targets: dict[str, dict] = dict(stream_targets or {})
        # Per-batch (packet-boundary) state resets, resolved by the build from the
        # placed design's ``reset_per_batch`` StateVars (engine.build.
        # _resolve_batch_reset_writes → ChipBuild.batch_reset_writes → port_config.
        # batch_reset_writes). A list of ``(x, y, addr, value)``: the cell grid
        # position, register address, and cold-start value. Applied at the START of
        # every process_batch (each RPC = one explicit packet boundary — NOT per
        # sample, which would break the loop within a packet) so a persistently-hosted
        # receiver's loop MEMORY (Costas phase/freq, Gardner timing accumulators, the
        # matched-filter delay lines) cold-starts for each fresh packet, and repeated
        # GRC "Run" presses each recover the new packet from scratch instead of
        # inheriting the previous packet's converged lock.
        self._batch_reset_writes: list = list(batch_reset_writes or [])
        # Optional: called when a client requests a chip reset (new flowgraph
        # run). The host rebuilds a fresh chip and calls set_chip(); on_reset
        # returns the new chip (or None to keep the current one).
        self._on_reset = on_reset
        # Optional: called when a GRC client advertises its flowgraph's block
        # params (the ``set_grc_params`` op, or a ``grc_params`` field on a
        # process_batch header). Receives ``{placeKYT block name: params}``; the
        # host re-diffs against the placed design and flips the out-of-sync
        # indicator. Qt-free contract (the host marshals to the GUI thread).
        self._on_grc_params = on_grc_params
        self._sock: socket.socket | None = None
        # The socket of the client whose request is currently being served. The
        # per-sample batch loop polls it (non-blocking, every 32 samples) so a
        # mid-batch client disconnect (GRC Stop / flowgraph close) ABORTS the
        # burst promptly instead of running to completion — the sim should stop
        # when GRC stops.
        self._active_conn: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.bound_port: int | None = None
        # Per-tag output buffer for shared-port demux: read_port_tagged(tag=X)
        # drains the chip ONCE into these buckets and returns only tag X, leaving
        # the other tags' words buffered for their own reader (so two streams can
        # share one output port without one stealing the other's words).
        self._tag_buf: dict[int, list[int]] = {}

    def _clear_chip_trace(self) -> None:
        """Drop the hosted chip's trace buffer (keep tracing enabled). Called at a
        Run boundary on the server thread so the new Run's trace starts empty — see
        the callers in process_batch / _process_batch_duplex. Best-effort: a chip
        without tracing enabled simply no-ops."""
        try:
            self._chip.clear_trace()
        except Exception:  # noqa: BLE001 — tracing not enabled / unsupported
            pass

    def _apply_batch_reset(self) -> None:
        """Cold-start every flagged loop-state register on the hosted chip.

        SIMULATION-ONLY backdoor poke: writes each resolved ``(x, y, addr, value)``
        directly into cell memory via ``chip.write_cell_memory(cell_id, addr, value)``
        (the same simkyt cell-memory API the debug/inspector path reads via
        ``read_cell_memory``). This is the packet-boundary reset — applied once per
        process_batch, NOT per sample.

        REAL-CHIP PATH (DEFERRED): on silicon there is no memory backdoor. The SAME
        declarative reset spec (the placed StateVars' ``reset_per_batch`` flags,
        resolved to (cell, addr, value)) would instead drive a single-register WRITE
        sequence down the bus to each target cell (or a full reset + reprogram) at a
        packet boundary. The spec is the shared source of truth; only the delivery
        mechanism differs. That path is not built here (this bridge hosts the sim).
        """
        if not self._batch_reset_writes:
            return
        chip = self._chip
        cell_id_at = getattr(chip, "cell_id_at", None)
        write = getattr(chip, "write_cell_memory", None)
        if cell_id_at is None or write is None:
            return  # host chip lacks the backdoor API — nothing to do
        for (x, y, addr, value) in self._batch_reset_writes:
            write(cell_id_at(int(x), int(y)), int(addr), int(value) & 0xFFFF)

    def start(self) -> int:
        """Bind + listen, spawn the serve thread, return the bound port."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._req_port))
        self._sock.listen(1)
        self.bound_port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.bound_port

    def set_chip(self, chip) -> None:
        """Re-point the server at a fresh chip (e.g. after the host reset the
        simulation). The next client request uses the new chip. Existing client
        connections keep working — they just talk to the new chip."""
        self._chip = chip

    def stop(self) -> None:
        """Fully tear down so the SAME port can be re-bound on a restart.

        The serve thread is blocked in ``accept()``; closing the socket alone does
        not reliably wake it on Linux, leaving the listening port held ("Address
        already in use" on restart). So we ``shutdown(SHUT_RDWR)`` to break the
        accept, close the socket, AND join the serve thread before returning — the
        listening socket is gone once the thread exits."""
        self._running = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.bound_port = None

    def _serve(self) -> None:
        sock = self._sock          # capture: stop() nulls self._sock + shuts it down
        while self._running and sock is not None:
            try:
                conn, _addr = sock.accept()
            except OSError:
                break  # socket closed/shutdown by stop()
            try:
                self._handle_client(conn)
            except (ConnectionError, OSError):
                pass  # client went away — wait for the next one
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_client(self, conn: socket.socket) -> None:
        self._active_conn = conn
        try:
            while self._running:
                try:
                    header, payload = recv_message(conn)
                except (ConnectionError, OSError):
                    return
                reply, out_payload = self._dispatch(header, payload)
                try:
                    send_message(conn, reply, out_payload)
                except (ConnectionError, OSError):
                    # The client vanished mid-reply (e.g. an aborted batch's
                    # reply to a closed GRC socket) — drop it, serve the next.
                    return
        finally:
            self._active_conn = None

    def _client_alive(self) -> bool:
        """Non-blocking check that the serving client socket is still open. A
        closed connection reads EOF (empty peek) → the client (GRC) went away,
        so the batch should abort. Any transient error is treated as ALIVE
        (never abort a healthy burst on a spurious check). Called periodically
        by the per-sample batch loop — bounded, cheap (one MSG_PEEK recv)."""
        conn = self._active_conn
        if conn is None:
            return True
        try:
            conn.setblocking(False)
            try:
                data = conn.recv(1, socket.MSG_PEEK)
            finally:
                conn.setblocking(True)
            # EOF (b"") on a stream socket = the peer closed the connection.
            return data != b""
        except BlockingIOError:
            return True          # no data pending, but the socket is open
        except OSError:
            return True          # transient — do not abort a healthy burst

    def _dispatch(self, header: dict, payload):
        op = header.get("op")
        try:
            if op == "ping":
                return {"ok": True}, None
            if op == "set_grc_params":
                # A GRC client advertises its flowgraph's per-block params so the
                # host can detect a parameter drift from the placed design (the
                # GRC↔placeKYT sync indicator). Header carries
                # ``params`` = {placeKYT block name: {param: value}}. Forwarded to
                # the host; never touches the chip, so it's safe any time.
                if self._on_grc_params is not None:
                    self._on_grc_params(dict(header.get("params", {}) or {}))
                return {"ok": True}, None
            if op == "reset":
                # A new flowgraph run — rehost a fresh chip if the host supports
                # it (so the second run starts from clean state).
                self._tag_buf.clear()
                if self._on_reset is not None:
                    new_chip = self._on_reset()
                    if new_chip is not None:
                        self._chip = new_chip
                return {"ok": True}, None
            port = header.get("port")
            if op == "write_port":
                data = np.asarray(payload, dtype="<f4")
                # Optional per-stream JUMP entry tag (§ shared-port duplex): when
                # given, every sample is injected with that JUMP entry so a stream
                # routes to a specific landing-cell entry (e.g. a splitter's RX vs
                # TX arm). Absent ⇒ the port's configured entry (back-compat).
                jump_entry = header.get("jump_entry")
                if jump_entry is not None:
                    addrs = np.full(len(data), int(jump_entry) & 0xFF, dtype=np.uint8)
                    self._chip.write_port_tagged(port, data, addrs)
                else:
                    self._chip.write_port(port, data)
                return {"ok": True}, None
            if op == "write_port_complex":
                # COMPLEX input: the payload is interleaved [xi0,xq0,xi1,xq1,...]
                # floats. Each (xi,xq) pair is injected as ONE multi-word
                # transaction — WRITE xi→data_addrs[0], WRITE xq→data_addrs[1],
                # then a single JUMP to jump_entry — so a complex baseband stream
                # drives a 2-input landing cell (e.g. the Costas phase cell: xi@R0,
                # xq@R1). This is the I/Q analogue of write_port; the per-word dest
                # + hop tagging is exactly the tagged-injection mechanism (#207).
                data = np.asarray(payload, dtype="<f4")
                # data_addrs may carry ONE address (a real/float stream feeding a
                # single-word landing) or TWO (an I/Q packet into a 2-input cell).
                # Never unpack blindly — a 1-element list would ValueError.
                _das = list(header.get("data_addrs", [0, 1]))
                a0 = _das[0] if _das else 0
                a1 = _das[1] if len(_das) > 1 else a0 + 1
                # Use the client's jump_entry if given; else fall back to this
                # port's build-configured entry (so a block with entry != 0 works
                # without the GRC having to know it).
                raw_entry = header.get("jump_entry", None)
                if raw_entry is None or int(raw_entry) <= 0:
                    entry = int(self._default_entries.get(port, 0)) & 0xFF
                else:
                    entry = int(raw_entry) & 0xFF
                samples = []
                for k in range(0, len(data) - 1, 2):
                    samples.append([(int(a0), _float_to_q15(float(data[k]))),
                                    (int(a1), _float_to_q15(float(data[k + 1])))])
                if samples:
                    self._chip.write_port_multi_i16(port, samples, entry)
                return {"ok": True}, None
            if op == "process_batch":
                # BATCH (run-to-completion) processing — the right model for a
                # multi-cell DUT (BPSK receiver and up) whose per-sample event
                # count makes real-time per-sample socket streaming crawl. The
                # WHOLE interleaved-I/Q burst is processed here on the server in
                # one RPC: no per-sample socket round-trip, no per-sample GUI
                # refresh. Each complex sample is still injected + run
                # sequentially (the loop's NCO feedback is sequential), but the
                # overhead is paid ONCE for the burst, not N times.
                #
                # header: data_addrs=[a0,a1], jump_entry (opt), max_events_per
                #   (opt, per-sample event cap). payload: [xi0,xq0,xi1,xq1,...].
                # reply payload: the full recovered output stream (float32).
                #
                # NEW-RUN DETECTION by STREAM CYCLING (see __init__): a Run sends
                # each stream_id exactly once (tx once, rx once). A stream_id we've
                # already seen this Run means a NEW Run has started with this
                # batch → fire on_new_run (host resets the trace ONCE per Run) and
                # start a fresh seen-set. Fires BEFORE the batch runs, so the
                # reset is consumed before this batch's events are drained; the
                # Run's remaining streams then accumulate onto the clean trace.
                _sid = header.get("stream_id")
                _run_key = _sid if _sid is not None else "__single__"
                if _run_key in self._run_seen_streams:
                    self._run_seen_streams = {_run_key}
                    if self._on_new_run is not None:
                        try:
                            self._on_new_run()
                        except Exception:  # noqa: BLE001 — never break the run
                            pass
                    # Clear the chip trace on the server thread at the Run boundary
                    # so this Run's drain sees ONLY this Run's events (see the note
                    # in _process_batch_duplex — prevents the "blank on rerun" bug
                    # where a stale previous-Run event anchors the time rebase).
                    self._clear_chip_trace()
                else:
                    self._run_seen_streams.add(_run_key)
                # FRESH-BUILD GUARD: rebuild the hosted chip from the CURRENT
                # project if it was edited since the last build, so this batch
                # runs the design as it stands NOW (not the stale build hosted at
                # server-start). A DRC failure on the edited design returns an
                # error rather than running a stale chip.
                if self._on_before_batch is not None:
                    new_chip, err = self._on_before_batch()
                    if err is not None:
                        return {"ok": False, "error": str(err)}, None
                    if new_chip is not None:
                        self._chip = new_chip
                # ADDITIVE GRC-sync detection (backward compatible): an optional
                # ``grc_params`` header field lets a client advertise its
                # flowgraph's per-block params alongside a batch, so the host can
                # flag a parameter drift from the placed design. Detected HERE at
                # the top of the batch (not in the per-sample loop) — absent ⇒
                # unchanged behaviour.
                grc_params = header.get("grc_params")
                if grc_params and self._on_grc_params is not None:
                    self._on_grc_params(dict(grc_params))
                # PACKET-BOUNDARY LOOP-MEMORY RESET (each process_batch = one fresh
                # packet). Cold-start every flagged loop-state register BEFORE injecting
                # the burst, so a persistently-hosted receiver doesn't carry the previous
                # packet's converged Costas/Gardner/matched-filter lock into the new
                # packet (which corrupts its first samples). Done ONCE per RPC — never
                # per sample (that would break the loop mid-packet).
                self._apply_batch_reset()
                # Re-arm the debug hooks for THIS batch: a previous abort (Stop /
                # client disconnect) left the one-shot stop latch set; the hooks
                # persist for the whole server session, so without clearing it
                # every later Run would abort at its first sample.
                if self._debug_hooks is not None:
                    self._debug_hooks.clear_stop()
                data = np.asarray(payload, dtype="<f4")
                # Robust to a real (1-addr) or I/Q (2-addr) stream — see above.
                _das = list(header.get("data_addrs", [0, 1]))
                a0 = _das[0] if _das else 0
                a1 = _das[1] if len(_das) > 1 else a0 + 1
                in_name = header.get("in_port", "x16_in")
                raw_entry = header.get("jump_entry", None)
                if raw_entry is None or int(raw_entry) <= 0:
                    # fall back to the INPUT port's build-configured entry.
                    entry = int(self._default_entries.get(in_name, 0)) & 0xFF
                else:
                    entry = int(raw_entry) & 0xFF
                # SHARED-INPUT-PORT DUPLEX (§ stream_id): when the client names a
                # ``stream_id`` the server knows (engine.port_config.stream_targets
                # resolved at server start), the SERVER is the source of truth for
                # this stream's placement — OVERRIDE the header's entry/hop/data
                # addrs with the resolved values so two GR sources sharing x16_in
                # each inject at their own block's landing cell without the source
                # knowing any placement-dependent value. ``out_tag`` then demuxes
                # this stream's recovered words off the shared output port.
                stream_id = header.get("stream_id")
                out_tag = None
                out_complex = False
                if stream_id and stream_id in self._stream_targets:
                    tgt = self._stream_targets[stream_id]
                    entry = int(tgt["entry_addr"]) & 0xFF
                    hop_override = int(tgt["hop_count"]) & 0x1F
                    das = list(tgt.get("data_addrs") or [])
                    if das:
                        a0 = das[0]
                        a1 = das[1] if len(das) > 1 else a0 + 1
                    in_name = tgt.get("in_port", in_name)
                    out_tag = tgt.get("out_tag")
                    # COMPLEX EGRESS: the chain ends in a complex-output cell whose I
                    # and Q rails exit on tags (out_tag, out_tag+1). Collect BOTH and
                    # interleave [I0,Q0,I1,Q1,…] so the GR complex sink reassembles the
                    # I/Q stream (the output-side mirror of the complex INPUT path).
                    out_complex = bool(tgt.get("complex_out"))
                else:
                    hop_override = None
                # PLACEMENT-DEPENDENT injection hop (INV-1): use the input port's
                # build-configured hop (31 - distance to the block's landing
                # cell), NOT a hardcoded 30. A header `jump_hop` overrides; else
                # the per-port default; else 30 (the 1-hop, on-the-edge case).
                # Hardcoding 30 made any block NOT 1 hop from the port silently
                # produce NO output — the WRITE/JUMP lands at the wrong cell so
                # the block never fires.
                raw_hop = header.get("jump_hop", None)
                if hop_override is not None:
                    # The resolved stream's hop (server source of truth) wins.
                    hop = hop_override
                elif raw_hop is None:
                    hop = int(self._default_hops.get(in_name, 30)) & 0x1F
                else:
                    hop = int(raw_hop) & 0x1F
                mx = int(header.get("max_events_per", 40000))
                # `raw`: return the raw int16 output WORDS (as float32, exact for
                # the small integers a packer/slicer emits) instead of Q15-scaled
                # floats. A bit-packing receiver (CoherentRXBlock) emits the
                # decoded bit in the word's LSB, which Q15 scaling (word/32768)
                # would crush to ~0 — so those blocks must read raw. A recovered-I
                # receiver (CoherentBPSKRxBlock) emits a Q15 value and wants the
                # default Q15 float. Default False keeps the existing behavior.
                raw = bool(header.get("raw", False))
                # `complex` (default True for back-compat): the payload is
                # interleaved I/Q [xi0,xq0,...], two operands per sample — inject
                # xi→a0 AND xq→a1. When False, the payload is a REAL burst
                # [x0,x1,...], ONE operand per sample — inject ONLY xi→a0. A
                # single-input float block (e.g. a gain) keeps state (its
                # coefficient) in a1; injecting a phantom xq=0 there would clobber
                # it and zero the output, so a real burst must NOT touch a1.
                is_complex = bool(header.get("complex", True))
                out_vals: list[float] = []
                # Pull any words for THIS stream's tag that a prior (other-stream)
                # process_batch parked while draining its own tag, so they aren't
                # lost across interleaved RPCs.
                if out_tag is not None:
                    parked = self._tag_buf.pop(int(out_tag), [])
                    for v in parked:
                        out_vals.append(float(int(v) & 0xFFFF) if raw
                                        else _q15_to_float(int(v)))
                nsamp = (len(data) // 2) if is_complex else len(data)
                _t_batch0 = time.perf_counter()
                if os.environ.get("KYTTAR_PERF_DEBUG") == "1":
                    import sys as _sysP
                    _sysP.stderr.write(f"[PERF] process_batch START nsamp={nsamp} "
                                       f"complex={is_complex}\n")
                    _sysP.stderr.flush()
                aborted = False
                nrun = nsamp
                # Drive each sample the PROVEN way: inject xi→a0, run; (complex:
                # xq→a1, run;) JUMP entry, run; then drain the output port. (The
                # write_port_multi_i16 path stalls the loop after one sample; the
                # raw inject path advances every sample and is what the on-chip lock
                # tests use.) `hop` is placement-dependent (31 - distance to the
                # landing cell), resolved above — NOT a hardcoded 30. Wrapped so a
                # debug-hook STOP (BatchAborted) returns the samples produced so
                # far instead of the whole burst.
                try:
                    for k in range(nsamp):
                        if is_complex:
                            xi = _float_to_q15(float(data[2 * k]))
                            xq = _float_to_q15(float(data[2 * k + 1]))
                        else:
                            # REAL burst, ONE operand per sample. `raw` selects the
                            # INPUT encoding, mirroring how it selects the OUTPUT
                            # encoding: a `raw` stream carries INTEGER words (a 0/1
                            # TX bit), so it injects UNSCALED — bit 1 → 0x0001 — and
                            # the TX-INPUT trace reads on the same scale as the
                            # RX-OUTPUT bit trace (both raw 0x0001, not Q15 0x7FFF).
                            # The PSK mapper masks its input to the LSB, so raw vs
                            # Q15 give the SAME bit — BER is unaffected. A NON-raw
                            # real stream (e.g. a FIR fed fractional analog samples)
                            # still injects Q15 — 0.95 must stay a Q15 fraction, not
                            # round to the integer 1 (~0 in Q15).
                            xi = (_float_to_raw_i16(float(data[k])) if raw
                                  else _float_to_q15(float(data[k])))
                            xq = None
                        self._chip.inject_data_physical([xi], target_hop_cnt=hop,
                                                        target_addr=int(a0))
                        self._chip.run(max_events=3000)
                        if xq is not None:
                            self._chip.inject_data_physical([xq], target_hop_cnt=hop,
                                                            target_addr=int(a1))
                            self._chip.run(max_events=3000)
                        self._chip.inject_jump_physical(target_hop_cnt=hop,
                                                        entry_addr=entry)
                        self._chip.run(max_events=mx)
                        if out_tag is not None and out_complex:
                            # COMPLEX EGRESS: the I rail is tagged out_tag, the Q rail
                            # out_tag+1. Collect this sample's I and Q (in (v,d) order)
                            # and emit them INTERLEAVED [I,Q] so the GR complex sink
                            # reassembles the I/Q stream. Any foreign tag is parked.
                            i_tag, q_tag = int(out_tag), int(out_tag) + 1
                            si = sq = None
                            for (v, d, _t) in self._chip.read_port_words_timed(port):
                                if int(d) == i_tag:
                                    si = float(int(v) & 0xFFFF) if raw else _q15_to_float(int(v))
                                elif int(d) == q_tag:
                                    sq = float(int(v) & 0xFFFF) if raw else _q15_to_float(int(v))
                                else:
                                    self._tag_buf.setdefault(int(d), []).append(int(v))
                            if si is not None:
                                out_vals.append(si)
                                out_vals.append(sq if sq is not None else 0.0)
                        elif out_tag is not None:
                            # SHARED-OUTPUT-PORT DEMUX: drain the chip's tagged
                            # output WORDS (value, dest, t) and keep only those
                            # whose dest == this stream's out_tag; OTHER tags are
                            # buffered in self._tag_buf so the other stream's
                            # process_batch (its own RPC) can still claim them.
                            # Mirrors bpsk_modem_demo._drain_tagged's tag filter.
                            for (v, d, _t) in self._chip.read_port_words_timed(port):
                                if int(d) == int(out_tag):
                                    if raw:
                                        out_vals.append(float(int(v) & 0xFFFF))
                                    else:
                                        out_vals.append(_q15_to_float(int(v)))
                                else:
                                    self._tag_buf.setdefault(int(d), []).append(
                                        int(v))
                        elif raw:
                            got = self._chip.read_port_i16(port)
                            if got is not None and len(got):
                                out_vals.extend(float(int(v)) for v in got)
                        else:
                            got = self._chip.read_port(port)
                            if got is not None and len(got):
                                out_vals.extend(float(v) for v in got)
                        # Make the GUI debug controls first-class for this batch
                        # run: after each sample, let the hooks pause on a
                        # breakpoint, block while paused, single-step, and pace by
                        # the speed setting. No hooks ⇒ no-op (flat-out, original
                        # behavior). Raises BatchAborted if the user stops.
                        if self._debug_hooks is not None:
                            self._debug_hooks.after_sample(self._chip, k, port)
                        # GRC STOP / client disconnect: poll the serving socket
                        # every 32 samples (bounded, cheap) and abort the burst
                        # if the client went away — placeKYT must stop when the
                        # GRC flowgraph stops, not run the whole burst.
                        if (k & 31) == 31 and not self._client_alive():
                            raise BatchAborted()
                except BatchAborted:
                    aborted = True
                    nrun = k + 1   # samples actually driven before the stop
                # Throughput metric: how fast simKYT processes I/Q samples on THIS
                # machine. simkyt is an event-accurate async-ASIC sim, not a
                # real-time DSP source — this tells the user roughly how long a given
                # burst length will take (e.g. 1 s of 48 kHz audio ≈ nsamp/sps_rate
                # seconds of wall time). Reported in the reply header and to the GUI.
                _dt = max(1e-9, time.perf_counter() - _t_batch0)
                sps = nrun / _dt
                # OBSERVABILITY: one concise line per batch to the server console
                # (the GUI's terminal). Turns "x16_out is flat, why?" into a precise
                # readout — which stream, the resolved inject landing (entry/hop/
                # data_addrs/out_tag), samples in, words out, and the distinct output
                # tags actually seen on the port. A produced-zero batch shows the
                # resolved landing so a stale/wrong stream_target is obvious at a
                # glance. Gated off only by an env var for a totally quiet run.
                if os.environ.get("KYTTAR_SERVER_QUIET") != "1":
                    import sys as _sys
                    seen_tags = sorted(self._tag_buf.keys())
                    _sys.stderr.write(
                        f"[placeKYT batch] stream={stream_id!r} in={in_name} "
                        f"entry={entry} hop={hop} addrs=[{a0},{a1}] "
                        f"out_tag={out_tag} | {nsamp} samples -> {len(out_vals)} "
                        f"words (other-tag buf: {seen_tags})\n")
                    _sys.stderr.flush()
                if os.environ.get("KYTTAR_PERF_DEBUG") == "1":
                    import sys as _sysP
                    _sysP.stderr.write(
                        f"[PERF] process_batch END nrun={nrun} "
                        f"wall={round((time.perf_counter()-_t_batch0)*1000)}ms\n")
                    _sysP.stderr.flush()
                if self._on_activity is not None:
                    # Pass the metric if the callback accepts it; else ping plainly.
                    try:
                        self._on_activity(samples=nrun, seconds=_dt,
                                          samples_per_sec=sps)
                    except TypeError:
                        self._on_activity()
                return ({"ok": True, "samples": nrun, "seconds": _dt,
                         "samples_per_sec": sps, "aborted": aborted,
                         "out_tag": out_tag, "stream_id": stream_id},
                        np.asarray(out_vals, dtype="<f4"))
            if op == "process_batch_duplex":
                return self._process_batch_duplex(header, payload)
            if op == "output_available":
                return {"ok": True, "available":
                        int(self._chip.output_available(port))}, None
            if op == "run_until_output":
                count = int(header.get("count", 0))
                max_events = int(header.get("max_events", count * 500 or 1))
                self._chip.run_until_output(port, count, max_events)
                if self._on_activity is not None:
                    self._on_activity()
                return {"ok": True}, None
            if op == "read_port":
                samples = np.asarray(self._chip.read_port(port), dtype="<f4")
                return {"ok": True}, samples
            if op == "read_port_tagged":
                # Drain output WRITE words with their dest TAGS (§ shared-port
                # duplex demux), bucketing by tag so a filtered read does NOT
                # discard another tag's words (two streams share one output port).
                want = header.get("tag")
                for (v, d, _t) in self._chip.read_port_words_timed(port):
                    self._tag_buf.setdefault(int(d), []).append(int(v))
                if want is None:
                    dests, vals = [], []
                    for d in sorted(self._tag_buf):
                        for v in self._tag_buf[d]:
                            dests.append(d); vals.append(_q15_to_float(v))
                    self._tag_buf.clear()
                else:
                    bucket = self._tag_buf.pop(int(want), [])
                    dests = [int(want)] * len(bucket)
                    vals = [_q15_to_float(v) for v in bucket]
                return ({"ok": True, "dests": dests},
                        np.asarray(vals, dtype="<f4"))
            return {"ok": False, "error": f"unknown op {op!r}"}, None
        except Exception as exc:  # noqa: BLE001 — surface to the client
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, None

    def _process_batch_duplex(self, header: dict, payload):
        """TRUE FULL-DUPLEX batch: run TWO+ streams CONCURRENTLY on the shared
        input port, INTERLEAVED sample-by-sample, so both chains advance on the
        array at the same sim-time and the shared output port emits both streams'
        words interleaved (demuxed by out_tag). This is what a full-duplex modem
        actually is — NOT the previous behaviour where each stream's whole burst
        ran to completion before the next started (tx-then-rx, 1.7M ns apart).

        header:
          streams: [{stream_id, complex(bool), raw(bool), n_samples}]  — in order.
        payload: every stream's float32 samples concatenated in stream order; a
          complex stream contributes 2*n_samples floats (xi,xq interleaved), a
          real stream n_samples floats.

        reply payload: every stream's recovered words concatenated in stream order
          (float32); reply header ``lengths`` gives each stream's word count so the
          client can split them. Each stream's out_tag demuxes its own words.
        """
        streams_hdr = list(header.get("streams") or [])
        if not streams_hdr:
            return {"ok": False, "error": "process_batch_duplex: no streams"}, None
        # A duplex batch IS one whole Run (all streams in one RPC) → signal a new
        # Run once so the host resets the waveform trace for it.
        if self._on_new_run is not None:
            try:
                self._on_new_run()
            except Exception:  # noqa: BLE001
                pass
        # DROP the chip's trace buffer at the Run boundary, on the SERVER thread,
        # BEFORE this Run injects anything. The host's per-refresh clear_trace runs
        # on the GUI thread and can lag behind a fast Stop→Run, leaving the previous
        # Run's events in the chip trace when this Run starts. The host then drains
        # Run N-1 + Run N together and anchors the per-Run time rebase on Run N-1's
        # start, pushing Run N's events ~1e6 ns off the visible window → EVERY trace
        # renders blank on the rerun (the reported "empty on every subsequent run").
        # Clearing here — race-free, the server owns the chip and no batch has run —
        # guarantees this Run's drain contains ONLY this Run's events.
        self._clear_chip_trace()
        # Fresh-build guard + loop-memory reset, ONCE for the whole duplex run.
        if self._on_before_batch is not None:
            new_chip, err = self._on_before_batch()
            if err is not None:
                return {"ok": False, "error": str(err)}, None
            if new_chip is not None:
                self._chip = new_chip
        grc_params = header.get("grc_params")
        if grc_params and self._on_grc_params is not None:
            self._on_grc_params(dict(grc_params))
        self._apply_batch_reset()
        # Re-arm the one-shot stop latch for this Run (see process_batch).
        if self._debug_hooks is not None:
            self._debug_hooks.clear_stop()

        data = np.asarray(payload, dtype="<f4") if payload is not None else np.array([])
        # Resolve each stream's injection landing + slice its samples out of the
        # concatenated payload.
        streams = []
        off = 0
        for sh in streams_hdr:
            sid = sh.get("stream_id")
            is_complex = bool(sh.get("complex", True))
            raw = bool(sh.get("raw", False))
            n = int(sh.get("n_samples", 0))
            width = 2 * n if is_complex else n
            seg = data[off:off + width]
            off += width
            # Server is the source of truth for a KNOWN stream's placement.
            entry = int(self._default_entries.get("x16_in", 0)) & 0xFF
            hop = int(self._default_hops.get("x16_in", 30)) & 0x1F
            a0, a1, out_tag, in_name = 0, 1, None, "x16_in"
            if sid and sid in self._stream_targets:
                tgt = self._stream_targets[sid]
                entry = int(tgt["entry_addr"]) & 0xFF
                hop = int(tgt["hop_count"]) & 0x1F
                das = list(tgt.get("data_addrs") or [])
                if das:
                    a0 = das[0]
                    a1 = das[1] if len(das) > 1 else a0 + 1
                in_name = tgt.get("in_port", in_name)
                out_tag = tgt.get("out_tag")
            streams.append({
                "sid": sid, "complex": is_complex, "raw": raw, "n": n,
                "seg": seg, "entry": entry, "hop": hop, "a0": a0, "a1": a1,
                "out_tag": out_tag, "out": [], "port": header.get("port", "x16_out"),
            })

        mx = int(header.get("max_events_per", 40000))
        n_max = max((s["n"] for s in streams), default=0)
        _t0 = time.perf_counter()
        aborted = False
        # ROUND-ROBIN by sample index: at each step k, drive stream 0's sample k,
        # then stream 1's sample k, … so all chains advance together. A stream that
        # has run out of samples is skipped. Output is drained after EACH stream's
        # step and demuxed by out_tag into that stream's bucket (other tags parked
        # in _tag_buf and swept up whenever their owning stream drains). Wrapped so
        # a debug-hook STOP or a client disconnect (BatchAborted) returns the
        # samples produced so far instead of running the whole burst.
        try:
            for k in range(n_max):
                for s in streams:
                    if k >= s["n"]:
                        continue
                    seg = s["seg"]
                    hop, a0, a1, entry = s["hop"], s["a0"], s["a1"], s["entry"]
                    if s["complex"]:
                        xi = _float_to_q15(float(seg[2 * k]))
                        xq = _float_to_q15(float(seg[2 * k + 1]))
                    else:
                        xi = (_float_to_raw_i16(float(seg[k])) if s["raw"]
                              else _float_to_q15(float(seg[k])))
                        xq = None
                    self._chip.inject_data_physical([xi], target_hop_cnt=hop,
                                                    target_addr=int(a0))
                    self._chip.run(max_events=3000)
                    if xq is not None:
                        self._chip.inject_data_physical([xq], target_hop_cnt=hop,
                                                        target_addr=int(a1))
                        self._chip.run(max_events=3000)
                    self._chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
                    self._chip.run(max_events=mx)
                    # Drain + demux by tag into EACH stream's bucket.
                    for (v, d, _t) in self._chip.read_port_words_timed(s["port"]):
                        dst = None
                        for s2 in streams:
                            if s2["out_tag"] is not None and int(d) == int(s2["out_tag"]):
                                dst = s2
                                break
                        if dst is not None:
                            dst["out"].append(float(int(v) & 0xFFFF) if dst["raw"]
                                              else _q15_to_float(int(v)))
                        else:
                            self._tag_buf.setdefault(int(d), []).append(int(v))
                    # Same first-class debug controls as process_batch: pause /
                    # step / speed / stop after each stream's sample (and the
                    # lockstep frame gate when animation is on).
                    if self._debug_hooks is not None:
                        self._debug_hooks.after_sample(self._chip, k, s["port"])
                # GRC STOP / client disconnect: abort the interleaved burst
                # promptly (see process_batch — same bounded poll cadence).
                if (k & 31) == 31 and not self._client_alive():
                    raise BatchAborted()
        except BatchAborted:
            aborted = True
        # Sweep any parked words that belong to a stream (late/ordering).
        for s in streams:
            if s["out_tag"] is not None:
                for v in self._tag_buf.pop(int(s["out_tag"]), []):
                    s["out"].append(float(int(v) & 0xFFFF) if s["raw"]
                                    else _q15_to_float(int(v)))

        _dt = max(1e-9, time.perf_counter() - _t0)
        out_all = []
        lengths = []
        for s in streams:
            out_all.extend(s["out"])
            lengths.append(len(s["out"]))
        if os.environ.get("KYTTAR_SERVER_QUIET") != "1":
            import sys as _sys
            summary = ", ".join(
                f"{s['sid']}:{s['n']}in->{len(s['out'])}out(tag{s['out_tag']})"
                for s in streams)
            _sys.stderr.write(f"[placeKYT duplex] INTERLEAVED {summary}\n")
            _sys.stderr.flush()
        if self._on_activity is not None:
            try:
                self._on_activity(samples=n_max, seconds=_dt,
                                  samples_per_sec=n_max / _dt)
            except TypeError:
                self._on_activity()
        return ({"ok": True, "samples": n_max, "seconds": _dt,
                 "lengths": lengths, "aborted": aborted,
                 "stream_ids": [s["sid"] for s in streams]},
                np.asarray(out_all, dtype="<f4"))
