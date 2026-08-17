# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the source<->sink BatchSession (server-batch mode).

These are plain ``unittest`` tests that load ``_batch_session.py`` by file path so
they run under ANY python with just numpy (the module itself imports only socket +
numpy, no gnuradio) — mirroring how the OOT source/sink use it.

The key regression (``test_repeated_run_does_not_replay_stale_result``) reproduces
the user-reported FLAT-PLOT bug: the session is process-global and OUTLIVES a
single flowgraph Run, so after run N's sink drains the burst, a stale ``done=True``
must NOT let run N+1's sink re-take the already-consumed (empty) result. Before the
generation (``_seq``) gate, a repeated Run whose sink polled before the source's
fresh dispatch got 0 samples and plotted flat even though the chip re-emitted.

Run:
    python3 -m unittest gnuradio.kyttar.qa_batch_session      # installed
    python3 gr-kyttar/python/kyttar/qa_batch_session.py       # in-tree
"""

import importlib.util
import os
import threading
import unittest

import numpy as np

_BS_PATH = os.path.join(os.path.dirname(__file__), "_batch_session.py")


def _load_session_module():
    spec = importlib.util.spec_from_file_location("_kyttar_batch_session", _BS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BatchSessionTests(unittest.TestCase):
    def setUp(self):
        self.bs = _load_session_module()

    def _dispatch(self, sess, values):
        """Simulate a source dispatch: publish a fresh burst generation."""
        with sess._cv:
            sess._result = np.asarray(values, dtype=np.float32)
            sess.done = True
            sess._seq += 1
            sess._cv.notify_all()

    def test_single_run_delivers_once(self):
        sess = self.bs.BatchSession("dev")
        self._dispatch(sess, range(120))
        r = sess.take_result(timeout=0.1)
        self.assertIsNotNone(r)
        self.assertEqual(len(r), 120)

    def test_repeated_run_does_not_replay_stale_result(self):
        """RUN N drains; RUN N+1's sink polls BEFORE the source re-dispatches — it
        must BLOCK (return None on timeout), not re-take the consumed empty result.
        Then after the fresh dispatch it gets the full new burst."""
        sess = self.bs.BatchSession("dev")

        # RUN 1: dispatch + drain.
        self._dispatch(sess, range(120))
        r1 = sess.take_result(timeout=0.1)
        self.assertEqual(len(r1), 120)

        # RUN 2: sink polls FIRST (the race). Must not return the stale burst.
        r2 = sess.take_result(timeout=0.05)
        self.assertIsNone(r2, "stale result replayed on repeated run -> FLAT plot")

        # RUN 2 continues: source re-dispatches; sink now gets the fresh burst.
        self._dispatch(sess, range(64))
        r3 = sess.take_result(timeout=0.1)
        self.assertIsNotNone(r3)
        self.assertEqual(len(r3), 64)

    def test_sink_blocks_until_dispatch(self):
        """A sink that waits gets the burst as soon as the source dispatches (the
        normal in-order case), across a fresh session with no prior run."""
        sess = self.bs.BatchSession("dev")
        got = {}

        def waiter():
            got["r"] = sess.take_result(timeout=2.0)

        t = threading.Thread(target=waiter)
        t.start()
        # Dispatch shortly after the sink began waiting.
        self._dispatch(sess, range(50))
        t.join(timeout=3.0)
        self.assertIn("r", got)
        self.assertIsNotNone(got["r"])
        self.assertEqual(len(got["r"]), 50)

    def test_reset_then_dispatch_still_delivers(self):
        """reset() marks 'no result pending' (not 're-deliver'); a following
        dispatch still bumps the generation so the sink sees a fresh burst."""
        sess = self.bs.BatchSession("dev")
        self._dispatch(sess, range(10))
        self.assertEqual(len(sess.take_result(timeout=0.1)), 10)
        sess.reset()
        self.assertIsNone(sess.take_result(timeout=0.05))
        self._dispatch(sess, range(7))
        self.assertEqual(len(sess.take_result(timeout=0.1)), 7)

    def test_sessions_keyed_by_stream_id(self):
        """rx and tx streams get SEPARATE sessions (shared duplex chip)."""
        rx = self.bs.get_session("dev", "rx")
        tx = self.bs.get_session("dev", "tx")
        self.assertIsNot(rx, tx)
        self.assertIs(rx, self.bs.get_session("dev", "rx"))

    def test_duplex_rendezvous_sends_one_combined_rpc(self):
        """The DuplexRendezvous coordinates BOTH stream sources into ONE
        process_batch_duplex RPC carrying both streams (so they run interleaved on
        the server), and splits the reply back per stream. Two source threads
        submit tx + rx; a tiny fake server verifies a single duplex RPC arrived
        with both streams and replies per-stream words."""
        import json
        import socket
        import struct
        import threading as _th

        HDR = struct.Struct(">I")
        received = {}

        def _fake_server(sock):
            conn, _ = sock.accept()
            hlen = HDR.unpack(conn.recv(4))[0]
            header = json.loads(conn.recv(hlen).decode())
            n = int(header.get("n", 0))
            if n:
                need = n * 4
                buf = b""
                while len(buf) < need:
                    buf += conn.recv(need - len(buf))
            received["header"] = header
            # Reply: 2 words for tx, 3 for rx (lengths + concatenated payload).
            out = np.array([10, 11, 20, 21, 22], dtype="<f4")
            rhdr = {"ok": True, "lengths": [2, 3],
                    "stream_ids": ["tx", "rx"], "n": int(out.size)}
            hb = json.dumps(rhdr).encode()
            conn.sendall(HDR.pack(len(hb))); conn.sendall(hb)
            conn.sendall(out.tobytes())
            conn.close()

        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        host, port = srv.getsockname()
        t = _th.Thread(target=_fake_server, args=(srv,), daemon=True); t.start()

        rv = self.bs.get_rendezvous("dev-duplex")
        results = {}

        def _submit(sid, samples, cplx):
            results[sid] = rv.submit(host, port, sid, samples,
                                     complex_=cplx, raw=True, collect_window=0.3)

        tx_t = _th.Thread(target=_submit, args=("tx", np.array([1.0, 0.0]), False))
        rx_t = _th.Thread(target=_submit,
                          args=("rx", np.array([0.5 + 0j, 0.5 + 0j, 0.5 + 0j]), True))
        tx_t.start(); rx_t.start()
        tx_t.join(timeout=5); rx_t.join(timeout=5)
        t.join(timeout=5)
        srv.close()

        # ONE combined RPC carrying BOTH streams.
        self.assertIn("header", received)
        self.assertEqual(received["header"]["op"], "process_batch_duplex")
        sids = {s["stream_id"] for s in received["header"]["streams"]}
        self.assertEqual(sids, {"tx", "rx"})
        # Each stream got ITS OWN words back (split by lengths).
        self.assertEqual(list(results["tx"]), [10.0, 11.0])
        self.assertEqual(list(results["rx"]), [20.0, 21.0, 22.0])


if __name__ == "__main__":
    unittest.main()
