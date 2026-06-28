"""bpsk_modem_demo - full-duplex BPSK modem on ONE 10x12 array, ONE bitstream.

ONE built bitstream runs BOTH a coherent BPSK RX chain AND a production BPSK TX
chain, sharing ONE input port (``x16_in``) and ONE output port (``x16_out``).

THE MECHANISM (DIRECT per-burst JUMP entry off x16_in — no splitter):
  The input nets are NOT routed through the bus at all. The host PORT-INJECTS each
  burst directly to its chain's landing cell using a per-burst ``target_hop_cnt`` +
  ``entry_addr``; the injected word transits the programmed FWD_FACE chain to
  whichever landing cell the hop selects. Only the two OUTPUTS are routed (to
  ``x16_out``, each carrying a DISTINCT ``out_tag`` so the shared port demuxes by
  tag). This sidesteps the bus router's inability to fan one input port out to two
  parallel input nets.

  * The RX matched filter (``ComplexRRCMatchedFilterBlock``) owns the input-port
    cell (0,0): I/Q inject to R0/R1 there, ``JUMP rx_entry`` at hop 30.
  * The TX PSK mapper sits at (0,1) — the FIRST free cell the FWD_FACE chain
    reaches AFTER transiting the whole MF snake ((1,1) faces WEST into it). A TX
    bit injected at the mapper's hop transits the MF (HOP<31 → forwarded by the
    hardware, never executes the MF) and lands at the mapper (HOP==31 → executes),
    which emits the BPSK symbol into the TX chain. RX I/Q (hop 30) and TX bits
    (the mapper's hop) ride the SAME (0,0) corridor and diverge purely by HOP.

  Chains (lib ``lattrex.official``):
    RX: ComplexRRC-MF → ComplexCostas → Gardner → BPSKSlicer(bit)   (tag RX_TAG)
    TX: PSKSymbolMapper(bpsk) → Upsampler → RRC → IQUpconvert        (tag TX_TAG)

GATES (simulated on the ONE built bitstream, both chains co-resident):
  * RX recovers bits at BER 0 (lag-aligned, inversion-tolerant) from an RRC BPSK
    burst with carrier + timing offset injected to the MF landing cell.
  * TX is value-exact (max_abs_diff <= TX_TOL LSB) vs the composed TX reference,
    full-rate (sps samples per bit), injected to the mapper landing cell.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m engine.bpsk_modem_demo
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- shared parameters -------------------------------------------------------
LIB = "lattrex.official"

# TX-direction passband params (the catalog IQUpconvert demo point).
TX_SPS = 4
TX_SAMPLE_RATE = 32000.0
TX_FREQUENCY = 4000.0
TX_TOL = 4  # IQUpconvert Q15 datapath vs the composed float ref (a few LSB)

# Distinct output dest TAGs so the shared x16_out demuxes the two chains.
RX_TAG = 5    # recovered RX bits
TX_TAG = 10   # TX passband samples

# --- explicit, congestion-free floorplan (auto_place congests; see module doc) -
# RX: MF owns the input-port cell (0,0); proven BER-0 downstream coords.
RX_ANCHORS = {"mf": (0, 0), "cos": (1, 2), "gar": (6, 2), "sli": (8, 4)}
# TX: mapper at (0,1) (on the FWD_FACE inject chain, after the MF snake), then the
# chain heads straight down column 0 and the passband stage sits in the bottom band.
TX_ANCHORS = {"mapper": (0, 1), "up": (0, 7), "rrc": (2, 7), "upc": (5, 9)}
# The MF->Costas net is pinned through the mapper cell (0,1) so the FWD_FACE chain
# from the port reaches the mapper: (1,1) WEST into (0,1), SOUTH to (0,2), then
# EAST toward Costas. (At (0,2) the MF stream and the mapper output diverge.)
MF_COSTAS_ROUTE = [(1, 1), (0, 1), (0, 2), (1, 2)]
# The mapper output runs straight down column 0 to the upsampler at (0,7).
MAPPER_UP_ROUTE = [(0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6), (0, 7)]

# Per-burst injection. The MF head is the port cell (0,0): distance 1 -> hop 30.
RX_HOP = 30
# The mapper at (0,1) is reached AFTER transiting all 11 MF cells then (1,1)->(0,1):
# 12 cells from the port edge -> hop 31 - 12 = 19.
TX_HOP = 19


def _chip_yaml() -> str:
    here = Path(__file__).resolve().parents[1]
    return str(here / "resources" / "chips" / "kyttar_10x12.yaml")


# --- RRC BPSK burst (carrier + timing offset) — RX stimulus ------------------
def _make_rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0, amp=0.9):
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    pk = max(abs(b) for b in out) or 1.0
    out = [amp * b / pk for b in out]      # full-scale ADC-grade drive
    return out, syms


def _ber_with_lag(rx, tx, max_lag=24, min_overlap=40):
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)        # inversion tolerant (BPSK 180° ambiguity)
        if e < best[0]:
            best = (e, m, lag)
    return best


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def _s16(w):
    return w - 0x10000 if w & 0x8000 else w


# -----------------------------------------------------------------------------
# Build the co-resident duplex (both chains, ONE in + ONE out port).
# -----------------------------------------------------------------------------
def _env():
    from PySide6.QtWidgets import QApplication
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    QApplication.instance() or QApplication([])
    ct_path = _chip_yaml()
    catalog = BlockCatalog.from_gr_kyttar()
    chip_type = load_chip_type(ct_path)
    k = getattr(chip_type, "name", None) or "kyttar_10x12"
    return catalog, chip_type, k, ct_path


def build_modem():
    """Place + route the full-duplex modem and build the ONE bitstream.

    Returns a dict with the built bitstream, chip path, catalog, and the resolved
    per-direction (entry, hop, data_addr) injection parameters.
    """
    from engine.build import BuildEngine
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController

    catalog, chip_type, k, ct_path = _env()
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("bpsk_modem", k)
    P = lambda t, xy, p=None: ctrl.place_block(  # noqa: E731
        t, 0, xy[0], xy[1], library=LIB, params=p or {})
    R = lambda s, t, pts=[]: ctrl.add_route(s, t, pts)  # noqa: E731

    # --- RX chain (MF owns the input-port cell (0,0)) -----------------------
    mf = P("ComplexRRCMatchedFilterBlock", RX_ANCHORS["mf"])
    cos = P("ComplexCostasLoopBlock", RX_ANCHORS["cos"])
    gar = P("GardnerTimingRecovery", RX_ANCHORS["gar"])
    sli = P("BPSKSlicerBlock", RX_ANCHORS["sli"], {"out_mode": "bit"})
    # --- TX chain (mapper at (0,1), on the inject chain) --------------------
    mp = P("PSKSymbolMapperBlock", TX_ANCHORS["mapper"], {"modulation": "bpsk"})
    up = P("UpsamplerBlock", TX_ANCHORS["up"], {"sps": TX_SPS})
    rrc = P("RRCPulseShaperBlock", TX_ANCHORS["rrc"])
    upc = P("IQUpconvertBlock", TX_ANCHORS["upc"],
            {"sample_rate": TX_SAMPLE_RATE, "frequency": TX_FREQUENCY})

    # RX internal nets. MF->Costas pinned through the mapper cell so the FWD_FACE
    # inject chain reaches the mapper; Costas->Gardner->Slicer auto-routed.
    R(BlockEndpoint(block=mf, port="yi"), BlockEndpoint(block=cos, port="xi"),
      MF_COSTAS_ROUTE)
    R(BlockEndpoint(block=mf, port="yq"), BlockEndpoint(block=cos, port="xq"),
      MF_COSTAS_ROUTE)
    R(BlockEndpoint(block=cos, port="yi_tap"), BlockEndpoint(block=gar, port="xi"))
    R(BlockEndpoint(block=gar, port="out"), BlockEndpoint(block=sli, port="llr"))
    # RX OUTPUT net -> x16_out, tagged RX (auto egress).
    rxo = R(BlockEndpoint(block=sli, port="out"),
            ChipPortEndpoint(chip=0, port="x16_out"))
    next(c for c in ctrl.project.connections if c.name == rxo).out_tag = RX_TAG

    # TX internal nets. mapper->up pinned straight down column 0; up->rrc->upc auto.
    R(BlockEndpoint(block=mp, port="out"), BlockEndpoint(block=up, port="x"),
      MAPPER_UP_ROUTE)
    R(BlockEndpoint(block=up, port="out"), BlockEndpoint(block=rrc, port="sample"))
    R(BlockEndpoint(block=rrc, port="out"), BlockEndpoint(block=upc, port="xi"))
    # TX OUTPUT net -> x16_out, tagged TX (auto egress sets the exit-WRITE hop so
    # the passband traverses the shared corridor and exits the port).
    txo = R(BlockEndpoint(block=upc, port="out"),
            ChipPortEndpoint(chip=0, port="x16_out"))
    next(c for c in ctrl.project.connections if c.name == txo).out_tag = TX_TAG

    rep = ctrl.auto_route_all({k: chip_type}, auto_orient=False, use_bus="always")
    if not rep.ok:
        raise RuntimeError(
            "route failed: " + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(catalog, ct_path).build(ctrl.project, {k: chip_type})
    if not bres.ok:
        raise RuntimeError("build failed: " + "; ".join(str(e) for e in bres.errors))

    rx_entry, _ = catalog.resolved_io("ComplexRRCMatchedFilterBlock")
    tx_entry, tx_ins = catalog.resolved_io(
        "PSKSymbolMapperBlock", {"modulation": "bpsk"})
    return dict(
        bres=bres, ct_path=ct_path, catalog=catalog,
        rx=dict(entry=rx_entry, hop=RX_HOP),
        tx=dict(entry=tx_entry, hop=TX_HOP, da=(tx_ins[0] if tx_ins else 0)),
    )


def _drain_tagged(chip, tag):
    """Drain x16_out, returning the values whose dest tag == ``tag``."""
    vals = []
    while chip.output_available("x16_out"):
        for (v, d, _t) in chip.read_port_words_timed("x16_out"):
            if int(d) == tag:
                vals.append(int(v) & 0xFFFF)
        chip.release_output_ack("x16_out")
        chip.run(max_events=8000)
    return vals


# -----------------------------------------------------------------------------
# Drive + verify each direction on the SAME built bitstream.
# -----------------------------------------------------------------------------
def run_rx_direction(built, nsym=160, foff=0.008, toff=0.45, seed=5):
    """Direct-inject an RRC BPSK burst (carrier+timing offset) to the MF landing
    cell off the SHARED x16_in; demux x16_out by RX_TAG. Returns
    (ber, errors, matched, lag, nbits)."""
    import simkyt

    bres, ct_path = built["bres"], built["ct_path"]
    entry, hop = built["rx"]["entry"], built["rx"]["hop"]
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nsym)]
    sig, syms = _tx_signal(bits, timing_offset=toff, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * foff * kk)).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(ct_path)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    rx = []
    for n in range(len(sig)):
        chip.inject_data_physical([_fq(float(iq[n].real))], target_hop_cnt=hop,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([_fq(float(iq[n].imag))], target_hop_cnt=hop,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=90000)
        rx.extend(v & 1 for v in _drain_tagged(chip, RX_TAG))
    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = _ber_with_lag(rx, tx)
    return (e / m if m else 1.0), e, m, lag, len(rx)


def _tx_reference(bits):
    """Composed TX reference: drive the SAME chip's mapper->upsample->RRC stages,
    capture the real chip RRC output, and feed it to the IQUpconvert Q15 reference
    (the proven per-sample upconvert) — exactly test_tx_chain_fullrate's method, so
    only the IQUpconvert fan-in is a DUT/ref boundary (a few Q15 LSB)."""
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock

    rrc_q15 = _run_tx_to_rrc(bits)
    iq = np.array([complex(_s16(w) / 32768.0, 0.0) for w in rrc_q15])
    ref = IQUpconvertBlock("iq", sample_rate=TX_SAMPLE_RATE,
                           frequency=TX_FREQUENCY).process_reference(iq)
    return [_s16(int(v) & 0xFFFF) for v in ref]


def _run_tx_to_rrc(bits):
    """Build mapper->upsampler->RRC (RRC->x16_out) and capture the chip RRC output
    for the same bits — the reference's upstream stages taken verbatim from a chip
    so only IQUpconvert is the ref boundary."""
    import simkyt
    from engine.build import BuildEngine
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController

    catalog, chip_type, k, ct_path = _env()
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("tx_ref", k)
    mp = ctrl.place_block("PSKSymbolMapperBlock", 0, 0, 0, library=LIB,
                          params={"modulation": "bpsk"})
    up = ctrl.place_block("UpsamplerBlock", 0, 0, 2, library=LIB,
                          params={"sps": TX_SPS})
    rr = ctrl.place_block("RRCPulseShaperBlock", 0, 0, 4, library=LIB, params={})
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mp, port="sample"), [])
    ctrl.add_route(BlockEndpoint(block=mp, port="out"),
                   BlockEndpoint(block=up, port="x"), [])
    ctrl.add_route(BlockEndpoint(block=up, port="out"),
                   BlockEndpoint(block=rr, port="sample"), [])
    ctrl.add_route(BlockEndpoint(block=rr, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_route_all({k: chip_type})
    bres = BuildEngine(catalog, ct_path).build(ctrl.project, {k: chip_type})
    entry, ins = catalog.resolved_io("PSKSymbolMapperBlock", {"modulation": "bpsk"})
    da = ins[0] if ins else 0
    port = chip_type.port("x16_in")
    landing = ctrl.project.block(mp).placement.cells[0]
    dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(ct_path)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    out = []
    for b in bits:
        chip.inject_data_physical([int(b) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=da)
        chip.run(max_events=8000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=600000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return out


def run_tx_direction(built, bits=None):
    """Direct-inject TX bits to the mapper landing cell off the SHARED x16_in;
    demux x16_out by TX_TAG, draining the full per-bit passband burst. Returns
    (max_abs_diff, got, ref, per_trigger_counts)."""
    import simkyt

    if bits is None:
        bits = [0, 1, 1, 0, 1, 0, 0, 1]
    bres, ct_path = built["bres"], built["ct_path"]
    entry, hop, da = built["tx"]["entry"], built["tx"]["hop"], built["tx"]["da"]

    chip = simkyt.Chip.from_yaml(ct_path)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    per = []
    for b in bits:
        chip.inject_data_physical([int(b) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=da)
        chip.run(max_events=15000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=1500000)
        per.append([_s16(w) for w in _drain_tagged(chip, TX_TAG)])
    got = [x for g in per for x in g]
    ref = _tx_reference(bits)
    n = min(len(got), len(ref))
    maxd = max((abs(got[i] - ref[i]) for i in range(n)), default=99999)
    return maxd, got, ref, [len(g) for g in per]


def main():
    print("=" * 72)
    print("FULL-DUPLEX BPSK MODEM — ONE bitstream, ONE in/out port")
    print("=" * 72)

    built = build_modem()
    print(f"[build] co-resident duplex: {len(built['bres'].words(0))} words  "
          f"(RX hop {built['rx']['hop']}, TX hop {built['tx']['hop']}, "
          f"entry {built['tx']['entry']})")

    ber, e, m, lag, nbits = run_rx_direction(built)
    rx_ok = bool(m) and e == 0
    print(f"[RX] {nbits} bits, BER={ber:.4f} ({e}/{m}, lag={lag})  "
          f"{'OK' if rx_ok else 'FAIL'}")

    maxd, got, ref, counts = run_tx_direction(built)
    tx_ok = (len(got) == len(ref)) and maxd <= TX_TOL
    print(f"[TX] {len(got)} samples (per-bit {counts}, expect {TX_SPS}/bit), "
          f"max_abs_diff={maxd} (tol {TX_TOL})  {'OK' if tx_ok else 'FAIL'}")
    if not tx_ok:
        print("   GOT:", got[:12])
        print("   REF:", ref[:12])

    print("-" * 72)
    print(f"RX direction (BER 0):       {'PASS' if rx_ok else 'FAIL'}")
    print(f"TX direction (value-exact): {'PASS' if tx_ok else 'FAIL'}")
    print(f"DUPLEX-IN-ONE-BITSTREAM:    {'PASS' if (rx_ok and tx_ok) else 'FAIL'}")
    return 0 if (rx_ok and tx_ok) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
