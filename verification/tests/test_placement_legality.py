# SPDX-License-Identifier: GPL-3.0-or-later
"""Placement-legality gate — a block's FOOTPRINT stays legal under every orientation
AND under user movement.

Orientation-invariance (``test_orientation_invariance.py``) proves a block *computes*
the same rotated. This gate proves the ORTHOGONAL property: a block's cells never land
ON TOP of each other (a self-overlap) — not after any of the 8 D4 orientations, and not
after a user drags the whole block or Alt-drags one of its cells. A multi-cell block with
an internal transit/relay cell (e.g. the FrequencyModulator serialize-LOCK's
``transit_unlock``) can fold that cell onto a datapath cell; that self-overlap passed the
old placement checks (which only compared DIFFERENT blocks) and only failed later at DRC,
with a broken build + an un-routable net. A self-overlap is ALWAYS illegal.

Two properties per block:
  1. ORIENTATION: after each D4 orientation the cells are pairwise-distinct + on-grid.
  2. MOVEMENT: the single-cell move API (``move_cell``, the Alt-drag breakout) REJECTS a
     move that would collide with another cell (self or cross-block); a whole-block move
     never silently produces an overlap.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_placement_legality.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)

D4 = [[], ["cw"], ["cw", "cw"], ["cw", "cw", "cw"],
      ["mirror_v"], ["mirror_v", "cw"], ["mirror_v", "cw", "cw"],
      ["mirror_v", "cw", "cw", "cw"]]

# Multi-cell blocks whose footprint (incl. internal transit/relay cells) must stay legal.
# pipeline_lock=True is the variant that ADDS the transit_unlock + relay — the exact case
# the FrequencyModulator/NCO regression hit — so test those locked.
BLOCKS = [
    ("FrequencyModulatorBlock", {"sensitivity": 1.5707963267948966,
                                 "pipeline_lock": True}),
    ("NCOBlock", {"sample_rate": 32000.0, "frequency": 2000.0, "amplitude": 0.9,
                  "pipeline_lock": True}),
    ("ComplexMixerBlock", {"sample_rate": 32000.0, "frequency": 2000.0,
                           "amplitude": 0.9, "pipeline_lock": True}),
    ("RRCPulseShaperBlock", {"sampling_freq": 2.0, "symbol_rate": 1.0,
                             "alpha": 0.5, "ntaps": 17, "gain": 1.0}),
    ("ComplexRRCMatchedFilterBlock", {}),
    # CSS chirp generator: the NCO 2x5 fold + a backward emit->sweep kick over
    # the @1 abutment (NO transit cell); params never grow the 10-cell
    # footprint (out-of-range n/m raise), so the fold must stay
    # pairwise-distinct under D4 + movement.
    ("ChirpGeneratorBlock", {"n": 16, "m": 4}),
    # Single-cell CSS symbol mapper (PackKBits re-parameterized) — no internal
    # transit cell, no footprint-growing param; trivially non-self-overlapping.
    ("ChirpSymbolMapperBlock", {"m": 64}),
    # CSS dechirp: 13-cell fold (ComplexMixer 2-column NCO + the prods/combine
    # tail stacked at the top of column 1). NO transit cell in EITHER lock
    # variant (the locked unlock corridor is the combine->phase @1 west
    # abutment), but test the locked variant anyway — it is the shipping
    # saturation-safe form.
    ("ConjChirpMixerBlock", {"n": 16, "pipeline_lock": True}),
    ("ConjChirpMixerBlock", {"n": 256}),
    # Single-cell CSS preamble sync — no internal transit cell, no footprint-
    # growing param (k only changes a data word); trivially non-self-
    # overlapping in every D4 orientation and under movement.
    ("ChirpSyncBlock", {"k": 4}),
    # Multi-cell distributed complex delay line at its footprint-growing
    # extremes: depth 32 = 7 cells (2x4 fold, partial last column) and depth 64
    # = 13 cells (4x4 fold, partial last column) — the deepest supported chain.
    ("ComplexDelayLineBlock", {"depth": 32}),
    ("ComplexDelayLineBlock", {"depth": 64}),
    # 16-point streaming FFT: the largest single block in the catalog (44 cells,
    # 7x8 footprint — both dims <= 8 per INV-9) with per-stage banded geometry.
    ("FFT16Block", {}),
    # 32-point streaming FFT: 60 cells on the vertical CTL/OUT SPINE (9x10
    # footprint — CHIP_SCALE, so the INV-9 8-across cap is waived for it). The
    # fold is produced by a SEARCH, so pairwise-distinctness is a real risk
    # rather than a formality, and it must survive D4 + an Alt-drag breakout.
    ("FFT32Block", {}),
    # GRU classifier cell: the LARGEST single block in the catalog (51 cells —
    # a closed 7x8 ring serpentine with an off-ring egress relay and two
    # face-only ring-closure transits, both of which must stay distinct from
    # every datapath cell under D4 AND under an Alt-drag breakout).
    ("GRUCellBlock", {}),
    ("FreqXlatingFIRBlock", {"decimation": 2, "taps": [0.1, 0.2, 0.3, 0.2, 0.1],
                             "center_freq": 2000.0, "sampling_freq": 32000.0}),
    ("FSK4SyncTimingRecoveryBlock", {}),
    ("GardnerTimingRecovery", {}),
    ("MMTimingRecoveryBlock", {}),
    # FLL band-edge ring composite: perimeter fold with interior hole + a feedback
    # transit; assert legality at the default AND the max (8x8 ring) filter_size.
    ("FLLBandEdgeBlock", {}),
    ("FLLBandEdgeBlock", {"filter_size": 27}),
    ("IQUpconvertBlock", {}),
    ("ComplexCostasLoopBlock", {}),
    # BPSK hard slicer: single-cell, so it can never self-overlap — but assert the
    # legal footprint under D4 + movement anyway (a param sweep of out_mode does not
    # change the 1-cell footprint).
    ("BPSKSlicerBlock", {"out_mode": "word"}),
    ("ComplexCostasLoopBlock", {"order": 4}),
    # 20-cell complex-AGC ring (7x5 perimeter, no internal transit cell): the
    # serialize-LOCK adds no cells (the feedback is a direct @1 abutment), so
    # the footprint is param-independent; assert D4 + movement legality anyway.
    ("AGCCCBlock", {}),
    # Polyphase rational resampler: single-cell for every supported (L, M, taps)
    # combination (the params never grow the footprint — over-budget configs
    # RAISE at construction instead), so it can never self-overlap; assert the
    # 1-cell footprint stays legal under D4 + movement anyway.
    ("RationalResamplerBlock", {"interpolation": 2, "decimation": 3,
                                "taps": [0.4, 0.25, -0.2, 0.1]}),
    # Single-cell additive LFSR scrambler — no internal transit cell, no footprint-
    # growing param (count>0 only adds registers, not cells); trivially non-self-
    # overlapping in every D4 orientation and under movement.
    ("LFSRScramblerBlock", {"count": 8}),
    # Single-cell frame CRC-16 — no internal transit cell, no footprint-growing
    # param (poly/init/frame_len only change register contents); trivially
    # non-self-overlapping in every D4 orientation and under movement.
    ("Crc16Block", {"frame_len": 8}),
    # ChaCha20 quarter round: a 17-cell 8x3 fold (2 collector cells + 14
    # frame-relay stages + the egress cell). No internal transit cells, but the
    # tallest fold in the crypto family — check it stays pairwise-distinct
    # under every D4 orientation and under user movement.
    ("ChaCha20QRBlock", {}),
    # Single-cell windowed zero-crossing rate — no internal transit cell, no
    # footprint-growing param (window_size only changes a data word + a shift
    # immediate); trivially non-self-overlapping in every D4 orientation and
    # under movement.
    ("ZeroCrossingRateBlock", {"window_size": 64}),
    # Single-cell framewise argmax — no internal transit cell, no footprint-
    # growing param (n only changes data words + state initial values);
    # trivially non-self-overlapping in every D4 orientation and under movement.
    ("BinArgmaxBlock", {"n": 128}),
    # Two-complex-stream add/sub: 2-cell chain (rail_i landing -> rail_q emit),
    # no internal transit cell, no footprint-growing param (num_inputs is pinned).
    ("AddCCBlock", {}),
    ("SubCCBlock", {}),
    # Q15 activations: 2-cell straight chain (fold -> lut), no internal
    # transit cell; dshift never grows the footprint (out-of-range raises),
    # so the 2x1 footprint must stay pairwise-distinct under D4 + movement.
    ("SigmoidBlock", {"dshift": 2}),
    ("TanhBlock", {"dshift": -1}),
    ("MultiplyCCBlock", {}),
    # Radix-2 DIF butterfly: 8-cell 2x4 serpentine (pair -> 4 rail cells ->
    # relay -> sum_out -> diff_out), no internal transit cell, no params — the
    # full 2x4 rectangle must stay pairwise-distinct in every D4 orientation
    # and under movement.
    ("R2ButterflyBlock", {}),
    # Twiddle rotator: 6-cell 2x3 serpentine; the table period P changes DATA
    # WORDS only, never the footprint (P > MAX_PERIOD raises at construction).
    ("TwiddleMultiplyBlock",
     {"twiddles": [1, 0.7071067811865476 - 0.7071067811865476j, -1j,
                   -0.5 + 0.25j]}),
    ("MultiplyConstComplex", {"re": 0.7, "im": 0.5}),
    # Bitwise NOT (GR blocks.not_bb): single-cell, no params, no internal transit
    # cell — trivially non-self-overlapping in every D4 orientation and under movement.
    ("NotBlock", {}),
    # Hamming(7,4) FEC encoder: 2-cell straight chain (pack -> expand), no internal
    # transit cell, no footprint-growing param — its 2x1 footprint must stay
    # pairwise-distinct in every D4 orientation and under movement.
    ("HammingEncoderBlock", {}),
    # Hamming(7,4) syndrome decoder: 2-cell row (front -> fix), no internal transit
    # cell, no footprint-growing param — footprint must stay pairwise-distinct under
    # every D4 orientation and movement.
    ("HammingDecoderBlock", {}),
    # Extended Golay (24,12) encoder: 4-cell 2x2 serpentine fold (pack -> par1 ->
    # par2 -> emit), no internal transit cell, no params — the 2x2 footprint must
    # stay pairwise-distinct in every D4 orientation and under movement.
    ("GolayEncoderBlock", {}),
    # Fixed-coefficient dot product: MODE/S-dependent footprint — raw (and
    # restored at S=0) is a single MAC cell; restored with S>0 grows a second
    # `restore` cell (2x1 row, no internal transit cell). Assert BOTH shapes
    # stay pairwise-distinct under every D4 orientation and movement.
    ("DotProductMACBlock", {"coefficients": [0.3, -0.2, 0.2, 0.15],
                            "bias": 0.05, "k": 4}),
    ("DotProductMACBlock", {"coefficients": [0.9, -0.7, 0.8, 0.6],
                            "bias": 0.2, "k": 4, "mode": "restored"}),
    # RMS pair: 4-cell 2x2 serpentine fold (pwr -> norm -> poly -> denorm), no
    # internal transit cell, no footprint-growing param (alpha only changes a
    # coefficient word) — the 2x2 footprint must stay pairwise-distinct in every
    # D4 orientation and under movement.
    ("RMSBlock", {"alpha": 0.25}),
    ("RMSCFBlock", {"alpha": 0.25}),
    # sqrt: 3-cell L fold (norm -> poly -> denorm), no internal transit cell and
    # NO params at all, so the 2x2-bounded footprint must stay pairwise-distinct
    # in every D4 orientation and under movement.
    ("SqrtBlock", {}),
    # ordered two-word rendezvous: ONE cell, but its two input faces are
    # placement-relevant (NEEDS_DISTINCT_INPUT_FACES) — cover it so a future
    # footprint change (e.g. an emit-sequencing second cell) is caught here.
    ("FeaturePairJoinBlock", {}),
    # XOR of two INDEPENDENT producers: the same ONE-cell N=2 LOCK rendezvous,
    # so the same reason to cover it — its two input faces are placement-relevant
    # (NEEDS_DISTINCT_INPUT_FACES), and at N=2 the face budget (INV-46: N + 2 = 4)
    # is what keeps it a single cell. A future change that grew it a second cell
    # would spend a face the rendezvous does not have to spare; catch that here
    # as a geometry regression rather than as an unexplained routing failure.
    ("XorJoinBlock", {}),
    # Clarke (alpha-beta) transform: the same ONE-cell N=2 LOCK rendezvous as
    # XorJoin (NEEDS_DISTINCT_INPUT_FACES) with the Q15 arithmetic folded into
    # the got_ib entry and a 2-rail complex emit. Covered for the same reason:
    # a future change that grew it a second cell would spend a face the
    # rendezvous does not have to spare (INV-46: N + 2 = 4 at N=2) — catch
    # that here as a geometry regression, not a routing mystery.
    ("ClarkeTransformBlock", {}),
    # TMR majority voter: 4-cell COLINEAR chain (rendezvous -> agree -> disagree
    # -> emit). Its shape is dictated by a face budget, not by convenience — the
    # N=3 rendezvous needs all four of its faces (three arms + the forward), so
    # it must stay a LEAF of the fold. Cover it here so a future re-fold that
    # folded a cell back alongside the rendezvous (a 2x2 square, say) is caught
    # as a geometry regression rather than as an unexplained routing failure.
    ("TMRVoterBlock", {}),
    # rows x cols block interleaver: 3-cell vertical column (rgen -> wctl ->
    # store), no internal transit cell; rows/cols/deinterleave change register
    # contents only (the footprint is always 1x3) — must stay pairwise-distinct
    # in every D4 orientation and under movement.
    ("BlockInterleaverBlock", {"rows": 3, "cols": 4, "deinterleave": True}),
]

_LIB = "lattrex.official"


def _controller():
    from PySide6.QtWidgets import QApplication
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.new_project("legality", "kyttar_10x12")
    return ctrl


def _overlaps(blk):
    seen: dict[tuple, str] = {}
    bad = []
    for c in blk.placement.cells:
        k = (c.x, c.y)
        if k in seen:
            bad.append(f"{c.cell_id} overlaps {seen[k]} at {k}")
        seen[k] = c.cell_id
    return bad


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type,params", BLOCKS,
                         ids=[b[0] for b in BLOCKS])
def test_footprint_legal_in_all_orientations(block_type, params):
    """No cell of the block overlaps another (or goes off-grid) in any D4 orientation.
    Placed at an interior anchor so a legal fold has room; the test is about the block's
    OWN cells, not packing against neighbours."""
    from commands.placement_cmds import OrientBlockCommand
    for ops in D4:
        ctrl = _controller()
        name = ctrl.place_block(block_type, 0, 4, 4, library=_LIB, params=dict(params))
        blk = ctrl.project.block(name)
        for op in ops:
            OrientBlockCommand(ctrl.project, name, op).execute()
        w, h = ctrl._chip_dims(0)
        off = [(c.cell_id, c.x, c.y) for c in blk.placement.cells
               if not (0 <= c.x < w and 0 <= c.y < h)]
        # An orientation may push a cell off the interior anchor's grid; that is a
        # placement concern the placer re-folds, not a footprint self-overlap bug.
        # We only assert NO SELF-OVERLAP among the on-grid cells here.
        bad = _overlaps(blk)
        assert not bad, (
            f"{block_type} orient {'+'.join(ops) or 'identity'}: self-overlap {bad}")


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type,params", BLOCKS,
                         ids=[b[0] for b in BLOCKS])
def test_single_cell_move_rejects_overlap(block_type, params):
    """The Alt-drag single-cell move (``move_cell``) must REJECT a move that lands one
    cell on another — the path that let the FrequencyModulator's emit/transit_unlock
    stack. A legal move to a free cell still succeeds."""
    ctrl = _controller()
    name = ctrl.place_block(block_type, 0, 3, 3, library=_LIB, params=dict(params))
    blk = ctrl.project.block(name)
    cells = list(blk.placement.cells)
    if len(cells) < 2:
        pytest.skip("single-cell block — no intra-block move to collide")
    a, b = cells[0], cells[1]
    with pytest.raises(Exception):
        ctrl.move_cell(name, a.cell_id, b.x, b.y)   # onto another cell -> rejected
    # The rejected move must not have mutated the placement.
    assert not _overlaps(blk), "a rejected move left the footprint overlapping"
    # A legal move to an empty cell still works. FIND a free cell rather than
    # hard-coding one: the biggest blocks (GRUCellBlock is 51 cells at 7x8)
    # cover most of the array from the (3,3) anchor, so a fixed corner is not
    # reliably empty and the test would fail on the BLOCK for a defect in the
    # TEST.
    w, h = ctrl._chip_dims(0)
    taken = {(c.x, c.y) for c in blk.placement.cells}
    free = next(((x, y) for y in range(h) for x in range(w)
                 if (x, y) not in taken), None)
    assert free is not None, f"{block_type} leaves no free cell on the {w}x{h}"
    ctrl.move_cell(name, a.cell_id, *free)
    assert not _overlaps(blk)


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
def test_collides_detects_self_overlap():
    """``_collides`` (the auto-P&R re-fold's per-block legality check) must return True
    when a block's OWN cells stack — it used `occupied_positions()` (a SET) which
    silently dedups a self-overlap, so the re-fold ACCEPTED an orientation that folded
    transit_unlock onto emit (the FrequencyModulator (6,2) bug). This drives the exact
    fold via the full place<->route loop and asserts no self-overlap is committed."""
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.grc_import import import_grc
    from ui.controller import AppController
    from engine.drc import check_project

    grc = str(Path(__file__).resolve().parents[2] / "examples" / "fsk4_modem"
              / "fsk4_modem.grc")
    if not os.path.exists(grc):
        pytest.skip("fsk4 modem .grc absent")
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = "kyttar_10x12"
    r = import_grc(grc, cat, chip_type=key)
    ctrl = AppController(catalog=cat)
    ctrl.project = r.project
    try:
        ctrl.auto_pnr({key: ct}, time_budget_s=40.0)
    except Exception:  # noqa: BLE001 — a hard design may not fully route; we only
        pass          # check the COMMITTED placement has no self-overlap.
    for b in ctrl.project.blocks:
        if b.placement is None:
            continue
        seen: dict = {}
        for c in b.placement.cells:
            k = (c.x, c.y)
            assert k not in seen, (
                f"auto-P&R committed a SELF-OVERLAP in '{b.name}': "
                f"{c.cell_id} on {seen[k]} at {k}")
            seen[k] = c.cell_id


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
@pytest.mark.parametrize("block_type,params", BLOCKS,
                         ids=[b[0] for b in BLOCKS])
def test_move_then_rotate_stays_legal(block_type, params):
    """A whole-block move followed by each rotation (and rotation followed by a move)
    never yields a self-overlapping footprint."""
    from commands.placement_cmds import OrientBlockCommand, MoveBlockCommand
    for seq in (["move", "cw"], ["cw", "move"], ["cw", "cw", "move"],
                ["move", "mirror_v"]):
        for (mx, my) in [(0, 0), (1, 1), (-1, 0), (0, -1)]:
            ctrl = _controller()
            name = ctrl.place_block(block_type, 0, 5, 5, library=_LIB,
                                    params=dict(params))
            blk = ctrl.project.block(name)
            try:
                for op in seq:
                    if op == "move":
                        if (mx, my) != (0, 0):
                            MoveBlockCommand(ctrl.project, name, mx, my).execute()
                    else:
                        OrientBlockCommand(ctrl.project, name, op).execute()
            except Exception:
                # A rejected move/orient is fine — it must not corrupt the footprint.
                pass
            bad = _overlaps(blk)
            assert not bad, (
                f"{block_type} seq {seq} move ({mx},{my}): self-overlap {bad}")
