"""Live placeKYT-hosted chip CLIENT for GNURadio.

The companion to placeKYT's ``engine/sim_bridge.py`` SimServer. placeKYT owns the
running chip (with its live debug views); this GNURadio device connects to it
over a localhost TCP socket and exposes a ``ChipProxy`` whose ``write_port`` /
``output_available`` / ``run_until_output`` / ``read_port`` calls are forwarded
across the wire. The existing :class:`kyttar_source` / :class:`kyttar_sink`
use the proxy exactly like a local chip — so a GNURadio flowgraph drives the
chip that's running LIVE inside placeKYT, and you watch the canvas / transaction
log / waveform / cursor update in real time over there.

The wire protocol is duplicated verbatim from ``engine/sim_bridge.py`` (the two
processes run in different Python envs and can't import each other).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import socket
import struct

import numpy as np

# GNURadio is OPTIONAL here: the pure-socket ``ChipProxy`` client below needs only
# sockets + numpy, so a non-GR consumer (a headless test, the placeKYT batch demo)
# can import + use it without GNURadio installed. The ``placekyt_sim_client`` GR
# BLOCK at the bottom needs ``gr`` and is only defined when GNURadio is present.
try:
    from gnuradio import gr
    _HAVE_GR = True
except ImportError:  # noqa: BLE001 — headless / non-GR environment
    gr = None
    _HAVE_GR = False

_HDR = struct.Struct(">I")


def _recv_exactly(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(conn):
    hlen = _HDR.unpack(_recv_exactly(conn, 4))[0]
    header = json.loads(_recv_exactly(conn, hlen).decode("utf-8"))
    n = int(header.get("n", 0))
    payload = np.frombuffer(_recv_exactly(conn, n * 4), dtype="<f4") if n else None
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


class ChipProxy:
    """Remote stand-in for a simkyt.Chip, forwarding the port API over a
    socket to placeKYT's SimServer. Exposes only what the source/sink call."""

    def __init__(self, host: str, port: int, input_port: str, output_port: str):
        self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._conn.connect((host, port))
        # Advertise the configured ports so the source/sink can discover them
        # without a separate chip object.
        self._input_port = input_port
        self._output_port = output_port

    # The source/sink consult these to validate the port names.
    @property
    def input_port_names(self):
        return [self._input_port] if self._input_port else []

    @property
    def output_port_names(self):
        return [self._output_port] if self._output_port else []

    def _rpc(self, header, payload=None):
        _send_message(self._conn, header, payload)
        reply, out = _recv_message(self._conn)
        if not reply.get("ok"):
            raise RuntimeError(f"SimServer error: {reply.get('error')}")
        return reply, out

    def write_port(self, port, data, jump_entry=None):
        """Send samples to a chip input port. ``jump_entry`` (optional) tags the
        whole stream with a fixed JUMP entry address — used for shared-port duplex
        so RX vs TX bursts route to different landing-cell entries."""
        h = {"op": "write_port", "port": port}
        if jump_entry is not None:
            h["jump_entry"] = int(jump_entry)
        self._rpc(h, np.asarray(data, dtype="<f4"))

    def write_port_complex(self, port, iq_interleaved, data_addrs=(0, 1),
                           jump_entry=0):
        """Send a COMPLEX baseband stream as interleaved [xi0,xq0,xi1,xq1,...]
        floats. Each (xi,xq) pair is injected as one multi-word transaction —
        xi→data_addrs[0], xq→data_addrs[1], then a JUMP to ``jump_entry`` — so the
        stream drives a 2-input landing cell (e.g. the Costas phase cell xi@R0,
        xq@R1)."""
        h = {"op": "write_port_complex", "port": port,
             "data_addrs": [int(data_addrs[0]), int(data_addrs[1])],
             "jump_entry": int(jump_entry)}
        self._rpc(h, np.asarray(iq_interleaved, dtype="<f4"))

    def process_batch(self, in_port, out_port, iq_interleaved, data_addrs=(0, 1),
                      jump_entry=0, max_events_per=40000, raw=False,
                      grc_params=None):
        """BATCH (run-to-completion) processing — the right model for a multi-cell
        DUT (BPSK receiver and up). The WHOLE interleaved-I/Q burst
        ([xi0,xq0,xi1,xq1,...]) is processed on the server in ONE RPC: each
        complex sample is injected + run sequentially there (sequential NCO
        feedback), but with no per-sample socket round-trip and one debug-view
        refresh at the end. Returns the full recovered output stream (float32).

        ``raw=True`` returns the raw int16 output WORDS (as float32) instead of
        Q15-scaled floats — needed for a bit-packing receiver (CoherentRXBlock)
        whose decoded bit is the output word's LSB (Q15 scaling would crush it).

        Use this instead of write_port_complex + run_until_output + read_port in a
        loop: same result, but it does not crawl on anything past a toy block."""
        h = {"op": "process_batch", "port": out_port, "in_port": in_port,
             "data_addrs": [int(data_addrs[0]), int(data_addrs[1])],
             "jump_entry": int(jump_entry),
             "max_events_per": int(max_events_per),
             "raw": bool(raw)}
        # Optional GRC-sync: advertise this flowgraph's per-block params alongside
        # the batch so the host can flag a parameter drift (additive header field).
        if grc_params:
            h["grc_params"] = dict(grc_params)
        _reply, out = self._rpc(h, np.asarray(iq_interleaved, dtype="<f4"))
        return out if out is not None else np.array([], dtype=np.float32)

    def set_grc_params(self, params_by_block):
        """Advertise this flowgraph's per-block params to the host so placeKYT
        can detect a parameter drift from the placed design (the GRC↔placeKYT
        sync indicator). ``params_by_block`` = {placeKYT block name: {param:
        value}}. Backward compatible: an older host that doesn't know the op
        replies with an 'unknown op' error, which we swallow."""
        try:
            self._rpc({"op": "set_grc_params",
                       "params": dict(params_by_block or {})})
        except RuntimeError:
            pass  # host predates GRC-sync — nothing to do

    def read_port_tagged(self, port, tag=None):
        """Read output WRITE values with their dest TAGS (shared-port demux).
        Returns ``(values, dests)``. ``tag`` (optional) filters to one dest."""
        h = {"op": "read_port_tagged", "port": port}
        if tag is not None:
            h["tag"] = int(tag)
        reply, out = self._rpc(h)
        vals = out if out is not None else np.array([], dtype=np.float32)
        return vals, list(reply.get("dests", []))

    def output_available(self, port):
        reply, _ = self._rpc({"op": "output_available", "port": port})
        return int(reply.get("available", 0))

    def run_until_output(self, port, count, max_events=None):
        h = {"op": "run_until_output", "port": port, "count": int(count)}
        if max_events is not None:
            h["max_events"] = int(max_events)
        self._rpc(h)

    def reset(self):
        """Ask the server to rehost a FRESH chip — call at the start of a run so
        each flowgraph run begins from clean chip state (the host rebuilds)."""
        self._rpc({"op": "reset"})

    def read_port(self, port):
        _reply, out = self._rpc({"op": "read_port", "port": port})
        return out if out is not None else np.array([], dtype=np.float32)

    def close(self):
        try:
            self._conn.close()
        except OSError:
            pass


if not _HAVE_GR:
    # No GNURadio → expose only the pure-socket ChipProxy (defined above). The GR
    # block below is skipped so the module imports cleanly in a headless venv.
    placekyt_sim_client = None  # type: ignore[assignment]
else:
  from .registry import DeviceType, get_registry  # noqa: E402

  class placekyt_sim_client(gr.basic_block):
    """Connects to a placeKYT-hosted chip (SimServer) and registers a ChipProxy
    so the Kyttar source/sink stream through the LIVE placeKYT simulation.

    Parameters:
        device_id:   shared with the source/sink blocks
        host, port:  the SimServer address printed by placeKYT
        input_port:  the chip input port name (e.g. 'x16_in')
        output_port: the chip output port name (e.g. 'x16_out')
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        host: str = "127.0.0.1",
        port: int = 0,
        input_port: str = "x16_in",
        output_port: str = "x16_out",
    ):
        gr.basic_block.__init__(
            self, name="placeKYT Sim Client", in_sig=[], out_sig=[])
        self._device_id = device_id
        self._host = host
        self._port = port
        self._input_port = input_port
        self._output_port = output_port
        self._proxy: ChipProxy | None = None

        get_registry().register_device(
            device_id=device_id, chip_config="<remote>",
            device_type=DeviceType.SIMULATOR)
        self._connect()
        print(f"[placeKYT-Client] '{device_id}' → {host}:{port}")

    def _connect(self) -> None:
        try:
            self._proxy = ChipProxy(self._host, self._port,
                                    self._input_port, self._output_port)
            # A no-port block isn't guaranteed start(); register the proxy now.
            get_registry().set_chip(self._device_id, self._proxy)
        except Exception as e:  # noqa: BLE001
            print(f"[placeKYT-Client] WARN: connect deferred: {e}")
            self._proxy = None

    def start(self) -> bool:
        # Reconnect fresh each run (the previous run closed the socket in stop())
        # and ask the host for a clean chip, so every flowgraph run starts from
        # reset chip state rather than carrying the prior run's delay lines.
        self._connect()
        if self._proxy is not None:
            try:
                self._proxy.reset()
            except Exception as e:  # noqa: BLE001
                print(f"[placeKYT-Client] WARN: reset on start failed: {e}")
        return True

    def stop(self) -> bool:
        if self._proxy is not None:
            self._proxy.close()
            self._proxy = None
        return True

    def general_work(self, input_items, output_items):
        return 0

    def get_chip(self):
        return get_registry().get_chip(self._device_id)

    def get_device_id(self) -> str:
        return self._device_id

    def is_initialized(self) -> bool:
        return self._proxy is not None
