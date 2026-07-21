# SPDX-License-Identifier: GPL-3.0-or-later
"""Universal orientation-invariance gate — a block computes IDENTICALLY in all 8 D4
orientations.

A block placed on the array is a RIGID unit: rotating/mirroring it changes only where
it sits and which way its ports face, never what it computes. This gate drives each
block at every D4 orientation and asserts the on-chip output EQUALS the identity output.

FIXED (was the "single-block port-input fan-in" residual): a complex-INPUT block wired
STRAIGHT from the chip input port and rotated into a 180°-family "anti-orientation" used
to emit ZERO. Root cause was THREE distinct defects at those geometries — (1) the block's
internal-forward face was not re-asserted for NAMED-cell blocks (the reassert pass only
handled integer cell ids), so the input wavefront died; (2) the CP-SAT router wove the
output egress / a mis-coalesced port fan-in through block cells; (3) the port complex
fan-in's two rails were double-relayed / split across divergent corridors. Fixed in
``build._reassert_internal_forward_faces`` (named cells), ``bus_router.broker_plan``
(emit the port-complex operand group once), and the router validation in
``controller._run_router`` (escalate a block-crossing / split-fan-in route to the
node-disjoint maze router). ComplexMixer + ComplexRRC are now invariant in all 8 D4
orientations.

FIXED (was the NCOBlock single-rail residual): NCOBlock's REAL (single-rail) input at two
180°-family anti-orientations (``cw+cw`` / ``mirror_v+cw+cw+cw``) used to emit nothing —
but this was NEVER a datapath or routing defect. The build is fully invariant there: the
datapath cells transform correctly and every corridor cell forwards the sample straight to
the block's input cell. The failure was in the DUT HARNESS: it derived the port target hop
count from the MANHATTAN span (``|dx|+|dy|``) to the landing cell, but under those rotations
the auto-router draws a corridor that SNAKES (longer than manhattan), so the injected word
stopped SHORT and never reached the block. Fixed in ``kyttar_verify.run_block_dut`` by
deriving the hop from the ACTUAL routed corridor length (``len(route)``) when the input net
is routed from the port cell. All blocks are now invariant in all 8 D4 orientations; no
xfails remain.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_orientation_invariance.py -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_block_dut_complex, D4_ORIENTATIONS, compare_dut_results)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_OK = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_OK),
    reason="chip yaml or GNU Radio interpreter absent")


def _label(orient):
    return "identity" if not orient else "+".join(orient)


# (block_type, params, kind, ports). kind: "real" | "complex" | "complex_wps1".
_CASES = [
    ("GainBlock", {"gain": 0.5}, "real", ("sample", "out")),
    ("FIRFilterBlock", {"coefficients": [0.2, 0.3, 0.2]}, "real", ("sample", "out")),
    ("ComplexUpsamplerBlock", {"sps": 2}, "complex", ("xi", "xq", "yi")),
    ("ComplexRRCMatchedFilterBlock", {}, "complex", ("xi", "xq", "yi")),
    ("ComplexCostasLoopBlock", {"order": 2}, "complex", ("xi", "xq", "yi_tap")),
    ("ComplexCostasLoopBlock", {"order": 4}, "complex", ("xi", "xq", "yi_tap")),
    ("GardnerTimingRecovery", {"complex": True}, "complex", ("xi", "xq", "yi_e")),
    ("IQUpconvertBlock", {}, "complex_wps1", ("xi", "xq", "out")),
    ("ComplexMixerBlock", {}, "complex", ("xi", "xq", "yi")),
    ("NCOBlock", {}, "real", ("sample", "yi")),
    # M17 4FSK modem blocks: single real rail in/out. The mapper emits one PAM
    # level per two input bits (None-gaps on the odd samples); the slicer emits two
    # bits per level (the harness drains one per trigger) — both must produce the
    # IDENTICAL output word list in every D4 orientation.
    ("FSK4SymbolMapperBlock", {}, "real", ("sample", "out")),
    ("FSK4SlicerBlock", {}, "real", ("sample", "out")),
]

# No orientation residuals remain: every block in _CASES is invariant in all 8 D4
# orientations. (The former NCOBlock single-rail xfails were a DUT-harness manhattan-hop
# bug — the routed corridor snaked longer than the manhattan span — now fixed in
# kyttar_verify.run_block_dut; see module docstring.)
_XFAIL: set[tuple[str, str]] = set()


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _run(btype, params, kind, ports, orient):
    if kind in ("complex", "complex_wps1"):
        rng = random.Random(3)
        stim = [complex(rng.uniform(-0.5, 0.5), rng.uniform(-0.5, 0.5))
                for _ in range(24)]
        xi, xq, out = ports
        wps = 1 if kind == "complex_wps1" else 2
        return run_block_dut_complex(btype, stim, params=params, chip_yaml=CHIP_YAML,
                                     in_ports=(xi, xq), out_port=out,
                                     words_per_sample=wps, orient=orient)
    rng = random.Random(3)
    inq = [_fq(rng.uniform(-0.6, 0.6)) for _ in range(16)]
    inp, out = ports
    return run_block_dut(btype, inq, params=params, chip_yaml=CHIP_YAML,
                         in_port=inp, out_port=out, orient=orient)


_PARAMS = [
    pytest.param(btype, params, kind, ports, orient,
                 id=f"{btype}-{tuple(sorted(params.items()))}-{_label(orient)}")
    for (btype, params, kind, ports) in _CASES
    for orient in D4_ORIENTATIONS[1:]
]


@pytest.mark.parametrize("btype,params,kind,ports,orient", _PARAMS)
def test_orientation_invariant(btype, params, kind, ports, orient):
    """The block's on-chip output under ``orient`` must EQUAL its identity output."""
    if (btype, _label(orient)) in _XFAIL:
        pytest.xfail("known single-block port-input fan-in residual (not a datapath "
                     "bug; block is invariant block->block — see module docstring)")
    base = _run(btype, params, kind, ports, [])
    assert getattr(base, "ok", True), \
        f"identity build failed for {btype}: {getattr(base,'reason','?')}"
    res = _run(btype, params, kind, ports, list(orient))
    ok, detail = compare_dut_results(base, res)
    assert ok, f"{btype} {_label(orient)}: {detail}"
