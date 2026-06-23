"""End-to-end demo test: the full pipeline drives output through the chip.

Closes the gap noted when the CLI landed: a fully connected + routed project
(`tests/data/demo/gain_demo.kyt`) actually produces output on x16_out, so
`--test` and `sim.compare` validate against a real captured golden.

Requires gr_kyttar + simkyt (venv).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli
from engine.build import BuildEngine
from engine.catalog import BlockCatalog
from engine.io.project_io import load_project
from engine.registry import ChipTypeRegistry
from engine.simulator import SimulationEngine

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
DEMO_DIR = Path(__file__).parent / "data" / "demo"
DEMO = DEMO_DIR / "gain_demo.kyt"
BPSK_DEMO = DEMO_DIR / "bpsk_demo.kyt"
BPSK_DUPLEX_DEMO = DEMO_DIR / "bpsk_duplex_demo.kyt"
QAM16_DEMO = DEMO_DIR / "qam16_demo.kyt"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and DEMO.exists()),
    reason="chip-type yaml or demo project absent",
)


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def registry():
    reg = ChipTypeRegistry()
    reg.register_file(CT_PATH)
    return reg


class TestDemoBuildsAndFlows:
    def test_demo_builds_clean(self, catalog, registry):
        project = load_project(DEMO)
        res = BuildEngine(catalog, registry.paths()).build(
            project, registry.chip_types()
        )
        assert res.ok, [str(e) for e in res.errors]
        # connected demo -> no unused_port warning
        assert not any(w.category == "unused_port" for w in res.warnings)

    def test_output_flows_to_x16_out(self, catalog, registry):
        """The core regression: data injected at x16_in appears at x16_out."""
        project = load_project(DEMO)
        res = BuildEngine(catalog, registry.paths()).build(
            project, registry.chip_types()
        )
        # v2 blocks resolve entry + input register dynamically — use the
        # build-resolved values, not the static interface defaults.
        entry, in_regs = catalog.resolved_io("GainBlock")
        sim = SimulationEngine(CT_PATH)
        sim.load(res.words(0))
        sim.configure_input_port(
            "x16_in",
            entry_addr=entry,
            hop_count=30,
            data_addr=in_regs[0],
        )
        ins = [0x0CCD, 0x199A, 0x2666, 0x4000]
        sim.inject("x16_in", ins)
        sim.run_until_output("x16_out", len(ins))
        out = sim.capture("x16_out")
        assert len(out) == len(ins)
        # gain 0.5 -> (x * 16384) >> 15
        assert out == [(v * 16384) >> 15 for v in ins]


class TestBpskDemo:
    """The BPSK symbol-level loopback demo: bits in == bits out (clean channel)."""

    def test_bpsk_builds_clean(self, catalog, registry):
        project = load_project(BPSK_DEMO)
        res = BuildEngine(catalog, registry.paths()).build(
            project, registry.chip_types()
        )
        assert res.ok, [str(e) for e in res.errors]
        assert not any(w.category == "unused_port" for w in res.warnings)

    def test_bpsk_cli_test_passes(self, capsys):
        rc = cli.main(["--test", str(BPSK_DEMO), "--chip-type", str(CT_PATH)])
        assert rc == cli.EXIT_OK
        assert "PASSED" in capsys.readouterr().out

    def test_bpsk_identity_bits_in_equal_bits_out(self):
        """Inject DEMO_BITS, slice the output LLR bits, expect them back exactly."""
        from engine.bpsk_demo import DEMO_BITS, build_stimulus, _mapper_entry

        project = load_project(BPSK_DEMO)
        catalog = BlockCatalog.from_gr_kyttar()
        reg = ChipTypeRegistry()
        reg.register_file(CT_PATH)
        res = BuildEngine(catalog, reg.paths()).build(project, reg.chip_types())
        assert res.ok, [str(e) for e in res.errors]

        stim = build_stimulus(_mapper_entry(res))
        sim = SimulationEngine(CT_PATH)
        sim.load(res.words(0))
        sim.inject_words(stim, port="x16_in")
        for _ in range(20000):
            info = sim.chip.run(max_events=64)
            if (isinstance(info, dict)
                    and info.get("stop_reason") == "QueueEmpty"
                    and sim.chip.run(max_events=0).get("stop_reason") == "QueueEmpty"):
                break
        out = sim.capture_output_words("x16_out")
        bits = [w.value for w in out if not w.is_jump]
        assert bits == DEMO_BITS


class TestQam16Demo:
    """The 16-QAM symbol-level loopback demo (one composed transceiver block):
    symbols in == symbols out (clean channel, the mapper+slicer identity)."""

    def test_qam16_builds_clean(self, catalog, registry):
        project = load_project(QAM16_DEMO)
        res = BuildEngine(catalog, registry.paths()).build(
            project, registry.chip_types()
        )
        assert res.ok, [str(e) for e in res.errors]

    def test_qam16_cli_test_passes(self, capsys):
        rc = cli.main(["--test", str(QAM16_DEMO), "--chip-type", str(CT_PATH)])
        assert rc == cli.EXIT_OK
        assert "PASSED" in capsys.readouterr().out

    def test_qam16_identity_symbols_in_equal_symbols_out(self):
        """Inject DEMO_BITS (4 bits/symbol), expect the symbol indices back."""
        from engine.qam16_demo import DEMO_SYMBOLS, build_stimulus, _entry

        project = load_project(QAM16_DEMO)
        catalog = BlockCatalog.from_gr_kyttar()
        reg = ChipTypeRegistry()
        reg.register_file(CT_PATH)
        res = BuildEngine(catalog, reg.paths()).build(project, reg.chip_types())
        assert res.ok, [str(e) for e in res.errors]

        stim = build_stimulus(_entry(res))
        sim = SimulationEngine(CT_PATH)
        sim.load(res.words(0))
        sim.inject_words(stim, port="x16_in")
        for _ in range(20000):
            info = sim.chip.run(max_events=64)
            if (isinstance(info, dict)
                    and info.get("stop_reason") == "QueueEmpty"
                    and sim.chip.run(max_events=0).get("stop_reason")
                    == "QueueEmpty"):
                break
        out = sim.capture_output_words("x16_out")
        syms = [w.value for w in out if not w.is_jump]
        assert syms == DEMO_SYMBOLS


class TestBpskDuplexDemo:
    """Full-duplex BPSK: RX + TX share one input + one output port, tag-routed."""

    def test_duplex_builds_clean(self, catalog, registry):
        project = load_project(BPSK_DUPLEX_DEMO)
        res = BuildEngine(catalog, registry.paths()).build(
            project, registry.chip_types()
        )
        assert res.ok, [str(e) for e in res.errors]

    def test_duplex_cli_test_passes(self, capsys):
        rc = cli.main(["--test", str(BPSK_DUPLEX_DEMO),
                       "--chip-type", str(CT_PATH)])
        assert rc == cli.EXIT_OK
        assert "PASSED" in capsys.readouterr().out

    def test_duplex_rx_and_tx_demux_by_tag(self):
        """RX symbols -> bits (tag 5); TX bits -> symbols (tag 10); on one port."""
        from engine.bpsk_duplex_demo import (
            RX_TAG, TX_TAG, RX_SYMBOLS, TX_BITS,
            Q15_PLUS_ONE, Q15_MINUS_ONE,
            _splitter_entries, build_stimulus, capture,
        )

        project = load_project(BPSK_DUPLEX_DEMO)
        catalog = BlockCatalog.from_gr_kyttar()
        reg = ChipTypeRegistry()
        reg.register_file(CT_PATH)
        res = BuildEngine(catalog, reg.paths()).build(project, reg.chip_types())
        assert res.ok, [str(e) for e in res.errors]

        rx_entry, tx_entry = _splitter_entries(res)
        ct = reg.require(project.chip_type).path
        _out, rx_bits, tx_syms = capture(
            ct, res.words(0), build_stimulus(rx_entry, tx_entry))

        def s16(v):
            return v - 0x10000 if v & 0x8000 else v
        exp_rx = [0 if s16(s) >= 0 else 1 for s in RX_SYMBOLS]
        exp_tx = [Q15_PLUS_ONE if b == 0 else Q15_MINUS_ONE for b in TX_BITS]
        assert rx_bits == exp_rx, f"RX (tag {RX_TAG}): {rx_bits} != {exp_rx}"
        assert tx_syms == exp_tx, f"TX (tag {TX_TAG}): {tx_syms} != {exp_tx}"


class TestCliTestPasses:
    def _argv(self, *parts):
        return [*parts, "--chip-type", str(CT_PATH)]

    def test_cli_test_passes_on_demo(self, capsys):
        rc = cli.main(self._argv("--test", str(DEMO)))
        assert rc == cli.EXIT_OK
        assert "PASSED" in capsys.readouterr().out

    def test_cli_build_demo_clean(self, tmp_path, capsys):
        out = tmp_path / "demo.kbs"
        rc = cli.main(self._argv("--build", str(DEMO), "-o", str(out)))
        assert rc == cli.EXIT_OK  # connected -> no warnings
        assert out.exists()

    def test_cli_drc_demo_clean(self):
        rc = cli.main(self._argv("--drc", str(DEMO)))
        assert rc == cli.EXIT_OK  # no errors, no warnings
