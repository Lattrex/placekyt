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
        stream_id: str = "",
        pipelined: bool = False,
        schedule: str = "interleaved",
        repeat: bool = False,
        output_words: str = "auto",
    ):
        # SERVER-BATCH MODE (server_port > 0): drive a placeKYT-hosted chip via ONE
        # process_batch RPC instead of building/owning a local chip. The input is
        # the whole complex burst; the matching kyttar_sink (same device_id) drains
        # the recovered words. This is the GRC-first demo path — the REAL DSP blocks
        # stay in the GR graph (so the flowgraph imports into placeKYT) while the
        # actual DSP runs on the hosted chip. `complex_in` accepts the I/Q burst.
        self._server_mode = int(server_port) > 0
        # INPUT type is driven by complex_in ALONE — NOT by server mode. A
        # server-mode source can be float (e.g. a gain demo) or complex (an I/Q
        # receiver); the user states which via complex_in. This io_signature is
        # what GRC's runtime connect() checks for item size, so it MUST match the
        # .block.yml port type (the Input Type enum). Forcing complex whenever a
        # server port was set made the float gain stimulus fail to connect
        # ("itemsize mismatch: ... using 4, Kyttar Source ... using 8").
        # OUTPUT now MIRRORS the input type (complex in -> complex out) so the
        # source connects to the single-complex marker chain (matched filter etc.)
        # with no dtype mismatch. The burst still travels via the batch session;
        # the GR stream is cosmetic (marker-chain data is unused).
        in_dtype = np.complex64 if complex_in else np.float32
        out_dtype = in_dtype
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
        self._complex_in = bool(complex_in)
        self._server_host = str(server_host) or "127.0.0.1"
        self._server_port = int(server_port)
        self._burst_len = int(burst_len)
        # SHARED-INPUT-PORT DUPLEX: when two sources share one chip device (the
        # full-duplex modem), each names a distinct stream_id ("tx"/"rx") so its
        # burst is injected at its own block and its sink (same stream_id) drains
        # only ITS recovered words. Empty ⇒ today's single-stream behavior.
        self._stream_id = str(stream_id or "")
        # OUTPUT WORD ENCODING of the recovered stream:
        #   "auto" (legacy): raw int16 words for a COMPLEX-input chain (the
        #     bit-packing receiver convention — a slicer's decoded bit lives in
        #     the word LSB, which Q15 scaling would crush), Q15-scaled floats
        #     for a real-input chain (gain/FIR values).
        #   "q15": ALWAYS Q15-scaled floats — for a complex-input chain whose
        #     output is a Q15 VALUE, not packed bits (the LMS equalizer's
        #     equalized I/Q, the CORDIC magnitude/phase). Without this, a
        #     value-output complex chain displays raw +-30000 "floats" (the
        #     missing-constellation report).
        #   "raw": ALWAYS raw int16 words.
        _ow = str(output_words or "auto").lower()
        if _ow not in ("auto", "q15", "raw"):
            raise ValueError(f"output_words must be auto/q15/raw (got {_ow!r})")
        self._raw_out = (bool(complex_in) if _ow == "auto"
                         else (_ow == "raw"))
        # SCHEDULE (timing-analysis knob, no design change): how the two duplex
        # streams are driven on the shared input port.
        #   "interleaved" (default): TX + RX round-robin sample-by-sample, so the
        #     chains contend for the port and throttle each other (the real
        #     full-duplex rate — this is the honest steady-state number).
        #   "sequential"/"simplex": each stream's WHOLE burst runs before the next,
        #     so each direction is measured ALONE at its compute-bound ceiling.
        # A GRC-settable dropdown on this block (see kyttar_source.block.yml). Set it
        # before Run; the .kyt design is untouched. Only meaningful with a stream_id
        # (duplex); a single-stream source ignores it. Two duplex sources both carry
        # this param — they should agree, and the rendezvous takes whichever names a
        # NON-default value so setting it on either source (or both) works.
        self._schedule = str(schedule or "interleaved").lower()
        # FULL-SPEED: drive the whole burst SATURATED on the hosted chip (queue the
        # word stream + run to completion) rather than per-sample-to-quiescence. Only
        # safe for a saturation-tolerant (point-to-point-routed) chip design.
        self._pipelined = bool(pipelined)
        # REPEAT (the live-demo burst loop): after the sink drains a burst's
        # result, re-arm and dispatch the NEXT burst_len samples — the flowgraph
        # becomes a continuous burst loop, so scopes refresh every burst and a
        # LIVE slider change (see gain.set_gain) shows up one burst later
        # WITHIN the same Run. Off (default) = the classic one-burst-per-Run.
        # Pair repeat sources with server_repeat sinks (or rely on the sink's
        # own repeat detection) so the graph does not end between bursts.
        self._repeat = bool(repeat)
        self._dispatch_failed = False
        self._inbuf = []          # server mode: accumulated complex burst
        self._dispatched = False
        # streaming (hardware) vs batch (sim) is decided by the SERVER at start();
        # None until queried. No user-facing switch — see start().
        self._streaming = None
        # max samples shipped per streaming work() call. The server's HW fast-path
        # batches a whole chunk's WRITE/DATA/JUMP into a few big USB writes (~500k+
        # samp/s), so a big chunk amortizes the per-RPC socket overhead. Keep it under
        # the board's ~65k-word (~21k-sample) output FIFO with margin. (Was 64, sized
        # for the OLD per-sample board loop; that throttled streaming to ~64k samp/s.)
        self._STREAM_CHUNK = 8192
        # streaming accumulator: batch small GR work() calls into one big RPC so the
        # scheduler can't fragment us into tiny per-16-sample round-trips.
        self._stream_acc = []
        self._stream_acc_n = 0

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
        """Called when flowgraph starts. Import-light; never touches heavy libs.

        Asks the server which mode it hosts: hardware -> continuous streaming, sim
        -> batch. The user does nothing; toggling Hardware Mode in placeKYT changes
        the server's backend, which this query reflects. Same .grc either way."""
        if self._server_mode:
            from ._batch_session import query_mode
            mode = query_mode(self._server_host, self._server_port)
            self._streaming = (mode == "streaming")
            print(f"[kyttar.source] Starting ({'STREAMING (hardware)' if self._streaming else 'server-batch (sim)'}), "
                  f"device='{self._device_id}', port='{self._port_name}'")
        return True

    def stop(self) -> bool:
        """Called when flowgraph stops."""
        if self._server_mode:
            if self._streaming:
                # flush any remaining accumulated stream samples
                try:
                    self._flush_stream_acc()
                except Exception as e:  # noqa: BLE001
                    print(f"[kyttar.source] final stream flush failed: {e}", flush=True)
            else:
                # Flush the burst if it never hit burst_len (e.g. burst_len=0).
                # Degrade gracefully if the server is absent/refused — never raise.
                try:
                    self._server_dispatch()
                except Exception as e:  # noqa: BLE001
                    print(f"[kyttar.source] server dispatch failed (degrading, no output): {e}",
                          flush=True)
        return True

    def _flush_stream_acc(self):
        """Send the accumulated streaming samples to the chip in ONE RPC (the server's
        HW fast-path batches them), and stash the recovered words for the sink."""
        if self._stream_acc_n <= 0:
            return
        import numpy as _np
        batch = _np.concatenate(self._stream_acc)
        self._stream_acc = []
        self._stream_acc_n = 0
        from ._batch_session import stream_chunk, get_session
        try:
            rec = stream_chunk(
                self._server_host, self._server_port, batch,
                in_port=self._port_name, complex_=self._complex_in,
                raw=self._raw_out)
        except Exception as e:  # noqa: BLE001
            print(f"[kyttar.source] stream chunk failed: {e}", flush=True)
            return
        if rec is not None and len(rec):
            get_session(self._device_id, self._stream_id).push_stream(
                _np.asarray(rec, dtype=_np.float32))

    # --- server-batch mode ---------------------------------------------------
    def _server_dispatch(self):
        """Send the accumulated complex burst to the placeKYT SimServer in ONE
        process_batch RPC; stash the recovered words for the matching sink."""
        if self._dispatched or not self._inbuf:
            return
        from ._batch_session import get_rendezvous, get_session
        sess = get_session(self._device_id, self._stream_id)
        # OUTPUT representation: a COMPLEX-input block is a bit-packing receiver
        # (Costas/Gardner/slicer) whose recovered bit lives in the word LSB — it
        # must read RAW int16 (Q15 scaling would crush the bit to ~0). A
        # REAL/float-input DSP block (gain, FIR, ...) emits a Q15 VALUE the sink
        # should rescale to float. So tie raw to complex: raw for the receiver
        # path, Q15-float for the value path.
        if self._stream_id:
            # DUPLEX / MULTI-CHIP: rendezvous with the other streams' sources so
            # ALL bursts dispatch in ONE RPC. The source carries only stream_id
            # (the LOGICAL identity) — placeKYT (the PHYSICAL side) resolves which
            # chip/port/landing that stream maps to from the placed+routed design.
            # A multi-chip design is handled server-side (its stream_targets carry
            # chip_id); the flowgraph is identical to the single-chip duplex case.
            # INTERLEAVED on the shared input port in ONE process_batch_duplex RPC
            # (not tx-whole-burst-then-rx, which put the streams ~1.7M ns apart on
            # the chip clock). The rendezvous collects each stream, dispatches once,
            # and returns THIS stream's recovered words; we stash them in the
            # per-stream session so this stream's sink drains them as usual.
            rv = get_rendezvous(self._device_id)
            out = rv.submit(self._server_host, self._server_port, self._stream_id,
                            self._inbuf, complex_=self._complex_in,
                            raw=self._raw_out, schedule=self._schedule,
                            pipelined=self._pipelined)
            with sess._cv:            # deliver to this stream's sink
                sess._result = out
                sess._seq += 1
                sess._cv.notify_all()
        else:
            out = sess.dispatch(self._server_host, self._server_port, self._inbuf,
                                in_port=self._port_name, complex=self._complex_in,
                                raw=self._raw_out, stream_id=self._stream_id,
                                pipelined=self._pipelined)
        self._dispatched = True
        print(f"[kyttar.source] SERVER-BATCH: sent {len(self._inbuf)} samples "
              f"-> {len(out)} recovered ({'duplex' if self._stream_id else 'single'} RPC)",
              flush=True)

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
            if np.iscomplexobj(out):
                out[:] = np.asarray(inp, dtype=np.complex64)
            else:
                out[:] = np.real(np.asarray(inp, dtype=np.complex64)).astype(np.float32)

            # STREAMING (hardware): ship a SMALL chunk to the chip now and stash the
            # recovered words for the matching sink. No accumulate/EOF/burst_len —
            # continuous real-time flow, paced by the board's USB handshake.
            #
            # CAP the chunk: process_batch runs one inject+trigger+read board round-trip
            # PER SAMPLE serially, so a big GR work chunk (thousands of samples) would
            # take many seconds and blow the socket timeout ('stream chunk failed:
            # timed out'). We consume at most _STREAM_CHUNK samples per work() call and
            # let GR call us again for the rest — small, fast RPCs that keep flowing.
            if self._streaming:
                # Consume THIS call's input into an accumulator and only fire a
                # stream_chunk RPC once we have a worthwhile batch (_STREAM_CHUNK) — or
                # flush a partial batch if that's all GR will give us. This prevents
                # GR's scheduler from fragmenting us into tiny 16-sample RPCs (each a
                # full socket round-trip), which made the rate bounce. Each RPC then
                # carries a big batch, hitting the server's HW fast-path throughput.
                take = n_samples
                real_in = (np.real(np.asarray(inp[:take], dtype=np.complex64))
                           .astype("<f4"))
                self._stream_acc.append(real_in)
                self._stream_acc_n += take
                if self._stream_acc_n >= self._STREAM_CHUNK:
                    self._flush_stream_acc()
                # sync_block: echo the consumed input to the marker-chain output.
                if take:
                    out[:take] = (real_in.astype(out.dtype)
                                  if not np.iscomplexobj(out)
                                  else np.asarray(inp[:take], dtype=np.complex64))
                return take

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
                        self._dispatch_failed = True
                        print(f"[kyttar.source] server dispatch failed (degrading, "
                              f"no output): {e}", flush=True)
            elif self._repeat and not self._dispatch_failed:
                # REPEAT: re-arm for the next burst once the sink drained the
                # previous generation (the session gate — never overrun a slow
                # sink). Samples arriving in between are consumed and dropped
                # (a repeating vector source keeps producing regardless).
                from ._batch_session import get_session
                if get_session(self._device_id,
                               self._stream_id).result_consumed():
                    self._dispatched = False
                    self._inbuf = []
            return n_samples

        # NO server configured: harmless pass-through. No chip, no heavy imports.
        out[:] = inp.real.astype(np.float32) if np.iscomplexobj(inp) else inp[:]
        return n_samples
