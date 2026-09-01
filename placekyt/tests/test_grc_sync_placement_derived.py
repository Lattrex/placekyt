# SPDX-License-Identifier: GPL-3.0-or-later
"""Placement-derived params must not raise the GRC out-of-sync banner.

The second false-positive class of the "N block(s) out of sync" banner (the
first — default-vs-absent and dual-namespace blocks — is covered by
``test_grc_sync_effective_diff.py``):

The panel-backed blocks (LZ4 encoder/decoder, Varicode encoder/decoder, the CW
keyer/decoder) reach the SRAM panel and their egress port over ROUTED
corridors. Their descriptors / hop counts / dest tags (``panel_hop``,
``emit_hop``, ``read_wr_desc``, ``read_jp_desc``, ``out_dest``, …) are a
function of that geometry, and ``panel_pnr.refresh_panel_params`` re-authors
them from the current routes on every build.

A GNURadio flowgraph has no geometry, so a .grc can only advertise the
constructor DEFAULTS for those keys. Measured before the fix: the shipped
lz4_stream, cw_transceiver and psk31_transceiver examples EACH reported
"2 block(s) out of sync" on a freshly-opened, untouched design, with every
flagged key placement-derived. Worse than cosmetic — a resync would have
written the defaults over the routed values and broken the chip.

A REAL DSP change, and a DELIBERATE non-default placement override, must both
still flag."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Every shipped panel-backed example: the .kyt carries hand-placed geometry,
# the .grc carries only the DSP intent.
_PANEL_EXAMPLES = ("lz4_stream", "cw_transceiver", "psk31_transceiver")


def _paths(name):
    d = _ROOT / "examples" / name
    return d / f"{name}.kyt", d / f"{name}.grc"


@pytest.fixture(scope="module")
def catalog():
    from engine.catalog import BlockCatalog

    return BlockCatalog.from_gr_kyttar()


def _diff(name, catalog, mutate=None):
    from engine.grc_import import grc_block_params
    from engine.grc_sync import compute_param_diff
    from engine.io.project_io import load_project

    kyt, grc = _paths(name)
    if not kyt.exists() or not grc.exists():
        pytest.skip(f"{name} example absent")
    project = load_project(kyt)
    advertised = grc_block_params(grc, catalog)
    if mutate is not None:
        mutate(advertised)
    return compute_param_diff(project, catalog, advertised)


@pytest.mark.parametrize("name", _PANEL_EXAMPLES)
def test_shipped_panel_example_is_in_sync(name, catalog):
    """An untouched shipped example must not raise the banner."""
    diffs = _diff(name, catalog)
    assert diffs == {}, (
        f"false out-of-sync on the untouched {name} example: "
        f"{ {k: d.changes for k, d in diffs.items()} } — a resync would "
        f"overwrite routed corridor values with GRC defaults")


def test_real_dsp_change_still_flags(catalog):
    """``hash_bits`` is a genuine DSP knob, not geometry — it must flag."""
    def mutate(adv):
        adv["lz4encoder"] = dict(adv["lz4encoder"], hash_bits=10)

    diffs = _diff("lz4_stream", catalog, mutate)
    assert "lz4encoder" in diffs, (
        "a REAL hash_bits change was suppressed — the sync banner would "
        "never fire for the LZ4 encoder")
    assert "hash_bits" in diffs["lz4encoder"].changes


def test_window_words_change_still_flags(catalog):
    """``window_words`` resizes the panel regions — must never be suppressed."""
    def mutate(adv):
        adv["lz4decoder"] = dict(adv["lz4decoder"], window_words=16384)

    diffs = _diff("lz4_stream", catalog, mutate)
    assert "lz4decoder" in diffs
    assert "window_words" in diffs["lz4decoder"].changes


def test_nondefault_placement_override_still_flags(catalog):
    """Suppression is only for the DEFAULT.

    A .grc that hand-sets a placement-derived key to a non-default value is a
    deliberate hand-placed override and must still reach the banner."""
    def mutate(adv):
        adv["lz4encoder"] = dict(adv["lz4encoder"], panel_hop=9)

    diffs = _diff("lz4_stream", catalog, mutate)
    assert "lz4encoder" in diffs, (
        "a deliberate non-default panel_hop override was suppressed — the "
        "suppression must be scoped to the block default only")
    assert diffs["lz4encoder"].changes["panel_hop"][1] == 9
