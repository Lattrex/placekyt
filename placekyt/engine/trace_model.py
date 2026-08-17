# SPDX-License-Identifier: GPL-3.0-or-later
"""TraceModel — the debug data spine.

A single, Qt-free model built from the simulator's trace records. Every debug
view derives from it; it owns the one global time cursor. Pure data
transformation — no Qt, independently testable (engine layer, §6).

A raw trace event is a dict from ``Chip.get_trace()`` / ``MultiChipSimulation.
get_trace(chip)``::

    {"time_ns": float, "cell_id": int, "kind": str, …kind-specific…}

``ingest`` normalizes these into ordered :class:`Transaction` objects, tagged
with the chip id and the cell's (x, y) (cell_id mapped via the chip width).
Multi-chip traces are merged by ``time_ns`` into one global stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Trace kinds (mirror the engine's trace record kinds).
KIND_PORT_IN = "port_injection"
KIND_INSTR = "instr_arrival"
KIND_DATA = "data_arrival"
KIND_EXEC = "exec_tick"
KIND_OUTPUT = "output_ready"
KIND_PORT_OUT = "port_capture"
KIND_STALL = "stall"


_DECODE_CACHE: dict[int, str] = {}


def decode_word(word: int) -> str:
    """Disassemble a single instruction word to its mnemonic (e.g. 'Write …').

    Returns ''.join on failure. Used by the Transaction Log to show what an
    instruction word IS, alongside the raw hex. Memoized — the same opcode word
    recurs across many rows/refreshes, so we decode each distinct word once."""
    w = word & 0xFFFF
    cached = _DECODE_CACHE.get(w)
    if cached is not None:
        return cached
    result = ""
    try:
        import simkyt

        txt = simkyt.Program.from_words("decode", [w]).disassemble()
        # disassemble() lines look like "  00: 63C0  Write { … }" — take the
        # mnemonic + fields after the address/hex.
        for line in txt.splitlines():
            s = line.strip()
            if ":" in s:
                after = s.split(":", 1)[1].strip()       # "63C0  Write { … }"
                parts = after.split(None, 1)               # ["63C0", "Write { … }"]
                if len(parts) == 2:
                    result = parts[1]
                    break
    except Exception:  # noqa: BLE001
        result = ""
    _DECODE_CACHE[w] = result
    return result


def _to_int(v) -> int | None:
    """Parse a trace value that may be a hex string ('0x...') or an int."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return None
    return int(v)


@dataclass
class Transaction:
    """One normalized trace record."""

    time_ns: float
    chip: int
    cell: tuple[int, int]          # (x, y) on its chip
    kind: str
    face: str | None = None        # S/E/W/N where relevant
    word: int | None = None        # the instruction/data word
    data: int | None = None        # the payload value (uint16)
    pc: int | None = None          # for exec_tick
    hop_cnt: int | None = None
    dest: int | None = None        # WRITE destination register
    port: str | None = None        # for port_injection / port_capture
    detail: dict = field(default_factory=dict)  # raw extras

    @property
    def cx(self) -> int:
        return self.cell[0]

    @property
    def cy(self) -> int:
        return self.cell[1]


# Detail keys we already promote to named fields — don't duplicate in detail.
_PROMOTED = {"time_ns", "cell_id", "kind", "face", "word", "data", "pc",
             "hop_cnt", "dest", "port_name", "data_raw"}


def _median(xs: list[float]) -> float | None:
    """Median of a non-empty list (returns None for empty). Used for the settled
    inter-sample gap so a single outlier gap can't skew the reported rate."""
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _tag_sort_key(d):
    """Total order over per-stream tags, which may be None (untagged), an int
    (output tag / dest-only input), or a (hop, dest) tuple (input stream). Sorts
    None last; ints before tuples; each group by natural order — so a mixed set
    (output ints + input tuples) never raises on comparison."""
    if d is None:
        return (2,)
    if isinstance(d, tuple):
        return (1,) + tuple(int(x) for x in d)
    return (0, int(d))


def _normalize(ev: dict, chip: int, width: int) -> Transaction:
    cid = ev.get("cell_id")
    if cid is None:
        cell = (-1, -1)
    else:
        cell = (int(cid) % width, int(cid) // width)
    detail = {k: v for k, v in ev.items() if k not in _PROMOTED}
    return Transaction(
        time_ns=float(ev.get("time_ns", 0.0)),
        chip=chip,
        cell=cell,
        kind=str(ev.get("kind", "")),
        face=ev.get("face"),
        word=_to_int(ev.get("word")),
        data=_to_int(ev.get("data")),
        pc=ev.get("pc"),
        hop_cnt=ev.get("hop_cnt"),
        dest=ev.get("dest"),
        port=ev.get("port_name"),
        detail=detail,
    )


class TraceModel:
    """Ordered transaction stream + the global time cursor (§2)."""

    def __init__(self) -> None:
        self.transactions: list[Transaction] = []
        self.cursor_ns: float = 0.0
        self._by_cell: dict[tuple[int, int, int], list[Transaction]] | None = None
        # (chip, cx, cy, time_ns) -> the WRITE dest of the data_arrival that lands
        # at that cell/time. An OUTPUT port_capture event carries NO dest (simkyt
        # doesn't record it on the capture), but the co-located data_arrival that
        # FEEDS the capture does — so an output port can be demuxed by tag too.
        self._capture_dest: dict[tuple[int, int, int, float], int] | None = None

    # -- ingest ---------------------------------------------------------------

    def _invalidate(self) -> None:
        self._by_cell = None
        self._capture_dest = None

    def clear(self) -> None:
        self.transactions = []
        self.cursor_ns = 0.0
        self._invalidate()

    def ingest(self, chip: int, raw_events, width: int) -> None:
        """Add one chip's raw trace events. Re-sorts the global stream by time
        and invalidates the lazy indexes."""
        for ev in raw_events or ():
            self.transactions.append(_normalize(ev, chip, width))
        # Stable sort by time keeps same-timestamp ordering as inserted.
        self.transactions.sort(key=lambda t: t.time_ns)
        self._invalidate()

    def trim_to(self, max_events: int) -> None:
        """Keep only the most-recent ``max_events`` transactions (a scrolling
        window for live streaming). Drops the oldest; invalidates indexes."""
        if len(self.transactions) > max_events:
            self.transactions = self.transactions[-max_events:]
            self._invalidate()

    def append_live(self, chip: int, raw_events, width: int) -> None:
        """Fast append for the LIVE path: the chip's drained events are already
        time-ordered and arrive AFTER the existing window, so we normalise +
        append WITHOUT re-sorting the whole list (the full ``ingest`` sort is
        O(n log n) every refresh — too slow for a large rolling window). Only
        merges if the batch's first timestamp is >= the current last; otherwise
        falls back to a full sort to stay correct."""
        if not raw_events:
            return
        new = [_normalize(ev, chip, width) for ev in raw_events]
        new.sort(key=lambda t: t.time_ns)
        if self.transactions and new[0].time_ns < self.transactions[-1].time_ns:
            # Out of order (e.g. after a chip reset) — full re-sort to be safe.
            self.transactions.extend(new)
            self.transactions.sort(key=lambda t: t.time_ns)
        else:
            self.transactions.extend(new)
        self._invalidate()

    # -- indexes (lazy) -------------------------------------------------------

    def _ensure_index(self) -> None:
        if self._by_cell is not None:
            return
        idx: dict[tuple[int, int, int], list[Transaction]] = {}
        for t in self.transactions:
            idx.setdefault((t.chip, t.cx, t.cy), []).append(t)
        self._by_cell = idx

    def _ensure_capture_dest(self) -> None:
        """Build the ``(chip, cx, cy, time_ns) -> dest`` map from data_arrival
        events so an OUTPUT port_capture (which carries no dest of its own) can be
        tagged by the WRITE that fed it. A capture and the data_arrival that lands
        its value at the egress cell share the same cell + sim-time."""
        if self._capture_dest is not None:
            return
        idx: dict[tuple[int, int, int, float], int] = {}
        for t in self.transactions:
            if t.kind == KIND_DATA and t.dest is not None:
                idx[(t.chip, t.cx, t.cy, t.time_ns)] = int(t.dest)
        self._capture_dest = idx

    def by_cell(self, chip: int, x: int, y: int) -> list[Transaction]:
        self._ensure_index()
        return self._by_cell.get((chip, x, y), [])

    def exec_ticks(self, chip: int, x: int, y: int) -> list[Transaction]:
        """The PC trail for a cell — its exec_tick transactions, in order."""
        return [t for t in self.by_cell(chip, x, y) if t.kind == KIND_EXEC]

    def port_streams(self) -> dict[tuple[int, str], list[tuple[float, int]]]:
        """Output/input port sample streams: ``(chip, port) -> [(time_ns, value)]``.

        Captures (``port_capture``) and injections (``port_injection``) — the
        time-series the waveform viewer plots."""
        streams: dict[tuple[int, str], list[tuple[float, int]]] = {}
        for t in self.transactions:
            if t.kind in (KIND_PORT_OUT, KIND_PORT_IN) and t.port is not None:
                val = t.data if t.data is not None else 0
                streams.setdefault((t.chip, t.port), []).append((t.time_ns, val))
        return streams

    def port_streams_by_tag(
        self,
    ) -> dict[tuple[int, str, int | None], list[tuple[float, int]]]:
        """Port streams DEMULTIPLEXED by destination tag:
        ``(chip, port, tag) -> [(time_ns, value)]``.

        A chip port is a TIME-MULTIPLEXED bus — several logical streams can share
        it (e.g. an input port carries xi and xq; an output port can carry two
        tagged nets). Each port event is tagged by its stream (see ``_port_tag``):
        an INPUT injection by its target-address ``dest``, an OUTPUT capture by the
        WRITE ``dest`` of the data_arrival that fed it. Bucketing by it lets the
        waveform viewer plot ONE stream at a time instead of all interleaved words.
        A tag of ``None`` (single-stream port, untagged) buckets under ``tag=None``
        so the port still appears."""
        self._ensure_capture_dest()
        streams: dict[tuple[int, str, int | None], list[tuple[float, int]]] = {}
        # RAW-WORD (pipelined/saturated) INPUT coalescing: the pipelined drive path
        # (sim_bridge process_batch pipelined branch → queue_words_physical) injects a
        # PRE-ENCODED word stream — per complex sample: WRITE(hop,d0) → payload_xi →
        # WRITE(hop,d1) → payload_xq → JUMP. simKYT records a port_injection for EVERY
        # word and recovers (hop,dest) by DECODING each word's bits. That's fine for
        # the control words, but a bare DATA payload has no framing: a Q15 value in
        # [0x6000,0x7FFF] is bit-identical to a WRITE/JUMP opcode, so ~1 in 8 payloads
        # decodes as a spurious WRITE(hop=X,dest=Y) → a phantom "hop N" input trace.
        # simKYT CANNOT tell them apart from bits alone, and neither can the panel.
        #
        # The reliable structure is POSITION, not bits: the stream STRICTLY alternates
        # WRITE → DATA (an addressing WRITE is ALWAYS followed by exactly one data
        # payload — it can never be any other way), with a JUMP terminating each
        # packet. So run a per-port STATE MACHINE that sequences on position:
        #   • expecting WRITE: an addressing word (opcode 0x6, real target_hop) arms
        #     (hop,dest) and we switch to expecting DATA. A JUMP (opcode 0x7) is a
        #     packet terminator — skip it, stay expecting WRITE.
        #   • expecting DATA: the NEXT event IS the payload, UNCONDITIONALLY (whatever
        #     its bits/decoded-hop) — plot its value under the armed (hop,dest), then
        #     switch back to expecting WRITE.
        # This ignores the payload's own (mis)decoded hop entirely, so a 0x6xxx /
        # 0x7xxx data value can never masquerade as a control word.
        #
        # Applied ONLY to ports that use the raw-word path — detected by the presence
        # of a target_hop==0 port_injection (a framing-less payload). The PER-SAMPLE
        # path (inject_data_physical) emits ONE addressed event per operand, ALL with a
        # real target_hop and the value in ``data`` (no hop-0 events), so it's left on
        # the legacy per-event tag and plots unchanged.
        _OP_WRITE = 0x6
        _OP_JUMP = 0x7
        raw_ports: set[tuple[int, str]] = set()
        for t in self.transactions:
            if (t.kind == KIND_PORT_IN and t.port is not None
                    and not t.detail.get("target_hop")):
                raw_ports.add((t.chip, t.port))
        # Per-port machine state: armed (hop,dest) tag when expecting DATA, else None.
        armed: dict[tuple[int, str], tuple[int, int] | None] = {}
        for t in self.transactions:
            if t.kind == KIND_PORT_IN and t.port is not None:
                pkey = (t.chip, t.port)
                d = t.data if t.data is not None else 0
                if pkey not in raw_ports:
                    # Per-sample / untagged port: legacy per-event tag, unchanged.
                    streams.setdefault((t.chip, t.port, self._port_tag(t)), []).append(
                        (t.time_ns, d))
                    continue
                pending = armed.get(pkey)
                if pending is not None:
                    # Expecting DATA: this event IS the payload, no matter its bits.
                    # Plot its value under the armed WRITE's (hop,dest), then re-expect
                    # a WRITE.
                    hop, dest = pending
                    streams.setdefault((t.chip, t.port, (hop, dest)), []).append(
                        (t.time_ns, d))
                    armed[pkey] = None
                    continue
                # Expecting a control word. Classify by opcode nibble (control words
                # ARE real opcodes here — the ambiguous case is only the data slot,
                # which is handled above).
                op = (int(d) >> 12) & 0xF
                th = t.detail.get("target_hop")
                if op == _OP_WRITE and th:
                    # Addressing WRITE: arm (hop,dest), switch to expecting DATA.
                    armed[pkey] = (int(th), int(t.dest) if t.dest is not None else 0)
                elif op == _OP_JUMP:
                    pass  # packet terminator — skip, keep expecting a WRITE.
                else:
                    # Unexpected word while expecting a control word (shouldn't happen
                    # for a well-formed packet). Plot under legacy tag rather than drop.
                    streams.setdefault((t.chip, t.port, self._port_tag(t)), []).append(
                        (t.time_ns, d))
            elif t.kind == KIND_PORT_OUT and t.port is not None:
                val = t.data if t.data is not None else 0
                streams.setdefault((t.chip, t.port, self._port_tag(t)), []).append(
                    (t.time_ns, val))
        return streams

    def _port_tag(self, t):
        """The per-stream tag of a port event.

        INPUT injection: keyed by ``(target_hop, dest)`` — BOTH the hop count (which
        cell along the shared input port the word routes to) and the dest (which
        register) together determine stream identity, exactly as the hardware
        routes it. Two streams sharing one input port can collide on ``dest`` alone
        (e.g. rx-xq @hop22/addr1 vs tx-bit @hop29/addr1) but are DISTINCT by hop.
        When the hop isn't recorded (older trace), falls back to ``dest`` alone.
        Else the JUMP ``entry_address`` that triggered the inject.

        OUTPUT capture: the capture event itself carries NO dest (simkyt doesn't
        record it), so we look up the WRITE ``dest`` of the co-located
        data_arrival (same cell + sim-time) that landed the value at the egress
        cell — that IS the output net's tag (e.g. RX-bits vs TX-passband on one
        shared output port). ``None`` when no tag is recoverable — a single
        untagged stream."""
        if t.kind == KIND_PORT_IN and t.dest is not None:
            hop = t.detail.get("target_hop")
            if hop is not None:
                return (int(hop), int(t.dest))   # (hop, dest) = stream identity
            return t.dest                        # older trace: dest only
        if t.dest is not None:
            return t.dest
        if t.kind == KIND_PORT_OUT:
            self._ensure_capture_dest()
            d = self._capture_dest.get((t.chip, t.cx, t.cy, t.time_ns))
            if d is not None:
                return d
        ea = t.detail.get("entry_address")
        return int(ea) if ea is not None else None

    def port_tags(self, chip: int, port: str) -> list[int | None]:
        """The distinct tags the trace shows on ``(chip, port)``, sorted (a
        ``None`` tag — untagged single-stream — sorts last). Drives the channel
        picker when a port is dragged to the waveform viewer."""
        tags = {key[2] for key in self.port_streams_by_tag()
                if key[0] == chip and key[1] == port}
        return sorted(tags, key=_tag_sort_key)

    def _stream_directions(self) -> dict[tuple[int, str, int | None], str]:
        """``(chip, port, tag) -> "in" | "out"`` for every stream the trace shows.

        Direction is intrinsic to the underlying event kind: a stream built from
        ``port_injection`` events is an INPUT, one built from ``port_capture`` is
        an OUTPUT. We recompute the same (chip, port, tag) keys ``port_streams_by_tag``
        produces, tagging each by the kind of event that fed it. (A given port is
        always one direction, but we key by the full stream tag so the summary can
        label each demuxed sub-stream.)"""
        self._ensure_capture_dest()
        dirs: dict[tuple[int, str, int | None], str] = {}
        for t in self.transactions:
            if t.kind == KIND_PORT_IN and t.port is not None:
                # raw-word input payloads decode their own (possibly wrong) tag;
                # the authoritative per-stream keys come from port_streams_by_tag,
                # so we only need to know THIS port is an input direction.
                dirs.setdefault((t.chip, t.port, None), "in")  # placeholder
            elif t.kind == KIND_PORT_OUT and t.port is not None:
                dirs.setdefault((t.chip, t.port, None), "out")
        # Map each real stream key to its port's direction.
        by_port: dict[tuple[int, str], str] = {}
        for (c, p, _tag), d in dirs.items():
            by_port[(c, p)] = d
        out: dict[tuple[int, str, int | None], str] = {}
        for key in self.port_streams_by_tag():
            c, p, tag = key
            out[key] = by_port.get((c, p), "out")
        return out

    def stream_summary(self) -> list[dict]:
        """Per-stream throughput summary — ONE row per logical DATA stream.

        For every ``(chip, port, tag)`` stream the trace demultiplexes (see
        ``port_streams_by_tag``), reports the SETTLED DATA sample rate: the steady-
        state rate at which real DATA samples cross that port, computed from the
        cycle-accurate inter-sample gaps (simKYT's GLS timing), NOT host wall-clock.

        The "settled" rate uses the MEDIAN inter-sample gap over the stream's
        steady state (dropping the first gap, which includes pipeline fill), so a
        one-off startup transient doesn't skew it — the same honest chip-time
        measurement ``throughput_bench.py`` reports, but per stream.

        Each row::

            {chip, port, tag, direction ("in"|"out"), samples,
             first_ns, last_ns, span_ns,
             mean_gap_ns, median_gap_ns, settled_sps, mean_sps}

        ``settled_sps``/``mean_sps`` are samples/second (Sa/s); divide by 1e6 for
        MSa/s. ``None`` where a stream has too few samples to measure a gap."""
        streams = self.port_streams_by_tag()
        dirs = self._stream_directions()
        rows: list[dict] = []
        for key, series in streams.items():
            chip, port, tag = key
            times = sorted(t for (t, _v) in series)
            n = len(times)
            first_ns = times[0] if n else None
            last_ns = times[-1] if n else None
            span = (last_ns - first_ns) if n >= 2 else None
            gaps = [times[i + 1] - times[i] for i in range(n - 1)]
            # Drop the first gap (pipeline fill) for the settled measure when we
            # have enough samples; keep it for the plain mean.
            settled_gaps = gaps[1:] if len(gaps) >= 3 else gaps
            mean_gap = (sum(gaps) / len(gaps)) if gaps else None
            median_gap = _median(settled_gaps) if settled_gaps else None
            settled_sps = (1e9 / median_gap) if median_gap else None
            mean_sps = (1e9 / mean_gap) if mean_gap else None
            rows.append({
                "chip": chip, "port": port, "tag": tag,
                "direction": dirs.get(key, "out"),
                "samples": n,
                "first_ns": first_ns, "last_ns": last_ns, "span_ns": span,
                "mean_gap_ns": mean_gap, "median_gap_ns": median_gap,
                "settled_sps": settled_sps, "mean_sps": mean_sps,
            })
        rows.sort(key=lambda r: (r["direction"] != "in", r["chip"], r["port"],
                                 _tag_sort_key(r["tag"])))
        return rows

    def io_latency_ns(self) -> float | None:
        """Chip fill latency: first INPUT DATA sample in → first OUTPUT sample out.

        The pipeline depth in chip-time (ns) — how long after the first sample
        arrives the first result emerges. Returns ``None`` if the trace lacks
        either end. This is the AGGREGATE latency; per-stream input↔output
        association would need the dataflow graph (a block-level concern), so the
        summary reports this one honest end-to-end number."""
        first_in = None
        first_out = None
        for t in self.transactions:
            if t.kind == KIND_PORT_IN and t.port is not None and first_in is None:
                first_in = t.time_ns
            elif t.kind == KIND_PORT_OUT and t.port is not None and first_out is None:
                first_out = t.time_ns
            if first_in is not None and first_out is not None:
                break
        if first_in is None or first_out is None or first_out <= first_in:
            return None
        return first_out - first_in

    def cell_busy_ns(self) -> dict[tuple[int, int, int], float]:
        """Per-cell EXECUTING chip-time: ``(chip, x, y) -> busy_ns``.

        A cell records one ``exec_tick`` per instruction it runs. The chip-time
        that instruction OCCUPIED the cell is the gap to that cell's NEXT exec_tick
        (simKYT's cycle-accurate GLS timing — the ticks are spaced by the real
        per-instruction latency). Summing those gaps gives how long the cell was
        busy computing over the run. The cell's LAST tick has no following tick to
        measure against, so it's charged the cell's own MEDIAN instruction gap (a
        neutral estimate — never zero, never the whole tail-to-end idle span). A
        cell that ran a single instruction is charged one median-of-all-cells gap.

        This is the raw signal behind ``block_utilization`` — the "where do samples
        get stuck" bottleneck view. Pure trace math, no Qt, independently testable."""
        # Gather each cell's exec-tick timestamps in order.
        by_cell: dict[tuple[int, int, int], list[float]] = {}
        for t in self.transactions:
            if t.kind == KIND_EXEC:
                by_cell.setdefault((t.chip, t.cx, t.cy), []).append(t.time_ns)

        # A global fallback gap (median of every intra-cell gap) for cells that ran
        # exactly one instruction (no self-gap to measure).
        all_gaps: list[float] = []
        per_cell_gaps: dict[tuple[int, int, int], list[float]] = {}
        for key, ts in by_cell.items():
            ts.sort()
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            per_cell_gaps[key] = gaps
            all_gaps.extend(gaps)
        global_med = _median(all_gaps) or 0.0

        busy: dict[tuple[int, int, int], float] = {}
        for key, ts in by_cell.items():
            gaps = per_cell_gaps[key]
            # Charge the final (unmeasured) instruction the cell's own median gap,
            # or the global median if this cell ran only one instruction.
            tail = _median(gaps) if gaps else global_med
            busy[key] = sum(gaps) + (tail or 0.0)
        return busy

    def block_utilization(
        self, block_lookup: dict[tuple[int, int, int], str],
        block_types: dict[str, str] | None = None,
        busy: dict[tuple[int, int, int], float] | None = None,
        span_ns: float | None = None,
    ) -> list[dict]:
        """Per-BLOCK execution utilization — the throughput-bottleneck view.

        Aggregates :meth:`cell_busy_ns` up to whole blocks: a block's busy time is
        the sum over its cells. ``util_pct`` normalizes that against the run SPAN
        (first exec_tick → last exec_tick across the whole chip) — the fraction of
        the run this block spent computing. The block with the highest busy time is
        the WORST-CASE SERIAL PATH: the one the pipeline waits on, the place to
        optimize for more throughput (for the coherent RX this is the Costas loop).

        ``block_lookup`` maps ``(chip, x, y) -> block name`` (the caller builds it
        from the placement — keeps this model project-free / Qt-free). Cells with no
        block entry (plain routing/transit cells) are bucketed under ``"(routing)"``
        so their transit cost is visible but never mistaken for a DSP block.
        ``block_types`` optionally maps block name -> block type for display.

        ``util_pct`` is the block's CRITICAL-CELL busy time (its busiest single
        cell — the longest serial path through it) as a percentage of the busiest
        DSP block's critical cell (bottleneck = 100%). It ranks by the critical
        cell, NOT the sum over cells, because a block's cells run CONCURRENTLY: the
        summed busy-time is ≈ cells × occupancy, which on a saturated array just
        crowns whichever block has the MOST cells (a wide FIR), not the true
        bottleneck. ``occupancy_pct`` (per-cell average, a true 0..100% of the run)
        is the SATURATION indicator — ~100% for every block ⇒ throughput-bound, no
        single-block stall. ``instr_per_cell`` (mean instructions per cell) is the
        size-independent work-per-sample: a feedback loop runs many, a flat filter
        few.

        Each row (sorted by critical cell busiest-first, ``rank`` 1 = bottleneck)::

            {block, type, cells, exec_count, busy_ns, crit_ns, util_pct,
             occupancy_pct, instr_per_cell, rank}
        ``busy``/``span_ns`` let a caller SUPPLY the per-cell busy map + run span
        instead of deriving them from this model's transactions — needed when the
        exec_ticks live on the hosted chip's trace but were dropped from the GUI's
        retained TraceModel (the live GRC-server path). When omitted they're
        computed here (the headless / test path).
        """
        busy = busy if busy is not None else self.cell_busy_ns()
        # Run span: first → last exec_tick over the whole chip.
        if span_ns is not None:
            span = span_ns
        else:
            exec_times = [t.time_ns for t in self.transactions if t.kind == KIND_EXEC]
            span = (max(exec_times) - min(exec_times)) if len(exec_times) >= 2 else None

        # exec counts per cell, to report per-block instruction volume. Only
        # available from this model's own exec_ticks; when a caller supplies
        # ``busy`` (ticks not in this model), exec counts read 0 (the table still
        # ranks by busy-time, which is the point).
        exec_count: dict[tuple[int, int, int], int] = {}
        for t in self.transactions:
            if t.kind == KIND_EXEC:
                k = (t.chip, t.cx, t.cy)
                exec_count[k] = exec_count.get(k, 0) + 1

        agg: dict[str, dict] = {}
        for key, ns in busy.items():
            name = block_lookup.get(key) or "(routing)"
            row = agg.setdefault(name, {"block": name, "cells": 0, "exec_count": 0,
                                        "busy_ns": 0.0, "crit_ns": 0.0})
            row["cells"] += 1
            row["busy_ns"] += ns
            row["crit_ns"] = max(row["crit_ns"], ns)   # busiest single cell
            row["exec_count"] += exec_count.get(key, 0)

        rows = list(agg.values())
        # RANK BY THE CRITICAL CELL, NOT THE SUM. A block's SUMMED busy-time is
        # ~ (cells × per-cell occupancy) — on a saturated array where every cell is
        # ~99% busy that just crowns the block with the MOST cells (the wide RRC
        # matched filter), NOT the true bottleneck. A block's cells run CONCURRENTLY,
        # so its throughput cost is its CRITICAL (busiest) cell — the longest single
        # serial path through it — which is size-independent. That's the honest
        # "where does a sample dwell longest" measure.
        rows.sort(key=lambda r: (r["crit_ns"], r["busy_ns"]), reverse=True)
        peak = max((r["crit_ns"] for r in rows if r["block"] != "(routing)"),
                   default=0.0)
        for i, r in enumerate(rows):
            r["type"] = (block_types or {}).get(r["block"], "")
            # 'util_pct' = critical-cell busy relative to the busiest block's
            # critical cell (bottleneck = 100%). NOT the summed-over-cells total.
            r["util_pct"] = (100.0 * r["crit_ns"] / peak) if peak else None
            # Per-cell average duty cycle: what fraction of the run each of the
            # block's cells was busy on average (a true 0..100% figure). When this
            # is ~100% for EVERY block, the array is saturated / throughput-bound —
            # the panel says so instead of fingering one block. Capped at 100%: the
            # per-instruction tail estimate in ``busy_ns`` can nudge a fully-busy
            # cell slightly over the run span, but a duty cycle can't exceed 100%.
            r["occupancy_pct"] = (
                min(100.0, 100.0 * r["busy_ns"] / (span * r["cells"]))
                if span and r["cells"] else None)
            # Instructions per cell — the size-independent WORK-PER-SAMPLE signal.
            # A feedback loop (Costas/Gardner) runs many instructions per sample;
            # a flat FIR runs few. Distinguishes real compute cost from mere width.
            r["instr_per_cell"] = (r["exec_count"] / r["cells"]) if r["cells"] else 0
            r["rank"] = i + 1
        return rows

    def block_bottleneck(
        self, block_cells: dict[str, list[tuple[int, int, int]]],
        block_types: dict[str, str] | None = None,
    ) -> list[dict]:
        """Per-BLOCK serial-barrier bottleneck — the HONEST "which block throttles
        the sample rate" view, from the simKYT ``stall`` (backpressure) event.

        The definition (refined): the culprit is the block where INPUT samples pile
        up (its input REQs park) but the OUTPUT is NOT held up — the downstream block
        accepts everything it produces as fast as it's made. That block MANUFACTURES
        the backpressure. A block that merely RELAYS upstream backpressure stalls on
        BOTH sides roughly equally (it can't take input because it can't hand off
        output), so it is NOT the culprit even though its input stalls a lot.

        So the metric is the **input/output stall DIFFERENTIAL**:
            barrier(block) = stall(input landing cell) − min(stall over its cells)
        The landing cell (``cells[0]``) is the input; the freest-draining cell (min
        stall over the block's cells) is the output side when the block drains freely.
        - Costas: input stalls (loop LOCKs, samples pile up) but output drains freely
          (Gardner takes every symbol) → large differential → THE bottleneck.
        - RRC matched filter: input stalls AND its handoff cell stalls (both waiting
          on Costas downstream) → differential ≈ 0 → correctly NOT the culprit, even
          though its landing cell shows large stall. THIS is what made the earlier
          landing-cell-only metric wrongly crown the matched filter.
        - Slicer / feed-forward: no stall either side → 0.

        Drive-independent: whether the matched filter's landing stalls a lot (GUI
        per-sample overlap) or zero (saturated burst), its output stalls the same
        amount, so the differential cancels it and crowns Costas either way. Requires
        SOME backpressure in the run (a saturated / overlapping drive); a strictly
        one-sample-at-a-time-to-quiescence run parks nothing and reports all zero.

        ``block_cells`` maps ``block name -> [ (chip,x,y), … ]`` with ``[0]`` the
        input LANDING cell (caller builds it from the placement — keeps this Qt-free).
        Each row, ranked by ``stall_ns`` (the differential) busiest-first
        (``rank`` 1 = bottleneck)::

            {block, type, stall_ns, in_stall_ns, out_stall_ns, max_stall_ns,
             n_stalls, barrier_pct, rank}
        ``stall_ns`` is the differential (the ranked metric); ``in_stall_ns`` /
        ``out_stall_ns`` expose the two sides for transparency.
        """
        # Median stall per cell (empty → 0).
        waits_by_cell: dict[tuple[int, int, int], list[float]] = {}
        for t in self.transactions:
            if t.kind == KIND_STALL:
                key = (t.chip, t.cx, t.cy)
                w = t.detail.get("waited_ns")
                if w is not None:
                    waits_by_cell.setdefault(key, []).append(float(w))

        def cell_stall(cell):
            return _median(waits_by_cell.get(cell, [])) or 0.0

        rows: list[dict] = []
        for name, cells in block_cells.items():
            if not cells:
                continue
            in_stall = cell_stall(cells[0])
            per_cell = [cell_stall(c) for c in cells]
            if len(cells) > 1:
                # Multi-cell block: input stall minus the freest-draining cell (the
                # output side when the block drains freely). A relayer stalls on both
                # sides → differential ~0; a manufacturer stalls only on input.
                out_stall = min(per_cell)
                diff = max(0.0, in_stall - out_stall)
            else:
                # Single-cell block: no internal output cell to net against, so the
                # differential is undefined. Fall back to the raw landing stall — a
                # 1-cell block that stalls IS holding its input (it has no separate
                # egress cell whose stall would reveal downstream backpressure). Rare
                # in practice (loops are multi-cell); keeps a genuine 1-cell throttle
                # visible instead of always reporting 0.
                out_stall = 0.0
                diff = in_stall
            n = sum(len(waits_by_cell.get(c, [])) for c in cells)
            rows.append({
                "block": name,
                "type": (block_types or {}).get(name, ""),
                "stall_ns": diff,              # the DIFFERENTIAL — the ranked metric
                "in_stall_ns": in_stall,
                "out_stall_ns": out_stall,
                "max_stall_ns": max(per_cell),
                "n_stalls": n,
            })
        rows.sort(key=lambda r: (r["stall_ns"], r["max_stall_ns"]), reverse=True)
        peak = max((r["stall_ns"] for r in rows), default=0.0)
        for i, r in enumerate(rows):
            # Fraction of the worst block's serial barrier (bottleneck = 100%).
            r["barrier_pct"] = (100.0 * r["stall_ns"] / peak) if peak else None
            r["rank"] = i + 1
        return rows

    def register_stream(self, chip: int, x: int, y: int,
                        addr: int) -> list[tuple[float, int]]:
        """Value-over-time of one cell register ``(chip, x, y, addr)`` —
        ``[(time_ns, value)]``. Built from ``data_arrival`` events that wrote to
        that register (``dest == addr``). Used by the waveform viewer to plot a
        register dragged from the Program pane (a bus/hex trace, not analog)."""
        out: list[tuple[float, int]] = []
        for t in self.by_cell(chip, x, y):
            if (t.kind == KIND_DATA and t.dest == addr
                    and t.data is not None):
                out.append((t.time_ns, t.data))
        return out

    # -- cursor ---------------------------------------------------------------

    def latest_ns(self) -> float:
        """Time of the last (newest) transaction, or 0.0 if empty. Used to tell
        whether the cursor is at the live edge vs scrubbed back into history."""
        return self.transactions[-1].time_ns if self.transactions else 0.0

    def set_cursor(self, ns: float) -> None:
        self.cursor_ns = float(ns)

    def step_to_next(self, kind: str | None = None,
                     after: float | None = None) -> float | None:
        """Time of the next transaction (optionally of ``kind``) strictly after
        ``after`` (default: the cursor). Returns None if none remain."""
        t0 = self.cursor_ns if after is None else after
        for t in self.transactions:
            if t.time_ns > t0 and (kind is None or t.kind == kind):
                return t.time_ns
        return None

    def transactions_until(self, ns: float | None = None) -> list[Transaction]:
        """All transactions with ``time_ns <= ns`` (default: the cursor)."""
        limit = self.cursor_ns if ns is None else ns
        return [t for t in self.transactions if t.time_ns <= limit]

    # -- state reconstruction (state_at) -------------

    def cell_pc_at(self, chip: int, x: int, y: int,
                   ns: float | None = None) -> int | None:
        """The PC of the most recent exec_tick on a cell at/<= ``ns`` (cursor by
        default) — for the PC highlight."""
        limit = self.cursor_ns if ns is None else ns
        pc = None
        for t in self.exec_ticks(chip, x, y):
            if t.time_ns <= limit:
                pc = t.pc
            else:
                break
        return pc

    def cell_registers_at(self, chip: int, x: int, y: int,
                          ns: float | None = None) -> dict[int, int]:
        """Reconstruct a cell's register values at/<= ``ns`` from the data that
        was written to it (``data_arrival`` with a ``dest``). This is the
        post-run / scrub view; the LIVE view may instead read the engine
        directly (read_cell_memory). Returns ``{addr: value}`` for touched regs."""
        limit = self.cursor_ns if ns is None else ns
        regs: dict[int, int] = {}
        for t in self.by_cell(chip, x, y):
            if t.time_ns > limit:
                break
            if t.kind == KIND_DATA and t.dest is not None and t.data is not None:
                regs[int(t.dest)] = t.data
        return regs
