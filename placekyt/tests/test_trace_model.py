# SPDX-License-Identifier: GPL-3.0-or-later
"""TraceModel — the debug data spine (engine-layer, Qt-free)."""

from __future__ import annotations

from engine.trace_model import (
    KIND_DATA,
    KIND_EXEC,
    KIND_PORT_IN,
    KIND_PORT_OUT,
    TraceModel,
    Transaction,
)

# A small hand-built raw trace (the shape Chip.get_trace() returns), width 10.
RAW = [
    {"time_ns": 10.0, "cell_id": 0, "kind": "port_injection",
     "port_name": "x16_in", "data": "0x4000", "entry_address": 28},
    {"time_ns": 20.0, "cell_id": 0, "kind": "instr_arrival", "face": "N",
     "word": "0x63C0", "hop_cnt": 31, "action": "execute_locally"},
    {"time_ns": 25.0, "cell_id": 0, "kind": "data_arrival", "face": "N",
     "data": "0x4000", "dest": 0, "action": "write_local"},
    {"time_ns": 30.0, "cell_id": 0, "kind": "exec_tick", "pc": 28,
     "word": "0xC401", "result": "continue"},
    {"time_ns": 40.0, "cell_id": 11, "kind": "output_ready", "face": "E",
     "word": "0x2000", "is_data": True, "destination": "to_neighbor",
     "neighbor_id": 12},
    {"time_ns": 90.0, "cell_id": 9, "kind": "port_capture",
     "port_name": "x16_out", "data": "0x2000"},
]


class TestDecodeWord:
    def test_decodes_instructions(self):
        from engine.trace_model import decode_word

        assert "Write" in decode_word(0x63C0)
        assert "Jump" in decode_word(0x73DC)
        assert "Mul" in decode_word(0xC401)
        assert "Halt" in decode_word(0x0000)


class TestIngest:
    def test_normalizes_and_orders(self):
        tm = TraceModel()
        # ingest out of order to prove the model sorts.
        tm.ingest(0, list(reversed(RAW)), 10)
        assert len(tm.transactions) == 6
        times = [t.time_ns for t in tm.transactions]
        assert times == sorted(times)

    def test_cell_xy_from_id(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        # cell_id 11 → (1, 1) on a width-10 chip.
        out = next(t for t in tm.transactions if t.kind == "output_ready")
        assert (out.cx, out.cy) == (1, 1)

    def test_hex_fields_parsed(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        data = next(t for t in tm.transactions if t.kind == KIND_DATA)
        assert data.data == 0x4000 and data.dest == 0

    def test_multichip_merge_by_time(self):
        tm = TraceModel()
        tm.ingest(0, [RAW[0]], 10)   # t=10 on chip 0
        tm.ingest(1, [RAW[5]], 10)   # t=90 on chip 1
        tm.ingest(0, [RAW[2]], 10)   # t=25 on chip 0
        assert [t.time_ns for t in tm.transactions] == [10.0, 25.0, 90.0]
        assert [t.chip for t in tm.transactions] == [0, 0, 1]


class TestIndexes:
    def test_port_streams(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        streams = tm.port_streams()
        assert streams[(0, "x16_in")] == [(10.0, 0x4000)]
        assert streams[(0, "x16_out")] == [(90.0, 0x2000)]

    def test_exec_ticks_for_cell(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        ticks = tm.exec_ticks(0, 0, 0)
        assert len(ticks) == 1 and ticks[0].pc == 28


class TestCursorAndState:
    def test_step_to_next_kind(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        tm.set_cursor(0)
        assert tm.step_to_next(KIND_PORT_OUT) == 90.0
        assert tm.step_to_next(KIND_PORT_IN) == 10.0

    def test_cell_pc_at(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        assert tm.cell_pc_at(0, 0, 0, 35.0) == 28  # after the exec_tick
        assert tm.cell_pc_at(0, 0, 0, 5.0) is None  # before any exec

    def test_cell_registers_at(self):
        tm = TraceModel()
        tm.ingest(0, RAW, 10)
        assert tm.cell_registers_at(0, 0, 0, 100.0) == {0: 0x4000}
        assert tm.cell_registers_at(0, 0, 0, 20.0) == {}  # before data_arrival


class TestRawWordInputCoalescing:
    """The pipelined/saturated drive path (queue_words_physical) injects a raw
    WRITE→DATA→WRITE→DATA→JUMP word stream. simKYT records a port_injection per
    word and decodes (hop,dest) from each word's bits — but a Q15 DATA payload in
    [0x6000,0x7FFF] is bit-identical to a WRITE/JUMP opcode, so ~1/8 payloads
    decode as a spurious WRITE and would spawn a phantom 'hop N' input trace.
    port_streams_by_tag must sequence on POSITION (WRITE is ALWAYS followed by its
    DATA) so a colliding payload can't masquerade as a control word."""

    def _raw(self, hop=28, entry=13):
        # 3 complex samples: WRITE(d0)->xi, WRITE(d1)->xq, JUMP. Two of the payload
        # values (0x6ABC, 0x7DEF) deliberately collide with WRITE/JUMP opcodes and
        # would, under a bit-decoding demux, land as bogus hops. target_hop mirrors
        # what simKYT records: a real hop for control words / a decoded-from-bits hop
        # for the colliding payloads, 0 for the clean ones.
        def W(d):
            return (0x6 << 12) | ((hop & 0x1F) << 5) | (d & 0x1F)
        def J():
            return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)
        rows = []
        t = 0.0
        payloads = [
            (0x0123, 0), (0x6ABC, 21),   # xi clean, xq collides (0x6xxx -> bogus hop)
            (0x7DEF, 15), (0x0456, 0),   # xi collides (0x7xxx), xq clean
            (0x0789, 0), (0x00AB, 0),    # both clean
        ]
        for s in range(3):
            xi_val, xi_bogus = payloads[2 * s]
            xq_val, xq_bogus = payloads[2 * s + 1]
            # WRITE d0 (real hop), xi payload, WRITE d1, xq payload, JUMP
            for word, dest, th in (
                (W(0), 0, hop), (xi_val, xi_bogus, 0 if xi_bogus == 0 else xi_bogus),
                (W(1), 1, hop), (xq_val, xq_bogus, 0 if xq_bogus == 0 else xq_bogus),
                (J(), entry, 0),
            ):
                rows.append({"time_ns": t, "cell_id": 0, "kind": "port_injection",
                             "port_name": "x16_in", "data": hex(word), "dest": dest,
                             "target_hop": th})
                t += 1.0
        return rows

    def test_pipelined_input_collapses_to_two_iq_streams(self):
        tm = TraceModel()
        tm.ingest(0, self._raw(), 10)
        streams = tm.port_streams_by_tag()
        tags = {k[2] for k in streams if k[1] == "x16_in"}
        # EXACTLY the two rails — (hop, dest=0)=I and (hop, dest=1)=Q — no phantoms.
        assert tags == {(28, 0), (28, 1)}, tags
        assert len(streams[(0, "x16_in", (28, 0))]) == 3   # 3 xi payloads
        assert len(streams[(0, "x16_in", (28, 1))]) == 3   # 3 xq payloads
        # The colliding payloads are plotted as DATA under the correct rail, by
        # position — NOT dropped and NOT re-armed as WRITEs.
        xi_vals = [v for _, v in streams[(0, "x16_in", (28, 0))]]
        xq_vals = [v for _, v in streams[(0, "x16_in", (28, 1))]]
        assert xi_vals == [0x0123, 0x7DEF, 0x0789]
        assert xq_vals == [0x6ABC, 0x0456, 0x00AB]

    def test_per_sample_input_unchanged(self):
        # Per-sample inject_data_physical: ONE addressed event per operand, all with
        # a real target_hop and the VALUE in data (no hop-0 payloads). Must keep the
        # legacy per-(hop,dest) tag untouched.
        rows = []
        for i, (val, dest) in enumerate([(0x0111, 0), (0x0222, 1),
                                         (0x0333, 0), (0x0444, 1)]):
            rows.append({"time_ns": float(i), "cell_id": 0, "kind": "port_injection",
                         "port_name": "x16_in", "data": hex(val), "dest": dest,
                         "target_hop": 28})
        tm = TraceModel()
        tm.ingest(0, rows, 10)
        streams = tm.port_streams_by_tag()
        assert [v for _, v in streams[(0, "x16_in", (28, 0))]] == [0x0111, 0x0333]
        assert [v for _, v in streams[(0, "x16_in", (28, 1))]] == [0x0222, 0x0444]


class TestStreamSummary:
    """stream_summary() + io_latency_ns(): per-stream settled DATA rate and the
    aggregate in→out fill latency (#479). Rates are honest chip-time — 1/median
    inter-sample gap — with the pipeline-fill first gap dropped."""

    def _trace(self):
        # 2 input operand streams (xi @ (22,0), xq @ (22,1)) one sample/1000ns each,
        # 1 output stream every 5000ns. First output at t=3000.
        evs = []
        t = 0.0
        for _k in range(6):
            evs.append({"time_ns": t, "cell_id": 0, "kind": "port_injection",
                        "port_name": "x16_in", "data": 1234, "dest": 0,
                        "target_hop": 22})
            evs.append({"time_ns": t + 500, "cell_id": 0, "kind": "port_injection",
                        "port_name": "x16_in", "data": 5678, "dest": 1,
                        "target_hop": 22})
            t += 1000
        for k in range(6):
            evs.append({"time_ns": 3000 + 5000 * k, "cell_id": 66,
                        "kind": "port_capture", "port_name": "x16_out",
                        "data": k & 1})
        return evs

    def test_per_stream_settled_rate(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = {(r["direction"], r["tag"]): r for r in tm.stream_summary()}
        # Two input operands, each 1 sample / 1000 ns = 1.0 MSa/s.
        for tag in ((22, 0), (22, 1)):
            r = rows[("in", tag)]
            assert r["samples"] == 6
            assert abs(r["settled_sps"] - 1e6) < 1.0
        # Output every 5000 ns = 0.2 MSa/s.
        out = rows[("out", None)]
        assert out["direction"] == "out"
        assert abs(out["settled_sps"] - 2e5) < 1.0

    def test_direction_and_ordering(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = tm.stream_summary()
        # Inputs sort before outputs.
        dirs = [r["direction"] for r in rows]
        assert dirs == sorted(dirs, key=lambda d: d != "in")
        assert dirs.count("in") == 2 and dirs.count("out") == 1

    def test_io_latency(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        # first input at t=0, first output at t=3000.
        assert tm.io_latency_ns() == 3000.0

    def test_latency_none_without_output(self):
        tm = TraceModel()
        tm.ingest(0, [{"time_ns": 0.0, "cell_id": 0, "kind": "port_injection",
                       "port_name": "x16_in", "data": 1, "dest": 0,
                       "target_hop": 22}], 10)
        assert tm.io_latency_ns() is None


class TestBlockUtilization:
    """The bottleneck view: sum each block's cells' exec busy-time; rank hottest."""

    def _trace(self):
        # width 10. Two cells: (0,0)=cell 0 runs a SLOW block (big gaps between
        # exec ticks), (1,0)=cell 1 runs a FAST block (small gaps). The slow one
        # must rank #1 (the bottleneck).
        ev = []
        # Slow block cell 0: ticks at 0,100,200,300 → 3 gaps of 100 each.
        for i, t in enumerate([0.0, 100.0, 200.0, 300.0]):
            ev.append({"time_ns": t, "cell_id": 0, "kind": "exec_tick", "pc": i})
        # Fast block cell 1: ticks at 0,10,20,30 → 3 gaps of 10 each.
        for i, t in enumerate([0.0, 10.0, 20.0, 30.0]):
            ev.append({"time_ns": t, "cell_id": 1, "kind": "exec_tick", "pc": i})
        return ev

    def test_ranks_busiest_first(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        lookup = {(0, 0, 0): "SlowBlock", (0, 1, 0): "FastBlock"}
        rows = tm.block_utilization(lookup)
        assert [r["block"] for r in rows] == ["SlowBlock", "FastBlock"]
        assert rows[0]["rank"] == 1 and rows[1]["rank"] == 2
        # SlowBlock busy = 3*100 + median-tail(100) = 400; FastBlock = 3*10 + 10 = 40.
        assert rows[0]["busy_ns"] == 400.0
        assert rows[1]["busy_ns"] == 40.0
        # SlowBlock is ~10x busier — it IS the bottleneck.
        assert rows[0]["busy_ns"] > 5 * rows[1]["busy_ns"]

    def test_util_pct_is_relative_to_peak(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = tm.block_utilization({(0, 0, 0): "SlowBlock", (0, 1, 0): "FastBlock"})
        # util_pct is normalized so the BUSIEST block = 100%, others relative.
        slow = next(r for r in rows if r["block"] == "SlowBlock")
        fast = next(r for r in rows if r["block"] == "FastBlock")
        assert slow["util_pct"] == 100.0
        assert abs(fast["util_pct"] - (100.0 * 40.0 / 400.0)) < 1e-6

    def test_ranks_by_critical_cell_not_block_size(self):
        # THE key regression: a WIDE block (4 cells, each 50 ns busy → 200 summed)
        # must NOT out-rank a NARROW block whose single cell is busier (150 ns).
        # The old sum-over-cells metric crowned the wide block (200 > 150); the
        # correct critical-cell metric crowns the narrow one (150 > 50). This is
        # exactly the RRC-matched-filter-vs-Costas artifact.
        ev = []
        # Wide block: cells 0..3, each does 2 gaps of 25 ns (busy ≈ 50 + tail 25).
        for cid in (0, 1, 2, 3):
            for i, t in enumerate([0.0, 25.0, 50.0]):
                ev.append({"time_ns": t, "cell_id": cid, "kind": "exec_tick",
                           "pc": i})
        # Narrow block: cell 5 does 2 gaps of 75 ns (busy ≈ 150 + tail 75).
        for i, t in enumerate([0.0, 75.0, 150.0]):
            ev.append({"time_ns": t, "cell_id": 5, "kind": "exec_tick", "pc": i})
        tm = TraceModel()
        tm.ingest(0, ev, 10)
        lookup = {(0, c, 0): "Wide" for c in (0, 1, 2, 3)}
        lookup[(0, 5, 0)] = "Narrow"
        rows = tm.block_utilization(lookup)
        wide = next(r for r in rows if r["block"] == "Wide")
        narrow = next(r for r in rows if r["block"] == "Narrow")
        # Summed busy: Wide (4 cells) > Narrow (1 cell) — the OLD (wrong) ranking.
        assert wide["busy_ns"] > narrow["busy_ns"]
        # But per-CRITICAL-cell, Narrow's cell is the longest serial path → #1.
        assert narrow["crit_ns"] > wide["crit_ns"]
        assert rows[0]["block"] == "Narrow"
        assert narrow["util_pct"] == 100.0

    def test_instr_per_cell_is_size_independent_work(self):
        # A feedback loop runs MANY instructions per cell per sample; a flat block
        # runs few. instr_per_cell captures that regardless of cell count.
        ev = []
        # Loopy: one cell, 6 exec ticks (heavy per-sample work).
        for i, t in enumerate([0.0, 10, 20, 30, 40, 50]):
            ev.append({"time_ns": float(t), "cell_id": 0, "kind": "exec_tick",
                       "pc": i})
        # Flat: two cells, 2 ticks each (light per-sample work, but wider).
        for cid in (1, 2):
            for i, t in enumerate([0.0, 10]):
                ev.append({"time_ns": float(t), "cell_id": cid, "kind": "exec_tick",
                           "pc": i})
        tm = TraceModel()
        tm.ingest(0, ev, 10)
        rows = tm.block_utilization({(0, 0, 0): "Loopy", (0, 1, 0): "Flat",
                                     (0, 2, 0): "Flat"})
        loopy = next(r for r in rows if r["block"] == "Loopy")
        flat = next(r for r in rows if r["block"] == "Flat")
        assert loopy["instr_per_cell"] > flat["instr_per_cell"]

    def test_occupancy_pct_is_capped_fraction_of_run(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = tm.block_utilization({(0, 0, 0): "SlowBlock", (0, 1, 0): "FastBlock"})
        # SlowBlock's single cell is busy the whole run (its tail estimate even
        # nudges busy_ns past the span) → occupancy CAPPED at 100% (a duty cycle
        # can't exceed 100%). FastBlock's cell is mostly idle → well under 100%.
        slow = next(r for r in rows if r["block"] == "SlowBlock")
        fast = next(r for r in rows if r["block"] == "FastBlock")
        assert slow["occupancy_pct"] == 100.0
        assert fast["occupancy_pct"] < 50.0

    def test_unmapped_cells_bucket_as_routing(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        # Only map cell 0; cell 1 falls into "(routing)".
        rows = tm.block_utilization({(0, 0, 0): "SlowBlock"})
        names = {r["block"] for r in rows}
        assert names == {"SlowBlock", "(routing)"}

    def test_type_column_from_map(self):
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = tm.block_utilization({(0, 0, 0): "cost0"},
                                    block_types={"cost0": "ComplexCostasLoopBlock"})
        assert next(r for r in rows if r["block"] == "cost0")["type"] == \
            "ComplexCostasLoopBlock"

    def test_empty_trace_is_empty(self):
        tm = TraceModel()
        assert tm.block_utilization({}) == []


class TestBlockBottleneck:
    """The serial-barrier view: rank blocks by the INPUT/OUTPUT stall DIFFERENTIAL —
    the block where input backpressure piles up but the output drains freely. That
    block MANUFACTURES the backpressure; a block that merely RELAYS it stalls on both
    sides equally and is NOT the culprit."""

    def _trace(self):
        # width 10. Two 2-cell blocks:
        #  - Costas: input cell 0 stalls a LOT (2300,2400 — loop LOCKs); output cell 1
        #    NEVER stalls (Gardner downstream takes everything). MANUFACTURES it.
        #  - MF: input cell 2 stalls a LOT (2400,2400 — GUI per-sample overlap), AND
        #    output cell 3 ALSO stalls a lot (2400 — waiting on Costas). RELAYS it.
        ev = [
            {"time_ns": 100.0, "cell_id": 0, "kind": "stall", "waited_ns": 2300.0},
            {"time_ns": 200.0, "cell_id": 0, "kind": "stall", "waited_ns": 2400.0},
            # Costas output cell 1: no stall (drains freely).
            {"time_ns": 110.0, "cell_id": 2, "kind": "stall", "waited_ns": 2400.0},
            {"time_ns": 210.0, "cell_id": 2, "kind": "stall", "waited_ns": 2400.0},
            {"time_ns": 120.0, "cell_id": 3, "kind": "stall", "waited_ns": 2400.0},
            {"time_ns": 220.0, "cell_id": 3, "kind": "stall", "waited_ns": 2400.0},
        ]
        return ev

    def _blocks(self):
        return {"Costas": [(0, 0, 0), (0, 1, 0)], "MF": [(0, 2, 0), (0, 3, 0)]}

    def test_manufacturer_ranks_above_relayer(self):
        # THE key regression: the MF landing STALLS AS MUCH as Costas (2400 each),
        # but the MF also stalls on its OUTPUT, so its differential is ~0 — while
        # Costas's output drains freely, giving it the full differential. Costas #1.
        tm = TraceModel()
        tm.ingest(0, self._trace(), 10)
        rows = tm.block_bottleneck(self._blocks())
        assert rows[0]["block"] == "Costas"
        assert rows[0]["rank"] == 1
        costas = next(r for r in rows if r["block"] == "Costas")
        mf = next(r for r in rows if r["block"] == "MF")
        # Costas: in 2350 (median of 2300,2400), out 0 → differential 2350.
        assert costas["in_stall_ns"] == 2350.0
        assert costas["out_stall_ns"] == 0.0
        assert costas["stall_ns"] == 2350.0
        # MF: in 2400, out 2400 → differential 0. RELAYS, not the culprit.
        assert mf["in_stall_ns"] == 2400.0
        assert mf["out_stall_ns"] == 2400.0
        assert mf["stall_ns"] == 0.0
        assert mf["rank"] == len(rows)

    def test_differential_never_negative(self):
        # If a block's output stalls MORE than its input (odd, but possible), the
        # differential floors at 0 — it isn't a negative bottleneck.
        tm = TraceModel()
        tm.ingest(0, [
            {"time_ns": 0.0, "cell_id": 0, "kind": "stall", "waited_ns": 10.0},
            {"time_ns": 1.0, "cell_id": 1, "kind": "stall", "waited_ns": 500.0},
        ], 10)
        rows = tm.block_bottleneck({"X": [(0, 0, 0), (0, 1, 0)]})
        assert rows[0]["stall_ns"] == 0.0

    def test_barrier_pct_relative_to_worst(self):
        tm = TraceModel()
        # Costas differential 2350; a second loop with in 500 / out 0 = 500.
        ev = self._trace() + [
            {"time_ns": 5.0, "cell_id": 5, "kind": "stall", "waited_ns": 500.0},
        ]
        tm.ingest(0, ev, 10)
        blocks = dict(self._blocks(), Loop2=[(0, 5, 0), (0, 6, 0)])
        rows = tm.block_bottleneck(blocks)
        costas = next(r for r in rows if r["block"] == "Costas")
        loop2 = next(r for r in rows if r["block"] == "Loop2")
        assert costas["barrier_pct"] == 100.0
        assert abs(loop2["barrier_pct"] - (100.0 * 500.0 / 2350.0)) < 1e-6

    def test_no_stalls_all_zero(self):
        # Sequential per-sample drive → nothing parks → every block 0, ranks stable.
        tm = TraceModel()
        tm.ingest(0, [{"time_ns": 0.0, "cell_id": 0, "kind": "exec_tick", "pc": 0}],
                  10)
        rows = tm.block_bottleneck({"A": [(0, 0, 0)], "B": [(0, 1, 0)]})
        assert all(r["stall_ns"] == 0.0 for r in rows)
        assert all(r["barrier_pct"] is None for r in rows)

    def test_single_cell_block_uses_raw_stall(self):
        # A single-cell block has no internal output cell to net against, so the
        # differential is undefined — fall back to the raw landing stall (rare; loops
        # are multi-cell, so this keeps a genuine 1-cell throttle visible).
        tm = TraceModel()
        tm.ingest(0, [
            {"time_ns": 0.0, "cell_id": 0, "kind": "stall", "waited_ns": 900.0},
        ], 10)
        rows = tm.block_bottleneck({"Slicer": [(0, 0, 0)]})
        assert rows[0]["stall_ns"] == 900.0
        assert rows[0]["out_stall_ns"] == 0.0

    def test_empty_block_cells(self):
        tm = TraceModel()
        assert tm.block_bottleneck({}) == []
