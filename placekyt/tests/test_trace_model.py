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
