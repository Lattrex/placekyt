"""SRAM panel device + driver tests (the SRAM panel notes, task #166)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.sram_panel import (  # noqa: E402
    REG_ADDR_BASE,
    REG_PAYLOAD,
    REG_READ_JP_DESC,
    REG_READ_TRIGGER,
    REG_READ_WR_DESC,
    REG_WRITE_TRIGGER,
    PanelDriver,
    PushRead,
    SramPanelDevice,
    _decode,
)

from tests.conftest import CHIP_YAML  # noqa: E402


def _write_word(hop: int, dest: int) -> int:
    """Encode a WRITE descriptor (OP=0x6) with hop + dest."""
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (dest & 0x1F)


def _jump_word(hop: int, entry: int) -> int:
    """Encode a JUMP descriptor (OP=0x7) with hop + entry."""
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


class TestDecode:
    def test_write_word_fields(self):
        d = _decode(_write_word(10, 3))
        assert d.opcode == 0x6 and d.hop_cnt == 10 and d.dest == 3
        assert not d.is_noop

    def test_jump_word_fields(self):
        d = _decode(_jump_word(7, 1))
        assert d.opcode == 0x7 and d.hop_cnt == 7 and d.dest == 1

    def test_local_hop_is_noop(self):
        assert _decode(_write_word(31, 5)).is_noop      # @0 == HOP_CNT 31


class TestWritePath:
    def test_commit_write_stores_payload_at_address(self):
        dev = SramPanelDevice(size_words=1 << 16)
        dev.on_write(REG_PAYLOAD, 0xBEEF)
        dev.on_write(REG_ADDR_BASE, 0x0042)
        assert dev.on_jump(REG_WRITE_TRIGGER) is None    # commit, no push
        assert dev.mem[0x42] == 0xBEEF
        assert dev.writes_committed == 1

    def test_latched_address_reused(self):
        dev = SramPanelDevice()
        dev.on_write(REG_ADDR_BASE, 5)
        dev.on_write(REG_PAYLOAD, 100)
        dev.on_jump(REG_WRITE_TRIGGER)
        # address latched — second write to same address without rewriting R5
        dev.on_write(REG_PAYLOAD, 200)
        dev.on_jump(REG_WRITE_TRIGGER)
        assert dev.mem[5] == 200

    def test_write_to_trigger_register_ignored(self):
        dev = SramPanelDevice()
        dev.on_write(REG_WRITE_TRIGGER, 0x1234)          # WRITE to R0 → ignored
        assert dev.reg(REG_WRITE_TRIGGER) == 0


class TestReadPath:
    def _setup_read(self, dev, addr, value, wr_hop=10, wr_dest=7,
                    jp_hop=10, jp_entry=1):
        dev.mem[addr] = value
        dev.on_write(REG_READ_WR_DESC, _write_word(wr_hop, wr_dest))
        dev.on_write(REG_READ_JP_DESC, _jump_word(jp_hop, jp_entry))
        dev.on_write(REG_ADDR_BASE, addr)

    def test_push_read_emits_value_and_descriptors(self):
        dev = SramPanelDevice()
        self._setup_read(dev, addr=9, value=0xCAFE,
                         wr_hop=12, wr_dest=7, jp_hop=12, jp_entry=3)
        push = dev.on_jump(REG_READ_TRIGGER)
        assert isinstance(push, PushRead)
        assert push.value == 0xCAFE
        assert push.dest == 7 and push.write_hop == 12
        assert push.jump_entry == 3 and push.jump_hop == 12
        assert dev.reads_issued == 1

    def test_read_missing_address_is_zero(self):
        dev = SramPanelDevice()
        self._setup_read(dev, addr=3, value=0)
        dev.mem.pop(3, None)                              # never written
        push = dev.on_jump(REG_READ_TRIGGER)
        assert push.value == 0

    def test_disabled_write_descriptor_suppresses_read(self):
        dev = SramPanelDevice()
        # WRITE descriptor @0 (HOP=31) → disabled → no emission at all
        dev.on_write(REG_READ_WR_DESC, _write_word(31, 7))
        dev.on_write(REG_READ_JP_DESC, _jump_word(10, 1))
        assert dev.on_jump(REG_READ_TRIGGER) is None
        assert dev.reads_issued == 1                       # still counted

    def test_disabled_jump_descriptor_pushes_data_only(self):
        dev = SramPanelDevice()
        dev.mem[2] = 0x1111
        dev.on_write(REG_READ_WR_DESC, _write_word(10, 7))
        dev.on_write(REG_READ_JP_DESC, _jump_word(31, 1))  # JUMP @0 → disabled
        dev.on_write(REG_ADDR_BASE, 2)
        push = dev.on_jump(REG_READ_TRIGGER)
        assert push.value == 0x1111 and push.jump_entry is None


class TestMultiWordAddress:
    def test_two_register_address(self):
        dev = SramPanelDevice(size_words=1 << 18, addr_regs=2)
        dev.on_write(REG_ADDR_BASE, 0x0001)               # low 16b
        dev.on_write(REG_ADDR_BASE + 1, 0x0002)           # high bits
        dev.on_write(REG_PAYLOAD, 0xABCD)
        dev.on_jump(REG_WRITE_TRIGGER)
        assert dev.mem[0x20001] == 0xABCD                 # (2<<16)|1


class _FakeChip:
    """Minimal stand-in exercising the PanelDriver merge/inject logic."""

    def __init__(self):
        self._words = []      # (val, dest, time)
        self._jumps = []      # (entry, time)
        self.injected = []    # (port, samples, entry)
        self.hop_set = []     # (port, hop)

    def queue_word(self, val, dest, t):
        self._words.append((val, dest, t))

    def queue_jump(self, entry, t):
        self._jumps.append((entry, t))

    def read_port_words_timed(self, _port):
        out, self._words = self._words, []
        return out

    def read_port_jumps(self, _port):
        out, self._jumps = self._jumps, []
        return out

    def set_port_target_hop_count(self, port, hop):
        self.hop_set.append((port, hop))

    def write_port_multi_i16(self, port, samples, entry):
        self.injected.append((port, samples, entry))


class TestMycelisimPortApi:
    """The simkyt API additions that the host panel relies on
    (read_port_words_timed / read_port_jumps). Proven against a real chip:
    a cell emits a WRITE out a port and the host drains it with value + dest +
    time. (The full JUMP-trigger loop runs through the router in task #167.)"""

    CT = CHIP_YAML

    def _chip(self):
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        import simkyt
        return simkyt, simkyt.Chip.from_yaml(str(self.CT))

    def test_api_present_and_safe_on_empty_port(self):
        _, chip = self._chip()
        # Both drains exist and return empty (not error) on a quiet port.
        assert chip.read_port_words_timed("x16_out") == []
        assert chip.read_port_jumps("x16_out") == []

    def test_write_out_port_captured_with_dest_and_time(self):
        import numpy as np
        simkyt, chip = self._chip()
        W = 10
        def cid(x, y):
            return y * W + x
        # Program (0,0): on trigger, send R5 out east as a WRITE to dest=2.
        asm = "MOVE R0, R5\nWRITE @10, 2\nHALT\n"
        prg = simkyt.Program.from_source("emit", asm, base_address=1)
        chip.load_program(cid(0, 0), prg)
        chip.write_cell_memory(cid(0, 0), 5, 0x0042)       # value to send
        for x in range(10):                                # route east across row
            chip.set_fwd_face(cid(x, 0), "east")
        chip.set_port_entry_address("x16_in", 1)
        chip.set_port_target_hop_count("x16_in", 30)       # land locally at (0,0)
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        try:
            chip.run_until_output("x16_out", 1, 50000)
        except Exception:  # noqa: BLE001 — capture happens regardless
            pass
        words = chip.read_port_words_timed("x16_out")
        assert len(words) == 1
        value, dest, t = words[0]
        assert value == 0x0042 and dest == 2 and t > 0     # value + WRITE-dest + time


class TestFullLoopIntegration:
    """The complete cell → panel loop through REAL simkyt routing: a cell
    emits the panel write sequence (WRITE payload→R2, WRITE addr→R5, JUMP→R0)
    out a chip output port; the PanelDriver drains it (WRITEs + the JUMP trigger,
    captured via the new APIs) and commits to the array. This exercises the
    JUMP-trigger path deferred from #166."""

    CT = CHIP_YAML

    def test_cell_writes_word_into_panel(self):
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        import numpy as np

        import simkyt
        chip = simkyt.Chip.from_yaml(str(self.CT))
        W = 10
        def cid(x, y):
            return y * W + x
        # Program at (0,0): emit the panel WRITE sequence then the commit JUMP.
        # Payload/address live in HIGH registers (R20/R21) so they don't collide
        # with the program words at addresses 1..6.
        asm = ("MOVE R0, R20\n"      # R0 = payload
               "WRITE @10, 2\n"      # → panel R2 (payload)
               "MOVE R0, R21\n"      # R0 = address
               "WRITE @10, 5\n"      # → panel R5 (address)
               "JUMP @10, 0\n"       # → panel R0 (commit-write trigger)
               "HALT\n")
        prg = simkyt.Program.from_source("emit", asm, base_address=1)
        chip.load_program(cid(0, 0), prg)
        chip.write_cell_memory(cid(0, 0), 20, 0xBEEF)   # payload
        chip.write_cell_memory(cid(0, 0), 21, 0x0007)   # address
        for x in range(W):                              # route east to the port
            chip.set_fwd_face(cid(x, 0), "east")
        chip.set_port_entry_address("x16_in", 1)
        chip.set_port_target_hop_count("x16_in", 30)
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        try:
            chip.run_until_output("x16_out", 99, 80000)
        except Exception:  # noqa: BLE001
            pass
        dev = SramPanelDevice()
        drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
        drv.step()
        # The panel committed the payload at the addressed word.
        assert dev.writes_committed == 1
        assert dev.mem.get(0x0007) == 0xBEEF


class TestWriteReadRoundTrip:
    """The full demo mechanism through REAL routing: write a word into the
    panel, then READ it back via a push-read that delivers to a consumer cell
    which forwards it out the chip output port. Proves writes AND reads."""

    CT = CHIP_YAML

    @staticmethod
    def _wr(h, d):
        return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)

    @staticmethod
    def _jp(h, e):
        return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)

    def test_write_then_read_back_out_port(self):
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        import numpy as np

        import simkyt
        W = 10
        def cid(x, y):
            return y * W + x

        # --- WRITE phase: a cell writes 0xCAFE to panel address 3 ---
        chip = simkyt.Chip.from_yaml(str(self.CT))
        asm = "\n".join(["MOVE R0, R20", "WRITE @10, 2",
                         "MOVE R0, R21", "WRITE @10, 5",
                         "JUMP @10, 0", "HALT"])
        prg = simkyt.Program.from_source("w", asm, base_address=1)
        chip.load_program(cid(0, 0), prg)
        chip.write_cell_memory(cid(0, 0), 20, 0xCAFE)
        chip.write_cell_memory(cid(0, 0), 21, 3)
        for x in range(W):
            chip.set_fwd_face(cid(x, 0), "east")
        chip.set_port_entry_address("x16_in", 1)
        chip.set_port_target_hop_count("x16_in", 30)
        dev = SramPanelDevice()
        drv = PanelDriver(dev, chip, "x16_out", chip, "x16_in")
        chip.write_port("x16_in", np.array([0.0], dtype=np.float32))
        try:
            chip.run_until_output("x16_out", 99, 80000)
        except Exception:  # noqa: BLE001
            pass
        drv.step()
        assert dev.mem.get(3) == 0xCAFE          # write committed

        # --- READ phase: read address 3 back out through a consumer cell ---
        chip2 = simkyt.Chip.from_yaml(str(self.CT))
        consumer = "\n".join(["MOVE R0, R20", "WRITE @10, 0", "HALT"])
        cprg = simkyt.Program.from_source("c", consumer, base_address=1)
        chip2.load_program(cid(0, 0), cprg)
        for x in range(W):
            chip2.set_fwd_face(cid(x, 0), "east")
        drv2 = PanelDriver(dev, chip2, "x16_out", chip2, "x16_in")
        # Read-out descriptors: deliver value to (0,0).R20, then JUMP entry 1.
        dev.on_write(3, self._wr(30, 20))
        dev.on_write(4, self._jp(30, 1))
        dev.on_write(5, 3)
        push = dev.on_jump(1)                     # read trigger
        assert push is not None and push.value == 0xCAFE
        drv2._inject(push)
        try:
            chip2.run_until_output("x16_out", 1, 80000)
        except Exception:  # noqa: BLE001
            pass
        out = chip2.read_port_words_timed("x16_out")
        # the value read from the panel came back out the chip output port
        assert any(v == 0xCAFE for v, _d, _t in out)


class TestSramDemo:
    """The FULL runnable demo (engine/sram_demo.py): data enters x16_in, a
    placed SramController carries it to the panel over the x1 ports through a
    crossover relay, then reads it back out x16_out — all over REAL routing."""

    CT = CHIP_YAML

    def test_full_demo_write_then_read_out_x16(self):
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        from engine.sram_demo import run_demo
        r = run_demo(str(self.CT))
        # every word written IN x16_in reads back OUT x16_out, in order
        assert list(r.written.values()) == list(r.read_back)
        assert len(r.written) >= 4
        # the panel actually stored them (for the inspector)
        assert all(r.device.mem.get(a) == v for a, v in r.written.items())
        # the timeline has both write and read activity batches
        kinds = {k for batch in r.timeline for _addr, k in batch}
        assert kinds == {"w", "r"}

    def test_input_stimulus_is_paced_not_flooded(self):
        """The input port has NO FIFO: the 12 stimulus transactions (6 write +
        6 read JUMP triggers) enter x16_in at DISTINCT sim-times, one paced
        transaction at a time — not all dumped at the same instant (which made
        it look like a single input op drove everything)."""
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        import simkyt

        from engine.build import BuildEngine
        from engine.catalog import BlockCatalog
        from engine.io.chip_type_io import load_chip_type
        from engine.sram_demo import build_demo_project, build_demo_stimulus
        from engine.sram_panel import PanelDriver, SramPanelDevice
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(str(self.CT))
        res = BuildEngine(cat, str(self.CT)).build(
            build_demo_project(), {"kyttar_10x12": ct})
        chip = simkyt.Chip.from_yaml(str(self.CT))
        chip.load_bitstream_physical(res.words(0))
        chip.set_port_handshake("x1_out", True)
        chip.enable_trace()
        dev = SramPanelDevice()
        drv = PanelDriver(dev, chip, "x1_out", chip, "x1_in")
        chip.queue_words_physical("x16_in", build_demo_stimulus())  # PACED
        for _ in range(4000):
            chip.run(max_events=32)
            drv.step()
            if dev.writes_committed >= 6 and dev.reads_issued >= 6:
                break

        def _val(e):
            d = e.get("data", 0)
            return int(d, 0) if isinstance(d, str) else int(d)
        # Count the STIMULUS triggers injected at x16_in only. (The panel's
        # read push-backs into x1_in are now ALSO paced port injections — a
        # correct #192 improvement — and would otherwise inflate the count.)
        trig_times = [e.get("time_ns") for e in chip.get_trace()
                      if e.get("kind") == "port_injection"
                      and e.get("port_name") == "x16_in"
                      and (_val(e) >> 12) == 0x7]
        assert len(trig_times) == 12                     # 6 writes + 6 reads
        # The triggers are spread across time, NOT all at one instant.
        assert len(set(trig_times)) >= 10

    def test_panel_port_is_single_outstanding_no_fifo(self):
        """With the panel-fed output port marked HELD-ACK, the controller stalls
        after EACH word until the panel releases — there is no FIFO. Without any
        release the controller cannot dump its burst: at most ONE word is
        captured and an ack stays pending."""
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        import simkyt

        from engine.build import BuildEngine
        from engine.catalog import BlockCatalog
        from engine.io.chip_type_io import load_chip_type
        from engine.sram_demo import build_demo_project, build_demo_stimulus
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(str(self.CT))
        res = BuildEngine(cat, str(self.CT)).build(
            build_demo_project(), {"kyttar_10x12": ct})
        chip = simkyt.Chip.from_yaml(str(self.CT))
        chip.load_bitstream_physical(res.words(0))
        chip.set_port_handshake("x1_out", True)
        chip.inject_words_physical(build_demo_stimulus())
        # Run a lot WITHOUT releasing — the controller must stall, NOT dump.
        for _ in range(200):
            chip.run(max_events=64)
        assert chip.port_ack_pending("x1_out")          # a cell is stalled
        captured = chip.read_port_words_timed("x1_out")
        assert len(captured) <= 1                        # no FIFO dump

    def test_demo_project_builds_with_catalog_blocks(self):
        import pytest
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        from engine.build import BuildEngine
        from engine.catalog import BlockCatalog
        from engine.io.chip_type_io import load_chip_type
        from engine.sram_demo import build_demo_project
        p = build_demo_project()
        # the placeable demo uses the registered SramController + Crossover.
        types = {b.type for b in p.blocks}
        assert {"SramControllerBlock", "CrossoverBlock"} <= types
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(str(self.CT))
        res = BuildEngine(cat, str(self.CT)).build(p, {p.chip_type: ct})
        assert res.ok, "; ".join(str(e) for e in res.errors)


class TestPanelDriver:
    def test_write_then_trigger_in_time_order(self):
        dev = SramPanelDevice()
        chip = _FakeChip()
        drv = PanelDriver(dev, chip, "out", chip, "in")
        # WRITE payload@t=1, WRITE addr@t=2, JUMP commit@t=3 (out of insert order)
        chip.queue_jump(REG_WRITE_TRIGGER, 3.0)
        chip.queue_word(0x55, REG_PAYLOAD, 1.0)
        chip.queue_word(7, REG_ADDR_BASE, 2.0)
        drv.step()
        assert dev.mem[7] == 0x55                         # ordered correctly

    def test_read_injects_push_into_input_port(self):
        dev = SramPanelDevice()
        dev.mem[4] = 0x9999
        chip = _FakeChip()
        drv = PanelDriver(dev, chip, "out", chip, "in")
        chip.queue_word(_write_word(8, 6), REG_READ_WR_DESC, 1.0)
        chip.queue_word(_jump_word(8, 2), REG_READ_JP_DESC, 2.0)
        chip.queue_word(4, REG_ADDR_BASE, 3.0)
        chip.queue_jump(REG_READ_TRIGGER, 4.0)
        n = drv.step()
        assert n == 1
        port, samples, entry = chip.injected[0]
        assert port == "in"
        assert samples == [[(6, 0x9999)]]                 # dest=6, value
        assert entry == 2                                 # jump entry
        assert ("in", 8) in chip.hop_set                  # hop from descriptor
