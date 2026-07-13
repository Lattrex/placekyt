# SPDX-License-Identifier: GPL-3.0-or-later
"""HwChip — a real-hardware drop-in for ``simkyt.Chip`` behind ``SimServer._chip``.

The seam (see ``dev_docs/HARDWARE_BACKEND_PLAN.md``): ``SimServer`` drives a chip object
through ~13 methods. Today that object is a ``simkyt.Chip`` (the simulator). ``HwChip``
exposes the SAME method surface but routes the WRITE/DATA/JUMP words over USB to the devkyt
FPGA board (via :class:`~placekyt.engine.hw_transport.FX3Transport`). SimServer, the GR wire
protocol, the injection logic, and the ``.grc`` flowgraphs are all unchanged — ``set_chip``
swaps the backend.

**The single biggest semantic shift (plan §4.1):** the sim is event-driven
(inject → ``run()`` → quiescence → read); the real chip is asynchronous and free-running.
So on hardware ``run()`` is a NO-OP, and correctness comes from the FPGA's handshake pacing.
Concretely, the sim's per-sample call pattern is::

    inject_data_physical(xi); run(); [inject_data_physical(xq); run();]
    inject_jump_physical();    run()
    read_port_words_timed(port)   # drain this sample's output

HwChip realizes this by BUFFERING the DATA words on each ``inject_data_physical`` (emitting a
WRITE + DATA pair per operand), and on ``inject_jump_physical`` emitting the JUMP word and
**flushing the whole burst over the bulk endpoint**, then reading the tagged output words the
FPGA returns. ``run()`` does nothing. This mirrors the fake-gain gateware exactly: WRITE/DATA
buffer state, JUMP triggers execution + the framed output burst (held-ack paced).

First-bring-up scope (plan §6): single logical chip, stateless GAIN demo (per-batch reset is
a no-op), host-monotonic timestamps, run-only. Stateful receivers, multi-chip, and per-batch
backdoor reset are explicit LATER phases.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .hw_transport import FX3Transport, HwTransportError

# ---- Kyttar word encoding (from simkyt/src/instruction/encode.rs) ----
# [15:12]=opcode, [11]=RSV, [10]=CFG(write only), [9:5]=HOP_CNT, [4:0]=DEST/addr.
_OP_WRITE = 0x6
_OP_JUMP = 0x7


def _encode_write(hop_cnt: int, addr: int) -> int:
    return (_OP_WRITE << 12) | ((hop_cnt & 0x1F) << 5) | (addr & 0x1F)


def _encode_jump(hop_cnt: int, addr: int) -> int:
    return (_OP_JUMP << 12) | ((hop_cnt & 0x1F) << 5) | (addr & 0x1F)


class HwChipError(RuntimeError):
    pass


class HwChip:
    """Hardware backend with the ``simkyt.Chip`` method surface.

    Read-back words are tagged by the FPGA's output framing: each emitted value is a
    WRITE(dest)+DATA(value) pair (Mode-2 framing). ``read_port_words_timed`` returns
    ``(value, dest, t)`` triples with a host-monotonic ``t``, matching what SimServer's
    tagged-output demux expects.
    """

    def __init__(self, transport: Optional[FX3Transport] = None) -> None:
        self._t = transport if transport is not None else FX3Transport()
        # burst assembled between JUMPs: WRITE/DATA words awaiting the triggering JUMP.
        self._pending: List[int] = []
        # output words read back from the last flush, as (value, dest) pairs, FIFO.
        self._out_words: List[Tuple[int, int]] = []
        self._connected = False

    # ------------------------------------------------------------- connection
    # A known gain round-trip used as the data-plane liveness check: send
    # WRITE/DATA(x)/JUMP, expect the framed WRITE/DATA(2x)/JUMP back. This proves the
    # FPGA app is loaded AND streaming (not merely that the FX3 enumerated). The probe
    # value is arbitrary but small so 2x can't wrap.
    _PING_VALUE = 0x0007

    def connect(self, *, verify_dataplane: bool = True) -> None:
        """Open the USB link and verify the board is live.

        Two-stage check: (1) FX3 firmware liveness (cheap VR 0x64 read); (2) a gain
        round-trip through the gateware (send a burst, confirm the 2x response). Stage 2
        is what actually proves the loaded FPGA app is running — it's gateware-aware, so
        it lives here, not in the transport. Set ``verify_dataplane=False`` to skip stage
        2 (e.g. bringing up a non-gain gateware).
        """
        self._t.connect()
        if not self._t.ping():
            self._t.close()
            raise HwChipError(
                "board did not answer control transfers (FX3 firmware not alive?)"
            )
        if verify_dataplane and not self._verify_gain_roundtrip():
            self._t.close()
            raise HwChipError(
                "board enumerated but the gain gateway did not echo a 2x burst "
                "(FPGA app not loaded/streaming, or wrong bitstream flashed?)"
            )
        self._connected = True

    def _verify_gain_roundtrip(self) -> bool:
        """Send a WRITE/DATA(v)/JUMP burst; return True iff a DATA word == 2*v comes back."""
        try:
            self._t.reset(leave=False)
            words = self._t.probe_roundtrip([
                _encode_write(30, 0), self._PING_VALUE, _encode_jump(30, 1),
            ])
        except HwTransportError:
            return False
        expected = (self._PING_VALUE * 2) & 0xFFFF
        # parse WRITE/DATA pairs, look for the gained value
        i = 0
        while i + 1 < len(words):
            if ((words[i] >> 12) & 0xF) == _OP_WRITE:
                if words[i + 1] == expected:
                    return True
                i += 2
            else:
                i += 1
        return False

    @property
    def connected(self) -> bool:
        return self._connected and self._t.connected

    # ------------------------------------------------------------- programming
    def load_bitstream_physical(self, bitstream: Sequence[int]) -> None:
        """Program the array: stream the ChipBuild WRITE/DATA/JUMP words to the FPGA.

        These configure the array (the FPGA relays them in). We reset first so the array
        starts from a known state (plan §4.7: reset() = global board reset + reprogram).
        """
        self._require_connected()
        self._t.reset(leave=False)
        self._t.send_words([w & 0xFFFF for w in bitstream])
        # A program stream is WRITE/DATA pairs with no trailing JUMP-to-us, so there is
        # nothing to read back; drain any stray words the board may have echoed.
        self._drain_stray()

    # ------------------------------------------------------------- injection
    def inject_data_physical(self, data, target_hop_cnt: int, target_addr: int) -> None:
        """Buffer a DATA word as a WRITE(hop,addr)+DATA pair (emitted on the next JUMP)."""
        self._require_connected()
        for v in data:
            self._pending.append(_encode_write(target_hop_cnt, target_addr))
            self._pending.append(int(v) & 0xFFFF)

    def inject_jump_physical(self, target_hop_cnt: int, entry_addr: int) -> None:
        """Emit the triggering JUMP and FLUSH the buffered burst over USB, then read out.

        On the real chip the JUMP triggers execution; the FPGA's held-ack sequencing paces
        the output burst back. We read whatever tagged output the FPGA returns for this
        trigger and stash it for read_port_words_timed / read_port* to drain.
        """
        self._require_connected()
        self._pending.append(_encode_jump(target_hop_cnt, entry_addr))
        try:
            self._t.send_words(self._pending)
        except HwTransportError as exc:
            self._pending.clear()
            raise HwChipError(f"burst flush failed: {exc}") from exc
        self._pending.clear()
        self._read_output_burst()

    # How many bulk reads to attempt while waiting for a burst to arrive. The FPGA
    # output is ASYNCHRONOUS: after the JUMP flush the framed burst is in flight over
    # USB and a single recv can return empty before it lands (a read-timing race — the
    # gain math is correct, the words just arrive on a later read). So we POLL: each
    # recv_words blocks up to its timeout and returns [] on timeout, so a bounded retry
    # loop waits without a wall-clock sleep. We stop as soon as the burst's terminating
    # JUMP is seen (held-ack guarantees the whole burst precedes the JUMP ack).
    _BURST_READ_RETRIES = 8
    _BURST_READ_TIMEOUT_MS = 200

    def _read_output_burst(self) -> None:
        """Poll the FPGA's framed output for the just-flushed JUMP into ``_out_words``.

        Output framing = WRITE(dest)/DATA(value) pairs terminated by a JUMP (Mode-2).
        Accumulate raw words across retries until the terminating JUMP arrives (or the
        retries are exhausted), then parse WRITE/DATA pairs out of the accumulated stream.
        A leftover unpaired WRITE (split read) is carried forward for the next parse.
        """
        raw: List[int] = []
        for _ in range(self._BURST_READ_RETRIES):
            chunk = self._t.recv_words(4096, timeout_ms=self._BURST_READ_TIMEOUT_MS)
            if chunk:
                raw.extend(chunk)
                # end-of-burst = a JUMP word appeared; the whole burst is now in.
                if any(((w >> 12) & 0xF) == _OP_JUMP for w in chunk):
                    break
        i = 0
        while i < len(raw):
            w = raw[i]
            op = (w >> 12) & 0xF
            if op == _OP_WRITE and i + 1 < len(raw):
                self._out_words.append((raw[i + 1], w & 0x1F))
                i += 2
            else:
                # JUMP header / unpaired word: structural framing, skip.
                i += 1

    # --------------------------------------------------------------- run (no-op)
    def run(self, max_events: Optional[int] = None) -> None:
        """NO-OP on hardware. The chip is free-running; handshake pacing replaces run().

        (Kept so SimServer's inject→run→read pattern calls through unchanged.)
        """
        return None

    def run_until_output(self, port_name, count: int, max_events: Optional[int] = None):
        """Poll the input's already-read output until ``count`` words are available."""
        # Output is read at flush time; this just reports readiness.
        return None

    # ------------------------------------------------------------------ reads
    def read_port_words_timed(self, port_name) -> List[Tuple[int, int, float]]:
        """Drain output as (value, dest, t) with a host-monotonic timestamp for ordering."""
        out = [(v, d, time.monotonic()) for (v, d) in self._out_words]
        self._out_words.clear()
        return out

    def read_port_i16(self, port_name) -> List[int]:
        """Drain output values as raw i16 (tag ignored)."""
        out = [v for (v, _d) in self._out_words]
        self._out_words.clear()
        return out

    def read_port(self, port_name) -> List[int]:
        """Drain output values (same as i16 on hardware; SimServer converts)."""
        return self.read_port_i16(port_name)

    def output_available(self, port_name) -> int:
        return 1 if self._out_words else 0

    # ---------------------------------------------- per-sample write paths (rare)
    def write_port(self, port_name, data) -> None:
        # Per-sample path; batch mode uses inject_*. Route through the same buffering.
        self.inject_data_physical(list(data) if _iterable(data) else [data],
                                  target_hop_cnt=30, target_addr=0)

    def write_port_tagged(self, port_name, data, entry_addresses) -> None:
        for v, a in zip(data, entry_addresses):
            self.inject_data_physical([v], target_hop_cnt=30, target_addr=int(a))

    def write_port_multi_i16(self, port_name, samples, entry_address) -> None:
        for v in samples:
            self.inject_data_physical([int(v)], target_hop_cnt=30, target_addr=0)
        self.inject_jump_physical(target_hop_cnt=30, entry_addr=int(entry_address))

    # ------------------------------------------------------------- reset / trace
    def reset(self) -> None:
        """Global board reset. Caller re-programs via load_bitstream_physical."""
        self._pending.clear()
        self._out_words.clear()
        if self._t.connected:
            self._t.reset(leave=False)

    def clear_trace(self) -> None:
        return None  # no waveform on hardware

    def get_trace(self):
        return []  # no waveform on hardware

    # ----------------------------------------- sim-panel / handshake plumbing (no-op)
    def register_panel(self, *a, **k) -> None:
        return None

    def set_port_handshake(self, *a, **k) -> None:
        return None

    def port_ack_pending(self, *a, **k) -> bool:
        return False

    # ---------------------------------------------------------------- teardown
    def close(self) -> None:
        self._connected = False
        self._t.close()

    def __enter__(self) -> "HwChip":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- internals
    def _drain_stray(self) -> None:
        try:
            self._t.recv_words(256, timeout_ms=50)
        except HwTransportError:
            pass

    def _require_connected(self) -> None:
        if not self.connected:
            raise HwChipError("HwChip not connected — call connect() first")


def _iterable(x) -> bool:
    try:
        iter(x)
        return not isinstance(x, (str, bytes, int, float))
    except TypeError:
        return False
