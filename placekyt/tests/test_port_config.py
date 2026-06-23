"""Tests for engine.port_config — host-side I/O port config derivation."""
from __future__ import annotations
import os
from pathlib import Path
import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from engine.catalog import BlockCatalog
from engine.port_config import input_port_config, output_port_target
from engine.registry import ChipTypeRegistry
from model.connection import BlockEndpoint, ChipPortEndpoint
from model.project import Project

from tests.conftest import CHIP_YAML

CT_DIR = CHIP_YAML.parent
pytestmark = pytest.mark.skipif(
    not (CT_DIR / "kyttar_10x12.yaml").exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def registry():
    return ChipTypeRegistry.from_dirs([CT_DIR])


def _project_with_gain(catalog):
    from ui.controller import AppController
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("t", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official",
                         params={"gain": 0.5})
    ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x16_out"),
                   [(x, 0) for x in range(10)])
    return ctrl


def test_input_port_config_for_block_at_port(catalog, registry):
    ctrl = _project_with_gain(catalog)
    cfg = input_port_config(ctrl.project, registry, catalog, 0)
    assert cfg is not None
    port, kw = cfg
    assert port == "x16_in"
    assert "entry_addr" in kw and "hop_count" in kw and "data_addr" in kw

def test_output_target(catalog, registry):
    ctrl = _project_with_gain(catalog)
    tgt = output_port_target(ctrl.project)
    assert tgt == (0, "x16_out")

def test_no_blocks_no_input_config(catalog, registry):
    from model.project import ProjectMetadata
    p = Project(metadata=ProjectMetadata(name="empty"), chip_type="kyttar_10x12")
    assert input_port_config(p, registry, catalog, 0) is None
    assert output_port_target(p) is None
