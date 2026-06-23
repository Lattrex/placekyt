"""placekyt_chip — a single-block, threaded live bridge to a placeKYT chip.

A GNURadio ``sync_block`` (float in → float out) that streams samples through a
chip running LIVE inside placeKYT (over the SimServer socket), with a BACKGROUND
THREAD so GNURadio's scheduler never blocks on the network/RPC. ``work()`` only
moves bytes between GNURadio's buffers and two thread-safe queues; the worker
thread does the blocking socket I/O.

This replaces the source/sink + device trio for the live case with one block:

    sig_source → placekyt_chip → time_sink (ch1)
    sig_source → time_sink (ch0)      # tap the input for comparison

Throughput is decoupled from GNURadio's call granularity: the worker drains the
input queue in big chunks, runs the chip, and fills the output queue, so a
continuous QT scope keeps flowing.

Copyright 2026 Lattrex Kyttar Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import queue
import threading

import numpy as np
from gnuradio import gr

from .placekyt_sim_client import ChipProxy

_CHUNK = 256  # samples per chip run (amortizes the RPC round-trip)


class placekyt_chip(gr.sync_block):
    """Stream float samples through a placeKYT-hosted chip, 1 in → 1 out.

    Parameters:
        host, port:  the SimServer address placeKYT printed
        input_port:  chip input port name (e.g. 'x16_in')
        output_port: chip output port name (e.g. 'x16_out')
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        input_port: str = "x16_in",
        output_port: str = "x16_out",
        jump_entry: int = -1,
        out_tag: int = -1,
        complex_in: bool = False,
    ):
        # COMPLEX input (e.g. a coherent receiver): the input stream is gr_complex
        # and each sample is injected as an (xi→R0, xq→R1) pair + JUMP. Otherwise
        # the input is a real float stream (one value per sample → R0).
        self._complex_in = bool(complex_in)
        gr.sync_block.__init__(
            self, name="placeKYT Chip (live)",
            in_sig=[np.complex64 if self._complex_in else np.float32],
            out_sig=[np.float32])
        # Shared-port duplex (optional): when jump_entry >= 0, this instance's
        # whole input stream is injected with that JUMP entry (routing it to a
        # specific landing-cell entry, e.g. a splitter's RX or TX arm). When
        # out_tag >= 0, only output WRITE words with that dest tag are returned —
        # so multiple instances can share one chip output port, demuxed by tag.
        self._jump_entry = jump_entry if jump_entry is not None and jump_entry >= 0 else None
        self._out_tag = out_tag if out_tag is not None and out_tag >= 0 else None
        # Force the scheduler to call work() in SMALL chunks so samples flow
        # incrementally to a live scope instead of arriving in big bursts after a
        # long startup buffer-fill. (The chip is slow vs. raw DSP, so we trade
        # raw throughput for a responsive, smooth display.)
        self.set_max_noutput_items(_CHUNK)
        self._host = host
        self._port = port
        self._in_name = input_port
        self._out_name = output_port
        self._proxy: ChipProxy | None = None
        self._in_q: "queue.Queue" = queue.Queue(maxsize=64)
        self._out_buf = np.array([], dtype=np.float32)
        self._out_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._running = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        # Fresh connection + chip reset each run, then spin up the worker.
        try:
            self._proxy = ChipProxy(self._host, self._port,
                                    self._in_name, self._out_name)
            self._proxy.reset()
        except Exception as e:  # noqa: BLE001
            print(f"[placeKYT-Chip] connect failed: {e}")
            self._proxy = None
            return True  # don't crash the flowgraph; work() will output zeros
        self._out_buf = np.array([], dtype=np.float32)
        while not self._in_q.empty():
            try:
                self._in_q.get_nowait()
            except queue.Empty:
                break
        self._running = True
        self._worker = threading.Thread(target=self._stream, daemon=True)
        self._worker.start()
        return True

    def stop(self) -> bool:
        self._running = False
        # Unblock the worker if it's waiting on the queue.
        try:
            self._in_q.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self._proxy is not None:
            self._proxy.close()
            self._proxy = None
        return True

    # -- worker thread (blocking socket I/O lives here) -----------------------

    def _stream(self) -> None:
        """Pull input chunks from the queue, run them through the chip, append
        results to the output buffer. Runs until stop()."""
        _in_dt = np.complex64 if self._complex_in else np.float32
        pending = np.array([], dtype=_in_dt)
        _empty = np.array([], dtype=_in_dt)
        while self._running and self._proxy is not None:
            try:
                item = self._in_q.get(timeout=0.2)
            except queue.Empty:
                # No new input — flush whatever we have so output isn't withheld
                # waiting for a full chunk (keeps the scope flowing at low rates).
                if len(pending):
                    item = _empty
                else:
                    continue
            if item is None:  # stop sentinel
                break
            pending = np.concatenate([pending, item])
            # Drain greedily: grab any already-queued input so each RPC carries
            # as many samples as possible (amortizes the round-trip).
            while True:
                try:
                    more = self._in_q.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._running = False
                    break
                pending = np.concatenate([pending, more])
            # Process all pending in chunks (don't withhold a partial tail).
            while len(pending):
                take = pending[:_CHUNK]
                pending = pending[_CHUNK:]
                try:
                    if self._complex_in:
                        # Each complex sample → an (xi→R0, xq→R1) pair injected as
                        # one multi-word transaction; one chip pass → one output.
                        c = np.asarray(take, dtype=np.complex64)
                        iq = np.empty(2 * len(c), dtype=np.float32)
                        iq[0::2] = c.real
                        iq[1::2] = c.imag
                        entry = self._jump_entry if self._jump_entry is not None else 0
                        self._proxy.write_port_complex(
                            self._in_name, iq, data_addrs=(0, 1),
                            jump_entry=entry)
                    else:
                        self._proxy.write_port(self._in_name, take,
                                               jump_entry=self._jump_entry)
                    self._proxy.run_until_output(
                        self._out_name, len(take), len(take) * 500)
                    if self._out_tag is not None:
                        got, _dests = self._proxy.read_port_tagged(
                            self._out_name, tag=self._out_tag)
                    else:
                        got = self._proxy.read_port(self._out_name)
                except Exception as e:  # noqa: BLE001
                    print(f"[placeKYT-Chip] stream error: {e}")
                    self._running = False
                    break
                if got is not None and len(got):
                    with self._out_lock:
                        self._out_buf = np.concatenate([self._out_buf, got])

    # -- GNURadio work (non-blocking) -----------------------------------------

    def work(self, input_items, output_items):
        import time
        inp = input_items[0]
        out = output_items[0]
        n = len(out)

        if self._proxy is None:
            out[:] = 0.0
            return n

        # Hand this work() call's input to the worker thread (blocks briefly if
        # the chip is behind — natural backpressure, not a hard stall).
        try:
            dt = np.complex64 if self._complex_in else np.float32
            self._in_q.put(np.array(inp, dtype=dt), timeout=1.0)
        except queue.Full:
            pass

        # Produce EXACTLY n output samples, waiting (briefly) for the worker to
        # catch up so output tracks input 1:1 rather than emitting zeros ahead of
        # the chip. A short bounded wait keeps the scheduler responsive.
        deadline = time.time() + 1.0
        while True:
            with self._out_lock:
                if len(self._out_buf) >= n:
                    out[:] = self._out_buf[:n]
                    self._out_buf = self._out_buf[n:]
                    return n
            if not self._running or time.time() > deadline:
                break
            time.sleep(0.002)
        # Timed out (pipeline priming or stalled) — emit what we have + zeros.
        with self._out_lock:
            k = min(n, len(self._out_buf))
            if k:
                out[:k] = self._out_buf[:k]
                self._out_buf = self._out_buf[k:]
            if k < n:
                out[k:] = 0.0
        return n
