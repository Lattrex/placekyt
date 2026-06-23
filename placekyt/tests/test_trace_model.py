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
