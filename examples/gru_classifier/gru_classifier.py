# SPDX-License-Identifier: GPL-3.0-or-later
"""The GRU modulation-classifier chain: topology, feature references, goldens.

STATUS: the feature front end and the trained model are verified (see the gate,
``verification/tests/test_gru_classifier_example.py``). The assembled chain does
NOT yet place and route as one chip — it is always exactly one net short — so
this example has never been run end to end on a real array and ships no ``.kyt``
or ``.grc``. :func:`route_report` measures that wall live; the README's "Status"
section and the ``gru_classifier example`` lessons_log entry give the detail.

The design is ONE classifier on ONE array — a complex baseband stream in, one
class word (0..3 = SSB / BPSK / 4-FSK / noise) per 32-sample window out::

    re,im -> ComplexToMagSquared -> MovingAverage(32) -> Sqrt -> KeepOneInN(32) -.
                                                                                 |
                                                            FeaturePairJoin -> GRUCell -> class
                                                                                 |
    re ----> ZeroCrossingRate(32) -----------------------------------------------'

Why the RMS arm looks like that: the model's feature is
``rms = sqrt(mean |x|^2)`` over a NON-OVERLAPPING 32-sample window
(``ml/features.py``). ``MovingAverage(32, scale=1/32)`` is a BOXCAR mean of the
last 32 power samples — exactly the window mean — and ``KeepOneInN(32)`` keeps
phase 31, i.e. the sample where the boxcar has consumed precisely one whole
window. (The library's RMS block is an exponential IIR, a different filter; it
is NOT the model's feature and is deliberately not used here.)

Why the arms line up: ``KeepOneInN(n)`` keeps phase n-1, and
``ZeroCrossingRate(32)`` also emits on input indices 31, 63, ... — so the two
arms are index-aligned by construction, at one word each per 32 input samples.
``FeaturePairJoinBlock`` then turns each (rms, zcr) arrival pair into the two
SEQUENTIAL words the GRU cell's single input port expects, in a fixed order.

WORD ORDER IS PART OF THE CONTRACT. ``config.json`` pins
``features = ["rms", "zcr"]``, so word 0 of every timestep is RMS and word 1 is
ZCR — the join's ``a`` arm carries RMS and its ``b`` arm carries ZCR. Swapping
them feeds the trained weights a transposed feature vector; the gate proves that
mutation fails.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gru_stimulus import (CLASSES, SEGMENT_STEPS, WINDOW_N,  # noqa: E402
                          make_stimulus, peak_magnitude, to_q15)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips"
                / "kyttar_10x12.yaml")
LIB = "lattrex.official"
WEIGHTS = HERE / "ml" / "weights_single.json"
# NOTE: there is deliberately no .kyt / .grc here. The chain does not yet place
# and route as one chip (see README "Status" and the lessons_log entry), so
# shipping either would advertise a demo that cannot run.

#: the chain's blocks, in dataflow order, with the params that define it
BLOCK_SPECS = [
    ("power", "ComplexToMagSquaredBlock", {}),
    ("boxcar", "MovingAverageBlock", {"length": WINDOW_N,
                                      "scale": 1.0 / WINDOW_N}),
    ("root", "SqrtBlock", {}),
    ("decim", "KeepOneInNBlock", {"n": WINDOW_N}),
    ("zcr", "ZeroCrossingRateBlock", {"window_size": WINDOW_N}),
    ("join", "FeaturePairJoinBlock", {}),
    # weights_file="" selects GRUCellBlock's BUNDLED default weights. That file
    # is byte-identical to this example's ml/weights_single.json (the gate
    # asserts the digests match), and an empty param is the only spelling that
    # is portable: the block resolves a relative weights_file against the
    # CURRENT WORKING DIRECTORY or its own package directory, so any path
    # spelled here would break for a user running from elsewhere.
    ("gru", "GRUCellBlock", {"weights_file": ""}),
]


# --------------------------------------------------------------------------- #
#  Feature front end — the BLOCKS' OWN bit-exact references                    #
# --------------------------------------------------------------------------- #
def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def rms_feature_words(iq) -> list[int]:
    """The RMS arm's emitted Q15 words, bit-exact (block references, in the
    order the placed cells compute them)."""
    from gr_kyttar.placement.blocks import (ComplexToMagSquaredBlock,
                                            KeepOneInNBlock,
                                            MovingAverageBlock, SqrtBlock)
    re = to_q15(np.real(iq)).tolist()
    im = to_q15(np.imag(iq)).tolist()
    power = ComplexToMagSquaredBlock("power").process_reference_q15(re, im)
    mean = MovingAverageBlock("boxcar", length=WINDOW_N,
                              scale=1.0 / WINDOW_N).process_reference_q15(power)
    root = SqrtBlock("root").process_reference_q15(mean)
    kept = KeepOneInNBlock("decim", n=WINDOW_N).process_reference_q15(root)
    return [_s16(w) for w in kept]


def zcr_feature_words(iq) -> list[int]:
    """The ZCR arm's emitted Q15 words, bit-exact."""
    from gr_kyttar.placement.blocks import ZeroCrossingRateBlock
    re = to_q15(np.real(iq)).tolist()
    return [_s16(w) for w in
            ZeroCrossingRateBlock("zcr",
                                  window_size=WINDOW_N).process_reference_q15(re)]


def feature_words(iq) -> list[tuple[int, int]]:
    """The (rms, zcr) Q15 pairs, one per window — the GRU's input timesteps."""
    a, b = rms_feature_words(iq), zcr_feature_words(iq)
    return list(zip(a[:min(len(a), len(b))], b[:min(len(a), len(b))]))


# --------------------------------------------------------------------------- #
#  Golden — the offline chip-exact model on the SAME feature words             #
# --------------------------------------------------------------------------- #
def golden_classes(iq) -> list[int]:
    """The expected class word per window: the shipped feature front end into
    ``ml/gru_reference_chip.GRUChipModel`` (the bit-exact integer golden for
    ``GRUCellBlock``). ``h`` starts at 0 and persists across the whole clip —
    the model's streaming state contract."""
    sys.path.insert(0, str(HERE / "ml"))
    from gru_reference_chip import GRUChipModel
    model = GRUChipModel.load(WEIGHTS)
    pairs = feature_words(iq)
    cls, _h = model.forward(np.asarray(pairs, dtype=np.int64))
    return [int(c) for c in cls]


def segment_votes(classes, burn: int = 30) -> list[int]:
    """Per-segment majority vote over the class stream, discarding the first
    ``burn`` steps of each segment (the GRU's state has to re-settle after a
    class change — the training protocol votes the same way)."""
    votes = []
    for ci in range(len(CLASSES)):
        seg = classes[ci * SEGMENT_STEPS + burn:(ci + 1) * SEGMENT_STEPS]
        votes.append(int(np.argmax(np.bincount(seg, minlength=len(CLASSES))))
                     if seg else -1)
    return votes


# --------------------------------------------------------------------------- #
#  The chip: the topology, and the placement wall                              #
# --------------------------------------------------------------------------- #
#
# INGRESS. The chip has ONE 16-bit input port, and the chain needs Re at TWO
# blocks (the power cell and the ZCR cell) plus Im at the power cell. A COMPLEX
# ingress stream cannot fan out on-chip (the fan-out relay is single-rail — the
# Q rail has nowhere to land), so the design takes THREE single-rail nets off
# the one port: Re -> power.re, Im -> power.im, and Re again -> zcr.sample.
# As far as the router is concerned that is an ordinary INV-24 port fan-out —
# one shared corridor forking at a broker beyond the port cell.
#
# THE WALL. GRUCellBlock is 51 cells — 43% of the array — and costs several more
# for its own two port corridors. The join->GRU tail routes at 81/120; the RMS
# arm adds 11 block cells AND four more nets, and each net measured ~8.5 cells of
# corridor, which is what the remaining budget cannot buy.
#
# Two levers have now been measured, and NEITHER closes it:
#
#  * THE FOLD. GRUCellBlock was RE-FOLDED (8x7, transposed) so its input (0,0)
#    and egress (2,0) span the NORTH edge facing the chip's two row-0 ports,
#    cutting the block's own port-corridor cost from 11 cells to 7 and leaving
#    five free ROWS instead of three free columns. On the identical lane search
#    that took the old 7x8 fold to two nets short, the re-fold reaches ONE — a
#    real, measured improvement that still does not close the last net. 4180
#    further layouts (exhaustive lanes + guided perturbation from every 1- and
#    2-short seed) stayed at one.
#  * THE ARM. Shrinking the RMS arm was measured across boxcar lengths 32/16/8/4
#    with and without Sqrt: 65 block cells -> 4 nets short, 62 -> 2, 57 -> 1,
#    56 -> 1. Even a 4-tap boxcar with Sqrt dropped — which is no longer the
#    model's feature — is one net short.
#
# The hop ceiling was ruled out earlier (INV-36; no `hop_overflow` in 5039
# layouts). See :func:`route_report` and the example gates.

def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController
    return (BlockCatalog, load_chip_type, AppController, ChipPortEndpoint,
            BlockEndpoint)


#: the ingress net whose landing carries each arm's host-injected words.
#: ``power`` takes the (re, im) pair; ``zcr`` takes re again on its own net.
INGRESS_NETS = {"power": "in_re", "zcr": "in_zcr"}


def _connect(ctrl, CPE, BE, ids):
    """The chain's logical nets (see the ASCII topology in the module doc)."""
    def add(src, dst, name):
        ctrl.add_logical_connection(src, dst, name=name)

    # ingress: three nets off the one input port (INV-24 shared-port fan-out)
    add(CPE(chip=0, port="x16_in"), BE(block=ids["power"], port="re"),
        INGRESS_NETS["power"])
    add(CPE(chip=0, port="x16_in"), BE(block=ids["power"], port="im"), "in_im")
    add(CPE(chip=0, port="x16_in"), BE(block=ids["zcr"], port="sample"),
        INGRESS_NETS["zcr"])
    # the RMS arm
    add(BE(block=ids["power"], port="out"),
        BE(block=ids["boxcar"], port="sample"), "pow_mean")
    add(BE(block=ids["boxcar"], port="out"),
        BE(block=ids["root"], port="sample"), "mean_root")
    add(BE(block=ids["root"], port="out"), BE(block=ids["decim"], port="x"),
        "root_decim")
    # the rendezvous: word 0 = RMS (arm 'a'), word 1 = ZCR (arm 'b')
    add(BE(block=ids["decim"], port="out"), BE(block=ids["join"], port="a"),
        "rms_join")
    add(BE(block=ids["zcr"], port="out"), BE(block=ids["join"], port="b"),
        "zcr_join")
    add(BE(block=ids["join"], port="out"), BE(block=ids["gru"], port="f"),
        "join_gru")
    add(BE(block=ids["gru"], port="out"), CPE(chip=0, port="x16_out"),
        "gru_out")



#: the best-known whole-chain layout. It does NOT route (exactly one net short —
#: ``join_gru``); it is kept so the wall is reproducible and so the day it lifts
#: is visible. Found by the lane search over the RE-FOLDED (8x7) GRU: the six
#: small blocks laid along row 1 with the GRU below them on rows 5..11.
BEST_KNOWN_ANCHORS = {
    "gru": (0, 5), "power": (2, 1), "boxcar": (3, 1), "root": (5, 1),
    "decim": (7, 1), "zcr": (8, 1), "join": (9, 1),
}

#: the join->GRU tail alone DOES route, measured at 81/120 cells — so the wall
#: is the RMS arm's corridors, not the rendezvous or the recurrent block.
TAIL_ANCHORS = {"gru": (0, 3), "decim": (7, 0), "zcr": (3, 1), "join": (2, 1)}


def _hand_place(ctrl, anchors, specs):
    """Pin each block at its anchor (deterministic — no CP-SAT lottery)."""
    from model.placement import Placement
    ids = {}
    for nm, cls, params in specs:
        ids[nm] = ctrl.place_block(cls, 0, *anchors[nm], library=LIB,
                                   params=dict(params))
    by_name = {b.name: b for b in ctrl.project.blocks}
    for nm, _cls, _params in specs:
        b = by_name[ids[nm]]
        cells, _ = ctrl.default_cells(b.type, b.library, 0, *anchors[nm],
                                      b.params)
        b.placement = Placement(0, cells)
    return ids


def route_chain(anchors=None):
    """Hand-place the WHOLE chain and auto-route it.

    Returns ``(failed_net_names, cells_used)``. ``failed_net_names == []`` means
    the chain routed — which, as of this writing, has never happened: see
    :data:`BEST_KNOWN_ANCHORS` and the lessons_log entry.
    """
    from engine.build import BuildEngine

    (BlockCatalog, load_chip_type, AppController, CPE, BE) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("gru_classifier", ctk)
    ids = _hand_place(ctrl, anchors or BEST_KNOWN_ANCHORS, BLOCK_SPECS)
    _connect(ctrl, CPE, BE, ids)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    bad = [r.name for r in rep.results if not r.ok]
    if bad:
        return bad, None
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        return ["build:" + str(bres.errors[0])], None
    return [], sum(c.cell_count for c in bres.chips.values())


def route_tail(anchors=None):
    """Route the join->GRU tail alone (both ingress nets + the egress).

    Returns ``(failed_net_names, cells_used)``."""
    from engine.build import BuildEngine

    (BlockCatalog, load_chip_type, AppController, CPE, BE) = _engine()
    a = anchors or TAIL_ANCHORS
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("gru_tail", ctk)
    specs = [s for s in BLOCK_SPECS if s[0] in a]
    ids = _hand_place(ctrl, a, specs)
    for src, dst, name in [
            (CPE(chip=0, port="x16_in"), BE(block=ids["decim"], port="x"),
             "in_rms"),
            (CPE(chip=0, port="x16_in"), BE(block=ids["zcr"], port="sample"),
             "in_zcr"),
            (BE(block=ids["decim"], port="out"),
             BE(block=ids["join"], port="a"), "rms_join"),
            (BE(block=ids["zcr"], port="out"),
             BE(block=ids["join"], port="b"), "zcr_join"),
            (BE(block=ids["join"], port="out"),
             BE(block=ids["gru"], port="f"), "join_gru"),
            (BE(block=ids["gru"], port="out"),
             CPE(chip=0, port="x16_out"), "gru_out")]:
        ctrl.add_logical_connection(src, dst, name=name)
    rep = ctrl.auto_route_all({ctk: ct}, auto_orient=False)
    bad = [r.name for r in rep.results if not r.ok]
    if bad:
        return bad, None
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        return ["build:" + str(bres.errors[0])], None
    return [], sum(c.cell_count for c in bres.chips.values())


def route_report():
    """``[(label, failed_nets, cells_used), ...]`` — the measured placement
    status of the tail and of the whole chain."""
    tail_bad, tail_cells = route_tail()
    full_bad, full_cells = route_chain()
    return [("join->GRU tail", tail_bad, tail_cells),
            ("full chain", full_bad, full_cells)]


def main() -> int:
    for label, bad, used in route_report():
        print(f"{label}: " + (f"FAILS on {bad}" if bad else f"routes, {used}/120"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
