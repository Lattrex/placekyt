"""Tests for .kyt / chip-type / board serialization (engine/io, §2.1–2.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.io import (
    PathTraversalError,
    SchemaError,
    UnsupportedFormatVersion,
    load_board,
    load_chip_type,
    load_project,
    project_from_str,
    save_project,
    validate_reference,
)
from engine.io.board_io import board_from_str

from tests.conftest import CHIP_YAML
from engine.io.chip_type_io import chip_type_from_str
from model.connection import (
    NET_DATA,
    NET_DATA_TRIGGER,
    NET_TRIGGER,
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
)
from model.enums import Face, IQFormat, Modulation

# A representative project exercising every §2.1 section.
SAMPLE_KYT = """\
project:
  format_version: 1
  name: "110B HF Modem"
  version: "1.0"
  author: Lattrex
chip_type: KYT16A120
board:
  name: KYT-DEV-2
  config: boards/kyt-dev-2.kdb
chips:
  - id: 0
    label: RX Front-End
    position: {x: 0, y: 0}
  - id: 1
    label: RX Back-End
    position: {x: 720, y: 0}
inter_chip_connections:
  - from: {chip: 0, port: x16_out}
    to: {chip: 1, port: x16_in}
panels:
  - id: 0
    label: Symbol Table
    position: {x: -300, y: 0}
    ports:
      - {name: in, direction: input, width: 16, face: west}
      - {name: out, direction: output, width: 16, face: east}
panel_connections:
  - panel: {id: 0, port: out}
    chip: {id: 1, port: x16_in}
blocks:
  - name: agc
    type: AGCBlock
    library: lattrex.dsp
    params:
      target: 0.35
      attack_rate: 0.05
    placement:
      chip: 0
      cells:
        - {cell_id: 0, x: 1, y: 1, face: south}
  - name: dfe
    type: DFEEqualizerBlock
    library: lattrex.dsp
    custom_block_field: keep-me
connections:
  - name: adc_to_agc
    from: {chip_port: {chip: 0, port: x16_in}}
    to: {block: agc, port: in}
  - name: dfe_to_chip1
    from: {block: dfe, port: out}
    to: {chip_port: {chip: 0, port: x16_out}}
    route:
      - {x: 9, y: 6}
      - {x: 9, y: 0}
    modulation: qpsk
    code_rate: 0.5
    iq_format: q15_paired
simulation:
  default_stimulus: stimulus/bpsk_2400baud.csv
  golden_output: stimulus/golden_output.csv
top_level_mystery: preserved
"""


class TestProjectLoad:
    def test_metadata(self):
        p = project_from_str(SAMPLE_KYT)
        assert p.metadata.name == "110B HF Modem"
        assert p.metadata.author == "Lattrex"
        assert p.chip_type == "KYT16A120"

    def test_chips(self):
        p = project_from_str(SAMPLE_KYT)
        assert [c.id for c in p.chips] == [0, 1]
        assert p.chip(1).position == (720.0, 0.0)

    def test_blocks_and_params(self):
        p = project_from_str(SAMPLE_KYT)
        assert {b.name for b in p.blocks} == {"agc", "dfe"}
        assert p.block("agc").params["target"] == 0.35
        assert p.block("agc").is_placed
        assert not p.block("dfe").is_placed  # no placement key

    def test_connection_endpoints(self):
        p = project_from_str(SAMPLE_KYT)
        adc = p.connection("adc_to_agc")
        assert isinstance(adc.source, ChipPortEndpoint)
        assert isinstance(adc.target, BlockEndpoint)

    def test_connection_metadata(self):
        p = project_from_str(SAMPLE_KYT)
        c = p.connection("dfe_to_chip1")
        assert c.is_routed and len(c.route) == 2
        assert c.modulation is Modulation.QPSK
        assert c.iq_format is IQFormat.Q15_PAIRED
        assert c.code_rate == 0.5

    def test_inter_chip(self):
        p = project_from_str(SAMPLE_KYT)
        assert len(p.inter_chip_connections) == 1
        ic = p.inter_chip_connections[0]
        assert (ic.from_chip, ic.from_port, ic.to_chip, ic.to_port) == (
            0, "x16_out", 1, "x16_in"
        )

    def test_loaded_project_not_dirty(self):
        p = project_from_str(SAMPLE_KYT)
        assert not p.project_dirty
        assert p.build_dirty  # nothing built yet

    def test_panels(self):
        p = project_from_str(SAMPLE_KYT)
        assert len(p.panels) == 1
        panel = p.panel(0)
        assert panel.label == "Symbol Table"
        assert panel.position == (-300.0, 0.0)
        assert panel.size_words == 1 << 16          # default full array
        assert {pt.name for pt in panel.ports} == {"in", "out"}

    def test_panel_connections(self):
        p = project_from_str(SAMPLE_KYT)
        assert len(p.panel_connections) == 1
        pc = p.panel_connections[0]
        assert (pc.panel, pc.panel_port, pc.chip, pc.chip_port) == (
            0, "out", 1, "x16_in"
        )


class TestFormatVersion:
    def test_newer_version_rejected(self):
        bad = SAMPLE_KYT.replace("format_version: 1", "format_version: 99")
        with pytest.raises(UnsupportedFormatVersion):
            project_from_str(bad)

    def test_missing_version_defaults_to_1(self):
        text = "project:\n  name: M\nchip_type: X\n"
        assert project_from_str(text).metadata.format_version == 1


class TestRoundTrip:
    def test_load_save_load_identity(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        p2 = load_project(fp)
        assert p2.metadata.name == p.metadata.name
        assert p2.block("agc").params["target"] == 0.35
        assert p2.connection("dfe_to_chip1").modulation is Modulation.QPSK
        # panels + panel connections round-trip
        assert p2.panel(0).label == "Symbol Table"
        assert len(p2.panel_connections) == 1
        assert p2.panel_connections[0].chip_port == "x16_in"

    def test_placement_orientation_round_trips(self, tmp_path):
        """placement.orientation MUST survive save/load. A folded block's in-program
        FACE constants are transformed by it at build time; dropping it on save makes
        a loaded .kyt build with un-oriented faces (the (5,1) stray-exec regression)."""
        p = project_from_str(SAMPLE_KYT)
        blk = p.block("agc")
        blk.placement.orientation = ["mirror_h", "cw"]
        fp = tmp_path / "orient.kyt"
        save_project(p, fp)
        assert "orientation" in fp.read_text()
        p2 = load_project(fp)
        assert p2.block("agc").placement.orientation == ["mirror_h", "cw"]

    def test_idempotent_save(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        fp1, fp2 = tmp_path / "a.kyt", tmp_path / "b.kyt"
        save_project(p, fp1)
        save_project(load_project(fp1), fp2)
        assert fp1.read_text() == fp2.read_text()

    def test_unknown_fields_preserved(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        text = fp.read_text()
        assert "top_level_mystery" in text       # top-level unknown
        assert "custom_block_field" in text       # per-block unknown

    def test_float_precision_preserved(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        text = fp.read_text()
        assert "0.35" in text and "0.3500" not in text

    def test_no_explicit_null(self, tmp_path):
        # dfe has no placement; the key must be omitted, never written as null.
        p = project_from_str(SAMPLE_KYT)
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        assert "null" not in fp.read_text().lower()

    def test_save_clears_project_dirty(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        p.mark_dirty()
        assert p.project_dirty
        save_project(p, tmp_path / "m.kyt")
        assert not p.project_dirty
        # build_dirty is independent — save does not clear it.
        assert p.build_dirty

    def test_int_cell_id_roundtrips_unquoted(self, tmp_path):
        p = project_from_str(SAMPLE_KYT)
        # agc's cell_id was the integer 0; it should stay an int through reload.
        assert p.block("agc").placement.cells[0].cell_id == 0
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        assert "cell_id: 0," in fp.read_text()


class TestSaveFromScratch:
    """A project built in memory (no retained document) must save and reload."""

    def _fresh(self):
        from model.block import Block
        from model.chip import ChipInstance
        from model.connection import Connection
        from model.enums import Modulation
        from model.placement import Placement, PlacedCell
        from model.project import Project, ProjectMetadata, SimulationConfig

        p = Project(
            metadata=ProjectMetadata(name="Scratch", author="t"),
            chip_type="KYT16A120",
        )
        p.chips = [ChipInstance(0, "C0", 0.0, 0.0)]
        p.blocks = [
            Block(
                "agc",
                "AGCBlock",
                library="lattrex.dsp",
                params={"target": 0.7},
                placement=Placement(0, [PlacedCell(0, 1, 1, Face.SOUTH)]),
            )
        ]
        p.connections = [
            Connection(
                "c",
                source=ChipPortEndpoint(0, "x16_in"),
                target=BlockEndpoint("agc", "in"),
                modulation=Modulation.BPSK,
            )
        ]
        p.simulation = SimulationConfig(default_stimulus="stim/x.csv")
        return p

    def test_save_and_reload(self, tmp_path):
        p = self._fresh()
        fp = tmp_path / "scratch.kyt"
        save_project(p, fp)
        p2 = load_project(fp)
        assert p2.metadata.name == "Scratch"
        assert p2.block("agc").params["target"] == 0.7
        assert p2.block("agc").placement.cells[0].cell_id == 0
        assert p2.connection("c").modulation is Modulation.BPSK
        assert p2.simulation.default_stimulus == "stim/x.csv"

    def test_fresh_save_is_idempotent(self, tmp_path):
        p = self._fresh()
        save_project(p, tmp_path / "a.kyt")
        save_project(load_project(tmp_path / "a.kyt"), tmp_path / "b.kyt")
        assert (tmp_path / "a.kyt").read_text() == (tmp_path / "b.kyt").read_text()

    def test_connection_kind_roundtrips(self, tmp_path):
        """The LogicalNet ``kind`` (AUTO_PNR_DESIGN §4) round-trips; a non-default
        kind is persisted, the default (data+trigger) is omitted from the .kyt."""
        p = self._fresh()
        p.connections = [
            Connection("data_only", source=ChipPortEndpoint(0, "x16_in"),
                       target=BlockEndpoint("agc", "in"), kind=NET_DATA),
            Connection("trig_only", source=BlockEndpoint("agc", "out"),
                       target=ChipPortEndpoint(0, "x16_out"), kind=NET_TRIGGER),
            Connection("both", source=ChipPortEndpoint(0, "x16_in"),
                       target=BlockEndpoint("agc", "in")),  # default
        ]
        fp = tmp_path / "k.kyt"
        save_project(p, fp)
        text = fp.read_text()
        assert "kind: data" in text and "kind: trigger" in text
        # the default kind is omitted (kept byte-clean)
        assert "kind: data+trigger" not in text
        p2 = load_project(fp)
        assert p2.connection("data_only").kind == NET_DATA
        assert p2.connection("trig_only").kind == NET_TRIGGER
        assert p2.connection("both").kind == NET_DATA_TRIGGER
        # derived emit flags
        assert p2.connection("data_only").emits_write
        assert not p2.connection("data_only").emits_jump
        assert p2.connection("trig_only").emits_jump
        assert not p2.connection("trig_only").emits_write
        assert p2.connection("both").emits_write and p2.connection("both").emits_jump


class TestMultilineAssembly:
    """§2.1: assembly strings round-trip through literal block scalars,
    including '#', ':', blank lines, and trailing whitespace."""

    def test_block_scalar_roundtrip(self, tmp_path):
        asm = "start:\n    MULQ R0, R1   # scale: gain\n\n    HALT\n"
        text = (
            "project:\n  name: M\nchip_type: X\n"
            "blocks:\n  - name: b\n    type: T\n"
            "    asm: |\n"
            "      start:\n"
            "          MULQ R0, R1   # scale: gain\n"
            "\n"
            "          HALT\n"
        )
        p = project_from_str(text)
        # 'asm' is an unknown field on the block; it must survive round-trip
        # verbatim as a block scalar.
        fp = tmp_path / "m.kyt"
        save_project(p, fp)
        out = fp.read_text()
        assert "MULQ R0, R1   # scale: gain" in out
        assert "|" in out  # literal block scalar marker
        # reload yields identical bytes
        save_project(load_project(fp), tmp_path / "m2.kyt")
        assert fp.read_text() == (tmp_path / "m2.kyt").read_text()


class TestSchemaErrors:
    def test_endpoint_without_block_or_chipport(self):
        bad = (
            "project:\n  name: M\nchip_type: X\n"
            "connections:\n  - name: c\n    from: {nonsense: 1}\n"
            "    to: {block: b, port: in}\n"
        )
        with pytest.raises(SchemaError):
            project_from_str(bad)

    def test_missing_required_block_name(self):
        bad = (
            "project:\n  name: M\nchip_type: X\n"
            "blocks:\n  - type: AGCBlock\n"
        )
        with pytest.raises(SchemaError):
            project_from_str(bad)


class TestChipTypeLoad:
    REAL = CHIP_YAML

    @pytest.mark.skipif(not REAL.exists(), reason="real chip-type yaml absent")
    def test_load_real_chip_type(self):
        ct = load_chip_type(self.REAL)
        assert ct.width == 10 and ct.height == 12
        assert ct.cell_count == 120
        assert ct.cell_id(9, 0) == 9
        assert ct.port("x16_out").face is Face.EAST

    def test_from_str(self):
        ct = chip_type_from_str(
            "chip_type: {name: T}\nfabric: {width: 4, height: 5}\n"
        )
        assert ct.cell_count == 20


class TestBoardLoad:
    KDB = """\
board:
  name: KYT-DEV-2
  chips:
    - {id: 0, type: KYT16A120, label: U1}
    - {id: 1, type: KYT16A120, label: U2}
  chip_connections:
    - from: {chip: 0, port: x16_out}
      to: {chip: 1, port: x16_in}
      wire_delay: 1.0
  fpga_connections:
    - {name: adc, fpga_port: adc_out, chip: 0, chip_port: x16_in}
    - {name: sram, fpga_port: sram_0, chip: 0, chip_port: x1_in, chip_port_out: x1_out}
"""

    def test_load(self):
        b = board_from_str(self.KDB)
        assert b.name == "KYT-DEV-2"
        assert b.has_chip_connection(0, "x16_out", 1, "x16_in")
        assert b.fpga_connections[0].chip_port_out is None  # unidirectional
        assert b.fpga_connections[1].chip_port_out == "x1_out"  # bidirectional

    def test_zero_wire_delay_rejected(self):
        bad = self.KDB.replace("wire_delay: 1.0", "wire_delay: 0.0")
        with pytest.raises(SchemaError):
            board_from_str(bad)


class TestPathValidation:
    def test_in_bounds_relative(self, tmp_path):
        (tmp_path / "stimulus").mkdir()
        (tmp_path / "stimulus" / "s.csv").write_text("x")
        r = validate_reference("stimulus/s.csv", project_dir=tmp_path)
        assert r.name == "s.csv"

    @pytest.mark.parametrize(
        "bad",
        ["../secret", "a/../../etc/passwd", "..\\win", "//srv/share/x", "/etc/passwd"],
    )
    def test_traversal_rejected(self, tmp_path, bad):
        with pytest.raises(PathTraversalError):
            validate_reference(bad, project_dir=tmp_path)

    def test_extra_root_allowed(self, tmp_path):
        res = tmp_path / "resources"
        res.mkdir()
        (res / "b.kdb").write_text("x")
        r = validate_reference(
            str(res / "b.kdb"), project_dir=tmp_path / "proj", extra_roots=(res,)
        )
        assert r.name == "b.kdb"
