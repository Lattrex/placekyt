"""Captured-output + golden as a .kbs bitstream with the WRITE-descriptor tag
(#185): encode/decode round-trip + word-by-word tagged compare."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.io.output_bitstream import (
    OutWord,
    compare_output,
    decode_output,
    encode_output,
)

from tests.conftest import CHIP_YAML


def test_encode_decode_round_trip():
    ws = [OutWord(False, 0x1000, 1),   # WRITE value 0x1000 tagged dest 1 (I)
          OutWord(False, 0x2000, 11),  # WRITE value 0x2000 tagged dest 11 (Q)
          OutWord(True, 0, 5)]         # JUMP entry 5
    enc = encode_output(ws)
    # WRITE(dest)+DATA per write; JUMP standalone.
    assert enc == [0x6001, 0x1000, 0x600B, 0x2000, 0x7005]
    assert decode_output(enc) == ws


def test_compare_exact_pass():
    ws = [OutWord(False, 0x1000, 1), OutWord(True, 0, 5)]
    assert compare_output(ws, ws).passed


def test_compare_wrong_tag_is_mismatch():
    # Same VALUE but wrong dest tag → mismatch (virtual-channel correctness).
    golden = [OutWord(False, 0x1000, 1)]
    actual = [OutWord(False, 0x1000, 2)]
    r = compare_output(actual, golden)
    assert not r.passed
    assert r.first_mismatch == 0


def test_compare_value_tolerance():
    golden = [OutWord(False, 0x1000, 1)]
    actual = [OutWord(False, 0x1002, 1)]
    assert compare_output(actual, golden, tolerance=2).passed
    assert not compare_output(actual, golden, tolerance=1).passed


def test_compare_short_capture_fails():
    golden = [OutWord(False, 0x1000, 1), OutWord(False, 0x2000, 1)]
    actual = [OutWord(False, 0x1000, 1)]
    assert not compare_output(actual, golden).passed


def test_disassembles_as_real_bitstream():
    # The output bitstream is a real WRITE+DATA stream the disassembler reads.
    from engine.disasm import disassemble_bitstream
    enc = encode_output([OutWord(False, 0xBEEF, 11)])
    listing = disassemble_bitstream(enc)
    assert "WRITE" in listing and "11" in listing
    assert "DW   0xBEEF" in listing


class TestGoldenKbsCompare:
    """End-to-end: capture output as tagged words, write a golden .kbs, and
    compare a fresh run against it (value AND tag)."""

    CT = CHIP_YAML
    DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"

    def _setup(self):
        import pytest as _pytest
        if not (self.CT.exists() and self.DEMO.exists()):
            _pytest.skip("chip-type / demo absent")
        from engine.build import BuildEngine
        from engine.catalog import BlockCatalog
        from engine.io.chip_type_io import load_chip_type
        from engine.port_config import input_port_config, values_to_bitstream
        from ui.controller import AppController
        cat = BlockCatalog.from_gr_kyttar()
        ctrl = AppController(catalog=cat)
        ctrl.open_project(str(self.DEMO))
        ct = load_chip_type(str(self.CT))
        res = BuildEngine(cat, str(self.CT)).build(
            ctrl.project, {ctrl.project.chip_type: ct})
        _p, kw = input_port_config(ctrl.project, ctrl.registry, cat)
        stim = values_to_bitstream([0x4000, 0x2000], kw)
        return res.words(0), stim

    def _run_capture(self, prog, stim):
        from engine.simulator import SimulationEngine
        eng = SimulationEngine(str(self.CT))
        eng.load(prog)
        eng.inject_words(stim)
        for _ in range(3000):
            info = eng.chip.run(max_events=64)
            if (isinstance(info, dict)
                    and info.get("stop_reason") == "QueueEmpty"
                    and eng.chip.run(max_events=0).get("stop_reason")
                    == "QueueEmpty"):
                break
        return eng.capture_output_words("x16_out")

    def test_golden_roundtrip_and_compare(self, tmp_path):
        from engine.io.kbs import write_golden_kbs
        from engine.simulator import SimulationEngine
        prog, stim = self._setup()
        golden = encode_output(self._run_capture(prog, stim))
        gp = tmp_path / "g.kbs"
        write_golden_kbs(golden, gp)
        # fresh run, compare vs the golden .kbs
        eng = SimulationEngine(str(self.CT))
        eng.load(prog)
        r = eng.compare_bitstream("x16_out", gp, in_port="x16_in",
                                  stimulus_words=stim)
        assert r.passed and r.mismatches == 0 and r.compared >= 2

    def test_tampered_golden_fails(self, tmp_path):
        from engine.io.kbs import read_golden_kbs, write_golden_kbs
        from engine.simulator import SimulationEngine
        prog, stim = self._setup()
        golden = encode_output(self._run_capture(prog, stim))
        gp = tmp_path / "g.kbs"
        write_golden_kbs(golden, gp)
        dec = decode_output(read_golden_kbs(gp))
        dec[0] = OutWord(dec[0].is_jump, 0xDEAD, dec[0].dest)
        gp2 = tmp_path / "g2.kbs"
        write_golden_kbs(encode_output(dec), gp2)
        eng = SimulationEngine(str(self.CT))
        eng.load(prog)
        r = eng.compare_bitstream("x16_out", gp2, in_port="x16_in",
                                  stimulus_words=stim)
        assert not r.passed
