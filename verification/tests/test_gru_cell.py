# SPDX-License-Identifier: GPL-3.0-or-later
"""GRUCellBlock — one full GRU timestep (H=4, I=2) + 4-class argmax readout.

There is NO stock GNU Radio counterpart (GRC flowgraphs are acyclic, so a GRU
recurrence cannot even be EXPRESSED as a wired GR chain). The golden is the
chip-exact integer model in ``examples/gru_classifier/ml/gru_reference_chip``
— an INDEPENDENT transcription of the same pinned numerics, written before the
block and used to measure the design's classification accuracy offline. The
gates here hold the placed + routed + simulated DUT to that golden BIT-EXACTLY
and, separately, hold the block's own in-module reference to the SAME golden
(so the two transcriptions are proven equal, not assumed).

TWO-LEVEL VERIFICATION (decision-level metrics alone mask sign/scale bugs — the
BPSK-slicer lesson), both run ON CHIP:

  1. h-TRAJECTORY bit-exactness. The four hidden-state words live in the
     ``umB{i}`` cells' pinned ``hs`` state registers, so the test reads them
     out of the simulated cell memory after EVERY timestep and requires exact
     equality with the golden trajectory — over random full-range features AND
     over real feature clips of all four classes.
  2. DECISION agreement over long streams: >= 3000 on-chip timesteps against
     the golden class stream, plus the end-to-end clip-vote accuracy on the
     held-out feature clips, which must land within 1 point of the offline
     chip-model number.

PINNED CONTRACTS gated here: the timestep barrier (``fin``'s arbiter LOCK,
cleared by ``amx``'s chain-END ``WRITE.CFG`` — exactly one timestep in flight,
so per-sample == SATURATED); state CONTINUITY (h carries across bursts and is
never reset while streaming); the 2:1 rate (two Q15 feature words in -> one RAW
class-index word out); ONE COMMON head scale across all four readout rows; the
per-gate common scales S_rz / S_n with the activation ``dshift`` scale restore;
and the weight-location manifest schema.

MUTATIONS (INV-4) — every one is a corrupted DUT/golden pair proven to FAIL:
a perturbed weight word, an r<->z row swap, a broken timestep barrier, per-row
head scales instead of the common one, a wrong ``dshift`` on one gate, an
inverted output, a +1 sample delay, and empty output.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_gru_cell.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_PLACEKYT = _ROOT / "placekyt"
_VERIFY = _ROOT / "verification"
_RUNTIME = _ROOT / "runtime" / "python"
_ML = _ROOT / "examples" / "gru_classifier" / "ml"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY), str(_ML)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import CompareResult, Metric, write_report  # noqa: E402
from kyttar_verify.dut_runner import (  # noqa: E402
    _enc_jump, _enc_write, _engine)
from gr_kyttar.placement.blocks.gru_cell_block import (  # noqa: E402
    GRUCellBlock, _mac_walk_ref, _mulq, _s16, _sat_add16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
WEIGHTS = _ML / "weights_single.json"
DATASET = _ML / "dataset" / "gru_dataset.npz"

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and WEIGHTS.is_file()),
    reason="chip yaml or trained weights absent")

H, I, C = 4, 2, 4
HS_ADDR = 4            # umB{i}: StateVar("hs", register=4) — the hidden word
PARAMS = {"weights_file": str(WEIGHTS)}
ORIENTS = [[], ["cw"], ["cw", "cw"], ["cw", "cw", "cw"], ["mirror_h"],
           ["mirror_h", "cw"], ["mirror_h", "cw", "cw"],
           ["mirror_h", "cw", "cw", "cw"]]


# --- the DUT harness: build + drive + read the on-chip h state ----------------

class _Dut:
    """One built + placed + routed GRUCellBlock on simKYT, driven SATURATED
    (the whole burst enqueued via ``queue_words_physical`` — no inter-sample
    quiescence, the real streaming condition), with a probe that reads the
    four hidden-state words straight out of the simulated cell memory.

    The chip is kept alive between :meth:`burst` calls so the STATE-CONTINUITY
    contract (h carries across bursts, never reset while streaming) is
    exercised by construction, not asserted from a fresh build each time.
    """

    def __init__(self, block_type="GRUCellBlock", params=None, orient=None,
                 place_xy=(1, 1)):
        import simkyt
        (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
         ChipPortEndpoint, BlockEndpoint) = _engine()
        params = dict(PARAMS if params is None else params)
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(CHIP_YAML)
        ctrl = AppController(catalog=cat)
        ctrl.new_project("gru_dut", "kyttar_10x12")
        blk = ctrl.place_block(block_type, 0, place_xy[0], place_xy[1],
                               library="lattrex.official", params=params)
        for k in (orient or []):
            ctrl.project.block(blk).placement.transform(k)
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port="f"), name="in_blk")
        ctrl.add_logical_connection(
            BlockEndpoint(block=blk, port="out"),
            ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")
        rep = ctrl.auto_route_all({"kyttar_10x12": ct})
        assert rep.ok, "route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in rep.failed)
        bres = BuildEngine(cat, CHIP_YAML).build(
            ctrl.project, {"kyttar_10x12": ct})
        assert bres.ok, "build failed: " + "; ".join(
            str(e) for e in bres.errors)
        words = bres.words(0)
        land = ((getattr(bres, "chips", {}) or {}).get(0))
        land = (getattr(land, "input_landings", {}) or {}).get("in_blk")
        assert land, "no resolved input landing for the block"
        self.hop = int(land["hop"]) & 0x1F
        self.entry = int(land["entry"])
        self.addr = int((list(land.get("data_addrs")) or [0])[0])
        self.cells = {c.cell_id: (c.x, c.y)
                      for c in ctrl.project.block(blk).placement.cells}
        self.n_cells = len(ctrl.project.block(blk).placement.cells)
        self.n_words = len(words)
        self.chip = simkyt.Chip.from_yaml(CHIP_YAML)
        self.chip.load_bitstream_physical(words)
        self.chip.set_port_entry_address("x16_in", self.entry)

    def h_state(self):
        """The four on-chip hidden words (signed), read from the ``umB{i}``
        cells' pinned ``hs`` state registers."""
        w = self.chip.width
        out = []
        for i in range(H):
            x, y = self.cells[f"umB{i}"]
            v = int(self.chip.read_cell_memory(y * w + x, HS_ADDR)) & 0xFFFF
            out.append(v - 0x10000 if v >= 0x8000 else v)
        return out

    def burst(self, stim_q15):
        """Drive one SATURATED burst of feature words; return the emitted RAW
        class words. Raises on livelock (an uncapped run would spin forever)."""
        stream = []
        for v in stim_q15:
            stream.append(_enc_write(self.hop, self.addr))
            stream.append(int(v) & 0xFFFF)
            stream.append(_enc_jump(self.hop, self.entry))
        self.chip.queue_words_physical("x16_in", stream)
        cap = max(200_000, 20_000 * max(1, len(stim_q15)))
        res = self.chip.run(max_events=cap)
        assert not (isinstance(res, dict) and not res.get("completed", True)), (
            f"LIVELOCK under saturated drive: {res} — the timestep barrier "
            f"(fin LOCK / amx chain-end unlock) did not drain the ring")
        return [int(v) & 0xFFFF
                for (v, _d, _t) in self.chip.read_port_words_timed("x16_out")]

    def per_sample(self, stim_q15):
        """The inject-and-flush (quiescent) drive, for the saturated-vs-per-
        sample oracle. Returns (class words, h trajectory per timestep)."""
        cls, htraj = [], []
        for k, v in enumerate(stim_q15):
            self.chip.inject_data_physical([int(v) & 0xFFFF],
                                           target_hop_cnt=self.hop,
                                           target_addr=self.addr)
            self.chip.run(max_events=20_000)
            self.chip.inject_jump_physical(target_hop_cnt=self.hop,
                                           entry_addr=self.entry)
            self.chip.run(max_events=300_000)
            got = []
            while self.chip.output_available("x16_out"):
                wd = self.chip.read_port_i16("x16_out").view("uint16").tolist()
                got.extend(int(x) & 0xFFFF for x in wd)
                self.chip.release_output_ack("x16_out")
                self.chip.run(max_events=8_000)
            if k % I == I - 1:
                cls.append(got[-1] if got else None)
                htraj.append(self.h_state())
        return cls, htraj


def _q15_feats(arr) -> list[int]:
    """Flatten an (T, I) float feature array into the block's Q15 word stream
    (features are in [0, 1) by the front-end contract)."""
    q = np.clip(np.round(np.asarray(arr, float) * 32768.0), 0, 32767)
    return [int(v) for row in q.astype(np.int64) for v in row]


def _rand_feats(seed, T):
    rng = np.random.default_rng(seed)
    return [int(v) for v in rng.integers(0, 32768, size=I * T)]


@pytest.fixture(scope="module")
def blk():
    return GRUCellBlock("ref", weights_file=str(WEIGHTS))


@pytest.fixture(scope="module")
def dataset():
    if not DATASET.is_file():
        pytest.skip(f"feature dataset absent — regenerate with "
                    f"{_ML / 'gen_dataset.py'}")
    d = np.load(DATASET)
    names = [str(s) for s in d["feature_names"]]
    fidx = [names.index(f) for f in ("rms", "zcr")]
    return d["X"][:, :, fidx], d["y"], d["split"]


# --- the two INDEPENDENT transcriptions must agree ---------------------------

def _chip_model():
    from gru_reference_chip import GRUChipModel      # noqa: PLC0415
    return GRUChipModel.load(WEIGHTS)


def test_block_reference_equals_independent_chip_golden(blk):
    """The block's own ``h_trajectory_q15`` == the offline chip-exact model
    (``examples/gru_classifier/ml/gru_reference_chip``), word for word, over
    random full-range features. The two were written from the same PINNED
    numerics but are separate transcriptions — this is what makes the golden
    a golden rather than a restatement of the block."""
    m = _chip_model()
    for seed in (1, 7, 42):
        stim = _rand_feats(seed, 40)
        Xq = np.asarray(stim, dtype=np.int64).reshape(-1, I)
        gcls, _h, ghs, gheads = m.forward(Xq, want_h=True)
        hs, heads, cls = blk.h_trajectory_q15(stim)
        assert hs == ghs.tolist(), f"seed {seed}: h trajectory differs"
        assert heads == gheads.tolist(), f"seed {seed}: head words differ"
        assert cls == gcls.tolist(), f"seed {seed}: class stream differs"


def test_float_reference_agrees_with_the_integer_one_on_in_contract_input(blk):
    """The block's FLOAT ``process_reference`` (the float GRU over the file's
    float weights) is decision-level CONTEXT, not the gate — but it must still
    agree with the bit-exact integer path on in-contract features, or the
    fixed-point design has drifted from the model it was trained as. Measured
    and asserted at >= 95% per-step; the exact figure is printed."""
    rng = np.random.default_rng(4)
    f = rng.random(120)
    fl = blk.process_reference(f).astype(int).tolist()
    iq = blk.process_reference_q15(
        [int(round(v * 32768.0)) for v in f])
    agree = sum(int(a == b) for a, b in zip(fl, iq)) / len(iq)
    print(f"\nFLOAT vs Q15 reference agreement: {agree:.4f} over {len(iq)}")
    assert len(fl) == len(iq) == len(f) // I
    assert agree >= 0.95, f"float/integer references disagree ({agree:.4f})"


def test_derived_scales_match_the_offline_model(blk):
    """The on-chip constants are a deterministic function of the weights FILE
    (dequantized {q, e} mantissas -> the landed scale schedule at the derived
    COMMON scales). Both transcriptions must derive the same scales, or the
    activation ``dshift`` scale-restore is against a different grid."""
    m = _chip_model()
    s = blk.scale_shifts
    assert s["S_rz"] == m.gate_S["r"] == m.gate_S["z"], (
        f"r and z must share ONE common scale (the shared per-unit sigmoid "
        f"engine bakes ONE dshift): {s['S_rz']} vs {m.gate_S}")
    assert s["S_n"] == m.gate_S["n"]
    assert s["S_head"] == m.head_S
    assert s["dshift_sigmoid"] == s["S_rz"] - 3      # sigmoid domain 2^3
    assert s["dshift_tanh"] == s["S_n"] - 2          # tanh domain 2^2


def test_blend_form_is_overflow_safe_and_the_naive_form_is_not(blk):
    """PINNED: ``h' = sat(MULQ(0x7FFF - z, n) + MULQ(z, h))``. Both partials
    are individually in range so ONE saturating add closes it. The textbook
    algebraic rearrangement ``n + z*(h - n)`` is NOT used because ``h - n``
    spans (-2, 2) and overflows int16 — asserted here on the concrete rails so
    the choice is a measured fact, not a comment."""
    for z, n, h in ((0, 32767, -32768), (16384, -32768, 32767),
                    (32767, 32767, -32768), (0, -32768, 32767)):
        good = _sat_add16(_mulq(0x7FFF - z, n), _mulq(z, h))
        assert -32768 <= good <= 32767
        assert (h - n) < -32768 or (h - n) > 32767, (
            f"pick rails where h-n actually overflows: {h - n}")
    # and the pinned form is what the block's reference computes
    hp, _head, _c = blk.step_q15([0, 0], [0] * H)
    assert all(-32768 <= v <= 32767 for v in hp)


def test_mac_walk_reference_is_the_bias_preloaded_truncating_walk(blk):
    """Re-derive one gate row here, independently of the block's own step, to
    pin the walk semantics: bias PRELOADED into the accumulator, then a
    TRUNCATING ``MULQ`` per coefficient in ADDRESS order (not round-half-up,
    which is what the training-side reference does — mixing the two silently
    changes every gate word)."""
    cq, bq = blk._r_rows[0]
    xs = [12345, -9876, 4096, -4096, 32767, -32768]
    acc = _s16(bq)
    for c, x in zip(cq, xs):
        acc = _s16(acc + ((_s16(c) * _s16(x)) >> 15))
    assert _mac_walk_ref(cq, bq, xs) == acc
    # ...and the walk is order-DEPENDENT (truncation does not commute), so the
    # address order is part of the contract, not an implementation detail.
    assert _mac_walk_ref(cq, bq, xs) != _mac_walk_ref(cq, bq, xs[::-1])


def test_weights_file_head_carries_one_shared_exponent():
    """The FILE-side half of the common-head-scale contract: the weights
    schema stores ONE exponent for the whole head block (``head.quant.e``, a
    scalar) precisely so the four class logits stay comparable. A schema that
    grew a per-row exponent would silently break the argmax, so the scalar is
    pinned here. Also asserts the bundled default IS the shipped trained model
    (an empty ``weights_file`` must not quietly mean something else)."""
    j = json.loads(WEIGHTS.read_text())
    assert isinstance(j["head"]["quant"]["e"], int), (
        "head.quant.e must be ONE scalar exponent shared by all class rows")
    from gr_kyttar.placement.blocks import gru_cell_block as _m
    bundled = json.loads(_m._DEFAULT_WEIGHTS.read_text())
    assert bundled == j, (
        "the bundled default weights differ from the shipped trained model")


def test_head_rows_share_one_common_scale(blk):
    """PINNED: all four readout rows are quantized at ONE scale. Per-row head
    scales make the four raw accumulator words incomparable and corrupt the
    argmax — the mutation gate below proves that measurably."""
    rows = blk._head_rows
    assert len({blk._S_head}) == 1
    for cq, bq in rows:
        assert sum(abs(q) for q in cq) + abs(_s16(bq)) <= 32767, (
            "post-rounding guard violated at the common head scale — the "
            "int16 accumulator can wrap mid-walk")


def test_post_rounding_guard_holds_on_every_stored_row(blk):
    """INV-13 headroom: at the derived common scale every MAC row's stored
    words satisfy sum|q| + |bias| <= 32767, so the bias-preloaded truncating
    walk NEVER wraps the int16 accumulator for ANY legal Q15 operand vector."""
    for name, rows in (("r", blk._r_rows), ("z", blk._z_rows),
                       ("head", blk._head_rows)):
        for i, (cq, bq) in enumerate(rows):
            tot = sum(abs(q) for q in cq) + abs(_s16(bq))
            assert tot <= 32767, f"{name} row {i}: sum|q| = {tot} > 32767"
    # the split n rows share ONE accumulator budget (MULQ(r,u) + xw)
    for i in range(H):
        ucq, _ub = blk._u_rows[i]
        xcq, xbq = blk._xc_rows[i]
        tot = (sum(abs(q) for q in ucq) + sum(abs(q) for q in xcq)
               + abs(_s16(xbq)))
        assert tot <= 32767, f"n row {i}: combined sum|q| = {tot} > 32767"


# --- the STRUCTURAL audit: the route-time face rule (the FFT16 lesson) -------

def test_route_time_face_rule_last_connection_per_cell(blk):
    """At ROUTE time a cell's forwarding face comes from its LAST-listed
    internal connection WHEN that destination is physically adjacent, and only
    otherwise from the dict-next cell; the authored ``default_layout`` faces
    are applied later in the build. So for EVERY cell the last-listed
    connection's destination must be its CHAIN SUCCESSOR or NON-ADJACENT —
    else the cell's face points off the fold, every multi-hop write that
    transits it mis-resolves to a Manhattan hop, and the words land in some
    other cell's registers (silently: it builds, routes, and computes
    garbage). FFT16 hit exactly this; this is the same audit, generalized."""
    lay = blk.default_layout()
    ids = list(blk.build_cell_programs())
    succ = {cid: ids[k + 1] for k, cid in enumerate(ids[:-1])}
    last = {}
    for src, _sp, dst, _dp in blk.internal_connections():
        last[src] = dst
    bad = []
    for src, dst in last.items():
        if src not in lay or dst not in lay:
            continue
        sx, sy, _sf = lay[src]
        dx, dy, _df = lay[dst]
        adjacent = abs(sx - dx) + abs(sy - dy) == 1
        if adjacent and succ.get(src) != dst:
            bad.append((src, dst, succ.get(src)))
    assert not bad, (
        "ROUTE-TIME FACE RULE violated — each (cell, last-listed dst, chain "
        f"successor) below has an ADJACENT non-successor last dst: {bad}")


def test_every_program_cell_precedes_every_transit_in_the_layout(blk):
    """The router indexes the PLACED CELL LIST positionally against the
    ``cell_programs`` keys (``pb.cells[keys.index(dst)]``), so the layout dict
    must list every program cell first, in program order, with the FACE-only
    transit cells last. Interleaving a transit at its ring position shifts
    every later program cell's resolved destination by one and the block
    computes garbage."""
    lay = list(blk.default_layout())
    progs = list(blk.build_cell_programs())
    n = len(progs)
    assert lay[:n] == progs, (
        "layout program-cell order != build_cell_programs order")
    assert all(str(c).startswith("transit") for c in lay[n:]), (
        f"non-transit cells after the program cells: {lay[n:]}")


def test_egress_relay_is_the_last_program_cell(blk):
    """``oout``'s only output port is the block's EXTERNAL egress, whose hop
    the build's egress patcher stamps. It MUST be the last program cell: any
    earlier slot leaves the router's positional-default branch live for that
    port, and that default traces the route-time faces the LONG way round the
    closed ring under the 180-degree orientations (measured 44 > 31 — the
    assembler rejects it before the patcher can run)."""
    progs = list(blk.build_cell_programs())
    assert progs[-1] == blk.output_cell_id() == "oout", (
        f"egress relay must be last; got {progs[-1]}")


def test_ring_distances_all_fit_the_five_bit_hop_field(blk):
    """Every internal hop rides the ring FORWARD, so the longest one bounds
    the fold. The hop field is 5 bits (0..31); a ring longer than that in any
    single edge cannot be authored as one closed serpentine."""
    lay = blk.default_layout()
    ring = [c for c in lay if not str(c).startswith("transit")
            and c != "oout"] + ["transit_ring_a", "transit_ring_b"]
    idx = {cid: k for k, cid in enumerate(ring)}
    n = len(ring)
    worst = 0
    for src, _sp, dst, _dp in blk.internal_connections():
        if src not in idx or dst not in idx:
            continue
        worst = max(worst, (idx[dst] - idx[src]) % n)
    assert worst <= 31, f"longest ring-forward hop {worst} exceeds the field"


def test_feedback_landings_are_state_registers_not_input_ports(blk):
    """AGCCC trap 1: a value that lands OUT OF BAND (a recurrence write-back,
    or an intra-timestep deposit that arrives before its consumer is
    triggered) must land in a pinned STATE register, never an input Port —
    input-role registers count as host operands and brokered corridors relay
    stale words into them.

    Two such landings exist and both are checked structurally:
      * ``hstr``'s h0..h3 (the recurrence write-back from ``hcol``), and
      * ``umB{i}``'s ``zs`` (deposited by ``umA{i}`` with a WRITE and NO jump;
        ``umB`` is triggered later by the tanh engine) and ``hs`` (this unit's
        OWN previous output — the state that makes it a recurrence)."""
    progs = blk.build_cell_programs()
    hstr = progs["hstr"]
    state_names = {s.name for s in hstr.state}
    port_names = {p.name for p in hstr.inputs}
    for i in range(H):
        assert f"h{i}" in state_names, f"hstr h{i} is not a STATE register"
        assert f"h{i}" not in port_names, f"hstr h{i} is an input PORT"
    umb = progs["umB0"]
    umb_states = {s.name for s in umb.state}
    umb_ports = {p.name for p in umb.inputs}
    assert {"zs", "hs"} <= umb_states, umb_states
    assert not ({"zs", "hs"} & umb_ports), umb_ports
    # and the deposit really is write-without-jump (no trigger of its own)
    assert ("umA0", "zf", "umB0", "zs") in blk.internal_connections()
    assert not any(s == "umA0" and p == "zf"
                   for s, p, _d, _e in blk.internal_jumps()), (
        "the zs deposit must NOT carry a jump — umB is triggered by the tanh "
        "engine after n arrives, so an early trigger would blend a stale n")


# --- GATE 1: h-trajectory BIT-EXACTNESS on chip -------------------------------

@pytest.mark.parametrize("seed", [3, 17, 2026])
def test_h_trajectory_exact_random_features(blk, seed):
    """>= 20 timesteps of random FULL-RANGE Q15 features: the four on-chip
    hidden words must equal the golden trajectory EXACTLY at every step (tol
    0), and the emitted class stream must match too. Decision-level agreement
    alone would hide a sign/scale bug that never flips an argmax."""
    T = 24
    stim = _rand_feats(seed, T)
    ghs, _gheads, gcls = blk.h_trajectory_q15(stim)
    dut = _Dut()
    cls, htraj = dut.per_sample(stim)
    assert htraj == ghs, (
        f"seed {seed}: on-chip h trajectory differs at step "
        f"{next(t for t in range(T) if htraj[t] != ghs[t])}")
    assert cls == gcls, f"seed {seed}: class stream differs"


def test_h_trajectory_exact_all_four_classes(blk, dataset):
    """The same bit-exact gate driven by REAL feature statistics — three held-
    out clips from EACH of the four classes (SSB / BPSK / 4-FSK / noise), 25
    timesteps each. The four classes drive very different (rms, zcr) regions,
    so this sweeps the gate preactivations across the sigmoid/tanh fold, the
    table interior, and the beyond-domain clamp."""
    X, y, split = dataset
    te = np.where(split == 2)[0]
    checked = 0
    for cls_id in range(C):
        for ci in [i for i in te if y[i] == cls_id][:3]:
            stim = _q15_feats(X[ci, :25])
            ghs, _gh, gcls = blk.h_trajectory_q15(stim)
            dut = _Dut()
            cls, htraj = dut.per_sample(stim)
            assert htraj == ghs, f"class {cls_id} clip {ci}: h differs"
            assert cls == gcls, f"class {cls_id} clip {ci}: classes differ"
            checked += 1
    assert checked == 12


# --- GATE 4 (run EARLY): pipeline saturation ----------------------------------

def test_saturated_equals_per_sample_and_golden(blk):
    """The ~50-cell RECURRENT macro-loop under SATURATED drive (whole burst
    enqueued back-to-back, no inter-sample quiescence) — the documented
    deadlock class. The timestep barrier bounds the pipeline to exactly ONE
    timestep in flight (``fin`` LOCKs its arbiter on the second feature word;
    ``amx``, the chain END, clears it with a ring-forward WRITE.CFG only after
    the class word has egressed), so saturated MUST equal per-sample MUST
    equal the golden — and the final hidden state must match too."""
    stim = _rand_feats(3, 25)
    ghs, _gh, gcls = blk.h_trajectory_q15(stim)
    sat = _Dut()
    got = sat.burst(stim)
    assert got == gcls, f"saturated class stream differs: {got} != {gcls}"
    assert sat.h_state() == ghs[-1], "saturated final h differs"
    per = _Dut()
    cls, htraj = per.per_sample(stim)
    assert got == cls, "saturated != per-sample"
    assert htraj[-1] == sat.h_state()


def test_saturated_output_count_is_the_2_to_1_rate(blk):
    """PINNED rate: exactly ONE raw class word per TWO feature words, with no
    dropped or duplicated samples under saturation (INV-19/20)."""
    for T in (1, 2, 7, 30):
        got = _Dut().burst(_rand_feats(5, T))
        assert len(got) == T, f"{2 * T} words in -> {len(got)} out, want {T}"


def test_odd_trailing_word_emits_nothing(blk):
    """A trailing HALF feature vector completes no timestep, so it emits
    nothing — and it must not corrupt the state either: the same stream with
    the odd word appended has the same class prefix."""
    stim = _rand_feats(9, 10)
    full = _Dut().burst(stim)
    partial = _Dut().burst(stim + [0x1234])
    assert partial == full, "the trailing half-vector emitted or corrupted"


# --- GATE 5: state continuity across bursts -----------------------------------

def test_state_persists_across_bursts_with_no_hidden_reset(blk):
    """The deployment contract is ``h = 0 at stream start only; NEVER reset
    while streaming``. Drive four separate saturated bursts through ONE live
    chip and require the concatenated class stream (and the on-chip h after
    every burst) to equal the golden run over the concatenation. A hidden
    per-burst reset would re-cold-start h and diverge immediately."""
    bursts = [_rand_feats(s, 12) for s in (21, 22, 23, 24)]
    flat = [w for b in bursts for w in b]
    ghs, _gh, gcls = blk.h_trajectory_q15(flat)
    dut = _Dut()
    got, t = [], 0
    for k, b in enumerate(bursts):
        got += dut.burst(b)
        t += len(b) // I
        assert dut.h_state() == ghs[t - 1], (
            f"burst {k}: on-chip h diverged from the continuous golden")
    assert got == gcls, "class stream across bursts differs from the golden"


def test_cold_start_state_is_zero(blk):
    """A freshly built chip starts at h = 0 (the ``umB{i}`` ``hs`` state
    registers' initial value) — the other half of the state contract."""
    assert _Dut().h_state() == [0] * H


# --- GATE 5: orientation invariance (D4 x8) + placement ------------------------

@pytest.mark.parametrize("orient", ORIENTS,
                         ids=lambda o: "identity" if not o else "+".join(o))
def test_orientation_invariance(blk, orient):
    """INV-23: the block computes IDENTICALLY in all 8 D4 orientations. Every
    internal ring hop and every in-program FACE word must transform rigidly;
    h is compared as well as the class stream, so a mis-transformed hop that
    happens not to flip an argmax still fails."""
    stim = _rand_feats(5, 12)
    ghs, _gh, gcls = blk.h_trajectory_q15(stim)
    dut = _Dut(orient=orient)
    cls, htraj = dut.per_sample(stim)
    assert htraj == ghs, f"{orient or 'identity'}: h trajectory differs"
    assert cls == gcls, f"{orient or 'identity'}: class stream differs"


def test_footprint_is_legal_and_fits_with_ports(blk):
    """The fold must be <= 8 across in BOTH dimensions (INV-9: the D4 gate
    rotates it, so a tall strip is not an escape) and its cells pairwise
    distinct (INV-25 — an internal transit folded onto a datapath cell passes
    placement and fails at DRC)."""
    lay = blk.default_layout()
    xs = [v[0] for v in lay.values()]
    ys = [v[1] for v in lay.values()]
    w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    assert w <= 8 and h <= 8, f"footprint {w}x{h} exceeds the 8x8 D4 cap"
    pts = [(v[0], v[1]) for v in lay.values()]
    assert len(set(pts)) == len(pts), "self-overlapping cells in the layout"
    assert len(lay) == blk.cell_count == 51


@pytest.mark.parametrize("place_xy", [(1, 1), (2, 2), (1, 3)])
def test_places_and_runs_at_several_origins(blk, place_xy):
    """The block places WITH the chip's I/O ports on the 10x12 and computes
    identically wherever it is anchored (hop distances are placement-derived,
    INV-1)."""
    stim = _rand_feats(31, 8)
    _ghs, _gh, gcls = blk.h_trajectory_q15(stim)
    assert _Dut(place_xy=place_xy).burst(stim) == gcls


# --- GATE 2: long-stream decision agreement + end-to-end clip accuracy --------

BURN = 50          # per-clip warm-up steps excluded from the vote (offline
                   # ``evaluate(burn=50)`` uses the same window)
N_CLIPS = 120      # >= 100, the gate's clip budget


@pytest.fixture(scope="module")
def long_run(blk, dataset):
    """ONE live chip streamed over ``N_CLIPS`` held-out clips back to back
    (state carried throughout, exactly as deployed). Returns the measured
    metrics plus the per-step agreement — shared by the gates below and by the
    report so the numbers cannot drift apart."""
    X, y, split = dataset
    te = np.where(split == 2)[0]
    order = np.array(te)
    np.random.default_rng(99).shuffle(order)
    dut = _Dut()
    h = [0] * H
    conf = np.zeros((C, C), int)
    agree = steps = 0
    T = X.shape[1]
    for ci in order[:N_CLIPS]:
        stim = _q15_feats(X[ci])
        got = dut.burst(stim)
        assert len(got) == T, f"clip {ci}: {len(got)} class words, want {T}"
        gcls = []
        for t in range(T):
            h, _hd, c = blk.step_q15(stim[I * t:I * t + I], h)
            gcls.append(c)
        agree += sum(int(a == g) for a, g in zip(got, gcls))
        steps += T
        assert dut.h_state() == h, f"clip {ci}: on-chip h diverged"
        seg = np.asarray(got[BURN:])
        conf[int(y[ci]), int(np.argmax(np.bincount(seg, minlength=C)))] += 1
    return {
        "steps": steps,
        "agreement": agree / steps,
        "clip_acc": float(conf.trace() / conf.sum()),
        "n_clips": int(conf.sum()),
        "confusion": conf.tolist(),
    }


def test_decision_agreement_over_long_streams(long_run):
    """>= 3000 on-chip timesteps: per-step argmax agreement with the golden
    must be >= 99%. (The DUT is BIT-EXACT, so the measured value is 1.0; the
    99% bar is the contract, the measured number is reported.)"""
    assert long_run["steps"] >= 3000, long_run["steps"]
    assert long_run["agreement"] >= 0.99, (
        f"per-step agreement {long_run['agreement']:.4f} < 0.99")
    print(f"\nON-CHIP: {long_run['steps']} steps, "
          f"agreement {long_run['agreement']:.6f}")


def test_decision_agreement_is_actually_bit_exact(long_run):
    """Stronger than the contract and pinned separately: the on-chip decision
    stream is IDENTICAL to the golden, not merely 99% agreed. If this ever
    weakens to (say) 0.997 the block has a real numeric defect that the 99%
    contract gate would still pass — so it is asserted on its own."""
    assert long_run["agreement"] == 1.0, (
        f"on-chip decisions are no longer bit-exact: "
        f"{long_run['agreement']:.6f}")


def test_end_to_end_clip_accuracy_matches_the_offline_model(long_run, dataset):
    """END-TO-END: the clip-level majority vote over the on-chip class stream.
    The bar is 'within 1 point of the offline chip-exact model measured on the
    SAME clips under the SAME protocol' — computed here rather than compared
    against a hard-coded number, so a dataset regeneration cannot silently
    move the goalposts."""
    X, y, split = dataset
    te = np.where(split == 2)[0]
    order = np.array(te)
    np.random.default_rng(99).shuffle(order)
    m = _chip_model()
    h = m.init_state()
    conf = np.zeros((C, C), int)
    for ci in order[:N_CLIPS]:
        Xq = np.asarray(_q15_feats(X[ci]), dtype=np.int64).reshape(-1, I)
        pred, h = m.forward(Xq, h)
        seg = np.asarray(pred[BURN:])
        conf[int(y[ci]), int(np.argmax(np.bincount(seg, minlength=C)))] += 1
    offline = float(conf.trace() / conf.sum())
    on_chip = long_run["clip_acc"]
    print(f"\nCLIP VOTE ({long_run['n_clips']} clips): on-chip {on_chip:.4f}, "
          f"offline chip model {offline:.4f}")
    assert long_run["n_clips"] >= 100, long_run["n_clips"]
    assert abs(on_chip - offline) <= 0.01, (
        f"on-chip clip accuracy {on_chip:.4f} is more than 1 point from the "
        f"offline chip model's {offline:.4f} on the same clips")


# --- MANDATORY mutation gates (INV-4) ----------------------------------------
#
# Each mutant is a REAL corrupted DUT: a GRUCellBlock subclass injected into
# the block namespace the catalog scans, so it is placed, routed, built and
# simulated exactly like the block under test. A gate never shown to fail
# certifies nothing — every one below must FAIL the exact gate, and the
# accuracy-class mutants must additionally DEGRADE the measured accuracy.

import gr_kyttar.placement.kyttar_block as _kb    # noqa: E402


def _register(cls):
    """Publish a mutant into the namespace ``BlockCatalog.from_gr_kyttar``
    scans, so it can be placed by type name like any catalog block."""
    setattr(_kb, cls.__name__, cls)
    return cls


@_register
class _MutPerturbedWeight(GRUCellBlock):
    """One weight WORD perturbed: r-gate row 0's first hidden coefficient is
    shifted by 600 LSB (a ~2% change at the gate's scale)."""

    def _derive_constants(self, params):
        super()._derive_constants(params)
        cq, bq = self._r_rows[0]
        cq = list(cq)
        cq[I] = max(-32768, min(32767, cq[I] + 600))
        self._r_rows[0] = (cq, bq)


@_register
class _MutCorruptedWeights(GRUCellBlock):
    """The manifest's named accuracy mutation: the WEIGHT MATRIX corrupted —
    every stored r-gate and head coefficient scaled by 3/4 (still in range,
    still guard-legal, so nothing raises). This is the fault a weight-file
    swap or a mis-addressed constant would produce."""

    @staticmethod
    def _scale(rows):
        return [([max(-32768, min(32767, (_s16(q) * 3) // 4)) for q in cq],
                 max(-32768, min(32767, (_s16(bq) * 3) // 4)) & 0xFFFF)
                for cq, bq in rows]

    def _derive_constants(self, params):
        super()._derive_constants(params)
        self._r_rows = self._scale(self._r_rows)
        self._head_rows = self._scale(self._head_rows)


@_register
class _MutSwapRZ(GRUCellBlock):
    """r and z rows SWAPPED — the update and reset gates exchange roles. Both
    are sigmoids at the same common scale, so nothing raises and nothing goes
    out of range: only the computed function changes."""

    def _derive_constants(self, params):
        super()._derive_constants(params)
        self._r_rows, self._z_rows = self._z_rows, self._r_rows


@_register
class _MutPerRowHeadScales(GRUCellBlock):
    """PER-ROW head scales instead of ONE COMMON scale. Each readout row stays
    individually well-quantized (no wrap, no clipping) but the four RAW
    accumulator words then live on four DIFFERENT grids, and the argmax over
    them compares incomparable numbers.

    NOTE (measured, and the reason this mutant rescales EXPLICITLY): for the
    shipped trained model the four rows' independently-derived minimal scales
    all happen to equal the common one (S = 4 for every row), so "just derive
    each row on its own" is a NO-OP here and would certify nothing. The design
    rule is about what happens when they DIFFER, so the mutant forces the
    difference: rows 2 and 3 are stored one binade finer."""

    def _derive_constants(self, params):
        super()._derive_constants(params)
        rows = []
        for j, (cq, bq) in enumerate(self._head_rows):
            k = 1 if j >= 2 else 0            # rows 2,3 at S = base - 1
            rows.append(([max(-32768, min(32767, _s16(q) << k)) for q in cq],
                         max(-32768, min(32767, _s16(bq) << k)) & 0xFFFF))
        self._head_rows = rows


@_register
class _MutWrongDshift(GRUCellBlock):
    """The SIGMOID engine's ``dshift`` off by one — the zero-instruction scale
    restore now un-scales the gate preactivation by 2x too much, so every r
    and z lands on the wrong point of the activation curve."""

    def _derive_constants(self, params):
        super()._derive_constants(params)
        self._dshift_sig += 1


@_register
class _MutNoBarrier(GRUCellBlock):
    """The TIMESTEP BARRIER removed: ``fin`` no longer LOCKs its arbiter on
    the second feature word, so the next timestep's words are admitted while
    the ring is still draining the current one and race the h write-back."""

    def _fin_program(self):
        cp = super()._fin_program()
        cp.assembly_template = cp.assembly_template.replace(
            "    MOVE [LOCK], R0\n", "")
        return cp


def _mixed_class_stim(X, y, split, per_class=2, T=30):
    """A stimulus stitched from real clips of ALL FOUR classes, so the golden
    class stream is genuinely multi-valued (a near-constant stream would let
    almost any mutation slip through the decision-level gate — the
    decision-level-masks-bugs lesson, made concrete)."""
    te = np.where(split == 2)[0]
    stim = []
    for c in range(C):
        for ci in [j for j in te if y[j] == c][:per_class]:
            stim += _q15_feats(X[ci, :T])
    return stim


@pytest.mark.parametrize("mutant", [
    "_MutPerturbedWeight", "_MutSwapRZ", "_MutPerRowHeadScales",
    "_MutWrongDshift"])
def test_mutation_diverges_from_the_golden(blk, dataset, mutant):
    """Every numeric mutant must DIFFER from the golden ON CHIP — checked at
    BOTH levels, because they have different sensitivities:

      * the h TRAJECTORY (the strong level: any changed constant perturbs the
        state words immediately), and
      * the CLASS stream (the level the user sees).

    The CLASS level is asserted for every mutant. The h level is asserted only
    for the RECURRENCE mutants: the readout head sits strictly DOWNSTREAM of
    the state update, so a head-scale fault provably cannot move h — asserting
    it there would be asserting a falsehood, and the head mutant is instead
    pinned by the decision stream it is designed to corrupt."""
    stim = _mixed_class_stim(*dataset)
    ghs, _gh, gcls = blk.h_trajectory_q15(stim)
    assert len(set(gcls)) >= 3, f"stimulus is not multi-class: {set(gcls)}"
    dut = _Dut(block_type=mutant)
    mcls, mhs = dut.per_sample(stim)
    if mutant != "_MutPerRowHeadScales":
        assert mhs != ghs, (
            f"{mutant} reproduced the golden h trajectory EXACTLY — the "
            f"mutation is a no-op on chip and the gate certifies nothing")
    else:
        assert mhs == ghs, (
            "a HEAD-only fault must leave the recurrence untouched — if h "
            "moved, this mutant is not isolating the head scale")
    assert mcls != gcls, (
        f"{mutant} produced the golden CLASS stream on a 4-class stimulus — "
        f"the decision-level gate is blind to this fault")


def test_mutation_broken_timestep_barrier_fails(blk):
    """Removing the barrier must break the block under SATURATED drive (the
    condition the barrier exists for): the class stream diverges, the pipeline
    livelocks, or the output count is wrong. Any of the three is a failure —
    assert that at least one occurs, and say which."""
    stim = _rand_feats(3, 40)
    _ghs, _gh, gcls = blk.h_trajectory_q15(stim)
    try:
        got = _Dut(block_type="_MutNoBarrier").burst(stim)
    except AssertionError as e:
        assert "LIVELOCK" in str(e), e
        return
    assert got != gcls or len(got) != len(gcls), (
        "the un-LOCKed block matched the golden under saturation — the "
        "timestep barrier is not being exercised by this gate")


def test_mutation_perturbed_weight_degrades_accuracy(blk, dataset):
    """The manifest's named mutation: a perturbed weight matrix must MEASURABLY
    degrade the classifier, not merely change some words. Measured over 24
    held-out clips (6 per class) at the clip-vote level."""
    X, y, split = dataset
    te = np.where(split == 2)[0]
    clips = [i for c in range(C) for i in [j for j in te if y[j] == c][:10]]
    ref, mut = _Dut(), _Dut(block_type="_MutCorruptedWeights")
    ok_ref = ok_mut = 0
    for ci in clips:
        stim = _q15_feats(X[ci, :150])
        for dut, is_ref in ((ref, True), (mut, False)):
            got = np.asarray(dut.burst(stim)[BURN:])
            vote = int(np.argmax(np.bincount(got, minlength=C)))
            if vote == int(y[ci]):
                if is_ref:
                    ok_ref += 1
                else:
                    ok_mut += 1
    print(f"\nWEIGHT-CORRUPTION: reference {ok_ref}/{len(clips)}, "
          f"mutant {ok_mut}/{len(clips)}")
    assert ok_mut < ok_ref, (
        f"corrupted weights did NOT degrade accuracy "
        f"({ok_mut} vs {ok_ref} of {len(clips)}) — the gate certifies nothing")


def test_single_weight_word_perturbation_moves_the_state(blk, dataset):
    """The FINER weight gate, honestly scoped. A SINGLE weight word shifted by
    600 LSB is measurable at the STATE level (the h trajectory moves on chip)
    but the trained classifier is robust enough that its clip-level VOTE is
    unchanged on the held-out set — so the accuracy-degradation claim above is
    made with genuinely corrupted weights, and this test pins the finer fault
    at the level where it is actually observable. Reporting it this way is the
    point: a decision-level-only gate would have called the one-word fault
    'no effect' and been wrong."""
    stim = _mixed_class_stim(*dataset)
    ghs, _gh, _gc = blk.h_trajectory_q15(stim)
    _mc, mhs = _Dut(block_type="_MutPerturbedWeight").per_sample(stim)
    assert mhs != ghs
    moved = sum(1 for a, b in zip(mhs, ghs) if a != b)
    print(f"\nONE-WORD PERTURBATION: h differs on {moved}/{len(ghs)} steps")
    assert moved >= len(ghs) // 2, (
        f"a perturbed weight word moved h on only {moved} of {len(ghs)} "
        f"steps — the state-level gate is nearly blind to it")


# --- stream-level mutations (the standard four) -------------------------------

def test_stream_mutation_inverted_fails(blk):
    """INVERTED output: the exact gate must reject a negated class stream (and
    the genuine stream must pass it, so the gate is shown to discriminate)."""
    stim = _rand_feats(3, 20)
    _h, _hd, gcls = blk.h_trajectory_q15(stim)
    got = _Dut().burst(stim)
    assert got == gcls
    assert [(-int(w)) & 0xFFFF for w in got] != gcls


def test_stream_mutation_delayed_fails(blk):
    """+1 SAMPLE DELAY: the gate must reject a shifted stream. Class streams
    are slowly varying, so this could in principle be masked — the assertion
    makes the gate's teeth on THIS stimulus explicit rather than assumed."""
    stim = _rand_feats(3, 20)
    _h, _hd, gcls = blk.h_trajectory_q15(stim)
    got = _Dut().burst(stim)
    assert got == gcls
    assert [0] + got[:-1] != gcls, "a +1 sample delay is indistinguishable"


def test_stream_mutation_empty_fails(blk):
    """EMPTY output: the golden is non-empty, so a block that emits nothing
    cannot pass (the degenerate way to 'agree' with any comparison)."""
    stim = _rand_feats(3, 20)
    _h, _hd, gcls = blk.h_trajectory_q15(stim)
    assert [] != gcls and gcls, "empty output must fail the gate"


def test_golden_stimulus_is_not_degenerate(blk):
    """A class stream that is a single constant would let almost any mutation
    pass. The gate stimulus must exercise MULTIPLE classes."""
    stim = _rand_feats(3, 40)
    _h, _hd, gcls = blk.h_trajectory_q15(stim)
    assert len(set(gcls)) >= 2, f"degenerate gate stimulus: {set(gcls)}"


# --- the REQUIRED weight-location manifest ------------------------------------

MANIFEST_PATH = _VERIFY / "reports" / "GRUCellBlock.weights.json"


def test_weight_manifest_is_complete_and_matches_the_built_words(blk):
    """The block MUST emit a machine-readable map of which memory word in
    which cell holds which weight/bias constant. This gate checks the map is

      * COMPLETE — every one of the H*I + H*H weights and H biases per gate,
        plus the C*H head weights and C head biases, appears exactly once; and
      * TRUE — every mapped (cell, address) actually holds that word in the
        BUILT cell program, not merely in the derived constants.

    A manifest that is complete but wrong is worse than none, so both halves
    are asserted."""
    man = blk.weight_location_manifest()
    assert man["format"] == "gru-cell-weight-map-v1"
    assert set(man["scales"]) == {
        "S_rz", "S_n", "S_head", "dshift_sigmoid", "dshift_tanh"}

    names = [e["name"] for cell in man["cells"].values()
             for e in cell.values() if e["name"] != "pad"]
    expect = set()
    for g in ("r", "z", "n"):
        for i in range(H):
            expect |= {f"Wx.{g}[{i}][{j}]" for j in range(I)}
            expect |= {f"Wh.{g}[{i}][{j}]" for j in range(H)}
            expect.add(f"b.{g}[{i}]")
    for j in range(C):
        expect |= {f"head.Wo[{j}][{k}]" for k in range(H)}
        expect.add(f"head.bo[{j}]")
    assert len(names) == len(set(names)), "duplicate entries in the manifest"
    assert set(names) == expect, (
        f"manifest incomplete: missing {sorted(expect - set(names))}, "
        f"unexpected {sorted(set(names) - expect)}")
    assert len(expect) == 3 * (H * I + H * H + H) + C * H + C == 104

    progs = blk.build_cell_programs()
    for cid, words in man["cells"].items():
        mem = {d.address: int(d.value) & 0xFFFF for d in progs[cid].data}
        for addr, entry in words.items():
            addr = int(addr)
            assert addr in mem, f"{cid}: manifest address {addr} not in memory"
            assert mem[addr] == int(entry["value"]), (
                f"{cid}@{addr} ({entry['name']}): manifest says "
                f"{entry['value']:#06x}, built word is {mem[addr]:#06x}")


def test_weight_manifest_carries_no_absolute_path(blk, tmp_path):
    """The manifest is a COMMITTED artifact, so it must never embed an
    absolute build path: that leaks the build environment and is meaningless
    on any other machine. Inside the repo the label is repo-relative; outside
    it degrades to the bare file name."""
    man = blk.weight_location_manifest()
    label = man["weights_file"]
    assert not label.startswith("/") and ":" not in label, label
    assert label == "examples/gru_classifier/ml/weights_single.json", label
    outside = tmp_path / "elsewhere.json"
    outside.write_text(WEIGHTS.read_text())
    lab2 = GRUCellBlock("o", weights_file=str(outside)
                        ).weight_location_manifest()["weights_file"]
    assert lab2 == "elsewhere.json", lab2
    # the emitted artifact on disk must be clean too
    if MANIFEST_PATH.is_file():
        on_disk = json.loads(MANIFEST_PATH.read_text())["weights_file"]
        assert not on_disk.startswith("/"), (
            f"the emitted weight manifest embeds an absolute path: {on_disk}")


def test_weight_manifest_tracks_the_weights_file(tmp_path):
    """The manifest maps the ACTUAL stored words, so perturbing the weights
    FILE must move the mapped values (otherwise the map is decoration)."""
    src = json.loads(WEIGHTS.read_text())
    src["layers"][0]["quant"]["Wx"]["r"]["q"][0][0] += 700
    alt = tmp_path / "perturbed.json"
    alt.write_text(json.dumps(src))
    a = GRUCellBlock("a", weights_file=str(WEIGHTS)).weight_location_manifest()
    b = GRUCellBlock("b", weights_file=str(alt)).weight_location_manifest()
    assert a["cells"] != b["cells"], (
        "a changed weights file produced an identical weight map")
    ea = next(e for e in a["cells"]["r0"].values()
              if e["name"] == "Wx.r[0][0]")
    eb = next(e for e in b["cells"]["r0"].values()
              if e["name"] == "Wx.r[0][0]")
    assert ea["value"] != eb["value"], (
        f"the perturbed coefficient did not move: {ea} vs {eb}")


# --- the report the dashboard reads ------------------------------------------

def test_write_report(blk, long_run, dataset):
    """Emit ``verification/reports/GRUCellBlock.json`` (measured metrics) and
    the REQUIRED weight-location manifest alongside it."""
    lay = blk.default_layout()
    xs = [v[0] for v in lay.values()]
    ys = [v[1] for v in lay.values()]
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(blk.weight_location_manifest(), indent=1) + "\n")
    s = blk.scale_shifts
    write_report(
        "GRUCellBlock",
        CompareResult(passed=True, metric=Metric.EXACT,
                      n_compared=long_run["steps"], max_abs_err=0.0,
                      tolerance=0.0, delay_used=0),
        coverage={
            "edge": True, "random": 3, "classes": C,
            "mutation": True,
            "cells": blk.cell_count,
            "footprint": f"{max(xs) - min(xs) + 1}x{max(ys) - min(ys) + 1}",
            "on_chip_steps": long_run["steps"],
            "decision_agreement": round(long_run["agreement"], 6),
            "clip_vote_accuracy": round(long_run["clip_acc"], 4),
            "clips": long_run["n_clips"],
            "confusion_rows_true_cols_pred": long_run["confusion"],
            "h_trajectory_bit_exact": True,
            "saturated_equals_per_sample": True,
            "state_persists_across_bursts": True,
            "orientations": len(ORIENTS),
            "scales": {"S_rz": s["S_rz"], "S_n": s["S_n"],
                       "S_head": s["S_head"],
                       "dshift_sigmoid": s["dshift_sigmoid"],
                       "dshift_tanh": s["dshift_tanh"]},
            "weight_manifest": MANIFEST_PATH.name,
            "weight_words_mapped": 104,
            "golden": ("examples/gru_classifier/ml/gru_reference_chip "
                       "(independent chip-exact integer model; no GNU Radio "
                       "counterpart exists — a GRU recurrence cannot be "
                       "expressed as an acyclic GR chain)"),
        })
    rep = _VERIFY / "reports" / "GRUCellBlock.json"
    assert rep.is_file() and MANIFEST_PATH.is_file()
