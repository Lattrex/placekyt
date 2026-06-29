"""
Kyttar Sink Block for GNURadio

This block acts as the exit point from a Kyttar chip.
It reads data from the chip's OUTPUT PORT - the only valid way to get data out.

Usage:
    Source [GR] -> [kyttar.source] -> [kyttar.gain] -> [kyttar.sink] -> Sink [GR]

The Sink block:
1. Reads results from the specified output port using chip.read_port()
2. Outputs float32 samples to the GNURadio domain

Multi-channel I/Q mode:
    When used with demux/mux blocks for I/Q processing, set num_channels=2.
    The sink will pair samples from each channel using per-channel FIFOs.
    This handles async arrival order where I and Q may complete at different times.

    Channel entry addresses:
    - Channel 0 (I): R1
    - Channel 1 (Q): R11
    - Channel 2: R21 (if 3-channel mode)

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import time

import numpy as np
from gnuradio import gr
from typing import Optional, Any, List
from collections import deque

# SOCKET-ONLY: this block drains the recovered words from the matching
# kyttar.source's server-batch session (one process_batch RPC to a placeKYT-hosted
# chip). It imports gnuradio + numpy + socket ONLY (the session lives in
# _batch_session). It does NOT import gr_kyttar or simkyt and does NOT own a chip.
# When server_port <= 0 it degrades to a harmless no-op (outputs zeros), never
# crashes, never touches the heavy libraries.

# Channel entry addresses (must match demux/mux)
CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]


class sink(gr.sync_block):
    """
    Kyttar Sink - Exit point from Kyttar chip via OUTPUT PORT.

    Data exits the chip ONLY through the configured output port.
    There is no other way to get data out of the chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        port_name: Name of the chip output port (e.g., 'x16_out')
        num_channels: Number of channels (1=simple, 2=I/Q, 3=tri-channel)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        port_name: str = "x16_out",
        num_channels: int = 1,
        server_port: int = 0,
        server_repeat: bool = False,
        hold_secs: float = 5.0,
        stream_id: str = "",
    ):
        # SERVER-BATCH MODE (server_port > 0): the matching kyttar_source (same
        # device_id) batches the burst through the placeKYT-hosted chip; this sink
        # drains the recovered words from the shared session and emits them. Its GR
        # input is the marker-chain pass-through (ignored); its OUTPUT is the
        # recovered stream. Decimating, so a basic_block would be ideal — but a
        # sync_block works here because we emit whatever is ready each call and
        # signal WORK_DONE once drained.
        self._server_mode = int(server_port) > 0
        # Input is FLOAT in both modes (the marker chain is float). In server mode
        # the GR input is ignored — the recovered words come from the batch session.
        gr.sync_block.__init__(
            self,
            name="Kyttar Sink",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )

        self._device_id = device_id
        self._port_name = port_name
        self._num_channels = num_channels
        # SHARED-OUTPUT-PORT DUPLEX: a sink waits on ITS stream's session (keyed by
        # (device_id, stream_id)), so two sinks sharing one chip device each drain
        # only their own recovered words. Empty ⇒ today's single-stream behavior.
        self._stream_id = str(stream_id or "")
        self._server_repeat = bool(server_repeat)
        self._hold_secs = float(hold_secs)   # render window after the one emit
        self._emit_done_at = None            # wall time the burst finished emitting
        self._server_result = None   # the full recovered burst (once it arrives)
        self._server_outq = None     # current emit cursor into the burst

        # Single-channel mode: simple sample buffer
        # Multi-channel mode: per-channel FIFOs for pairing
        if num_channels == 1:
            # Buffer to hold excess samples from read_port() calls
            # read_port() drains ALL available samples, so we buffer any excess
            self._sample_buffer = np.array([], dtype=np.float32)
            self._channel_buffers = None
        else:
            # Per-channel FIFOs for I/Q pairing
            # Samples arrive tagged with channel ID (via entry address)
            # We pair them in order: I0, Q0, I1, Q1, ...
            self._sample_buffer = None
            self._channel_buffers: List[deque] = [deque() for _ in range(num_channels)]
            self._paired_output = deque()  # Paired samples ready for output

        if self._server_mode:
            print(f"[kyttar.sink] SERVER-BATCH mode (device '{device_id}', "
                  f"port '{port_name}') — drains the source's process_batch result")
            return

        # NO server configured. This block requires server-batch mode. Degrade to a
        # harmless no-op (outputs zeros). Do NOT import gr_kyttar/simkyt or crash.
        print("[kyttar.sink: set server_port to the port placeKYT prints under "
              "'Run as GNURadio Server']")

    def start(self) -> bool:
        """Called when flowgraph starts. Import-light; never touches heavy libs."""
        if self._server_mode:
            print(f"[kyttar.sink] Starting (server-batch), device='{self._device_id}', "
                  f"port='{self._port_name}'")
        return True

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        return True

    def work(self, input_items, output_items):
        """Process samples - run simulation and read from chip output port.

        CRITICAL: No samples may be lost. read_port() drains ALL available
        samples from the simulator, so we buffer any excess and return them
        in subsequent work() calls. This ensures every input sample produces
        exactly one output sample in the correct order.

        Multi-channel mode: Samples arrive with channel tags (via entry address).
        We maintain per-channel FIFOs and pair samples in order (I0, Q0, I1, Q1...).
        """
        inp = input_items[0]
        out = output_items[0]
        n_samples = len(inp)

        # === SERVER-BATCH MODE ===
        # simKYT processed the burst ONCE (one process_batch RPC); these are the GENUINE
        # recovered bits. We emit them ONCE, then HOLD the flowgraph open briefly (a
        # render window) so the QT GUI Time Sink actually paints the real result before
        # the graph ends — WITHOUT replaying the data (the plot is a faithful static
        # trace of the one batch, not a looping fake "stream"; simKYT is an event-
        # accurate async-ASIC sim, ~7.6k samples/s, and is NOT a real-time DSP source).
        #
        # Why not just emit + WORK_DONE: that tears the graph down before the sink's
        # ~100ms refresh fires, leaving a blank plot. Why not loop: that misrepresents
        # one batch as a continuous stream. So: emit once, then idle (produce nothing)
        # for `_hold_secs`, then end. ``server_repeat=True`` (opt-in) restores the
        # looping display for visual impact.
        if self._server_mode:
            from ._batch_session import get_session
            sess = get_session(self._device_id, self._stream_id)
            if self._server_result is None:
                # Degrade gracefully if the session never produced a result (server
                # absent/refused): the source logs the failure; here we simply keep
                # waiting/holding without raising into the GR scheduler.
                try:
                    r = sess.take_result(timeout=0.05)
                except Exception:  # noqa: BLE001
                    r = None
                if r is not None:
                    self._server_result = np.asarray(r, dtype=np.float32)
                    self._server_outq = self._server_result.copy()
            n = 0
            if self._server_outq is not None and len(self._server_outq):
                n = min(len(out), len(self._server_outq))
                out[:n] = self._server_outq[:n]
                self._server_outq = self._server_outq[n:]
                if not len(self._server_outq):
                    self._emit_done_at = time.time()   # burst fully emitted; start hold
                return n
            # Burst already emitted (or none yet). Decide: loop, hold-then-end, or wait.
            if self._server_result is not None and len(self._server_result):
                if self._server_repeat:
                    time.sleep(0.05)
                    self._server_outq = self._server_result.copy()
                    return 0
                # Emit-once: hold the graph open a few seconds so the GUI paints the
                # real trace, then WORK_DONE. (A headless vector_sink test sets
                # _hold_secs=0 to end immediately after the one emit.)
                if (self._emit_done_at is not None
                        and time.time() - self._emit_done_at >= self._hold_secs):
                    return -1   # WORK_DONE — the genuine batch result has been shown
                time.sleep(0.05)
            return 0

        # NO server configured: harmless no-op (output zeros). No chip, no
        # heavy imports, never raises.
        out[:] = 0.0
        return n_samples
