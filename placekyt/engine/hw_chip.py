# SPDX-License-Identifier: GPL-3.0-or-later
"""HwChip — a real-hardware drop-in for ``simkyt.Chip`` behind ``SimServer._chip``.

The seam: ``SimServer`` drives a chip object
through ~13 methods. Today that object is a ``simkyt.Chip`` (the simulator). ``HwChip``
exposes the SAME method surface but routes the WRITE/DATA/JUMP words over USB to the dev-kit
FPGA board (via :class:`~placekyt.engine.hw_transport.FX3Transport`). SimServer, the GR wire
protocol, the injection logic, and the ``.grc`` flowgraphs are all unchanged — ``set_chip``
swaps the backend.

**The single biggest semantic shift:** the sim is event-driven
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

First-bring-up scope: single logical chip, stateless GAIN demo (per-batch reset is
a no-op), host-monotonic timestamps, run-only. Stateful receivers, multi-chip, and per-batch
backdoor reset are explicit LATER phases.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .hw_transport import FX3Transport, HwTransportError

# ---- Kyttar word encoding (per the ISA word encoding) ----
# [15:12]=opcode, [11]=RSV, [10]=CFG(write only), [9:5]=HOP_CNT, [4:0]=DEST/addr.
_OP_WRITE = 0x6
_OP_JUMP = 0x7


def _encode_write(hop_cnt: int, addr: int) -> int:
    return (_OP_WRITE << 12) | ((hop_cnt & 0x1F) << 5) | (addr & 0x1F)


def _encode_jump(hop_cnt: int, addr: int) -> int:
    return (_OP_JUMP << 12) | ((hop_cnt & 0x1F) << 5) | (addr & 0x1F)


def _as_i16(word: int) -> int:
    """Reinterpret an unsigned 16-bit word as signed int16 (Q15 values are signed)."""
    w = word & 0xFFFF
    return w - 0x10000 if w & 0x8000 else w


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
        # output words read back so far, as (value, dest) pairs, FIFO.
        self._out_words: List[Tuple[int, int]] = []
        # a split WRITE word carried across a bulk-read boundary (see drain()).
        self._read_tail: List[int] = []
        self._connected = False

    # ------------------------------------------------------------- connection
    def connect(self, **_ignored) -> None:
        """Open the USB link and verify the board is PRESENT and its firmware responds.

        The connection check is deliberately shallow: it only confirms the board exists
        and the FX3 answers a control transfer (VR 0x64). It does NOT push data through
        the array — a data round-trip is not a valid liveness test because most real DSP
        designs (receivers with AGC/lock loops, decimators, anything with a startup
        transient) swallow input until steady state and would falsely report "not
        connected." Whether the loaded design produces output is a runtime concern, not
        a connection concern. (``**_ignored`` accepts a legacy ``verify_dataplane`` kw.)
        """
        self._t.connect()
        if not self._t.ping():
            self._t.close()
            raise HwChipError(
                "board not responding (FX3 firmware alive? board plugged in / flashed?)"
            )
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected and self._t.connected

    # ------------------------------------------------------------- programming
    def load_bitstream_physical(self, bitstream: Sequence[int]) -> None:
        """Program the array: stream the ChipBuild WRITE/DATA/JUMP words to the FPGA.

        These configure the array (the FPGA relays them in). We reset first so the array
        starts from a known state (reset() = global board reset + reprogram).
        """
        self._require_connected()
        self._t.reset(leave=False)
        self._t.send_words([w & 0xFFFF for w in bitstream])
        # A program stream is WRITE/DATA pairs with no trailing JUMP-to-us, so there is
        # nothing to read back; drain any stray words the board may have echoed.
        self._drain_stray()

    # ------------------------------------------------------------- injection
    # DECOUPLED write/read (the ping-pong contract): inject/jump ONLY buffer + send words —
    # they NEVER block waiting for this sample's output. The output is drained
    # independently by drain()/read_port*, so writing and reading run concurrently.
    # This is what unlocks throughput: batch many WRITE/DATA/JUMP into few USB writes,
    # and drain continuously so the board's output FIFO never fills. (The old code did
    # one send + a blocking poll-read PER JUMP — a per-sample USB round-trip that
    # capped the rate at a few thousand samples/s.)
    def inject_data_physical(self, data, target_hop_cnt: int, target_addr: int) -> None:
        """Buffer a DATA word as a WRITE(hop,addr)+DATA pair (sent on flush)."""
        self._require_connected()
        for v in data:
            self._pending.append(_encode_write(target_hop_cnt, target_addr))
            self._pending.append(int(v) & 0xFFFF)

    def inject_jump_physical(self, target_hop_cnt: int, entry_addr: int) -> None:
        """Append the triggering JUMP and FLUSH the buffered words over USB. Does NOT
        wait for output — the recovered words are read later by drain()/read_port*."""
        self._require_connected()
        self._pending.append(_encode_jump(target_hop_cnt, entry_addr))
        self._flush()

    def stream_samples(self, samples, target_hop_cnt: int, target_addr: int,
                       entry_addr: int, with_tags: bool = False):
        """FAST-PATH for a whole batch of REAL (single-operand) samples. Encodes ALL
        samples' WRITE/DATA/JUMP words and sends them in a few big USB writes while
        draining output concurrently — the batched, decoupled path that hits the
        board's real throughput (~1M samp/s) instead of one USB round-trip per sample.

        The WRITE/DATA carries the sample tagged by (target_hop_cnt, target_addr) — this
        is the intra-chip input tag routing the sample to a specific cell. The JUMP
        (target_hop_cnt, entry_addr) triggers that cell. For the multiplexed two-cell
        chip, a given (target_addr, entry_addr) selects one cell; its output comes back
        with that cell's own tag.

        with_tags=False (default): returns recovered signed-int16 values in order.
        with_tags=True: returns (value, out_tag) pairs so the caller can demux by the
        cell's output tag (the shared-output-port multiplex case)."""
        import time as _time
        self._require_connected()
        wr = _encode_write(target_hop_cnt, target_addr)
        jmp = _encode_jump(target_hop_cnt, entry_addr)
        n = len(samples)
        # chunk so each USB write + its drain stays well under the ~65k output FIFO
        # (3 words in + 3 words out per sample => keep 3*CHUNK < ~60000).
        CHUNK = 4000
        i = 0
        while i < n:
            m = min(CHUNK, n - i)
            words: List[int] = []
            for v in samples[i:i + m]:
                words.append(wr)
                words.append(int(v) & 0xFFFF)
                words.append(jmp)
            self._t.send_words(words)
            i += m
            # Drain THIS chunk's output fully: expect `m` new DATA words. Keep reading
            # until we have them or the board goes idle for a stretch (output lags
            # input, so an empty read mid-chunk is normal — only give up after
            # several consecutive empties past the expected count).
            target = i  # total DATA words expected so far == samples sent so far
            idle = 0
            deadline = _time.monotonic() + 2.0
            while len(self._out_words) < target and _time.monotonic() < deadline:
                got = self.drain(timeout_ms=30)
                idle = 0 if got else idle + 1
                if idle >= 8:  # ~240ms of silence with results missing → stop
                    break
        # collect all buffered outputs in order
        if with_tags:
            out = [(_as_i16(v), d) for (v, d) in self._out_words]
        else:
            out = [_as_i16(v) for (v, _d) in self._out_words]
        self._out_words.clear()
        return out

    def _flush(self) -> None:
        """Send all pending words in ONE USB write (batched), then opportunistically
        drain whatever output is already available (non-blocking-ish)."""
        if not self._pending:
            return
        try:
            self._t.send_words(self._pending)
        except HwTransportError as exc:
            self._pending.clear()
            raise HwChipError(f"burst flush failed: {exc}") from exc
        self._pending.clear()
        # opportunistic drain so the board's output FIFO doesn't back up mid-stream
        self.drain(timeout_ms=self._DRAIN_POLL_MS)

    # Short poll used for opportunistic draining after a flush (keep output moving so
    # the ~65k-word FIFO never fills). read_port* also call drain() to collect results.
    _DRAIN_POLL_MS = 5

    def drain(self, timeout_ms: Optional[int] = None) -> int:
        """Read available output words from the board and parse WRITE/DATA pairs into
        ``_out_words``. Non-blocking-ish: one bulk read (recv returns [] on timeout).
        Returns the number of new DATA words collected. A split WRITE (odd trailing
        word) is carried to the next drain via ``_read_tail``."""
        tmo = self._DRAIN_POLL_MS if timeout_ms is None else timeout_ms
        chunk = self._t.recv_words(32768, timeout_ms=tmo)
        if not chunk:
            return 0
        raw = self._read_tail + chunk
        self._read_tail = []
        i = 0
        n = 0
        while i < len(raw):
            w = raw[i]
            op = (w >> 12) & 0xF
            if op == _OP_WRITE:
                if i + 1 < len(raw):
                    self._out_words.append((raw[i + 1], w & 0x1F))
                    n += 1
                    i += 2
                else:
                    # split WRITE at the buffer boundary — hold it for the next drain
                    self._read_tail = [w]
                    break
            else:
                # JUMP header / stray word: structural framing, skip.
                i += 1
        return n

    def _drain_until(self, want: int, max_ms: int = 500) -> None:
        """Drain repeatedly until at least ``want`` output words are buffered or the
        time budget is spent (used by read paths that need N results ready)."""
        import time as _time
        deadline = _time.monotonic() + max_ms / 1000.0
        while len(self._out_words) < want and _time.monotonic() < deadline:
            if self.drain(timeout_ms=20) == 0 and self._out_words:
                break

    # --------------------------------------------------------------- run (no-op)
    def run(self, max_events: Optional[int] = None) -> None:
        """NO-OP on hardware. The chip is free-running; handshake pacing replaces run().

        (Kept so SimServer's inject→run→read pattern calls through unchanged.)
        """
        return None

    def run_until_output(self, port_name, count: int, max_events: Optional[int] = None):
        """Drain until ``count`` output words are buffered (or the time budget lapses)."""
        self._drain_until(int(count))
        return None

    # ------------------------------------------------------------------ reads
    # Each read drains available output first, then returns+clears the buffer. Draining
    # is cheap (one bulk read) and keeps the output FIFO flowing.
    def read_port_words_timed(self, port_name) -> List[Tuple[int, int, float]]:
        """Drain output as (value, dest, t) with a host-monotonic timestamp for ordering."""
        self.drain()
        out = [(v, d, time.monotonic()) for (v, d) in self._out_words]
        self._out_words.clear()
        return out

    def read_port_i16(self, port_name) -> List[int]:
        """Drain output values as SIGNED int16 (tag ignored). The words come off the
        wire as unsigned 16-bit; reinterpret >0x7FFF as negative so a Q15 negative
        (e.g. 0xE200) reads as -7680, not 57856. Matches simkyt.Chip.read_port_i16."""
        self.drain()
        out = [_as_i16(v) for (v, _d) in self._out_words]
        self._out_words.clear()
        return out

    def read_port(self, port_name) -> List[float]:
        """Drain output values as Q15-CONVERTED float32, matching simkyt.Chip.read_port
        ('with Q15 conversion'). The SimServer's non-raw path does float(v) on this and
        expects a scaled fraction — returning raw ints here gave garbage (0x2000 -> 8192
        instead of 0.25). So convert here: signed_word / 32768.0."""
        self.drain()
        out = [_as_i16(v) / 32768.0 for (v, _d) in self._out_words]
        self._out_words.clear()
        return out

    def output_available(self, port_name) -> int:
        if not self._out_words:
            self.drain()
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
        self._read_tail.clear()
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
