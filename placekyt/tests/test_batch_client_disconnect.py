# SPDX-License-Identifier: GPL-3.0-or-later
"""A GRC client disconnect mid-batch aborts the server loop (GRC Stop).

When the GRC flowgraph stops or closes its socket, placeKYT must abort the
in-progress process_batch promptly rather than running the whole burst to
completion (the user pressed Stop in GRC — the sim should stop). The server's
per-sample loop periodically checks the client connection; a closed connection
(EOF on a non-blocking peek) trips the debug-hook stop, raising BatchAborted.

We drive SimServer.process_batch against a tiny fake chip whose per-sample run
is a no-op, connect a real client socket, start a batch, then close the client
mid-flight and assert the server loop aborts (returns aborted, not the whole
burst).
"""
import os
import socket
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from engine import sim_bridge
from engine.batch_debug import BatchDebugHooks
from engine.sim_bridge import SimServer, recv_message, send_message


class _SlowChip:
    """A chip stub whose run() sleeps a touch so a long batch stays in flight
    long enough for the test to close the client. inject/read are no-ops."""

    def __init__(self):
        self.simulation_time = 0.0

    def inject_data_physical(self, *a, **k):
        pass

    def inject_jump_physical(self, *a, **k):
        pass

    def run(self, *a, **k):
        time.sleep(0.002)
        return {"events_processed": 1, "stop_reason": "QueueEmpty"}

    def read_port(self, port):
        return []

    def read_port_i16(self, port):
        return []

    def read_port_words_timed(self, port):
        return []

    def clear_trace(self):
        pass


def test_client_disconnect_aborts_batch():
    chip = _SlowChip()
    hooks = BatchDebugHooks()
    server = SimServer(chip, host="127.0.0.1", port=0, debug_hooks=hooks)
    bound = server.start()
    try:
        cli = socket.create_connection(("127.0.0.1", bound), timeout=2)
        # A long REAL burst so the loop stays busy while we disconnect.
        nsamp = 4000
        payload = np.zeros(nsamp, dtype="<f4")   # real burst (complex=False)
        send_message(cli, {"op": "process_batch", "complex": False,
                           "n": nsamp}, payload)

        # Give the server a moment to enter the loop, then hard-close the client.
        time.sleep(0.2)
        cli.close()

        # The server thread must abort the batch promptly (not run all 4000
        # samples). We can't read the reply (socket closed), so we assert the
        # server's per-sample loop noticed the dead connection and stopped: the
        # serve thread returns to accepting, so a NEW client can connect + ping.
        deadline = time.monotonic() + 5.0
        ok = False
        while time.monotonic() < deadline:
            try:
                c2 = socket.create_connection(("127.0.0.1", bound), timeout=1)
                send_message(c2, {"op": "ping"})
                reply, _ = recv_message(c2)
                c2.close()
                if reply.get("ok"):
                    ok = True
                    break
            except OSError:
                time.sleep(0.05)
        assert ok, "server must abort the batch and resume serving after a " \
                   "mid-batch client disconnect (not run the whole burst)"
    finally:
        server.stop()


def test_duplex_stop_aborts_then_next_batch_rearms():
    """The interleaved duplex loop honors the debug-hook STOP too (it drives
    the live GRC transceiver demos): hooks.stop() mid-burst aborts promptly and
    the reply reports it; the NEXT duplex batch re-arms (clear_stop) and runs
    to completion — Stop → Run-again must work on the persistent server."""
    chip = _SlowChip()
    hooks = BatchDebugHooks()
    server = SimServer(chip, host="127.0.0.1", port=0, debug_hooks=hooks)
    bound = server.start()
    try:
        cli = socket.create_connection(("127.0.0.1", bound), timeout=2)
        nsamp = 4000
        payload = np.zeros(nsamp, dtype="<f4")
        send_message(cli, {"op": "process_batch_duplex", "port": "x16_out",
                           "streams": [{"stream_id": "rx", "complex": False,
                                        "raw": False, "n_samples": nsamp}]},
                     payload)
        time.sleep(0.2)              # burst in flight on the server thread
        t0 = time.monotonic()
        hooks.stop()                 # the user hits Stop
        reply, _ = recv_message(cli)
        dt = time.monotonic() - t0
        assert reply.get("ok") and reply.get("aborted") is True, reply
        assert dt < 5.0, f"duplex abort must be prompt, took {dt:.1f}s"

        # Run-again: a fresh small batch on the same connection completes
        # (clear_stop at the top of the batch re-arms the one-shot latch).
        n2 = 8
        send_message(cli, {"op": "process_batch_duplex", "port": "x16_out",
                           "streams": [{"stream_id": "rx", "complex": False,
                                        "raw": False, "n_samples": n2}]},
                     np.zeros(n2, dtype="<f4"))
        reply2, _ = recv_message(cli)
        assert reply2.get("ok") and reply2.get("aborted") is False, reply2
        cli.close()
    finally:
        server.stop()
