"""
Kyttar Source Block for GNURadio

This block acts as the entry point into a Kyttar chip.
It writes data to the chip's INPUT PORT - the only valid way to get data in.

Usage:
    Source [GR] -> [kyttar.source] -> [kyttar.gain] -> [kyttar.sink] -> Sink [GR]

The Source block:
1. Receives float32 samples from the GNURadio domain
2. Writes them to the specified input port using chip.write_port()
3. Runs the simulation with TRUE PIPELINED operation

PIPELINING: Multiple samples can be in-flight simultaneously. The chip
processes data like a pipeline - sample N entering while sample N-1 is
mid-array and sample N-2 is exiting. We do NOT wait for each sample to
complete before injecting the next.

MULTI-CHANNEL MODE (num_channels > 1):
When num_channels is 2 (I/Q) or 3 (tri-channel), the source block expects
interleaved input and tags each sample with a channel-specific entry address.
This allows a demux block to route samples to different processing paths.

Channel entry addresses (from CHANNEL_ENTRY_ADDRESSES):
  - Channel 0 (I): R1
  - Channel 1 (Q): R11
  - Channel 2:     R21

IMPORTANT: This block triggers device initialization on first work() call,
since GNURadio doesn't call start() on blocks with no signal connections
(like the kyttar.device block).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr
from typing import Optional, Any

# SOCKET-ONLY: this block streams a burst to a placeKYT-hosted chip over a TCP
# socket (server-batch mode). It imports gnuradio + numpy + socket ONLY. It does
# NOT import gr_kyttar or simkyt and does NOT place/route/build a chip in the GR
# process. When server_port <= 0 it degrades to a harmless pass-through that
# produces no chip output and prints a one-line hint (it never crashes, never
# spawns a thread, never touches the heavy libraries).


class source(gr.sync_block):
    """
    Kyttar Source - Entry point into Kyttar chip via INPUT PORT.

    Data enters the chip ONLY through the configured input port.
    There is no other way to get data into the chip.

    This block implements TRUE PIPELINED operation:
    - All input samples are queued at once
    - Simulation runs until outputs are available
    - Multiple samples can be in-flight simultaneously

    Parameters:
        device_id: ID of the kyttar.device to use
        port_name: Name of the chip input port (e.g., 'x16_in')
        num_channels: Number of channels (1=simple, 2=I/Q, 3=tri-channel)
            - 1: All samples go to same entry address (default)
            - 2: Interleaved I/Q - alternates between R1 and R11
            - 3: Tri-channel - cycles through R1, R11, R21
    """

    # Channel entry addresses (must match CHANNEL_ENTRY_ADDRESSES in placement)
    CHANNEL_ENTRY_ADDRESSES = [1, 11, 21]  # R1, R11, R21

    def __init__(
        self,
        device_id: str = "kyttar_0",
        port_name: str = "x16_in",
        num_channels: int = 1,
        server_host: str = "",
        server_port: int = 0,
        complex_in: bool = False,
        burst_len: int = 0,
    ):
        # SERVER-BATCH MODE (server_port > 0): drive a placeKYT-hosted chip via ONE
        # process_batch RPC instead of building/owning a local chip. The input is
        # the whole complex burst; the matching kyttar_sink (same device_id) drains
        # the recovered words. This is the GRC-first demo path — the REAL DSP blocks
        # stay in the GR graph (so the flowgraph imports into placeKYT) while the
        # actual DSP runs on the hosted chip. `complex_in` accepts the I/Q burst.
        self._server_mode = int(server_port) > 0
        # In server mode the INPUT is the complex I/Q burst (the session carries it
        # to the chip), but the OUTPUT to the marker chain is FLOAT — the real DSP
        # blocks (costas/gardner/slicer) are float-stream markers, so the chain
        # source→costas→…→sink type-checks. The marker-chain data is unused; the
        # burst travels via the batch session, not the GR stream.
        in_dtype = np.complex64 if (complex_in or self._server_mode) else np.float32
        out_dtype = np.float32 if self._server_mode else in_dtype
        gr.sync_block.__init__(
            self,
            name="Kyttar Source",
            in_sig=[in_dtype],
            out_sig=[out_dtype],  # Pass through for GRC connection visualization
        )

        if num_channels < 1 or num_channels > 3:
            raise ValueError("num_channels must be 1, 2, or 3")

        self._device_id = device_id
        self._port_name = port_name
        self._num_channels = num_channels
        self._server_host = str(server_host) or "127.0.0.1"
        self._server_port = int(server_port)
        self._burst_len = int(burst_len)
        self._inbuf = []          # server mode: accumulated complex burst
        self._dispatched = False

        if self._server_mode:
            print(f"[kyttar.source] SERVER-BATCH mode -> "
                  f"{self._server_host}:{self._server_port} (device '{device_id}', "
                  f"port '{port_name}')")
            return

        # NO server configured. This block requires server-batch mode. Degrade to a
        # harmless pass-through (no output produced into the chip; the GR stream is
        # just forwarded). Do NOT import gr_kyttar/simkyt, place/route, or crash.
        print("[kyttar.source: set server_port to the port placeKYT prints under "
              "'Run as GNURadio Server']")

    def start(self) -> bool:
        """Called when flowgraph starts. Import-light; never touches heavy libs."""
        if self._server_mode:
            print(f"[kyttar.source] Starting (server-batch), device='{self._device_id}', "
                  f"port='{self._port_name}'")
        return True

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        if self._server_mode:
            # Flush the burst if it never hit burst_len (e.g. burst_len=0).
            # Degrade gracefully if the server is absent/refused — never raise.
            try:
                self._server_dispatch()
            except Exception as e:  # noqa: BLE001
                print(f"[kyttar.source] server dispatch failed (degrading, no output): {e}",
                      flush=True)
        return True

    # --- server-batch mode ---------------------------------------------------
    def _server_dispatch(self):
        """Send the accumulated complex burst to the placeKYT SimServer in ONE
        process_batch RPC; stash the recovered words for the matching sink."""
        if self._dispatched or not self._inbuf:
            return
        from ._batch_session import get_session
        sess = get_session(self._device_id)
        out = sess.dispatch(self._server_host, self._server_port, self._inbuf,
                            in_port=self._port_name)
        self._dispatched = True
        print(f"[kyttar.source] SERVER-BATCH: sent {len(self._inbuf)} samples "
              f"-> {len(out)} recovered (one process_batch RPC)", flush=True)

    def work(self, input_items, output_items):
        """Process samples - write to chip input port with TRUE PIPELINING.

        Now that the simulator implements proper 4-phase handshake protocol,
        we can queue all samples at once. The simulator will:
        1. Check if target cell is busy before injecting
        2. Wait (re-schedule) if cell is processing a previous sample
        3. Only proceed when cell completes and sends ACK

        This provides natural backpressure - samples flow through the pipeline
        at the rate the cells can process them, with multiple samples in-flight.

        Multi-channel mode:
        When num_channels > 1, samples are tagged with alternating entry addresses
        so a demux block can route them to different processing paths.
        """
        inp = input_items[0]
        out = output_items[0]
        n_samples = len(inp)

        # === SERVER-BATCH MODE ===
        # Accumulate the whole complex burst; dispatch it to the placeKYT server in
        # ONE process_batch RPC when burst_len is reached (or at stop()). The sink
        # (same device_id) drains the recovered words. No local chip is touched. The
        # float OUTPUT carries the input magnitude only (marker-chain viz; unused).
        if self._server_mode:
            out[:] = np.real(np.asarray(inp, dtype=np.complex64)).astype(np.float32)
            if not self._dispatched:
                self._inbuf.extend(np.asarray(inp, dtype=np.complex64).tolist())
                if self._burst_len > 0 and len(self._inbuf) >= self._burst_len:
                    del self._inbuf[self._burst_len:]
                    # If the server is absent/refused, degrade gracefully — log once,
                    # mark dispatched so we stop retrying, never raise into the
                    # GR scheduler thread.
                    try:
                        self._server_dispatch()
                    except Exception as e:  # noqa: BLE001
                        self._dispatched = True
                        print(f"[kyttar.source] server dispatch failed (degrading, "
                              f"no output): {e}", flush=True)
            return n_samples

        # NO server configured: harmless pass-through. No chip, no heavy imports.
        out[:] = inp.real.astype(np.float32) if np.iscomplexobj(inp) else inp[:]
        return n_samples
