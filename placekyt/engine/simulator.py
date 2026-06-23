"""SimulationEngine — thin adapter over simkyt (the architecture notes §4.3).

v1.0 single-chip path: load a bitstream into ``simkyt.Chip``, inject a
BITSTREAM stimulus, run, capture output as tagged words, and compare against a
golden ``.kbs`` (``sim.compare_bitstream()`` — §4.3 / #185-#186). This is the
engine the CLI ``--test`` mode and the GUI simulation use. It does NOT
reimplement event scheduling — that lives in the Rust engine.

Multi-chip round-based simulation (``MultiChipSimulation``) is wired in a later
milestone (§4.3 v1.0 limitation); ``--test`` targets the single-chip demo path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import simkyt

from .errors import SimulationError

# Default cap on events when running until the expected output count is reached.
DEFAULT_MAX_EVENTS = 5_000_000

# Per-cell simulation state derived from the trace (§3.2 cell-state overlay).
CELL_EXECUTING = "executing"  # had an exec_tick — green bright
CELL_ACTIVE = "active"        # had instr/data arrival but no exec — light activity
CELL_IDLE = "idle"            # no events this window — waiting
CELL_HALTED = "halted"        # program ends in HALT / reached auto-halt

# Trace event kinds that mean a cell did real work (vs. pure routing transit).
_EXEC_KINDS = {"exec_tick"}
_ACTIVE_KINDS = {"instr_arrival", "data_arrival", "output_ready"}


class SimulationEngine:
    """Single-chip simulation over a built bitstream."""

    def __init__(self, chip_type_path: str | Path):
        self._chip_type_path = str(chip_type_path)
        self.chip = simkyt.Chip.from_yaml(self._chip_type_path)
        self._loaded = False
        self._trace_cursor = 0  # index of next un-reported trace event

    @property
    def simkyt_version(self) -> str:
        return getattr(simkyt, "__version__", "0.0.0")

    def load(self, words: list[int], *, trace: bool = False,
             max_records: int | None = None) -> int:
        """Program the chip with a bitstream word list. Returns events processed.

        ``trace=True`` enables execution tracing so :meth:`cell_states` can
        derive the per-cell overlay (§3.2). ``max_records`` caps the chip's
        trace to a RING BUFFER of the last N events — essential for live/
        streaming runs, where an unbounded trace grows without limit (the chip
        drops the oldest events itself, so the host cost stays O(window))."""
        try:
            events = self.chip.load_bitstream_physical(words)
            if trace:
                self.chip.enable_trace(max_records)
        except Exception as exc:  # noqa: BLE001
            raise SimulationError(f"failed to load bitstream: {exc}") from exc
        self._loaded = True
        return events

    def clear_trace(self) -> None:
        """Drop all buffered trace events (keep tracing enabled). Used in live
        mode after each ingest so each refresh handles only NEW events."""
        try:
            self.chip.clear_trace()
        except Exception:  # noqa: BLE001 — tracing not enabled / unsupported
            pass

    def input_injection_count(self, port: str = "x16_in") -> int:
        """How many stimulus words have been INJECTED at ``port`` so far — the
        count of ``port_injection`` trace events for it. The Nth injected word is
        the Nth word of the stimulus bitstream, so this drives the Disassembly
        panel's live line highlight (#196)."""
        try:
            events = self.chip.get_trace()
        except Exception:  # noqa: BLE001
            return 0
        return sum(1 for ev in events
                   if ev.get("kind") == "port_injection"
                   and ev.get("port_name") == port)

    def cell_states(self, width: int) -> dict[tuple[int, int], str]:
        """Derive per-cell sim state from the trace (§3.2 overlay).

        Returns ``{(x, y): state}`` for every cell that has appeared in the
        trace. ``state`` is one of CELL_EXECUTING / CELL_ACTIVE / CELL_IDLE.
        Cells never seen in the trace are simply absent (the canvas leaves them
        in their static colour). Requires ``load(..., trace=True)``.
        """
        try:
            events = self.chip.get_trace()
        except Exception:  # noqa: BLE001 — tracing not enabled / unsupported
            return {}
        exec_cells: set[int] = set()
        active_cells: set[int] = set()
        for ev in events:
            kind = ev.get("kind")
            cid = ev.get("cell_id")
            if cid is None:
                continue
            if kind in _EXEC_KINDS:
                exec_cells.add(cid)
            elif kind in _ACTIVE_KINDS:
                active_cells.add(cid)
        out: dict[tuple[int, int], str] = {}
        for cid in active_cells | exec_cells:
            x, y = cid % width, cid // width
            out[(x, y)] = CELL_EXECUTING if cid in exec_cells else CELL_ACTIVE
        return out

    def cell_faces(self, width: int, cells=None) -> dict[tuple[int, int], str]:
        """Live output FACE per cell → ``{(x, y): "south"|"east"|"west"|"north"}``.

        A cell can change its FACE at runtime (``MOVE [FACE]``, e.g. the crossover
        relay), so the canvas arrow should follow the LIVE config, not the static
        build. ``cells`` optionally restricts the read to a set of ``(x, y)`` (the
        cells active this frame) to keep it cheap; otherwise every cell is read.
        """
        coords = (cells if cells is not None
                  else [(c % width, c // width) for c in range(width * 100)])
        out: dict[tuple[int, int], str] = {}
        for (x, y) in coords:
            try:
                cid = self.chip.cell_id_at(x, y)
                out[(x, y)] = self.chip.get_fwd_face(cid)
            except Exception:  # noqa: BLE001 — out of range / unsupported
                continue
        return out

    def read_cell_registers(self, x: int, y: int) -> dict[int, int]:
        """Read all 32 registers of cell ``(x, y)`` from the engine's live RAM
        (DEBUG_the architecture notes §3.2 live register view). Unlike trace
        reconstruction, this reflects values the cell COMPUTED itself (R0
        accumulator, ALU results), not just externally-written words. Returns
        ``{addr: value}`` (uint16), or ``{}`` if the read is unsupported."""
        try:
            cid = self.chip.cell_id_at(x, y)
            return {a: int(self.chip.read_cell_memory(cid, a)) & 0xFFFF
                    for a in range(32)}
        except Exception:  # noqa: BLE001 — read unsupported / out of range
            return {}

    def handshakes(self, width: int) -> dict:
        """NEW data transfers since the last call, GROUPED INTO PER-WORD STEPS by
        sim-time (#194) → ``{"steps": [{"cells": [(x,y,face),…], "ports": […]}, …]}``.

        A cell transfer is an ``output_ready`` event (a word leaving a cell on a
        face); a port transfer is a ``port_injection``/``port_capture`` (a word
        flowing through a chip port). Events are bucketed by their ``time_ns`` and
        the buckets returned in time order, so the canvas can flash them ONE WORD
        AT A TIME (a rolling wave) instead of all-at-once. Under the single-word
        handshake (#192/#193) each distinct sim-time carries essentially one word
        through the fabric, so a step == a word transacted. Uses an index cursor
        so each event is reported once.

        Also returns a flat ``cells``/``ports`` (the union across all steps) for
        backward compatibility with callers that don't play back per-word."""
        try:
            events = self.chip.get_trace()
        except Exception:  # noqa: BLE001
            return {"steps": [], "cells": [], "ports": []}
        # Bucket new events by sim-time, preserving first-seen order of times.
        buckets: dict[float, dict] = {}
        order: list[float] = []
        for ev in events[self._trace_cursor:]:
            kind = ev.get("kind")
            t = ev.get("time_ns", 0.0)
            cell = port = None
            if kind == "output_ready":
                cid = ev.get("cell_id")
                face = ev.get("face")
                if cid is not None and face:
                    cell = (cid % width, cid // width, face)
            elif kind in ("port_injection", "port_capture"):
                pn = ev.get("port_name")
                if pn:
                    port = pn
            if cell is None and port is None:
                continue
            b = buckets.get(t)
            if b is None:
                b = {"cells": [], "ports": []}
                buckets[t] = b
                order.append(t)
            if cell is not None:
                b["cells"].append(cell)
            if port is not None:
                b["ports"].append(port)
        self._trace_cursor = len(events)

        order.sort()
        steps = [buckets[t] for t in order]
        all_cells = [c for s in steps for c in s["cells"]]
        all_ports = [p for s in steps for p in s["ports"]]
        return {"steps": steps, "cells": all_cells, "ports": all_ports}

    def reset(self) -> None:
        """Re-create the chip (no save/restore in v1.0 — §0.3)."""
        self.chip = simkyt.Chip.from_yaml(self._chip_type_path)
        self._loaded = False
        self._trace_cursor = 0

    # -- port configuration ---------------------------------------------------

    def configure_input_port(
        self, port: str, *, entry_addr: int, hop_count: int, data_addr: int
    ) -> None:
        """Set how injected samples are routed to the target block (required
        before ``write_port_i16`` produces output).

        ``write_port_i16`` emits ``WRITE(data_addr, sample)`` + ``JUMP(entry_addr)``
        at ``hop_count`` hops from the port. These must match the placed block:
          * ``entry_addr``  — the block's entry address (from its interface),
          * ``hop_count``   — ``30 - distance`` from the port cell to the block,
          * ``data_addr``   — the block's INPUT register (e.g. R31 for GainBlock).
        Mismatches are the classic "runs but outputs zero" failure (the sample
        lands in the wrong register).
        """
        self.chip.set_port_entry_address(port, entry_addr)
        self.chip.set_port_target_hop_count(port, hop_count)
        self.chip.set_port_target_data_address(port, data_addr)

    # -- stimulus / capture ---------------------------------------------------

    def inject(self, port: str, data: list[int]) -> None:
        # uint16 → int16 view so values ≥ 0x8000 (negative in Q15) are accepted.
        arr = np.asarray([d & 0xFFFF for d in data], dtype=np.uint16).view(np.int16)
        self.chip.write_port_i16(port, arr)

    def inject_words(self, words: list[int], port: str = "x16_in") -> None:
        """QUEUE a raw 16-bit instruction-word stimulus for PACED injection
        through ``port`` (no FIFO at the input port). Each word is classified by
        the hardware protocol as an instruction (WRITE/JUMP) or a data word — a
        self-contained ``WRITE, DATA, JUMP`` burst stream — and delivered ONE AT
        A TIME, each waiting until the input cell has accepted the prior one (the
        run loop drives delivery). This is the bitstream-stimulus path; the words
        ARE the bursts."""
        self.chip.queue_words_physical(port, [w & 0xFFFF for w in words])

    def run_until_output(self, port: str, count: int,
                         max_events: int = DEFAULT_MAX_EVENTS) -> None:
        try:
            self.chip.run_until_output(port, count, max_events)
        except Exception as exc:  # noqa: BLE001
            raise SimulationError(f"run_until_output failed: {exc}") from exc

    def capture(self, port: str) -> list[int]:
        """Read buffered output samples as uint16 values."""
        arr = self.chip.read_port_i16(port)
        return [int(v) & 0xFFFF for v in np.asarray(arr).view(np.uint16)]

    def capture_output_words(self, port: str):
        """Drain captured output as TAGGED words (#185): each WRITE carries its
        ``value`` + ``dest`` (the virtual-channel tag); each JUMP its ``entry``.
        Merged in capture-time order so a 'WRITE then JUMP' sequence is faithful.
        Returns ``list[OutWord]`` (see ``engine.io.output_bitstream``)."""
        from engine.io.output_bitstream import OutWord
        writes = self.chip.read_port_words_timed(port)      # (value, dest, t)
        jumps = self.chip.read_port_jumps(port)             # (entry, t)
        events = [(t, 0, OutWord(False, v & 0xFFFF, d)) for (v, d, t) in writes]
        events += [(t, 1, OutWord(True, 0, e)) for (e, t) in jumps]
        events.sort(key=lambda ev: (ev[0], ev[1]))
        return [w for _t, _o, w in events]

    def compare_bitstream(
        self,
        out_port: str,
        golden: str | "Path",
        *,
        in_port: str | None = None,
        stimulus_words: list[int] | None = None,
        tolerance: int = 0,
        max_events: int = DEFAULT_MAX_EVENTS,
    ):
        """Inject a BITSTREAM ``stimulus_words`` (if given), run, capture
        ``out_port`` as TAGGED output words, and compare WORD-BY-WORD against a
        golden ``.kbs`` (value AND tag). Returns a
        ``engine.io.output_bitstream.TaggedCompare`` (#185)."""
        from engine.io.kbs import read_golden_kbs
        from engine.io.output_bitstream import (
            compare_output,
            decode_output,
        )

        golden_words = decode_output(read_golden_kbs(golden))
        if stimulus_words is not None:
            port = in_port or "x16_in"
            self.inject_words(stimulus_words, port=port)
            # Drive to completion (the words self-pace; run until idle).
            for _ in range(max_events // 64 + 1):
                info = self.chip.run(max_events=64)
                if (isinstance(info, dict)
                        and info.get("stop_reason") == "QueueEmpty"
                        and self.chip.run(max_events=0).get("stop_reason")
                        == "QueueEmpty"):
                    break
        actual = self.capture_output_words(out_port)
        return compare_output(actual, golden_words, tolerance=tolerance)

    # -- golden compare (§4.3) -----------------------------------------------


def _signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# Chip names used inside MultiChipSimulation — derived from project chip ids.
def _chip_name(chip_id: int) -> str:
    return f"chip{chip_id}"


class MultiChipSimEngine:
    """Round-based multi-chip simulation over ``simkyt.MultiChipSimulation``.

    Matches the validated reference model (``KyttarRefModel2Chip``): each chip is
    a self-contained pipeline; between rounds the value at a chip's output port is
    relayed verbatim to the connected chip's input port (the data is identical to
    the continuous-HOP_CNT hardware view — see memory ``project_interchip_hop_model``).

    The caller supplies per-chip ``ChipType`` paths, the inter-chip wiring, and
    each chip's input-port config. ``cell_states`` returns ``{(chip_id, x, y):
    state}`` so a single overlay covers every chip.
    """

    def __init__(self, chip_type_paths: dict[int, str], wire_delay_ns: float = 5.0):
        self._paths = dict(chip_type_paths)
        self._sim = simkyt.MultiChipSimulation.new("placekyt", wire_delay_ns)
        self._widths: dict[int, int] = {}
        self._chip_ids: list[int] = []
        self._trace_cursors: dict[int, int] = {}
        for cid, path in self._paths.items():
            ct = simkyt.ChipType.from_yaml(str(path))
            self._sim.add_chip(_chip_name(cid), ct)
            self._widths[cid] = int(ct.width)
            self._chip_ids.append(cid)
            self._trace_cursors[cid] = 0

    def connect(self, from_chip: int, from_port: str,
                to_chip: int, to_port: str) -> None:
        self._sim.connect(_chip_name(from_chip), from_port,
                          _chip_name(to_chip), to_port)

    def load(self, chip_id: int, words: list[int], *, trace: bool = False) -> None:
        self._sim.load_bitstream(_chip_name(chip_id),
                                 np.asarray([w & 0xFFFF for w in words],
                                            dtype=np.uint16).tolist())
        if trace:
            self._sim.enable_trace(_chip_name(chip_id), None)

    def configure_input_port(self, chip_id: int, port: str, *,
                             entry_addr: int, hop_count: int, data_addr: int) -> None:
        name = _chip_name(chip_id)
        self._sim.set_port_entry_address(name, port, entry_addr)
        self._sim.set_port_target_hop_count(name, port, hop_count)
        self._sim.set_port_target_data_address(name, port, data_addr)

    def inject(self, chip_id: int, port: str, data: list[int]) -> None:
        # uint16 → int16 view so values ≥ 0x8000 (negative in Q15) are accepted.
        arr = np.asarray([d & 0xFFFF for d in data], dtype=np.uint16).view(np.int16)
        self._sim.write_port_i16(_chip_name(chip_id), port, arr)

    def run(self, max_events_per_chip: int | None = None, rounds: int = 100) -> dict:
        return self._sim.run(max_events_per_chip, rounds)

    def run_until_output(self, chip_id: int, port: str, count: int,
                         max_events_per_chip: int | None = None,
                         max_rounds: int = 1000) -> dict:
        return self._sim.run_until_output(_chip_name(chip_id), port, count,
                                          max_events_per_chip, max_rounds)

    def capture(self, chip_id: int, port: str) -> list[int]:
        arr = self._sim.read_port_i16(_chip_name(chip_id), port)
        return [int(v) & 0xFFFF for v in np.asarray(arr).view(np.uint16)]

    def cell_states(self) -> dict[tuple[int, int, int], str]:
        """Per-chip cell states from each chip's trace, keyed ``(chip, x, y)``."""
        out: dict[tuple[int, int, int], str] = {}
        for cid in self._chip_ids:
            try:
                events = self._sim.get_trace(_chip_name(cid))
            except Exception:  # noqa: BLE001 — trace not enabled for this chip
                continue
            width = self._widths.get(cid, 10)
            exec_cells: set[int] = set()
            active_cells: set[int] = set()
            for ev in events:
                kind = ev.get("kind")
                cell = ev.get("cell_id")
                if cell is None:
                    continue
                if kind in _EXEC_KINDS:
                    exec_cells.add(cell)
                elif kind in _ACTIVE_KINDS:
                    active_cells.add(cell)
            for c in active_cells | exec_cells:
                x, y = c % width, c // width
                out[(cid, x, y)] = (CELL_EXECUTING if c in exec_cells
                                    else CELL_ACTIVE)
        return out

    def read_cell_registers(self, chip_id: int, x: int, y: int) -> dict[int, int]:
        """Live register read for a cell on ``chip_id``. MultiChipSimulation does
        NOT expose per-cell memory reads, so this returns ``{}`` — the inspector
        falls back to trace reconstruction for multi-chip live state."""
        return {}

    def handshakes(self) -> dict:
        """NEW data transfers since the last call → ``{"cells": [(chip,x,y,face),
        …], "ports": [(chip, port_name), …]}`` (per chip; see
        :meth:`SimulationEngine.handshakes`)."""
        cells: list[tuple[int, int, int, str]] = []
        ports: list[tuple[int, str]] = []
        for cid in self._chip_ids:
            try:
                events = self._sim.get_trace(_chip_name(cid))
            except Exception:  # noqa: BLE001
                continue
            width = self._widths.get(cid, 10)
            cursor = self._trace_cursors.get(cid, 0)
            for ev in events[cursor:]:
                kind = ev.get("kind")
                if kind == "output_ready":
                    cell = ev.get("cell_id")
                    face = ev.get("face")
                    if cell is not None and face:
                        cells.append((cid, cell % width, cell // width, face))
                elif kind in ("port_injection", "port_capture"):
                    if ev.get("port_name"):
                        ports.append((cid, ev["port_name"]))
            self._trace_cursors[cid] = len(events)
        return {"cells": cells, "ports": ports}
