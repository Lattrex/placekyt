"""Integration tests for the build pipeline (engine/build.py, §5.1).

Requires gr_kyttar + simkyt (both in the venv). Builds a real project to
bitstream and verifies the result loads into the simulator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.build import BuildEngine
from engine.catalog import BlockCatalog
from engine.io.chip_type_io import load_chip_type
from engine.io.kbs import Kbs, KbsChip, chip_type_hash, dumps_kbs, loads_kbs
from model.block import Block
from model.chip import ChipInstance
from model.enums import Face
from model.placement import Placement, PlacedCell
from model.project import Project

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip-type yaml absent")


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _agc_project() -> Project:
    p = Project(chip_type="kyttar_10x12")
    p.chips = [ChipInstance(0, "C0")]
    p.blocks = [
        Block(
            "agc",
            "AGCBlock",
            library="lattrex.official",
            params={"target": 0.7},
            placement=Placement(0, [PlacedCell(0, 0, 0, Face.EAST)]),
        )
    ]
    return p


class TestBuild:
    def test_single_block_builds(self, catalog, chip_type):
        p = _agc_project()
        eng = BuildEngine(catalog, str(CT_PATH))
        res = eng.build(p, {"kyttar_10x12": chip_type})
        assert res.ok, [str(e) for e in res.errors]
        assert 0 in res.chips
        assert len(res.words(0)) > 0
        assert res.chips[0].cell_count >= 1

    def test_build_clears_dirty(self, catalog, chip_type):
        p = _agc_project()
        assert p.build_dirty
        BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert not p.build_dirty

    def test_unknown_chip_type_errors(self, catalog, chip_type):
        p = _agc_project()
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {})  # no types provided
        assert not res.ok
        assert any(e.category == "unknown_chip_type" for e in res.errors)
        assert p.build_dirty  # not cleared on failure

    def test_unresolved_block_errors(self, catalog, chip_type):
        p = _agc_project()
        p.blocks[0].type = "NoSuchBlock"
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert not res.ok
        assert any(e.category == "unresolved_block" for e in res.errors)

    def test_bitstream_loads_into_simkyt(self, catalog, chip_type):
        """The strongest check: the generated bitstream is a valid chip program."""
        import simkyt

        p = _agc_project()
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert res.ok
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        events = chip.load_bitstream_physical(res.words(0))
        assert events > 0


class TestBuildErrors:
    def test_overlap_detected(self, catalog, chip_type):
        # Two single-cell blocks placed on the same coordinate.
        p = Project(chip_type="kyttar_10x12")
        p.chips = [ChipInstance(0, "C0")]
        p.blocks = [
            Block("a", "AGCBlock", library="lattrex.official",
                  placement=Placement(0, [PlacedCell(0, 2, 2, Face.EAST)])),
            Block("b", "DCBlockerBlock", library="lattrex.official",
                  params={"length": 2, "long_form": False},
                  placement=Placement(0, [PlacedCell(0, 2, 2, Face.EAST)])),
        ]
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert not res.ok
        assert any(e.category == "overlap" for e in res.errors)

    def test_bad_param_is_block_build_failure(self, catalog, chip_type):
        p = _agc_project()
        p.blocks[0].params = {"not_a_real_param": 1}
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert not res.ok
        assert any(e.category == "block_build_failed" for e in res.errors)

    def test_error_str_includes_location(self):
        from engine.drc import Severity
        from engine.build import BuildError

        e = BuildError(Severity.ERROR, "overlap", "boom", chip=1, x=3, y=4)
        s = str(e)
        assert "overlap" in s and "chip 1" in s and "(3,4)" in s

    def test_empty_project_builds_empty(self, catalog, chip_type):
        # A project with a chip but no blocks should still produce a (routing-
        # only / empty) build without errors.
        p = Project(chip_type="kyttar_10x12")
        p.chips = [ChipInstance(0, "C0")]
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert res.ok
        assert 0 in res.chips


class TestDrcBlocksBuild:
    def test_drc_error_blocks_generation(self, catalog, chip_type):
        # Two blocks overlapping -> DRC overlap error -> no bitstream generated.
        p = Project(chip_type="kyttar_10x12")
        p.chips = [ChipInstance(0, "C0")]
        p.blocks = [
            Block("a", "AGCBlock", library="lattrex.official",
                  placement=Placement(0, [PlacedCell(0, 3, 3, Face.EAST)])),
            Block("b", "AGCBlock", library="lattrex.official",
                  placement=Placement(0, [PlacedCell(0, 3, 3, Face.EAST)])),
        ]
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert not res.ok
        assert any(e.category == "overlap" for e in res.errors)
        assert 0 not in res.chips  # generation did not run
        assert p.build_dirty

    def test_clean_project_still_builds(self, catalog, chip_type):
        # The AGC project has no connections (warns unused_port) but no errors.
        p = _agc_project()
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert res.ok
        assert any(w.category == "unused_port" for w in res.warnings)
        assert len(res.words(0)) > 0


class TestBuildToKbs:
    def test_build_result_to_kbs_roundtrip(self, catalog, chip_type):
        p = _agc_project()
        res = BuildEngine(catalog, str(CT_PATH)).build(p, {"kyttar_10x12": chip_type})
        assert res.ok
        kbs = Kbs(
            chips=[
                KbsChip(chip_type_hash("kyttar_10x12"), res.words(cid))
                for cid in sorted(res.chips)
            ],
            metadata={"project_name": p.metadata.name},
        )
        reloaded = loads_kbs(dumps_kbs(kbs))
        assert reloaded.chips[0].words == res.words(0)
