"""Tests for the live GNURadio↔placeKYT chip bridge (engine.sim_bridge)."""
from __future__ import annotations
import socket
import numpy as np
import pytest

from engine.sim_bridge import SimServer, recv_message, send_message


class FakeChip:
    """Minimal chip stand-in: a gain of 0.5 with a 1-deep buffer."""
    def __init__(self):
        self._in = []
        self._out = []
    def write_port(self, port, data):
        self._in.extend(float(v) for v in np.asarray(data))
    def output_available(self, port):
        return len(self._out)
    def run_until_output(self, port, count, max_events):
        while self._in and len(self._out) < count:
            self._out.append(self._in.pop(0) * 0.5)
    def read_port(self, port):
        out = np.array(self._out, dtype=np.float32)
        self._out = []
        return out


class TaggedFakeChip:
    """Chip stand-in for the shared-port duplex ops: each tagged-write sample is
    echoed to the output as a WRITE word whose dest = (entry % 16), so RX vs TX
    streams (different jump_entry) come back with distinct dest tags."""
    def __init__(self):
        self._words = []  # (value_uint16, dest, t)
        self._t = 0
    def write_port(self, port, data):
        for v in np.asarray(data):
            q = int(round(max(-1.0, min(0.999, float(v))) * 32768)) & 0xFFFF
            self._words.append((q, 0, self._t)); self._t += 1
    def write_port_tagged(self, port, data, addrs):
        for v, a in zip(np.asarray(data), np.asarray(addrs)):
            q = int(round(max(-1.0, min(0.999, float(v))) * 32768)) & 0xFFFF
            self._words.append((q, int(a) & 0xF, self._t)); self._t += 1
    def output_available(self, port):
        return len(self._words)
    def run_until_output(self, port, count, max_events):
        pass
    def read_port_words_timed(self, port):
        out = list(self._words); self._words = []
        return out


class BatchFakeChip:
    """Chip stand-in for process_batch: the inject_data/jump + run + read_port
    sequence. Echoes each injected xi (the I component) back as the recovered
    output, so a batch of N complex samples returns N floats == the xi stream."""
    def __init__(self):
        self._xi = None
        self._pending = []
        self._out = []

    def inject_data_physical(self, vals, target_hop_cnt, target_addr):
        q = int(vals[0]) & 0xFFFF
        f = (q - 0x10000 if q & 0x8000 else q) / 32768.0
        if int(target_addr) == 0:      # xi
            self._xi = f

    def inject_jump_physical(self, target_hop_cnt, entry_addr):
        if self._xi is not None:
            self._pending.append(self._xi)
            self._xi = None

    def run(self, max_events):
        # On the JUMP's run, commit the pending sample to the output buffer.
        if self._pending:
            self._out.extend(self._pending)
            self._pending = []

    def read_port(self, port):
        out = np.array(self._out, dtype=np.float32)
        self._out = []
        return out


def _client(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    return c


def test_process_batch_runs_whole_burst_in_one_rpc():
    """process_batch injects+runs the WHOLE interleaved-I/Q burst on the server
    and returns the full recovered stream in one reply (no per-sample round-trip).
    The BatchFakeChip echoes each xi, so the reply == the input I stream."""
    srv = SimServer(BatchFakeChip(), default_entries={"x16_in": 17})
    p = srv.start()
    try:
        c = _client(p)
        # 4 complex samples: I = [0.5, -0.5, 0.25, -0.75]
        iq = np.array([0.5, 0.1, -0.5, 0.2, 0.25, -0.1, -0.75, 0.3],
                      dtype=np.float32)
        send_message(c, {"op": "process_batch", "port": "x1_out",
                         "in_port": "x16_in", "data_addrs": [0, 1]}, iq)
        h, out = recv_message(c)
        assert h["ok"]
        assert out is not None and len(out) == 4
        assert np.allclose(out, [0.5, -0.5, 0.25, -0.75], atol=1e-3)
        c.close()
    finally:
        srv.stop()


class RawBitBatchChip(BatchFakeChip):
    """A bit-packing receiver: emits the decoded bit (0/1) in the output word's
    LSB. ``read_port`` Q15-scales (word/32768 ~= 0 for a bit), so the bit is only
    recoverable via the raw ``read_port_i16`` path. Echoes sign(xi) as the bit
    (xi<0 -> 1)."""
    def run(self, max_events):
        if self._pending:
            self._out.extend(1 if x < 0 else 0 for x in self._pending)
            self._pending = []

    def read_port(self, port):
        # Q15-scaled: a bit (0/1) crushes to ~0.0 — the wrong path for bits.
        out = np.array([v / 32768.0 for v in self._out], dtype=np.float32)
        self._out = []
        return out

    def read_port_i16(self, port):
        out = np.array(self._out, dtype=np.int16)  # raw word: bit in the LSB
        self._out = []
        return out


def test_process_batch_raw_returns_packed_bits():
    """`raw=True` drains the output as raw int16 WORDS (bit in the LSB) instead of
    Q15-scaled floats — the path a bit-packing receiver (CoherentRXBlock) needs.
    Without it, Q15 scaling crushes the 0/1 bit to ~0 and every bit reads 0."""
    srv = SimServer(RawBitBatchChip(), default_entries={"x16_in": 17})
    p = srv.start()
    try:
        c = _client(p)
        # I = [+0.5, -0.5, -0.25, +0.75] -> bits [0, 1, 1, 0]
        iq = np.array([0.5, 0.0, -0.5, 0.0, -0.25, 0.0, 0.75, 0.0],
                      dtype=np.float32)
        # default (Q15) path: bits crushed to ~0
        send_message(c, {"op": "process_batch", "port": "x1_out",
                         "in_port": "x16_in", "data_addrs": [0, 1]}, iq)
        _h, q15_out = recv_message(c)
        assert np.allclose(q15_out, [0, 0, 0, 0], atol=1e-3)  # bits lost
        # raw path: bits preserved in the LSB
        send_message(c, {"op": "process_batch", "port": "x1_out",
                         "in_port": "x16_in", "data_addrs": [0, 1],
                         "raw": True}, iq)
        _h2, raw_out = recv_message(c)
        bits = [int(round(v)) & 1 for v in raw_out]
        assert bits == [0, 1, 1, 0], bits
        c.close()
    finally:
        srv.stop()


def test_process_batch_uses_default_entry_for_input_port():
    """The entry falls back to the INPUT port's configured entry (not the output
    port name) when the client sends no jump_entry."""
    chip = BatchFakeChip()
    seen_entries = []
    orig = chip.inject_jump_physical

    def spy(target_hop_cnt, entry_addr):
        seen_entries.append(entry_addr)
        return orig(target_hop_cnt=target_hop_cnt, entry_addr=entry_addr)

    chip.inject_jump_physical = spy
    srv = SimServer(chip, default_entries={"x16_in": 17})
    p = srv.start()
    try:
        c = _client(p)
        iq = np.array([0.5, 0.1, -0.5, 0.2], dtype=np.float32)
        send_message(c, {"op": "process_batch", "port": "x1_out",
                         "in_port": "x16_in", "data_addrs": [0, 1]}, iq)
        recv_message(c)
        c.close()
        assert seen_entries == [17, 17], seen_entries
    finally:
        srv.stop()


def test_server_serves_port_api():
    srv = SimServer(FakeChip())
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "write_port", "port": "x16_in"},
                     np.array([0.6, 0.4, 0.8], dtype=np.float32))
        assert recv_message(c)[0]["ok"]
        send_message(c, {"op": "run_until_output", "port": "x16_out",
                         "count": 3, "max_events": 1000})
        assert recv_message(c)[0]["ok"]
        send_message(c, {"op": "output_available", "port": "x16_out"})
        assert recv_message(c)[0]["available"] == 3
        send_message(c, {"op": "read_port", "port": "x16_out"})
        _h, payload = recv_message(c)
        assert np.allclose(payload, [0.3, 0.2, 0.4], atol=1e-4)
        c.close()
    finally:
        srv.stop()


def test_on_activity_fires_on_run():
    hits = []
    srv = SimServer(FakeChip(), on_activity=lambda: hits.append(1))
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "write_port", "port": "x16_in"},
                     np.array([1.0], dtype=np.float32))
        recv_message(c)
        send_message(c, {"op": "run_until_output", "port": "x16_out",
                         "count": 1, "max_events": 100})
        recv_message(c)
        c.close()
    finally:
        srv.stop()
    assert hits  # on_activity fired

def test_unknown_op_errors():
    srv = SimServer(FakeChip())
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "bogus"})
        reply, _ = recv_message(c)
        assert not reply["ok"] and "unknown op" in reply["error"]
        c.close()
    finally:
        srv.stop()

def test_write_port_jump_entry_tags_stream():
    """write_port with jump_entry routes the whole stream via that entry, and
    read_port_tagged returns the dest tags so two streams demux on one port."""
    srv = SimServer(TaggedFakeChip())
    p = srv.start()
    try:
        c = _client(p)
        # RX stream tagged entry 21 (-> dest 21%16 = 5), TX stream entry 26 (->10).
        send_message(c, {"op": "write_port", "port": "x16_in", "jump_entry": 21},
                     np.array([0.5, -0.5], dtype=np.float32))
        assert recv_message(c)[0]["ok"]
        send_message(c, {"op": "write_port", "port": "x16_in", "jump_entry": 26},
                     np.array([0.25], dtype=np.float32))
        assert recv_message(c)[0]["ok"]
        # Read only the RX tag (5).
        send_message(c, {"op": "read_port_tagged", "port": "x16_out", "tag": 5})
        h, payload = recv_message(c)
        assert h["ok"] and h["dests"] == [5, 5]
        assert np.allclose(payload, [0.5, -0.5], atol=1e-3)
        # Read only the TX tag (10).
        send_message(c, {"op": "read_port_tagged", "port": "x16_out", "tag": 10})
        h, payload = recv_message(c)
        assert h["dests"] == [10]
        assert np.allclose(payload, [0.25], atol=1e-3)
        c.close()
    finally:
        srv.stop()


def test_read_port_tagged_returns_all_tags_unfiltered():
    srv = SimServer(TaggedFakeChip())
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "write_port", "port": "x16_in", "jump_entry": 21},
                     np.array([0.5], dtype=np.float32))
        recv_message(c)
        send_message(c, {"op": "write_port", "port": "x16_in", "jump_entry": 26},
                     np.array([0.25], dtype=np.float32))
        recv_message(c)
        send_message(c, {"op": "read_port_tagged", "port": "x16_out"})
        h, _payload = recv_message(c)
        assert sorted(h["dests"]) == [5, 10]
        c.close()
    finally:
        srv.stop()


def test_duplex_chip_over_bridge_real():
    """End-to-end: the REAL built duplex chip, driven over the bridge with
    per-stream jump_entry, returns RX bits (tag 5) and TX symbols (tag 10)
    demuxed on the shared x16_out — the live-path proof, headless."""
    from tests.conftest import CHIP_YAML, DEMO_DIR

    ct = CHIP_YAML
    demo = DEMO_DIR / "bpsk_duplex_demo.kyt"
    if not (ct.exists() and demo.exists()):
        pytest.skip("chip yaml / duplex demo absent")

    import simkyt
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from engine.registry import ChipTypeRegistry
    from engine.bpsk_duplex_demo import (
        RX_TAG, TX_TAG, RX_SYMBOLS, TX_BITS, Q15_PLUS_ONE, Q15_MINUS_ONE,
        _splitter_entries,
    )

    project = load_project(demo)
    reg = ChipTypeRegistry(); reg.register_file(ct)
    res = BuildEngine(BlockCatalog.from_gr_kyttar(), reg.paths()).build(
        project, reg.chip_types())
    assert res.ok, [str(e) for e in res.errors]
    rx_entry, tx_entry = _splitter_entries(res)

    chip = simkyt.Chip.from_yaml(str(ct))
    chip.load_bitstream_physical(res.words(0))
    srv = SimServer(chip)
    p = srv.start()
    try:
        c = _client(p)

        def write(values, entry):
            send_message(c, {"op": "write_port", "port": "x16_in",
                             "jump_entry": entry},
                         np.asarray(values, dtype=np.float32))
            assert recv_message(c)[0]["ok"]

        def run(n):
            send_message(c, {"op": "run_until_output", "port": "x16_out",
                             "count": n, "max_events": n * 5000})
            assert recv_message(c)[0]["ok"]

        def read(tag):
            send_message(c, {"op": "read_port_tagged", "port": "x16_out",
                             "tag": tag})
            h, payload = recv_message(c)
            return list(payload) if payload is not None else [], h["dests"]

        # Drive both streams (symbols ±1.0 for RX, bits for TX), interleaved.
        rx_f = [(-1.0 if (s & 0x8000) else (s / 32768.0)) for s in RX_SYMBOLS]
        for i in range(max(len(TX_BITS), len(rx_f))):
            if i < len(TX_BITS):
                write([float(TX_BITS[i])], tx_entry)
            if i < len(rx_f):
                write([rx_f[i]], rx_entry)
        run(len(TX_BITS) + len(RX_SYMBOLS))

        rx_vals, rx_dests = read(RX_TAG)
        tx_vals, tx_dests = read(TX_TAG)

        def q15(v):  # float -> uint16 Q15 (round-trip the bridge's conversion)
            return int(round(max(-1.0, min(0.999969, v)) * 32768)) & 0xFFFF

        # RX chain emits the recovered BIT (0x0000 or 0x0001) — read the raw value.
        rx_bits = [q15(v) for v in rx_vals]
        exp_rx = [0 if not (s & 0x8000) else 1 for s in RX_SYMBOLS]
        assert rx_bits == exp_rx, f"RX {rx_bits} != {exp_rx}"
        assert all(d == RX_TAG for d in rx_dests)

        # TX chain maps each bit to a ±1.0 symbol — compare signs (full-scale).
        tx_q15 = [q15(v) for v in tx_vals]
        exp_tx = [Q15_PLUS_ONE if b == 0 else Q15_MINUS_ONE for b in TX_BITS]
        assert [0 if t < 0x8000 else 1 for t in tx_q15] == \
               [0 if e < 0x8000 else 1 for e in exp_tx], f"TX signs {tx_q15}"
        assert all(d == TX_TAG for d in tx_dests)
        c.close()
    finally:
        srv.stop()


def test_ping():
    srv = SimServer(FakeChip())
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "ping"})
        assert recv_message(c)[0]["ok"]
        c.close()
    finally:
        srv.stop()


def test_reset_rehosts_fresh_chip():
    # A 'reset' op asks the host for a fresh chip (so a second flowgraph run
    # starts clean). on_reset returns the new chip; the server swaps to it.
    chips = [FakeChip(), FakeChip()]
    order = iter(chips[1:])
    srv = SimServer(chips[0], on_reset=lambda: next(order))
    p = srv.start()
    try:
        c = _client(p)
        send_message(c, {"op": "write_port", "port": "x16_in"},
                     np.array([1.0], dtype=np.float32))
        recv_message(c)
        send_message(c, {"op": "reset"})           # swap to chips[1]
        assert recv_message(c)[0]["ok"]
        send_message(c, {"op": "output_available", "port": "x16_out"})
        assert recv_message(c)[0]["available"] == 0  # fresh chip → nothing buffered
        c.close()
    finally:
        srv.stop()


def test_set_chip_swaps_target():
    a, b = FakeChip(), FakeChip()
    a.write_port("x", [9.9])
    srv = SimServer(a)
    p = srv.start()
    try:
        srv.set_chip(b)  # swap before the client runs
        c = _client(p)
        send_message(c, {"op": "write_port", "port": "x16_in"},
                     np.array([0.8], dtype=np.float32))
        recv_message(c)
        send_message(c, {"op": "run_until_output", "port": "x16_out",
                         "count": 1, "max_events": 100})
        recv_message(c)
        send_message(c, {"op": "read_port", "port": "x16_out"})
        _h, payload = recv_message(c)
        assert np.allclose(payload, [0.4], atol=1e-4)  # only b's 0.8*0.5
        c.close()
    finally:
        srv.stop()
