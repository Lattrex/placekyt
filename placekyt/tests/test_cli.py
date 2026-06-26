"""Tests for the CLI, chip-type registry, stimulus parser, and sim compare.

CLI tests run in-process via ``cli.main(argv)`` and assert exit codes (§11.4)
and stdout/stderr. The build/test paths need gr_kyttar + simkyt (venv).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli
from engine.io.errors import ProjectFileError
from engine.registry import ChipTypeRegistry
from engine.errors import RegistryError

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
needs_chip = pytest.mark.skipif(not CT_PATH.exists(), reason="chip-type yaml absent")

SAMPLE_PROJECT = """\
project:
  name: CLI Test
chip_type: kyttar_10x12
chips:
  - id: 0
    label: C0
blocks:
  - name: agc
    type: AGCBlock
    library: lattrex.official
    params: {reference: 0.7}
    placement:
      chip: 0
      cells:
        - {cell_id: 0, x: 0, y: 0, face: east}
"""

OVERLAP_PROJECT = """\
project:
  name: Overlap
chip_type: kyttar_10x12
chips:
  - id: 0
blocks:
  - name: a
    type: AGCBlock
    library: lattrex.official
    placement: {chip: 0, cells: [{cell_id: 0, x: 3, y: 3, face: east}]}
  - name: b
    type: AGCBlock
    library: lattrex.official
    placement: {chip: 0, cells: [{cell_id: 0, x: 3, y: 3, face: east}]}
"""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@needs_chip
class TestRegistry:
    def test_register_and_resolve(self):
        reg = ChipTypeRegistry()
        reg.register_file(CT_PATH)
        assert reg.require("kyttar_10x12").chip_type.cell_count == 120
        assert "kyttar_10x12" in reg.paths()

    def test_unknown_raises(self):
        with pytest.raises(RegistryError):
            ChipTypeRegistry().require("nope")

    def test_scan_dir(self):
        reg = ChipTypeRegistry.from_dirs([CT_PATH.parent])
        assert "kyttar_10x12" in reg.names()


# --------------------------------------------------------------------------- #
# CLI (in-process)
# --------------------------------------------------------------------------- #


@needs_chip
class TestCli:
    def _argv(self, *parts):
        return [*parts, "--chip-type", str(CT_PATH)]

    def test_drc_warnings_only_exit_2(self, tmp_path, capsys):
        proj = tmp_path / "p.kyt"
        proj.write_text(SAMPLE_PROJECT)
        rc = cli.main(self._argv("--drc", str(proj)))
        assert rc == cli.EXIT_WARNINGS  # unused_port warning, no errors

    def test_drc_errors_exit_1(self, tmp_path):
        proj = tmp_path / "p.kyt"
        proj.write_text(OVERLAP_PROJECT)
        rc = cli.main(self._argv("--drc", str(proj)))
        assert rc == cli.EXIT_DRC_ERRORS

    def test_build_emits_json_and_kbs(self, tmp_path, capsys):
        proj = tmp_path / "p.kyt"
        proj.write_text(SAMPLE_PROJECT)
        out = tmp_path / "out.kbs"
        rc = cli.main(self._argv("--build", str(proj), "-o", str(out)))
        assert rc in (cli.EXIT_OK, cli.EXIT_WARNINGS)
        assert out.exists()
        captured = capsys.readouterr()
        summary = json.loads(captured.out.strip().splitlines()[-1])
        assert summary["status"] == "ok"
        assert summary["chips"] == 1
        assert summary["output"] == str(out)

    def test_build_errors_exit_1(self, tmp_path):
        proj = tmp_path / "p.kyt"
        proj.write_text(OVERLAP_PROJECT)
        rc = cli.main(self._argv("--build", str(proj), "-o", str(tmp_path / "o.kbs")))
        assert rc == cli.EXIT_DRC_ERRORS

    def test_info_prints_metadata(self, tmp_path, capsys):
        proj = tmp_path / "p.kyt"
        proj.write_text(SAMPLE_PROJECT)
        out = tmp_path / "out.kbs"
        cli.main(self._argv("--build", str(proj), "-o", str(out)))
        capsys.readouterr()  # clear
        rc = cli.main(["--info", str(out)])
        assert rc == cli.EXIT_OK
        info = json.loads(capsys.readouterr().out)
        assert info["chips"] == 1
        assert info["metadata"]["project_name"] == "CLI Test"

    def test_file_not_found_exit_3(self, tmp_path):
        rc = cli.main(self._argv("--drc", str(tmp_path / "nope.kyt")))
        assert rc == cli.EXIT_FILE_ERROR

    def test_test_without_golden_exit_3(self, tmp_path):
        proj = tmp_path / "p.kyt"
        proj.write_text(SAMPLE_PROJECT)  # no simulation.golden_output
        rc = cli.main(self._argv("--test", str(proj)))
        assert rc == cli.EXIT_FILE_ERROR

    @needs_chip
    def test_test_passes_against_kbs_golden(self, capsys):
        # End-to-end --test on the gain demo: a .kbs stimulus + .kbs golden
        # (bitstream, no CSV) → inject, run, capture tagged output, compare.
        demo = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
        if not demo.exists():
            pytest.skip("gain demo absent")
        rc = cli.main(self._argv("--test", str(demo)))
        assert rc == cli.EXIT_OK
        assert "test PASSED" in capsys.readouterr().out

    def test_test_build_error_exit_1(self, tmp_path):
        # A project that fails DRC, but with a golden configured, exercises the
        # --test build-failure path before any simulation runs.
        from engine.io.kbs import write_golden_kbs
        proj = tmp_path / "p.kyt"
        proj.write_text(OVERLAP_PROJECT + "simulation:\n  golden_output: g.kbs\n")
        write_golden_kbs([0x6000, 0x0001], tmp_path / "g.kbs")
        rc = cli.main(self._argv("--test", str(proj)))
        assert rc == cli.EXIT_DRC_ERRORS

    def test_build_without_output(self, tmp_path, capsys):
        # --build with no -o still runs DRC+build and prints JSON (output=null).
        proj = tmp_path / "p.kyt"
        proj.write_text(SAMPLE_PROJECT)
        rc = cli.main(self._argv("--build", str(proj)))
        assert rc in (cli.EXIT_OK, cli.EXIT_WARNINGS)
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert summary["output"] is None

    def test_malformed_yaml_exit_3(self, tmp_path):
        proj = tmp_path / "bad.kyt"
        proj.write_text("project: {name: [unterminated\n")  # invalid YAML
        rc = cli.main(self._argv("--drc", str(proj)))
        assert rc == cli.EXIT_FILE_ERROR

    def test_info_bad_magic_exit_3(self, tmp_path):
        bad = tmp_path / "bad.kbs"
        bad.write_bytes(b"NOTAKBS!" + b"\x00" * 20)
        rc = cli.main(["--info", str(bad)])
        assert rc == cli.EXIT_FILE_ERROR


class TestSimulatorMethods:
    """Methods that don't require driving data through a connected design."""

    @needs_chip
    def test_reset_recreates_chip(self):
        from engine.simulator import SimulationEngine

        sim = SimulationEngine(CT_PATH)
        first = sim.chip
        sim.reset()
        assert sim.chip is not first

    @needs_chip
    def test_simkyt_version(self):
        from engine.simulator import SimulationEngine

        v = SimulationEngine(CT_PATH).simkyt_version
        assert isinstance(v, str) and v


class TestSimulatorIOWithFakeChip:
    """Exercise inject/run/capture/compare plumbing against a fake chip, so the
    adapter logic is tested without needing a connected design that drives real
    output (that demo project is the Week 9 deliverable)."""

    @needs_chip
    def _sim_with_fake(self):
        import numpy as np

        from engine.simulator import SimulationEngine

        class FakeChip:
            def __init__(self):
                self.written = {}
                self.ran = []

            def write_port_i16(self, port, arr):
                self.written.setdefault(port, []).extend(int(v) for v in arr)

            def run_until_output(self, port, count, max_events):
                self.ran.append((port, count, max_events))

            def read_port_i16(self, port):
                # echo back whatever was written, as an int16 array
                return np.asarray(self.written.get("x16_in", []), dtype=np.int16)

        sim = SimulationEngine(CT_PATH)
        sim.chip = FakeChip()
        return sim

    def test_inject_and_capture(self):
        sim = self._sim_with_fake()
        sim.inject("x16_in", [0x0100, 0x0200])
        sim.run_until_output("x16_out", 2)
        assert sim.capture("x16_in") == [0x0100, 0x0200]
        assert sim.chip.ran == [("x16_out", 2, sim.run_until_output.__defaults__[0])]


class TestDisasm:
    """Bitstream disassembler (#183): WRITE+DATA+JUMP-aware mnemonic listing."""

    def test_disassemble_word_basic(self):
        from engine.disasm import disassemble_word
        assert disassemble_word(0x6204) == "WRITE @15, 4"   # WRITE dest R4
        assert disassemble_word(0x720F) == "JUMP @15, 15"   # JUMP entry 15
        assert disassemble_word(0x0000) == "HALT"
        # A data payload renders as a literal, not a decoded instruction.
        assert disassemble_word(0xCAFE, is_data=True) == "DW   0xCAFE"

    def test_stateful_tracks_write_data_jump(self):
        from engine.disasm import disassemble_bitstream
        # WRITE, then its DATA (0xCAFE — must NOT decode as an instruction), JUMP.
        listing = disassemble_bitstream([0x6204, 0xCAFE, 0x720F])
        lines = listing.splitlines()
        assert "WRITE @15, 4" in lines[0]
        assert "DW   0xCAFE" in lines[1]       # data payload, not MUL
        assert "JUMP @15, 15" in lines[2]

    def test_flat_decodes_every_word(self):
        from engine.disasm import disassemble_bitstream
        # Flat mode does NOT track WRITE->DATA: 0xCAFE decodes as an instruction.
        listing = disassemble_bitstream([0x6204, 0xCAFE], stateful=False)
        assert "DW" not in listing

    def test_cli_disasm_kbs(self, tmp_path, capsys):
        from engine.io.kbs import write_stimulus_kbs
        kbs = tmp_path / "s.kbs"
        write_stimulus_kbs([0x6204, 0xCAFE, 0x720F], kbs, name="t")
        rc = cli.main(["--disasm", str(kbs)])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "stimulus" in out               # auto-detected the kind
        assert "WRITE @15, 4" in out
        assert "DW   0xCAFE" in out
        assert "JUMP @15, 15" in out
