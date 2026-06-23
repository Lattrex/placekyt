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

from .registry import get_registry

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
        self._initialized = False
        self._chip = None
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

        # Register with device (lazy - device may not exist yet)
        registry = get_registry()
        self._block_id = f"sink_{id(self)}"
        registry.register_sink(self._block_id, device_id, port_name)

        print(f"[kyttar.sink] Registered with device '{device_id}', port '{port_name}', "
              f"channels={num_channels}")

    def start(self) -> bool:
        """Called when flowgraph starts."""
        print(f"[kyttar.sink] Starting, device='{self._device_id}', port='{self._port_name}', "
              f"channels={self._num_channels}")
        # Defer initialization to work() since device may not be ready yet
        # GNURadio calls start() on blocks in undefined order
        self._initialized = False
        self._chip = None
        # Clear any leftover samples from previous runs
        if self._num_channels == 1:
            self._sample_buffer = np.array([], dtype=np.float32)
        else:
            self._channel_buffers = [deque() for _ in range(self._num_channels)]
            self._paired_output = deque()
        return True

    def _try_initialize(self) -> bool:
        """Try to initialize connection to the device."""
        registry = get_registry()
        device = registry.get_device(self._device_id)

        if device is None:
            return False

        if not device.is_initialized:
            return False

        self._chip = device.chip

        if self._chip is None:
            return False

        # Verify the port exists and is an output port
        if self._port_name not in self._chip.output_port_names:
            print(f"[kyttar.sink] ERROR: '{self._port_name}' is not a valid output port")
            print(f"[kyttar.sink] Available output ports: {self._chip.output_port_names}")
            return False

        print(f"[kyttar.sink] Initialized, reading from output port '{self._port_name}'")
        return True

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        print("[kyttar.sink] Stopping")
        self._initialized = False
        self._chip = None
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
            sess = get_session(self._device_id)
            if self._server_result is None:
                r = sess.take_result(timeout=0.05)
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

        # Lazy initialization - try to connect to device if not yet done
        if not self._initialized:
            self._initialized = self._try_initialize()
            if not self._initialized:
                # Device not ready yet, output zeros (not passthrough!)
                out[:] = 0.0
                return n_samples

        if self._chip is None:
            out[:] = 0.0
            return n_samples

        if self._num_channels == 1:
            return self._work_single_channel(inp, out, n_samples)
        else:
            return self._work_multi_channel(inp, out, n_samples)

    def _work_single_channel(self, inp, out, n_samples: int) -> int:
        """Single-channel mode: simple FIFO buffering."""
        # Check how many samples we need beyond what's already buffered
        buffered = len(self._sample_buffer)
        needed = n_samples - buffered

        if needed > 0:
            # Need more samples from simulator
            # First check what's available in the chip's output port
            available = self._chip.output_available(self._port_name)

            if available < needed:
                # Run simulation in small batches to avoid event limit issues.
                # Each sample through a multi-cell pipeline requires ~2000-5000 events.
                # Process in batches of 32 samples max to keep simulation responsive.
                batch_size = min(32, needed)
                max_batches = (needed + batch_size - 1) // batch_size

                for _ in range(max_batches):
                    available = self._chip.output_available(self._port_name)
                    if available >= needed:
                        break
                    # Use higher event limit per sample for complex pipelines
                    self._chip.run_until_output(
                        self._port_name,
                        count=min(batch_size, needed - available),
                        max_events=batch_size * 5000  # Higher limit for multi-cell chains
                    )

            # Read ALL available samples from chip (read_port drains the buffer)
            available = self._chip.output_available(self._port_name)
            if available > 0:
                new_samples = self._chip.read_port(self._port_name)
                # Append to our buffer - NO SAMPLES LOST
                self._sample_buffer = np.concatenate([self._sample_buffer, new_samples])

        # Now serve samples from our buffer
        buffered = len(self._sample_buffer)

        # Debug: track sample counts
        if not hasattr(self, '_total_in'):
            self._total_in = 0
            self._total_out = 0
            self._debug_count = 0
        self._total_in += n_samples

        if buffered >= n_samples:
            # We have enough - take exactly what we need
            out[:n_samples] = self._sample_buffer[:n_samples]
            # Keep the rest for next time
            self._sample_buffer = self._sample_buffer[n_samples:]
            self._total_out += n_samples
        elif buffered > 0:
            # Partial data - output what we have, pad with zeros
            out[:buffered] = self._sample_buffer[:buffered]
            out[buffered:] = 0.0
            self._sample_buffer = np.array([], dtype=np.float32)
            self._total_out += buffered
        else:
            # No data at all
            out[:] = 0.0

        # Debug print every 100 calls
        self._debug_count += 1
        if self._debug_count <= 5 or self._debug_count % 100 == 0:
            print(f"[kyttar.sink] work#{self._debug_count}: in={n_samples}, buffered={buffered}, "
                  f"total_in={self._total_in}, total_out={self._total_out}, "
                  f"out[0:3]={out[:3] if len(out) >= 3 else out[:]}")

        return n_samples

    def _work_multi_channel(self, inp, out, n_samples: int) -> int:
        """Multi-channel mode: per-channel FIFOs with pairing.

        NOTE: This currently requires the Rust simulator to provide
        channel tags with each sample (read_port_with_channels).
        Until that API is available, this falls back to interleaved
        assumption where samples arrive alternating I, Q, I, Q...

        Future enhancement: Use read_port_with_channels() when available
        to get (sample, channel) tuples and route to per-channel FIFOs.
        """
        # Debug: track sample counts
        if not hasattr(self, '_total_in'):
            self._total_in = 0
            self._total_out = 0
            self._debug_count = 0

        # Check how many paired samples we need
        paired_available = len(self._paired_output)
        needed = n_samples - paired_available

        if needed > 0:
            # Need more samples from simulator
            # For multi-channel, we need num_channels samples to form one output group
            samples_needed = needed * self._num_channels

            available = self._chip.output_available(self._port_name)

            # Debug: show simulation state
            if not hasattr(self, '_sim_debug_count'):
                self._sim_debug_count = 0
            if self._sim_debug_count < 3:
                self._sim_debug_count += 1
                pending = self._chip.debug_port_pending(self._port_name.replace('out', 'in'))
                print(f"[kyttar.sink] DEBUG sim: needed={samples_needed}, avail={available}, pending_in={pending}")

            if available < samples_needed:
                result = self._chip.run_until_output(
                    self._port_name,
                    count=samples_needed,
                    max_events=samples_needed * 500
                )
                if self._sim_debug_count <= 3:
                    print(f"[kyttar.sink] DEBUG run_until_output: {result}")

            # Read available samples with channel tags
            available = self._chip.output_available(self._port_name)
            if available > 0:
                # Use read_port_with_channels() to get (sample, address) tuples
                # The address is from the WRITE instruction's dest:
                # - addr=1 for I channel (from R1 entry point)
                # - addr=11 for Q channel (from R11 entry point)
                samples_with_channels = self._chip.read_port_with_channels(self._port_name)

                # Debug: show first few addresses
                if not hasattr(self, '_addr_debug_count'):
                    self._addr_debug_count = 0
                if self._addr_debug_count < 3 and len(samples_with_channels) > 0:
                    self._addr_debug_count += 1
                    addrs = [addr for _, addr in samples_with_channels[:10]]
                    print(f"[kyttar.sink] DEBUG: first 10 addrs = {addrs}")

                for sample, addr in samples_with_channels:
                    # Map address to channel index: addr=1 -> ch0, addr=11 -> ch1, addr=21 -> ch2
                    channel = self._addr_to_channel(addr)
                    if channel < self._num_channels:
                        self._channel_buffers[channel].append(sample)

                # Pair samples: output one from each channel in order
                self._pair_samples()

        # Serve paired output
        self._total_in += n_samples
        paired_available = len(self._paired_output)

        if paired_available >= n_samples:
            for i in range(n_samples):
                out[i] = self._paired_output.popleft()
            self._total_out += n_samples
        elif paired_available > 0:
            for i in range(paired_available):
                out[i] = self._paired_output.popleft()
            out[paired_available:] = 0.0
            self._total_out += paired_available
        else:
            out[:] = 0.0

        # Debug print
        self._debug_count += 1
        if self._debug_count <= 5 or self._debug_count % 100 == 0:
            ch_sizes = [len(b) for b in self._channel_buffers]
            print(f"[kyttar.sink] work#{self._debug_count}: in={n_samples}, "
                  f"ch_buffers={ch_sizes}, paired={paired_available}, "
                  f"total_in={self._total_in}, total_out={self._total_out}")

        return n_samples

    def _addr_to_channel(self, addr: int) -> int:
        """Convert WRITE address to channel index.

        The WRITE address from the mux program is passed through to the output port.
        We use the channel entry addresses to determine which channel each sample belongs to:
        - addr=1 -> channel 0 (I)
        - addr=11 -> channel 1 (Q)
        - addr=21 -> channel 2

        Returns: channel index (0, 1, or 2), or -1 if not recognized
        """
        # Map known entry addresses to channels
        for idx, entry_addr in enumerate(CHANNEL_ENTRY_ADDRESSES):
            if addr == entry_addr:
                return idx
        # Fallback: unknown address - log warning
        if not hasattr(self, '_unknown_addrs'):
            self._unknown_addrs = set()
        if addr not in self._unknown_addrs:
            self._unknown_addrs.add(addr)
            print(f"[kyttar.sink] Warning: unknown channel address {addr}")
        return -1

    def _pair_samples(self):
        """Move samples from per-channel buffers to paired output.

        For I/Q mode: outputs I0, Q0, I1, Q1, ...
        For 3-channel: outputs C0_0, C1_0, C2_0, C0_1, ...
        """
        # Check if all channels have at least one sample
        while all(len(b) > 0 for b in self._channel_buffers):
            # Take one sample from each channel in order
            for ch in range(self._num_channels):
                sample = self._channel_buffers[ch].popleft()
                self._paired_output.append(sample)
