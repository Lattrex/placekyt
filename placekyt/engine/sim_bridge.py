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
import socket
import struct
import threading
import time

import numpy as np

_HDR = struct.Struct(">I")  # 4-byte big-endian frame-header length


def _q15_to_float(v: int) -> float:
    """uint16 Q15 → float in [-1, 1). Interprets bit 15 as the sign."""
    s = v - 0x10000 if (v & 0x8000) else v
    return s / 32768.0


def _float_to_q15(f: float) -> int:
    """float in [-1, 1) → uint16 Q15 (clipped). Inverse of _q15_to_float."""
    f = max(-1.0, min(0.999, float(f)))
    return int(round(f * 32768)) & 0xFFFF


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
                 default_entries=None):
        self._chip = chip
        self._host = host
        self._req_port = port
        self._on_activity = on_activity
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
        # Optional: called when a client requests a chip reset (new flowgraph
        # run). The host rebuilds a fresh chip and calls set_chip(); on_reset
        # returns the new chip (or None to keep the current one).
        self._on_reset = on_reset
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.bound_port: int | None = None
        # Per-tag output buffer for shared-port demux: read_port_tagged(tag=X)
        # drains the chip ONCE into these buckets and returns only tag X, leaving
        # the other tags' words buffered for their own reader (so two streams can
        # share one output port without one stealing the other's words).
        self._tag_buf: dict[int, list[int]] = {}

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
        while self._running:
            try:
                header, payload = recv_message(conn)
            except (ConnectionError, OSError):
                return
            reply, out_payload = self._dispatch(header, payload)
            send_message(conn, reply, out_payload)

    def _dispatch(self, header: dict, payload):
        op = header.get("op")
        try:
            if op == "ping":
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
                a0, a1 = header.get("data_addrs", [0, 1])
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
                data = np.asarray(payload, dtype="<f4")
                a0, a1 = header.get("data_addrs", [0, 1])
                in_name = header.get("in_port", "x16_in")
                raw_entry = header.get("jump_entry", None)
                if raw_entry is None or int(raw_entry) <= 0:
                    # fall back to the INPUT port's build-configured entry.
                    entry = int(self._default_entries.get(in_name, 0)) & 0xFF
                else:
                    entry = int(raw_entry) & 0xFF
                mx = int(header.get("max_events_per", 40000))
                # `raw`: return the raw int16 output WORDS (as float32, exact for
                # the small integers a packer/slicer emits) instead of Q15-scaled
                # floats. A bit-packing receiver (CoherentRXBlock) emits the
                # decoded bit in the word's LSB, which Q15 scaling (word/32768)
                # would crush to ~0 — so those blocks must read raw. A recovered-I
                # receiver (CoherentBPSKRxBlock) emits a Q15 value and wants the
                # default Q15 float. Default False keeps the existing behavior.
                raw = bool(header.get("raw", False))
                out_vals: list[float] = []
                npairs = len(data) // 2
                _t_batch0 = time.perf_counter()
                # Drive each complex sample the PROVEN way: inject xi→a0, run; xq→a1,
                # run; JUMP entry, run; then drain the output port. (The
                # write_port_multi_i16 path stalls the loop after one sample; the
                # raw inject path advances every sample and is what the on-chip lock
                # tests use.) target_hop_cnt=30 = @1 to the landing cell at the
                # input-port edge.
                for k in range(npairs):
                    xi = _float_to_q15(float(data[2 * k]))
                    xq = _float_to_q15(float(data[2 * k + 1]))
                    self._chip.inject_data_physical([xi], target_hop_cnt=30,
                                                    target_addr=int(a0))
                    self._chip.run(max_events=3000)
                    self._chip.inject_data_physical([xq], target_hop_cnt=30,
                                                    target_addr=int(a1))
                    self._chip.run(max_events=3000)
                    self._chip.inject_jump_physical(target_hop_cnt=30,
                                                    entry_addr=entry)
                    self._chip.run(max_events=mx)
                    if raw:
                        got = self._chip.read_port_i16(port)
                        if got is not None and len(got):
                            out_vals.extend(float(int(v)) for v in got)
                    else:
                        got = self._chip.read_port(port)
                        if got is not None and len(got):
                            out_vals.extend(float(v) for v in got)
                # Throughput metric: how fast simKYT processes I/Q samples on THIS
                # machine. simkyt is an event-accurate async-ASIC sim, not a
                # real-time DSP source — this tells the user roughly how long a given
                # burst length will take (e.g. 1 s of 48 kHz audio ≈ npairs/sps_rate
                # seconds of wall time). Reported in the reply header and to the GUI.
                _dt = max(1e-9, time.perf_counter() - _t_batch0)
                sps = npairs / _dt
                if self._on_activity is not None:
                    # Pass the metric if the callback accepts it; else ping plainly.
                    try:
                        self._on_activity(samples=npairs, seconds=_dt,
                                          samples_per_sec=sps)
                    except TypeError:
                        self._on_activity()
                return ({"ok": True, "samples": npairs, "seconds": _dt,
                         "samples_per_sec": sps},
                        np.asarray(out_vals, dtype="<f4"))
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
