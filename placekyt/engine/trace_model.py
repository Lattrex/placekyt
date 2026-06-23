"""TraceModel — the debug data spine (DEBUG_the architecture notes §2).

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

# Trace kinds (mirror the Rust engine's trace; DEBUG_the architecture notes §1).
KIND_PORT_IN = "port_injection"
KIND_INSTR = "instr_arrival"
KIND_DATA = "data_arrival"
KIND_EXEC = "exec_tick"
KIND_OUTPUT = "output_ready"
KIND_PORT_OUT = "port_capture"


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
    """One normalized trace record (DEBUG_the architecture notes §2)."""

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

    # -- ingest ---------------------------------------------------------------

    def clear(self) -> None:
        self.transactions = []
        self.cursor_ns = 0.0
        self._by_cell = None

    def ingest(self, chip: int, raw_events, width: int) -> None:
        """Add one chip's raw trace events. Re-sorts the global stream by time
        and invalidates the lazy indexes."""
        for ev in raw_events or ():
            self.transactions.append(_normalize(ev, chip, width))
        # Stable sort by time keeps same-timestamp ordering as inserted.
        self.transactions.sort(key=lambda t: t.time_ns)
        self._by_cell = None

    def trim_to(self, max_events: int) -> None:
        """Keep only the most-recent ``max_events`` transactions (a scrolling
        window for live streaming). Drops the oldest; invalidates indexes."""
        if len(self.transactions) > max_events:
            self.transactions = self.transactions[-max_events:]
            self._by_cell = None

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
        self._by_cell = None

    # -- indexes (lazy) -------------------------------------------------------

    def _ensure_index(self) -> None:
        if self._by_cell is not None:
            return
        idx: dict[tuple[int, int, int], list[Transaction]] = {}
        for t in self.transactions:
            idx.setdefault((t.chip, t.cx, t.cy), []).append(t)
        self._by_cell = idx

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
        tagged nets). Each port event carries its ``dest`` (the WRITE destination
        register for an output capture / the target address for an input
        injection) which IS the per-stream tag. Bucketing by it lets the waveform
        viewer plot ONE stream at a time instead of all interleaved words. A
        ``dest`` of ``None`` (single-stream port, untagged) buckets under
        ``tag=None`` so the port still appears."""
        streams: dict[tuple[int, str, int | None], list[tuple[float, int]]] = {}
        for t in self.transactions:
            if t.kind in (KIND_PORT_OUT, KIND_PORT_IN) and t.port is not None:
                val = t.data if t.data is not None else 0
                streams.setdefault((t.chip, t.port, self._port_tag(t)), []).append(
                    (t.time_ns, val))
        return streams

    @staticmethod
    def _port_tag(t) -> int | None:
        """The per-stream tag of a port event: the WRITE ``dest`` register for an
        output capture, else the JUMP ``entry_address`` for an input injection
        (which stream the inject triggered). ``None`` when neither is recorded —
        a single untagged stream. NOTE: simkyt's trace does not record a data
        injection's target ADDRESS, so two input streams that differ only by
        target address (e.g. an I/Q port's xi @0 vs xq @1, same entry) share a
        tag here and are not separable from the trace alone."""
        if t.dest is not None:
            return t.dest
        ea = t.detail.get("entry_address")
        return int(ea) if ea is not None else None

    def port_tags(self, chip: int, port: str) -> list[int | None]:
        """The distinct tags the trace shows on ``(chip, port)``, sorted (a
        ``None`` tag — untagged single-stream — sorts last). Drives the channel
        picker when a port is dragged to the waveform viewer."""
        tags = {key[2] for key in self.port_streams_by_tag()
                if key[0] == chip and key[1] == port}
        return sorted(tags, key=lambda d: (d is None, d))

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

    # -- state reconstruction (DEBUG_the architecture notes §2 state_at) -------------

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
