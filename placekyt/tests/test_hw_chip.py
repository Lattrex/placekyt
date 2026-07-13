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

    def ping(self, timeout_ms=500):
        # gateware-agnostic firmware liveness (VR 0x64) — always live once connected
        return True

    def probe_roundtrip(self, words, max_read=16, timeout_ms=800):
        self.send_words(words)
        return self.recv_words(max_read)

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
    # connect() runs a gain-probe round-trip (send burst + reset) as its data-plane
    # liveness check; clear the fake's logs so each test starts from a clean post-
    # connect baseline (the probe's _sent/reset_calls are not what the tests assert on).
    chip._t._sent.clear()
    chip._t.reset_calls = 0
    chip._out_words.clear()
    return chip


# ------------------------------------------------------------------ HwChip flow
def test_connect_requires_firmware_ping():
    """Stage-1 failure: FX3 firmware doesn't answer control transfers."""
    class Dead(FakeGainTransport):
        def ping(self, timeout_ms=500):
            return False
    chip = HwChip(transport=Dead())
    with pytest.raises(HwChipError):
        chip.connect()


def test_connect_requires_gain_dataplane():
    """Stage-2 failure: firmware alive but the gateware doesn't echo a 2x burst
    (e.g. wrong bitstream flashed). connect() must reject it."""
    class Mute(FakeGainTransport):
        def probe_roundtrip(self, words, max_read=16, timeout_ms=800):
            return []  # no response
    chip = HwChip(transport=Mute())
    with pytest.raises(HwChipError):
        chip.connect()


def test_connect_can_skip_dataplane_verify():
    """A non-gain gateware brings up with verify_dataplane=False (firmware ping only)."""
    class Mute(FakeGainTransport):
        def probe_roundtrip(self, words, max_read=16, timeout_ms=800):
            return []
    chip = HwChip(transport=Mute())
    chip.connect(verify_dataplane=False)  # must not raise
    assert chip.connected


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


def test_negative_q15_word_reads_signed():
    # a Q15 negative comes back as a SIGNED int16, not an unsigned wrap. The fake
    # x2 gain on 0xFFFF (-1) gives 0xFFFE, which read_port_i16 must report as -2.
    chip = _hw()
    chip.inject_data_physical([0xFFFF], target_hop_cnt=30, target_addr=0)
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    assert chip.read_port_i16("x16_out") == [-2]  # 0xFFFE signed


def test_read_port_q15_converts_to_float():
    # read_port must Q15-convert (word/32768) like simkyt.Chip.read_port — the server's
    # non-raw path does float(v) on this and expects a scaled fraction.
    chip = _hw()
    chip.inject_data_physical([0x4000], target_hop_cnt=30, target_addr=0)  # 0.5
    chip.inject_jump_physical(target_hop_cnt=30, entry_addr=1)
    out = chip.read_port("x16_out")   # fake x2: 0.5*2 = 1.0 -> 0xFFFE.../0x8000 range
    assert len(out) == 1 and isinstance(out[0], float)
    # 0x4000*2 = 0x8000 = -32768 signed -> -1.0 (the x2 fake overflows; value shape check)
    assert -1.0 <= out[0] <= 1.0


# ---------------------------------------------------- controller HW-mode wiring
# Exercises the SimController's hardware-mode methods (mode toggle, program,
# reset, guards) against a fake HwChip — the UI mode-toggle logic, no board and
# no Qt event loop. Mirrors the plan §3 Sim<->Hardware toggle behavior.

class _FakeHwChip:
    def __init__(self):
        self.connected = False
        self.programmed = None
        self.reset_calls = 0

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def load_bitstream_physical(self, words):
        self.programmed = list(words)

    def reset(self):
        self.reset_calls += 1


class _CtrlStub:
    """Minimal stand-in binding the real SimController HW methods, so we test the
    logic without constructing the full Qt controller."""
    _hardware_mode = False
    _hw_chip = None
    _gr_server = None

    class _App:
        @staticmethod
        def build():
            class _R:
                ok = True
                errors = []

                def words(self, i):
                    return [0x63C0, 0xABCD]
            return _R()

    class _State:
        @staticmethod
        def emit(*a):
            pass

    app = _App()
    state_changed = _State()


def _bind_ctrl(monkeypatch):
    pytest.importorskip("PySide6")
    from ui.sim_controller import SimController
    import engine.hw_chip as hc
    monkeypatch.setattr(hc, "HwChip", _FakeHwChip)
    for name in ("hardware_mode", "_ensure_hw_chip", "hardware_connection_check",
                 "set_hardware_mode", "hardware_program", "hardware_global_reset",
                 "_hw_reset_threadsafe"):
        setattr(_CtrlStub, name, getattr(SimController, name))
    return _CtrlStub()


def test_ctrl_connection_check_and_enter(monkeypatch):
    s = _bind_ctrl(monkeypatch)
    ok, _ = s.hardware_connection_check()
    assert ok
    ok, _ = s.set_hardware_mode(True)
    assert ok and s.hardware_mode is True


def test_ctrl_program_loads_built_words(monkeypatch):
    s = _bind_ctrl(monkeypatch)
    s.set_hardware_mode(True)
    ok, _ = s.hardware_program()
    assert ok and s._hw_chip.programmed == [0x63C0, 0xABCD]


def test_ctrl_global_reset(monkeypatch):
    s = _bind_ctrl(monkeypatch)
    s.set_hardware_mode(True)
    ok, _ = s.hardware_global_reset()
    assert ok and s._hw_chip.reset_calls == 1


def test_ctrl_hw_reset_callback_returns_same_chip(monkeypatch):
    s = _bind_ctrl(monkeypatch)
    s.set_hardware_mode(True)
    chip = s._hw_chip
    assert s._hw_reset_threadsafe() is chip
    assert chip.reset_calls == 1


def test_ctrl_leaving_hw_mode_closes_chip(monkeypatch):
    s = _bind_ctrl(monkeypatch)
    s.set_hardware_mode(True)
    s.set_hardware_mode(False)
    assert s.hardware_mode is False and s._hw_chip is None


def test_ctrl_toggle_while_server_running_restarts_it(monkeypatch):
    # Toggling Hardware Mode with a server up RESTARTS the server on the new backend
    # (so the user can Run-as-Server first, then flip to hardware — either order works).
    s = _bind_ctrl(monkeypatch)

    class _FakeServer:
        bound_port = 58950
    s._gr_server = _FakeServer()

    restarts = {"stop": 0, "start": None}
    s.stop_gnuradio_server = lambda: restarts.__setitem__("stop", restarts["stop"] + 1)
    s.start_gnuradio_server = lambda port=0: restarts.__setitem__("start", port)

    ok, msg = s.set_hardware_mode(True)
    assert ok and s.hardware_mode is True
    assert restarts["stop"] == 1 and restarts["start"] == 58950  # restarted, same port


def test_ctrl_absent_board_stays_in_sim_mode(monkeypatch):
    pytest.importorskip("PySide6")
    from ui.sim_controller import SimController
    from engine.hw_chip import HwChipError
    import engine.hw_chip as hc

    class _Dead:
        connected = False

        def connect(self):
            raise HwChipError("ZTEX board not found")

    monkeypatch.setattr(hc, "HwChip", _Dead)
    for name in ("hardware_mode", "_ensure_hw_chip", "hardware_connection_check",
                 "set_hardware_mode"):
        setattr(_CtrlStub, name, getattr(SimController, name))
    s = _CtrlStub()
    s._hardware_mode = False
    s._hw_chip = None
    s._gr_server = None
    ok, _ = s.set_hardware_mode(True)
    assert not ok and s.hardware_mode is False and s._hw_chip is None
