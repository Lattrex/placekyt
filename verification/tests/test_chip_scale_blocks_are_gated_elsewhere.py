# SPDX-License-Identifier: GPL-3.0-or-later
"""Coverage gate: a CHIP_SCALE block must be orientation-gated SOMEWHERE.

A chip-scale block (one that declares ``CHIP_SCALE = True`` to waive INV-9's
<= 8-across convention) cannot run the shared full-D4 sweep in
``test_orientation_invariance.py`` — a fold that spans the array width has no
room to rotate. The chip-scale rules say such a block DECLARES the orientations
it ships (``CHIP_SCALE_ORIENTATIONS``) and is gated in EXACTLY those, in its own
suite.

That leaves an obvious hole: drop the block from the shared list, forget to add
the per-block gate, and it is orientation-gated NOWHERE while every suite stays
green. This file closes it. It is deliberately cheap and static — it asserts the
BOOKKEEPING, not the arithmetic (the arithmetic is what the per-block gates do).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parents[1]

#: every chip-scale block that is manifest-``done``, and the suite that gates
#: its declared orientations. Adding a chip-scale block WITHOUT adding it here
#: fails ``test_every_chip_scale_block_is_listed``.
CHIP_SCALE_GATES = {
    "FFT32Block": "test_fft32.py",
    "FFT64Block": "test_fft64.py",
    "GRUCellBlock": "test_gru_cell.py",
}

#: chip-scale classes that are NOT manifest-``done`` and so are not required to
#: carry a declared-orientation gate yet. Listing one here is a statement that
#: it is quarantined, and the registry test RE-CHECKS the manifest — a block
#: that reaches ``done`` while parked here fails, rather than slipping through
#: with no orientation coverage at all.
CHIP_SCALE_QUARANTINED = ("FFT128Block", "FFT128Die0", "FFT128Die1")


def _all_chip_scale_blocks():
    from gr_kyttar.placement import blocks as B
    from gr_kyttar.placement.blocks._base import KyttarBlock
    out = {}
    for nm in dir(B):
        obj = getattr(B, nm)
        if (isinstance(obj, type) and issubclass(obj, KyttarBlock)
                and obj is not KyttarBlock
                and getattr(obj, "CHIP_SCALE", False)):
            out[nm] = obj
    return out


def _manifest_status(name):
    import json
    m = json.loads((_ROOT / "verification" / "manifest.json").read_text())
    rows = m["blocks"] if isinstance(m, dict) and "blocks" in m else m
    for b in rows:
        if (b.get("kyttar_block") or b.get("name")) == name:
            return b.get("status")
    return None


def test_every_chip_scale_block_is_listed():
    """The registry above must account for every chip-scale block: each is
    either gated (``CHIP_SCALE_GATES``) or explicitly quarantined."""
    found = set(_all_chip_scale_blocks())
    accounted = set(CHIP_SCALE_GATES) | set(CHIP_SCALE_QUARANTINED)
    assert found == accounted, (
        f"chip-scale blocks not accounted for here: {sorted(found - accounted)}"
        f"; accounted for but no longer chip-scale: "
        f"{sorted(accounted - found)}. A chip-scale block is exempt from the "
        f"shared full-D4 sweep, so it MUST name the suite that gates its "
        f"declared orientations instead (or be listed as quarantined).")


@pytest.mark.parametrize("name", sorted(CHIP_SCALE_QUARANTINED))
def test_a_quarantined_chip_scale_block_has_not_quietly_become_done(name):
    """Parking a block in ``CHIP_SCALE_QUARANTINED`` is only honest while it
    really is quarantined. If it reaches manifest-``done`` it needs a real
    declared-orientation gate, and this says so instead of letting it ship with
    no orientation coverage."""
    status = _manifest_status(name)
    if status is None:
        pytest.skip(f"{name} is not a manifest entry (an internal die class)")
    assert status != "done", (
        f"{name} is manifest-'done' but is listed as quarantined here — give "
        f"it a declared-orientation gate and move it into CHIP_SCALE_GATES")


@pytest.mark.parametrize("name", sorted(CHIP_SCALE_GATES))
def test_chip_scale_block_is_absent_from_the_shared_d4_sweep(name):
    """It must NOT be in the shared list — that sweep would fail it for
    geometry reasons and tell us nothing about the datapath."""
    src = (_TESTS / "test_orientation_invariance.py").read_text()
    cases = re.findall(r'^\s*\("(\w+)"', src, flags=re.M)
    assert name not in cases, (
        f"{name} is CHIP_SCALE but is still a case in the shared full-D4 "
        f"sweep; it cannot rotate, so remove it there and gate its declared "
        f"orientations in {CHIP_SCALE_GATES[name]}")


@pytest.mark.parametrize("name", sorted(CHIP_SCALE_GATES))
def test_chip_scale_block_declares_and_is_gated_in_its_own_suite(name):
    """It must declare an orientation set, and its own suite must reference
    ``CHIP_SCALE_ORIENTATIONS`` — i.e. gate the DECLARED set rather than
    silently skipping orientation entirely."""
    cls = _all_chip_scale_blocks()[name]
    ors = cls.CHIP_SCALE_ORIENTATIONS
    assert isinstance(ors, tuple) and ors, f"{name} declares no orientations"
    assert () in ors, f"{name} must ship the identity orientation"
    suite = _TESTS / CHIP_SCALE_GATES[name]
    assert suite.exists(), suite
    src = suite.read_text()
    assert "CHIP_SCALE_ORIENTATIONS" in src, (
        f"{CHIP_SCALE_GATES[name]} does not reference "
        f"CHIP_SCALE_ORIENTATIONS, so {name}'s declared orientation set is "
        f"not actually gated anywhere")


@pytest.mark.parametrize("name", sorted(CHIP_SCALE_GATES))
def test_chip_scale_caps_are_the_declared_panel(name):
    """``layout_caps()`` is the single source of truth for what a fold may
    occupy; a chip-scale block's caps must be the panel, and its fold must fit
    them."""
    cls = _all_chip_scale_blocks()[name]
    caps = cls.layout_caps()
    assert caps == (cls.CHIP_SCALE_MAX_WIDTH, cls.CHIP_SCALE_MAX_HEIGHT)
    assert caps != (8, 8), f"{name} declares CHIP_SCALE but keeps the 8x8 cap"


def test_a_non_chip_scale_block_still_gets_the_ordinary_cap():
    """The waiver is PER CLASS and never a global loosening (the whole point of
    the flag). A plain block must still report the 8x8 cap."""
    from gr_kyttar.placement.blocks import GainBlock
    from gr_kyttar.placement.blocks._base import KyttarBlock
    assert KyttarBlock.CHIP_SCALE is False
    assert GainBlock.CHIP_SCALE is False
    assert GainBlock.layout_caps() == (8, 8)
