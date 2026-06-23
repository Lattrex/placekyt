"""GRC import tests (auto-P&R P4.2 — the GRC-first flow): a GNURadio .grc
flowgraph imports as placeKYT blocks + logical nets, then auto-P&Rs + computes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.grc_import import _grc_id_to_type, import_grc  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC_GAIN = EXAMPLES_DIR / "kyttar_gain_test.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC_GAIN.exists()),
    reason="chip yaml or .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


# -- id mapping -----------------------------------------------------------------

def test_grc_id_mapping(catalog):
    assert _grc_id_to_type("kyttar_gain", catalog) == "GainBlock"
    assert _grc_id_to_type("kyttar_dc_blocker", catalog) == "DCBlockerBlock"
    assert _grc_id_to_type("kyttar_complex_mixer", catalog) == "ComplexMixerBlock"
    assert _grc_id_to_type("kyttar_agc", catalog) == "AGCBlock"
    # overrides
    assert _grc_id_to_type("kyttar_soft_demodulator", catalog) \
        == "SoftDemodulatorBlock"
    # non-kyttar / unknown
    assert _grc_id_to_type("blocks_throttle", catalog) is None
    assert _grc_id_to_type("kyttar_nonexistent", catalog) is None


# -- import ---------------------------------------------------------------------

def test_import_gain_grc(catalog):
    res = import_grc(str(GRC_GAIN), catalog)
    assert res.ok                                  # no unknown DSP blocks
    # exactly the one DSP block (source/sink/throttle/gui dropped)
    types = [b.type for b in res.project.blocks]
    assert types == ["GainBlock"]
    # gain param coerced from the .grc string '0.5' → float
    assert res.project.blocks[0].params.get("gain") == 0.5
    # two nets: chip-in → gain, gain → chip-out
    kinds = {(type(c.source).__name__, type(c.target).__name__)
             for c in res.project.connections}
    assert ("ChipPortEndpoint", "BlockEndpoint") in kinds
    assert ("BlockEndpoint", "ChipPortEndpoint") in kinds


def test_imported_project_auto_pnrs_and_builds(catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_GAIN), chip_type="kyttar_10x12")
    assert res.ok
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]


def test_grc_first_flow_computes(catalog, chip_type):
    """THE GRC-first end state: design in GNURadio → import → auto-P&R → build →
    SIMULATE → it computes. The gain (0.5, from the .grc) is applied: out=0.5×in."""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC_GAIN), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok

    entry, _in = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    ins = [0.6, -0.4, 0.8]
    outs = []
    for v in ins:
        chip.inject_data_physical([fq(v)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=20000)
        while chip.output_available("x16_out"):
            p = chip.read_port_i16("x16_out").tolist()
            outs.append(p[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=2000)
    assert len(outs) >= len(ins)
    for i, v in enumerate(ins):
        assert abs(outs[i] - 0.5 * v) < 0.02, \
            f"sample {i}: {outs[i]:.3f} != {0.5 * v:.3f}"


GRC_MULTI = EXAMPLES_DIR / "kyttar_dsp_comparison.grc"


@pytest.mark.skipif(not GRC_MULTI.exists(), reason=".grc absent")
def test_import_multiblock_pipeline_builds(catalog, chip_type):
    """A real MULTI-block flowgraph (source → DC blocker → FIR → AGC → sink)
    imports as 3 placeKYT blocks + the chain of logical nets, auto-places in flow
    order, auto-routes, and builds. Exercises the list-param coercion (FIR
    ``coefficients`` is a GRC variable expression — kept as the block default
    rather than crashing) and required-param merging."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MULTI), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    types = sorted(b.type for b in ctrl.project.blocks)
    assert types == ["AGCBlock", "DCBlockerBlock", "FIRFilterBlock"]
    plan = ctrl.auto_place(0)
    assert plan.ok
    # flow order: DC blocker → FIR → AGC
    order_types = [ctrl.project.block(n).type for n in plan.order]
    assert order_types == ["DCBlockerBlock", "FIRFilterBlock", "AGCBlock"]
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]


def test_coerce_list_param_keeps_default_for_expression(catalog):
    """A non-literal GRC param value (a variable name / expression) is omitted in
    favour of the block default — the FIR ``coefficients: fir_taps`` case that used
    to crash the importer."""
    from engine.grc_import import _coerce_params

    out = _coerce_params({"coefficients": "fir_taps"}, catalog, "FIRFilterBlock")
    # default kept (a list), not the string "fir_taps"
    assert isinstance(out.get("coefficients"), list)
    # a real literal list IS taken
    out2 = _coerce_params({"coefficients": "[0.5, 0.25]"}, catalog,
                          "FIRFilterBlock")
    assert out2.get("coefficients") == [0.5, 0.25]


def test_gui_import_action(qapp, catalog, monkeypatch):
    """The File ▸ Import GNURadio Flowgraph action imports + auto-P&Rs a .grc via
    the GUI handler (file dialog mocked). Uses the gain .grc (success path —
    status bar, no modal)."""
    from PySide6.QtWidgets import QFileDialog
    from ui.controller import AppController
    from ui.main_window import MainWindow

    ctrl = AppController(catalog=catalog)
    w = MainWindow(controller=ctrl)
    # Mock the open dialog to return our .grc.
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        staticmethod(lambda *a, **k: (str(GRC_GAIN), "")))
    # Mock the import-options dialog (a modal QDialog) → full place-and-route.
    monkeypatch.setattr(
        w, "_ask_import_options",
        lambda: {"place": True, "route": True, "use_bus": "always"})
    w._import_grc()
    # the imported design has the gain block, placed + routed
    types = [b.type for b in ctrl.project.blocks]
    assert types == ["GainBlock"]
    # Non-input nets route; a chip INPUT-port net needs no route (direct port
    # injection) and is left unrouted on purpose.
    from model.connection import ChipPortEndpoint
    non_input = [c for c in ctrl.project.connections
                 if not (isinstance(c.source, ChipPortEndpoint)
                         and c.source.port.endswith("_in"))]
    assert non_input and all(c.is_routed for c in non_input)




@pytest.mark.skipif(not EXAMPLES_DIR.exists(), reason="examples absent")
def test_import_never_crashes_on_any_example_grc(catalog):
    """ROBUSTNESS: importing ANY shipped example .grc must never raise — it either
    imports cleanly or NAMES the blocks it can't map (sound failure). A user must
    not hit a traceback when importing a real flowgraph (e.g. one with I/Q
    demux/mux blocks that have no placeKYT equivalent yet)."""
    import glob

    grcs = sorted(glob.glob(str(EXAMPLES_DIR / "*.grc")))
    assert grcs, "no example .grc files found"
    for g in grcs:
        # Must not raise; result is well-formed whether or not everything mapped.
        res = import_grc(g, catalog, chip_type="kyttar_10x12")
        assert res.project is not None
        # Every unknown is a (name, grc_id) pair — named, not silently dropped.
        for entry in res.unknown:
            assert len(entry) == 2 and all(entry)
