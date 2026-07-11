# SPDX-License-Identifier: GPL-3.0-or-later
"""HwChip / hw_transport unit tests — no board required.

Drives HwChip against a FakeTransport that models the devkyt fake_kyttar_gain gateware
(gain x2, output framed WRITE=0x63C0 / DATA(gained) / JUMP=0x73C0, hop=30). This proves the
placeKYT-side seam logic — word encoding, WRITE/DATA buffering, JUMP-triggered flush, and
tagged-output parsing — is correct BEFORE hardware bring-up. When the ZTEX board is flashed,
swap FakeTransport for the real FX3Transport and the same HwChip code runs against silicon.
"""

import struct

import pytest

from placekyt.engine.hw_chip import (
    HwChip, HwChipError, _encode_write, _encode_jump, _OP_WRITE, _OP_JUMP,
)
from placekyt.engine.hw_transport import pack_words, unpack_words


# --------------------------------------------------------------- pure helpers
def test_pack_unpack_roundtrip():
    words = [0x0000, 0x1234, 0xFFFF, 0x63C0, 0x73C0]
    assert unpack_words(pack_words(words)) == words


def test_pack_is_little_endian():
    # 0x1234 -> bytes 0x34, 0x12 (LE, matches FX3 fd[15:0] wire order)
    assert pack_words([0x1234]) == b"\x34\x12"


def test_unpack_drops_trailing_odd_byte():
    assert unpack_words(b"\x34\x12\xAA") == [0x1234]


def test_encode_write_jump_match_fpga_framing():
    # The fake-gain gateware emits WRITE=0x63C0 / JUMP=0x73C0 at hop=30, dest=0.
    assert _encode_write(30, 0) == 0x63C0
    assert _encode_jump(30, 0) == 0x73C0


def test_encode_fields():
    w = _encode_write(hop_cnt=30, addr=5)
    assert (w >> 12) & 0xF == _OP_WRITE
    assert (w >> 5) & 0x1F == 30
    assert w & 0x1F == 5


# ------------------------------------------------------- fake gateway transport
class FakeGainTransport:
    """Models the devkyt fake_kyttar_gain: WRITE/DATA buffer last_data; JUMP triggers
    gained = last_data*2 and emits WRITE(0x63C0)/DATA(gained)/JUMP(0x73C0)."""

    default_timeout_ms = 1000

    def __init__(self, gain=2):
        self.gain = gain
        self._last_data = 0
        self._out = []          # words queued for recv_words
        self._sent = []         # log of everything sent
        self.connected = False
        self.reset_calls = 0

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def ping(self, probe_word=0x1234, timeout_ms=500):
        # echo-style: a live board returns something
        return True

    def reset(self, leave=False):
        self.reset_calls += 1
        self._last_data = 0

    def send_words(self, words, timeout_ms=None):
        self._sent.append(list(words))
        i = 0
        while i < len(words):
            w = words[i]
            op = (w >> 12) & 0xF
            if op == _OP_WRITE and i + 1 < len(words):
                self._last_data = words[i + 1]
                i += 2
            elif op == _OP_JUMP:
                gained = (self._last_data * self.gain) & 0xFFFF
                self._out += [0x63C0, gained, 0x73C0]
                i += 1
            else:
                i += 1
        return len(words)

    def recv_words(self, max_words, timeout_ms=None):
        out, self._out = self._out[:max_words], self._out[max_words:]
        return out


def _hw():
    chip = HwChip(transport=FakeGainTransport())
    chip.connect()
    return chip


# ------------------------------------------------------------------ HwChip flow
def test_connect_requires_ping():
    class Dead(FakeGainTransport):
        def ping(self, probe_word=0x1234, timeout_ms=500):
            return False
    chip = HwChip(transport=Dead())
    with pytest.raises(HwChipError):
        chip.connect()


def test_gain_burst_roundtrip():
    chip = _hw()
    # sim's per-sample pattern: inject DATA=7, run (no-op), inject JUMP, run (no-op)
    chip.inject_data_physical([7], target_hop_cnt=30, target_addr=0)
    chip.run()  # NO-OP
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    chip.run()  # NO-OP
    out = chip.read_port_words_timed("x16_out")
    assert len(out) == 1
    value, dest, t = out[0]
    assert value == 14          # 7 * 2
    assert dest == 0            # WRITE dest field
    assert isinstance(t, float)


def test_run_is_noop():
    chip = _hw()
    assert chip.run(max_events=3000) is None


def test_write_data_buffers_until_jump():
    chip = _hw()
    chip.inject_data_physical([100], target_hop_cnt=30, target_addr=0)
    # nothing sent to the wire yet — WRITE/DATA is buffered pending the JUMP
    assert chip._t._sent == []
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    # now the whole WRITE/DATA/JUMP burst went in one flush
    assert chip._t._sent == [[_encode_write(30, 0), 100, _encode_jump(30, 1)]]


def test_multiple_samples_sequential():
    chip = _hw()
    for v in (3, 5, 8):
        chip.inject_data_physical([v], target_hop_cnt=30, target_addr=0)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    out = chip.read_port_words_timed("x16_out")
    assert [v for (v, _d, _t) in out] == [6, 10, 16]


def test_output_available():
    chip = _hw()
    assert chip.output_available("x16_out") == 0
    chip.inject_data_physical([2], target_hop_cnt=30, target_addr=0)
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    assert chip.output_available("x16_out") == 1
    chip.read_port_words_timed("x16_out")
    assert chip.output_available("x16_out") == 0


def test_read_port_i16_drains():
    chip = _hw()
    chip.inject_data_physical([9], target_hop_cnt=30, target_addr=0)
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    assert chip.read_port_i16("x16_out") == [18]
    assert chip.read_port_i16("x16_out") == []  # drained


def test_load_bitstream_resets_then_sends():
    chip = _hw()
    chip.load_bitstream_physical([_encode_write(30, 0), 0xABCD])
    assert chip._t.reset_calls == 1
    assert [_encode_write(30, 0), 0xABCD] in chip._t._sent


def test_trace_methods_are_noops():
    chip = _hw()
    assert chip.get_trace() == []
    assert chip.clear_trace() is None
    assert chip.port_ack_pending() is False


def test_requires_connect():
    chip = HwChip(transport=FakeGainTransport())  # not connected
    with pytest.raises(HwChipError):
        chip.inject_data_physical([1], target_hop_cnt=30, target_addr=0)


def test_negative_q15_word_wraps_clean():
    # a Q15 negative like -1 -> 0xFFFF must pack/encode without error
    chip = _hw()
    chip.inject_data_physical([0xFFFF], target_hop_cnt=30, target_addr=0)
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    out = chip.read_port_i16("x16_out")
    assert out == [(0xFFFF * 2) & 0xFFFF]  # 0xFFFE
