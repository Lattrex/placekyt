# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify XorBlock against GNU Radio blocks.xor_bb (2-input byte XOR).

GR's ``blocks.xor_bb`` XORs 2+ unsigned-char streams bit-for-bit. This suite
verifies the canonical TWO-input case ``out = a ^ b`` of two byte streams and
holds it to a BIT-EXACT bar — the FULL 8-bit result, not just the LSB.

Why a bespoke DUT driver (``_run_xor_dut``)?  The shipped complex / N-stream
drivers deliver each operand through ``_to_q15(float(...))`` — they quantise a
float in [-1, 1) to a Q15 word. A BYTE value (0..255) is a RAW word, not a Q15
sample: ``_to_q15(170.0)`` saturates to 0x7FFF, corrupting the operand. So this
test drives the block with the SAME proven build path (place -> 2 logical inputs
from x16_in -> route -> build -> derive placement-hop -> inject/JUMP/drain) but
injects the operands as RAW uint16 words via ``inject_data_physical`` — exactly
how ``xor_bb`` would see the bytes on the wire.

The gate is a DIRECT byte-for-byte equality (``_bitexact`` below): a byte XOR is
deterministic and integral, so ANY mismatched bit is a failure. NOTE the shipped
``compare_against_grc`` cannot be used here — it treats the reference as Q15
FLOATS (``_saturate_ref_q15``), so a byte value like 255 saturates to 0x7FFF and
every real byte reads "wrong"; and its DECISION metric compares only the packed
LSB (missing 7 of 8 bit errors). So this suite compares the raw byte words head
to head (all 8 bits), which is the strictly stronger, truly bit-exact gate — and
the mutation tests below prove it catches AND / OR / drop-input / passthrough.

Per INV-4 every gate is paired with mutations that MUST fail: AND / OR instead of
XOR, dropping one input (out=a or out=b), inverted output, +1 sample delay, and
empty output.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_xor.py -x -q
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_report  # noqa: E402
from gr_kyttar.placement.blocks.xor_block import XorBlock  # noqa: E402


class _BitExact:
    """A byte-for-byte equality result (the honest bit-exact gate for byte
    streams — see the module docstring on why ``compare_against_grc`` cannot be
    used). ``passed`` is True iff every DUT byte equals its reference byte and no
    output is missing."""

    def __init__(self, dut, ref):
        self.n_compared = min(len(dut), len(ref))
        self.missing = sum(1 for d in dut if d is None)
        if not dut:
            self.passed = False
            self.reason = "DUT produced no output"
        elif self.missing:
            self.passed = False
            self.reason = f"{self.missing} DUT outputs missing (no egress)"
        elif len(dut) != len(ref):
            self.passed = False
            self.reason = f"length mismatch: dut {len(dut)} vs ref {len(ref)}"
        else:
            diffs = [(i, d, r) for i, (d, r) in enumerate(zip(dut, ref)) if d != r]
            self.passed = not diffs
            self.reason = "" if self.passed else \
                f"{len(diffs)} of {len(ref)} bytes differ (first: {diffs[:3]})"
        # dashboard-report fields (mirror CompareResult's shape so write_report
        # can serialize this like a normal result).
        self.max_abs_err = 0 if self.passed else 255
        self.bit_errors = 0 if self.passed else (self.missing or 1)
        self.tolerance = 0
        self.nmse_db = float("nan")
        self.correlation = float("nan")
        self.delay_used = 0
        self.metric = SimpleNamespace(value="exact-byte")

    def summary(self):
        head = "PASS" if self.passed else "FAIL"
        return f"[{head}] byte-exact: n={self.n_compared}" + (
            f" — {self.reason}" if self.reason else "")


def _bitexact(dut, ref):
    return _BitExact(list(dut), list(ref))

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

_LIB = "lattrex.official"


# =============================================================================
# DUT — RAW-word 2-input byte driver (build path == the shipped complex driver)
# =============================================================================

def _run_xor_dut(a_bytes, b_bytes, *, orient=None, place_xy=(1, 1)):
    """Build XorBlock (x16_in -> a,b -> x16_out), inject each (a,b) pair as RAW
    uint16 words, and return the per-sample output words (one byte each)."""
    import simkyt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import ChipPortEndpoint, BlockEndpoint  # noqa: PLC0415

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_xor", ct_key)
    px, py = place_xy
    blk = ctrl.place_block("XorBlock", 0, px, py, library=_LIB, params={})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)

    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port="a"), name="in_a")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port="b"), name="in_b")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    assert rep.ok, "route failed: " + "; ".join(
        f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ct_key: ct})
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    words = bres.words(0)
    entry, ins = cat.resolved_io("XorBlock", {}, library=_LIB)
    assert len(ins) >= 2, f"need 2 input regs, got {ins}"
    a0, a1 = int(ins[0]), int(ins[1])

    # INV-1: placement-dependent hop from the built/routed landing.
    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)
    addr_a, addr_b, hop_a = a0, a1, hop
    entry_i = entry
    cb = getattr(bres, "chips", {}).get(0)
    il = (getattr(cb, "input_landings", {}) or {}) if cb is not None else {}
    best = None
    for lname in ("in_a", "in_b"):
        ld = il.get(lname)
        if ld and ld.get("data_addrs"):
            if best is None or len(ld["data_addrs"]) > len(best["data_addrs"]):
                best = ld
    if best is not None:
        das = best["data_addrs"]
        hop_a = int(best["hop"]) & 0x1F
        entry_i = int(best["entry"])
        addr_a = int(das[0])
        addr_b = int(das[1]) if len(das) > 1 else int(das[0])

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    out = []
    for a, b in zip(a_bytes, b_bytes):
        # ONE sample = WRITE a -> addr_a, WRITE b -> addr_b, JUMP entry. RAW words.
        chip.inject_data_physical([int(a) & 0xFFFF], target_hop_cnt=hop_a,
                                  target_addr=addr_a)
        chip.run(max_events=6000)
        chip.inject_data_physical([int(b) & 0xFFFF], target_hop_cnt=hop_a,
                                  target_addr=addr_b)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop_a, entry_addr=entry_i)
        chip.run(max_events=200000)
        got = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        out.append(got[0] if got else None)
    return out


# =============================================================================
# GR golden — LIVE blocks.xor_bb (2 connected byte inputs) in the GR subprocess
# =============================================================================

def _gr_xor(a_bytes, b_bytes):
    script = """
from gnuradio import gr, blocks
import json, sys
d = json.loads(sys.stdin.read())
a, b = d["a"], d["b"]
tb = gr.top_block()
sa = blocks.vector_source_b(a, False)
sb = blocks.vector_source_b(b, False)
op = blocks.xor_bb()
snk = blocks.vector_sink_b()
tb.connect(sa, (op, 0)); tb.connect(sb, (op, 1)); tb.connect(op, snk)
tb.run()
print(json.dumps(list(snk.data())))
"""
    r = subprocess.run([_GR_PY, "-c", script],
                       input=json.dumps({"a": list(map(int, a_bytes)),
                                         "b": list(map(int, b_bytes))}),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return [int(x) & 0xFFFF for x in json.loads(r.stdout.strip().splitlines()[-1])]


def _rand_bytes(seed, n=48):
    rng = random.Random(seed)
    return ([rng.randint(0, 255) for _ in range(n)],
            [rng.randint(0, 255) for _ in range(n)])


# =============================================================================
# Structure / smoke
# =============================================================================

def test_drives_and_captures():
    a, b = _rand_bytes(1, 12)
    dut = _run_xor_dut(a, b)
    assert all(v is not None for v in dut), f"missing egress: {dut}"
    e, ins = None, None
    from engine.catalog import BlockCatalog
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    e, ins = BlockCatalog.from_gr_kyttar().resolved_io("XorBlock", {}, library=_LIB)
    assert tuple(ins[:2]) == (0, 1), "a@R0, b@R1"


# =============================================================================
# Named edge cases (task): 0^0, 0xFF^0xFF, 0xFF^0x00, 0xAA^0x55
# =============================================================================

@pytest.mark.parametrize("a,b,exp", [
    (0x00, 0x00, 0x00),
    (0xFF, 0xFF, 0x00),
    (0xFF, 0x00, 0xFF),
    (0x00, 0xFF, 0xFF),
    (0xAA, 0x55, 0xFF),
    (0x55, 0xAA, 0xFF),
    (0xAA, 0xAA, 0x00),
    (0x0F, 0xF0, 0xFF),
    (0x3C, 0x5A, 0x66),
])
def test_edge_pairs(a, b, exp):
    dut = _run_xor_dut([a], [b])
    assert dut[0] == exp, f"{a:#04x}^{b:#04x}: got {dut[0]:#x}, want {exp:#x}"


def test_edge_vector_vs_gr():
    a = [0x00, 0xFF, 0xFF, 0x00, 0xAA, 0x55, 0xAA, 0x0F, 0xF0, 0x3C, 0x81, 0x7E]
    b = [0x00, 0xFF, 0x00, 0xFF, 0x55, 0xAA, 0xAA, 0xF0, 0x0F, 0x5A, 0x81, 0x01]
    dut = _run_xor_dut(a, b)
    gr = _gr_xor(a, b)
    res = _bitexact(dut, gr)
    print("\nxor edge:", res.summary())
    assert res.passed, res.summary()
    # Full 8-bit exactness (not just LSB): assert byte-for-byte equality directly.
    assert dut == gr, list(zip(dut, gr))


# =============================================================================
# Random (>= 3 seeds) vs LIVE GR — BIT-EXACT
# =============================================================================

@pytest.mark.parametrize("seed", [1, 7, 42, 256, 1337])
def test_random_vs_gr(seed):
    a, b = _rand_bytes(seed)
    dut = _run_xor_dut(a, b)
    gr = _gr_xor(a, b)
    res = _bitexact(dut, gr)
    print(f"\nxor random seed={seed}:", res.summary())
    assert res.passed, res.summary()
    assert dut == gr, f"seed {seed}: byte mismatch {list(zip(dut, gr))[:8]}"


def test_matches_local_reference():
    """DUT == the block's own bit-exact byte reference (independent of GR)."""
    a, b = _rand_bytes(99, 64)
    dut = _run_xor_dut(a, b)
    ref = XorBlock("r").process_reference_bytes(a, b)
    assert dut == ref, "on-chip XOR must equal process_reference_bytes"


# =============================================================================
# Orientation invariance — a correct feed-forward block is D4-invariant
# =============================================================================

_D4 = [
    [], ["cw"], ["cw", "cw"], ["cw", "cw", "cw"],
    ["mirror_h"], ["mirror_h", "cw"], ["mirror_v"], ["mirror_v", "cw"],
]


@pytest.mark.parametrize("orient", _D4)
def test_orientation_invariant(orient):
    a = [0x00, 0xFF, 0xAA, 0x55, 0x3C, 0x81, 0x0F, 0xF0]
    b = [0xFF, 0x00, 0x55, 0xAA, 0x5A, 0x7E, 0xF0, 0x0F]
    ref = XorBlock("r").process_reference_bytes(a, b)
    dut = _run_xor_dut(a, b, orient=orient)
    # Any orientation that egresses MUST be bit-exact (residual D4 no-output
    # anti-orientations of the single-block harness are tolerated as None, per
    # INV-23 — the feed-forward datapath is invariant where it emits at all).
    emitted = [(d, r) for d, r in zip(dut, ref) if d is not None]
    if not emitted:
        pytest.skip(f"orientation {orient}: no egress via single-block harness (INV-23)")
    assert all(d == r for d, r in emitted), \
        f"orientation {orient} not invariant: {[(hex(d), hex(r)) for d, r in emitted if d != r]}"


# =============================================================================
# MANDATORY mutation tests (INV-4) — the gate MUST catch each corruption
# =============================================================================

def _dut_gr(seed=7, n=48):
    a, b = _rand_bytes(seed, n)
    dut = _run_xor_dut(a, b)
    gr = _gr_xor(a, b)
    return a, b, dut, gr


def test_mutation_and_instead_of_xor_fails():
    """AND, not XOR — a real op corruption the gate must reject."""
    a, b, _, gr = _dut_gr()
    mutated = [(int(x) & int(y)) & 0xFFFF for x, y in zip(a, b)]
    res = _bitexact(mutated, gr)
    assert not res.passed, "gate failed to detect AND-instead-of-XOR!"


def test_mutation_or_instead_of_xor_fails():
    a, b, _, gr = _dut_gr()
    mutated = [(int(x) | int(y)) & 0xFFFF for x, y in zip(a, b)]
    res = _bitexact(mutated, gr)
    assert not res.passed, "gate failed to detect OR-instead-of-XOR!"


def test_mutation_drops_input_b_fails():
    """out = a (dropped the second stream) must be caught."""
    a, b, _, gr = _dut_gr()
    res = _bitexact([int(x) & 0xFFFF for x in a], gr)
    assert not res.passed, "gate failed to detect a dropped input (out=a)!"


def test_mutation_drops_input_a_fails():
    a, b, _, gr = _dut_gr()
    res = _bitexact([int(x) & 0xFFFF for x in b], gr)
    assert not res.passed, "gate failed to detect a dropped input (out=b)!"


def test_mutation_inverted_output_fails():
    _, _, dut, gr = _dut_gr()
    mutated = [(~int(w) & 0xFF) if w is not None else 0 for w in dut]
    res = _bitexact(mutated, gr)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_one_sample_offset_fails():
    _, _, dut, gr = _dut_gr()
    shifted = [0x00] + list(dut[:-1])
    res = _bitexact(shifted, gr)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, _, _, gr = _dut_gr()
    res = _bitexact([], gr)
    assert not res.passed, "gate failed to detect empty output!"


# =============================================================================
# Dashboard report
# =============================================================================

def test_emit_report():
    a = [0x00, 0xFF, 0xFF, 0x00, 0xAA, 0x55, 0xAA, 0x0F, 0xF0, 0x3C]
    b = [0x00, 0xFF, 0x00, 0xFF, 0x55, 0xAA, 0xAA, 0xF0, 0x0F, 0x5A]
    dut = _run_xor_dut(a, b)
    gr = _gr_xor(a, b)
    res = _bitexact(dut, gr)
    assert res.passed, res.summary()
    write_report("XorBlock", res, coverage={
        "edge": True, "random": 5, "orientation": True,
        "local_reference": True, "mutation": True, "bit_exact": True})
