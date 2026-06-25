"""Tests for the BlockCatalog adapter (engine/catalog.py, §0.1, §2.2, §3.4, §7.2).

These require gr_kyttar (installed editable in the venv) but no Qt, no
simkyt runtime, and no build.
"""

from __future__ import annotations

import pytest

from engine.catalog import OFFICIAL_LIBRARY, BlockCatalog

# The 8 categories from the architecture notes §3.2.
EXPECTED_CATEGORIES = {
    "signal_conditioning",
    "filtering",
    "recovery",
    "equalization",
    "demodulation",
    "fec",
    "frame_sync",
    "memory_interface",
    "routing",          # SplitterBlock (full-duplex shared-port fan-out)
    "modulation",       # IQUpconvertBlock (I/Q passband upconvert, s=I·cos−Q·sin)
    "sources",          # NCOBlock (complex Signal Source, analog.sig_source_c)
}


@pytest.fixture(scope="module")
def catalog() -> BlockCatalog:
    return BlockCatalog.from_gr_kyttar()


class TestDiscovery:
    def test_discovers_all_blocks(self, catalog):
        # §0.1 lists 25+ production blocks; the tree currently has 28 classes.
        assert len(catalog.all()) >= 25

    def test_all_categories_present(self, catalog):
        assert set(catalog.by_category().keys()) == EXPECTED_CATEGORIES

    def test_known_blocks_exist(self, catalog):
        for name in ("AGCBlock", "FIRFilterBlock", "DFEEqualizerBlock",
                     "CostasLoopBlock", "FpgaRamBlock"):
            assert catalog.get(name) is not None, name

    def test_viterbi_excluded(self, catalog):
        # ViterbiK7DecoderBlock was never completed (RAM-offload superseded it)
        # and has no usable v2 definition — excluded from the catalog.
        assert catalog.get("ViterbiK7DecoderBlock") is None

    def test_every_spec_has_metadata(self, catalog):
        for spec in catalog.all():
            assert spec.category in EXPECTED_CATEGORIES
            assert spec.tags  # non-empty
            assert spec.library == OFFICIAL_LIBRARY


class TestMetadata:
    def test_agc_params(self, catalog):
        agc = catalog.get("AGCBlock")
        names = {p.name for p in agc.params}
        assert {"target", "attack_rate", "decay_rate"} <= names
        target = agc.param("target")
        assert target.default == 0.7
        assert target.type_name == "float"
        assert not target.required

    def test_agc_interface(self, catalog):
        agc = catalog.get("AGCBlock")
        assert agc.default_cell_count == 1
        assert agc.entry_address == 1
        assert agc.input_registers == (31,)

    def test_description_from_docstring(self, catalog):
        agc = catalog.get("AGCBlock")
        assert "Automatic Gain Control" in agc.description

    def test_default_params(self, catalog):
        agc = catalog.get("AGCBlock")
        dp = agc.default_params()
        assert dp["target"] == 0.7
        assert "name" not in dp


class TestResolution:
    def test_explicit_library(self, catalog):
        assert catalog.get("AGCBlock", OFFICIAL_LIBRARY) is not None
        assert catalog.get("AGCBlock", "nonexistent.lib") is None

    def test_precedence_fallback(self, catalog):
        # No library given -> finds the official one.
        assert catalog.get("AGCBlock").library == OFFICIAL_LIBRARY

    def test_unknown_type(self, catalog):
        assert catalog.get("NoSuchBlock") is None


class TestSearch:
    def test_search_by_name(self, catalog):
        names = {s.type_name for s in catalog.search("costas")}
        assert "CostasLoopBlock" in names

    def test_search_by_tag_or_category(self, catalog):
        results = {s.type_name for s in catalog.search("filter")}
        assert "FIRFilterBlock" in results
        assert "IIRBiquadBlock" in results

    def test_empty_query_returns_all(self, catalog):
        assert len(catalog.search("")) == len(catalog.all())

    def test_no_match(self, catalog):
        assert catalog.search("zzz_no_such_thing") == []


class TestInstantiation:
    def test_instantiate_with_params(self, catalog):
        block = catalog.instantiate("AGCBlock", "agc1", {"target": 0.5})
        assert block.name == "agc1"
        assert type(block).__name__ == "AGCBlock"
        assert block.cell_count == 1

    def test_instantiate_unknown_raises(self, catalog):
        with pytest.raises(KeyError):
            catalog.instantiate("NoSuchBlock", "x")

    def test_parameterized_cell_count(self, catalog):
        # Tap-dependent block: cell count comes from real params.
        n = catalog.cell_count("FIRFilterBlock", {"coefficients": [0.1] * 9})
        assert n >= 1

    def test_bad_param_raises(self, catalog):
        with pytest.raises(TypeError):
            catalog.instantiate("AGCBlock", "agc1", {"not_a_param": 1})
