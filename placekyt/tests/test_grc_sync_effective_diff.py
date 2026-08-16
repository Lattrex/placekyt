# SPDX-License-Identifier: GPL-3.0-or-later
"""GRC-sync diffs compare EFFECTIVE chip config, not raw param dicts.

The out-of-sync-banner false-positive class (the QPSK example showed
"2 block(s) out of sync" on a freshly-opened, untouched design):

  * a marker advertises a param an older .kyt never stored (the block used
    its default) — absent must read as the default, not None;
  * dual-namespace blocks (the RRC matched filter accepts firdes-style
    gain/samp_rate/alpha/ntaps AND friendly beta/sps/span, each side
    defaulting the other to None) — the marker advertises one namespace,
    the import stored the other, and the RESOLVED chip config is identical.

A REAL param change must still flag."""
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

_KYT = _ROOT / "examples" / "qpsk_modem" / "qpsk_modem.kyt"

pytestmark = pytest.mark.skipif(not _KYT.exists(), reason="example kyt absent")

# Exactly what the qpsk flowgraph's markers advertise on Run (captured from
# the real marker registrations): constructor-namespace params, all equal to
# the effective placed config.
_QPSK_ADVERTISED = {
    "psksymbolmapper": {"modulation": "qpsk", "dimension": 1,
                        "bpsk_bit0_positive": True},
    "complexrrcmatchedfilter": {"gain": 0.7105, "samp_rate": 2.0,
                                "sym_rate": 1.0, "alpha": 0.35, "ntaps": 17,
                                "decimation": 1},
    "complexrrcmatchedfilter_2": {"gain": 0.7105, "samp_rate": 2.0,
                                  "sym_rate": 1.0, "alpha": 0.35, "ntaps": 17,
                                  "decimation": 1},
}


@pytest.fixture(scope="module")
def env():
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project

    return load_project(_KYT), BlockCatalog.from_gr_kyttar()


def test_default_equivalent_advertisement_is_in_sync(env):
    from engine.grc_sync import compute_param_diff

    project, cat = env
    diffs = compute_param_diff(project, cat, _QPSK_ADVERTISED)
    assert diffs == {}, (
        f"false out-of-sync on an untouched design: "
        f"{ {k: d.changes for k, d in diffs.items()} }")


def test_real_change_still_flags(env):
    from engine.grc_sync import compute_param_diff

    project, cat = env
    adv = {"complexrrcmatchedfilter":
           dict(_QPSK_ADVERTISED["complexrrcmatchedfilter"], alpha=0.5)}
    diffs = compute_param_diff(project, cat, adv)
    assert "complexrrcmatchedfilter" in diffs, (
        "a REAL roll-off change was suppressed — the sync banner would "
        "never fire")
    assert "alpha" in diffs["complexrrcmatchedfilter"].changes


def test_mapper_modulation_change_still_flags(env):
    from engine.grc_sync import compute_param_diff

    project, cat = env
    adv = {"psksymbolmapper": {"modulation": "8psk", "dimension": 1}}
    diffs = compute_param_diff(project, cat, adv)
    assert "psksymbolmapper" in diffs
