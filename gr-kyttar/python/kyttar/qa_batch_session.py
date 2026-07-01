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


if __name__ == "__main__":
    unittest.main()
