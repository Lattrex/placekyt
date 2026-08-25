# SPDX-License-Identifier: GPL-3.0-or-later
"""The GRU modulation-classifier chain: topology, feature references, goldens.

STATUS: SHIPPED. The whole chain places, routes and builds as ONE chip at 102
of 120 cells, and classifies the shipped stimulus end to end on the real placed
and routed array. The gate is
``verification/tests/test_gru_classifier_example.py``; :func:`route_report`
measures the placement live.

What unblocked it was a re-fold of ``GRUCellBlock``, not a change to this
chain: the block now declares ``CHIP_SCALE`` and folds WIDE-FLAT (10 wide x 6
tall, input and egress both on its north edge), which leaves six full-width
free rows as ONE contiguous through-channel. Under the previous <= 8-across
convention the block's free space was fragmented perimeter and the chain was
always exactly one net short. See ``ROUTING HISTORY`` below.

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
#: the shipped, placed-and-routed design and its GRC flowgraph.
KYT = HERE / "gru_classifier.kyt"
GRC = HERE / "gru_classifier.grc"
#: the golden class stream the shipped stimulus produces ON CHIP.
GOLDEN = HERE / "gru_classifier_golden.json"

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
# ROUTING HISTORY — why this took five dispatches, and what actually fixed it.
#
# GRUCellBlock is 51 cells, 43% of the array. For four dispatches the assembled
# chain was ALWAYS exactly one net short, and WHICH net failed rotated as blocks
# moved — the signature of a saturated array rather than one bad anchor. Three
# explanations were measured and ruled out:
#
#  * NOT CAPACITY. The blocks total 65 of 120 cells.
#  * NOT THE HOP CEILING. INV-36 lifted the 31-hop limit; no `hop_overflow` in
#    5039 measured layouts.
#  * NOT THE ARM. Shrinking the RMS arm was swept across boxcar lengths
#    32/16/8/4 with and without Sqrt: 65 block cells -> 4 nets short, 62 -> 2,
#    57 -> 1, 56 -> 1. Even a 4-tap boxcar with Sqrt dropped — no longer the
#    model's feature — was one net short.
#
# The fourth explanation was the fold, and it was RIGHT but under-scoped. The
# GRU was re-folded 8x7 (input and egress on the north edge facing the chip's
# two row-0 ports, port cost 11 -> 7), which took the search from two nets short
# to one; 4180 further layouts stayed at one. The conclusion drawn then was that
# no fold could close it, resting on a sound structural argument: a CLOSED RING
# can never contain a free through-channel (a cycle cannot jump a gap), so all
# of its free space is perimeter, and free-space quality measured IDENTICAL
# across every legal fold.
#
# That argument is correct. Its CONCLUSION was scoped to the three bounding
# boxes INV-9's <= 8-across convention allowed for a 51-cell block. Waiving that
# convention for this block — the CHIP_SCALE placement class, declared per class
# and never a global loosening — admits a 10x5 box, and the perimeter free space
# of a 10-wide block IS six contiguous full-width rows. That is the
# through-channel the ten nets could never find.
#
# The trade CHIP_SCALE demands is explicit and the block honours it: nothing can
# reach the far side of a 10-wide block, so its input and output must share ONE
# edge. GRUCellBlock's `fin` and `oout` are three cells apart on its north edge.
#
# RESULT: the whole chain routes and builds at 102/120 (65 block + 37 corridor)
# with the GRU on rows 6..11 and the front end in the free rows above it. The
# join->GRU tail alone still routes at 81/120, as before.

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


#: The stream identity the shipped ``.grc`` names on BOTH its ``kyttar_source``
#: and its ``kyttar_sink`` (``stream_id: "cls"``). The hosted server resolves a
#: net's injection landing ONLY for input nets that carry a stream_id
#: (``engine.port_config.stream_targets``) and demuxes the recovered words by
#: the egress net's ``out_tag``. Without them the server falls back to the
#: single-net ``input_port_config`` path, which resolves ONE arm and starves the
#: other two — the design still simulates headlessly (``run_on_chip`` reads the
#: landings itself) but returns NOTHING through the GUI/GRC user path.
STREAM_ID = "cls"

#: The egress tag for :data:`STREAM_ID`, from the importer's stable
#: ``_stream_tag`` mapping — the same tag a .grc import of this flowgraph would
#: assign, so the hand-placed .kyt and an imported one agree on the wire.
def _stream_out_tag() -> int:
    from engine.grc_import import _stream_tag
    return _stream_tag(STREAM_ID)


def _connect(ctrl, CPE, BE, ids):
    """The chain's logical nets (see the ASCII topology in the module doc)."""
    def add(src, dst, name, stream_id=None, src_complex=None, out_tag=None):
        ctrl.add_logical_connection(src, dst, name=name)
        conn = next(c for c in ctrl.project.connections if c.name == name)
        conn.stream_id = stream_id
        conn.src_complex = src_complex
        conn.out_tag = out_tag

    # ingress: three nets off the one input port (INV-24 shared-port fan-out).
    # All three carry the SAME stream_id: they are arms of ONE complex stream,
    # and the server injects the sample at every arm's resolved landing.
    add(CPE(chip=0, port="x16_in"), BE(block=ids["power"], port="re"),
        INGRESS_NETS["power"], stream_id=STREAM_ID, src_complex=True)
    add(CPE(chip=0, port="x16_in"), BE(block=ids["power"], port="im"), "in_im",
        stream_id=STREAM_ID, src_complex=True)
    add(CPE(chip=0, port="x16_in"), BE(block=ids["zcr"], port="sample"),
        INGRESS_NETS["zcr"], stream_id=STREAM_ID, src_complex=True)
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
        "gru_out", out_tag=_stream_out_tag())



#: THE SHIPPED LAYOUT — the whole chain, placed and routed and built on ONE
#: 10x12 at 102 of 120 cells (65 block + 37 corridor).
#:
#: The wide-flat GRU occupies rows 6..11 across the full width; the six
#: front-end blocks live in the six free rows above it, between the chain's
#: ingress and the GRU's north-edge input. This is the layout the ``.kyt``
#: ships and the gates assert.
#:
#: It is TIGHT: a 400-layout random search over the free band found exactly one
#: arrangement that routes AND builds, which is why these anchors are pinned
#: rather than left to the auto-placer. See ``ROUTING HISTORY`` below.
BEST_KNOWN_ANCHORS = {
    "gru": (0, 6), "power": (3, 2), "boxcar": (6, 1), "root": (8, 1),
    "decim": (4, 2), "zcr": (1, 1), "join": (2, 1),
}

#: the join->GRU tail alone, measured at 81/120 cells. Kept now that the whole
#: chain routes as a REGRESSION SPLIT: if the full chain ever stops routing,
#: this says immediately whether the tail or the RMS arm is responsible.
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


def build_chain(anchors=None):
    """Hand-place, route and BUILD the whole chain at ``anchors``.

    Returns ``(ctrl, build_result, ids, failed_net_names)``. This is the single
    place the design is assembled: :func:`route_chain` reports on it,
    :func:`run_on_chip` simulates it, and ``build_kyt.py`` saves it, so all
    three can never drift from one another.
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
        return ctrl, None, ids, bad
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    if not bres.ok:
        return ctrl, bres, ids, ["build:" + str(bres.errors[0])]
    return ctrl, bres, ids, []


def route_chain(anchors=None):
    """Hand-place the WHOLE chain and auto-route it.

    Returns ``(failed_net_names, cells_used)``. ``failed_net_names == []``
    means the chain routed; at :data:`BEST_KNOWN_ANCHORS` it does, and builds,
    at 102 of 120 cells.
    """
    _ctrl, bres, _ids, bad = build_chain(anchors)
    if bad:
        return bad, None
    return [], sum(c.cell_count for c in bres.chips.values())


# --------------------------------------------------------------------------- #
#  THE ON-CHIP RUN — the example's real gate                                   #
# --------------------------------------------------------------------------- #
#
# INGRESS PROTOCOL. The chip has ONE 16-bit input port and the chain takes
# THREE nets off it (INV-24 port fan-out): Re -> power.re, Im -> power.im, and
# Re again -> zcr.sample. The build reports the resolved landings in
# ``chips[0].input_landings`` keyed by net name.
#
# THE COMPLEX PAIR IS ONE DELIVERY, NOT TWO — read the landings, do not assume.
# ``power`` is a COMPLEX 2-rail consumer, so the router does NOT give its two
# rails independent deliveries: ``in_re`` and ``in_im`` share ONE corridor
# ending at a BROKER one cell short of the power cell, and ``in_re``'s landing
# is that broker with BOTH staging registers in its ``data_addrs``. The broker
# then hands the pair to the power cell as a single complex packet with ONE
# trigger.
#
# So the correct drive is: write Re AND Im to ``in_re``'s two ``data_addrs`` at
# ``in_re``'s hop, then fire ``in_re``'s entry ONCE. ``in_im``'s landing entry
# describes the FINAL destination (the power cell's own registers) and must NOT
# be driven from the port — writing there delivers Im into the power cell's Re
# register, which measured as power = re^2 with Im silently stuck at 0 (the
# whole clip classified 9/12 instead of exactly, and every stage downstream
# looked plausible). Whenever a complex consumer is fed from a port, drive the
# BROKER landing, not the per-rail one.
#
# The ZCR cell is single-rail and genuinely independent: its own hop, its own
# entry, its own write. It is driven after the pair.

def _enc_write(hop: int, addr: int) -> int:
    """WRITE opcode 0x6, hop in [9:5], dest register in [4:0]."""
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    """JUMP opcode 0x7, hop in [9:5], entry address in [4:0]."""
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def _sample_burst(landings, re_w, im_w):
    """The word stream that delivers ONE complex input sample to the chain.

    See the INGRESS PROTOCOL note above: the (Re, Im) pair is ONE delivery into
    the shared complex broker, and the ZCR copy of Re is a second, independent
    one.
    """
    lr, lz = landings["in_re"], landings["in_zcr"]
    pair_hop = int(lr["hop"]) & 0x1F
    pair_addrs = list(lr.get("data_addrs") or [])
    if len(pair_addrs) < 2:
        raise RuntimeError(
            f"the complex ingress landing carries {len(pair_addrs)} data "
            f"address(es), not the 2 a rail PAIR needs: {lr!r}. The router's "
            f"complex-broker resolution changed; re-read the landings before "
            f"driving (see the INGRESS PROTOCOL note).")
    zcr_hop = int(lz["hop"]) & 0x1F
    zcr_addr = int((list(lz.get("data_addrs")) or [0])[0])
    return [
        # the complex pair: BOTH rails into the broker, then ONE trigger
        _enc_write(pair_hop, int(pair_addrs[0])), re_w & 0xFFFF,
        _enc_write(pair_hop, int(pair_addrs[1])), im_w & 0xFFFF,
        _enc_jump(pair_hop, int(lr["entry"])),
        # the ZCR arm's own copy of Re, on its own net
        _enc_write(zcr_hop, zcr_addr), re_w & 0xFFFF,
        _enc_jump(zcr_hop, int(lz["entry"])),
    ]


def run_on_chip(iq, anchors=None, max_events_per_sample=40_000):
    """Drive the REAL placed + routed + built chip with a complex baseband
    clip and return the RAW class words it emits.

    This is the example's end-to-end proof: the bitstream is the one
    :func:`build_chain` produces from the shipped anchors, the stimulus goes in
    through the chip's real input port, and the class words come out of its real
    output port. Nothing is composed in Python.

    ``iq`` is a complex array; it is quantized to Q15 exactly as
    ``gru_stimulus.to_q15`` does. Returns ``(class_words, cells_used)``.
    """
    import simkyt
    from gru_stimulus import to_q15

    _ctrl, bres, _ids, bad = build_chain(anchors)
    if bad:
        raise RuntimeError(f"the chain did not build: {bad}")
    chip_res = bres.chips[0]
    landings = dict(getattr(chip_res, "input_landings", {}) or {})
    for k in ("in_re", "in_im", "in_zcr"):
        if k not in landings:
            raise RuntimeError(f"no resolved input landing for net {k!r}; "
                               f"got {sorted(landings)}")

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    # The port's own entry address is the RE net's entry: that is the net whose
    # landing the port cell forwards by default. The other two nets carry their
    # entries in their own JUMP words.
    chip.set_port_entry_address("x16_in", int(landings["in_re"]["entry"]))

    re_q = to_q15(np.real(iq)).tolist()
    im_q = to_q15(np.imag(iq)).tolist()
    stream = []
    for r, i in zip(re_q, im_q):
        stream += _sample_burst(landings, int(r), int(i))
    chip.queue_words_physical("x16_in", stream)
    res = chip.run(max_events=max(400_000,
                                  max_events_per_sample * max(1, len(re_q))))
    if isinstance(res, dict) and not res.get("completed", True):
        raise RuntimeError(
            f"LIVELOCK / event cap under saturated drive: {res} — the GRU's "
            f"timestep barrier did not drain, or a net is mis-delivered")
    words = [int(v) & 0xFFFF
             for (v, _d, _t) in chip.read_port_words_timed("x16_out")]
    return words, chip_res.cell_count


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
