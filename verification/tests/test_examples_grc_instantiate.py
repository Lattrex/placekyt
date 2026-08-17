# SPDX-License-Identifier: GPL-3.0-or-later
"""EVERY shipped example .grc must OPEN, GENERATE and INSTANTIATE under the
real GNU Radio Companion compiler (AGENTS.md honesty: "done" means the user
can open the flowgraph in GRC and it builds).

The static yml lint (test_examples_grc_valid.py) checks what the ymls
DECLARE; this gate checks what actually HAPPENS: GRC's own Platform loads the
repo ymls (a schema-invalid yml silently becomes "Missing Block"), generates
the Python flowgraph, and the generated top block is constructed with the
repo's kyttar MARKERS — GR's ``hier_block2.connect`` then enforces itemsize
equality between the real ``io_signature``s, which no yml can misrepresent.

The user-hit failure classes this pins (2026-08-10): the byte-out BPSK slicer
whose yml claimed float (crashed psk31_transceiver + latent in bpsk_modem and
coherent_bpsk_rx), a kyttar_nco yml missing ``file_format`` (Missing Block →
"Port is not connected" in effect_tremolo), ``None`` for a real_vector param
(effect_echo), and pack(byte) → sink(float) in data_link.

Runs under the SYSTEM GNU Radio interpreter via verification/
grc_instantiate_check.py — one subprocess for ALL files (the Platform library
build dominates the cost).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _ROOT / "verification" / "grc_instantiate_check.py"
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

EXAMPLE_GRCS = sorted((_ROOT / "examples").glob("*/*.grc"))
assert EXAMPLE_GRCS, "no example .grc files found"

pytestmark = pytest.mark.skipif(
    not os.path.exists(_GR_PYTHON), reason="GNU Radio interpreter absent")


@pytest.fixture(scope="module")
def results():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run(
        [_GR_PYTHON, str(_CHECKER)] + [str(g) for g in EXAMPLE_GRCS],
        capture_output=True, text=True, timeout=600, env=env)
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("OK   "):
            out[line[5:].strip()] = None
        elif line.startswith("FAIL "):
            tag, _, reason = line[5:].partition(":")
            out[tag.strip()] = reason.strip() or "unknown"
    assert out, f"checker produced no results:\n{r.stdout[-800:]}\n{r.stderr[-800:]}"
    return out


def _rel(p: Path) -> str:
    return p.parent.name + "/" + p.name


@pytest.mark.parametrize("grc", EXAMPLE_GRCS, ids=_rel)
def test_grc_opens_and_instantiates(results, grc):
    tag = _rel(grc)
    assert tag in results, f"{tag} missing from checker output"
    assert results[tag] is None, results[tag]
