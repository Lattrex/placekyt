"""Full-duplex BPSK modem on ONE 10x12 array — ONE bitstream, BOTH directions.

GATE: ONE built bitstream runs BOTH a coherent BPSK RX chain AND a production BPSK
TX chain, sharing ONE input port (x16_in) and ONE output port (x16_out), steered by
DIRECT per-burst JUMP entry off the shared input port (NO splitter, NO input routes
— the host port-injects each burst to its chain's landing cell by hop+entry), demuxed
at x16_out by a distinct per-chain out_tag.

What is asserted (all on the SAME built bitstream):
  * the co-resident modem (both chains, ONE in + ONE out port) BUILDS + ROUTES;
  * RX recovers bits at BER 0 (lag-aligned, inversion-tolerant) from an RRC BPSK
    burst with carrier + timing offset injected to the MF landing cell;
  * TX is value-exact (max_abs_diff <= TX_TOL) and full-rate (sps samples per bit)
    injected to the mapper landing cell, demuxed by the TX out_tag;
  * both directions are exercised on the SAME bitstream (interleaved drive).

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest placekyt/tests/test_bpsk_modem.py -x -q
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine import bpsk_modem_demo as M  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def built(qapp):
    """The ONE co-resident duplex bitstream, built once and shared by all tests."""
    return M.build_modem()


def test_modem_builds_and_routes(built):
    """The co-resident duplex (both chains, ONE input + ONE output port) places,
    routes, and builds into a loadable bitstream."""
    assert built["bres"] is not None and built["bres"].ok
    assert len(built["bres"].words(0)) > 0


def test_rx_direction_ber_zero(built):
    """RX direction: a full-scale RRC BPSK burst (carrier+timing offset) DIRECT-
    injected to the MF landing cell off the SHARED x16_in (I/Q to R0/R1, JUMP
    rx_entry, hop 30) recovers bits at BER 0 (lag-aligned, inversion-tolerant) —
    demuxed from x16_out by the RX out_tag."""
    ber, e, m, lag, nbits = M.run_rx_direction(built)
    assert m and e == 0, f"RX BER={ber:.4f} ({e}/{m}, lag={lag}); {nbits} bits"


def test_tx_direction_value_exact(built):
    """TX direction: bits DIRECT-injected to the mapper landing cell off the SHARED
    x16_in (the mapper sits on the FWD_FACE inject chain after the MF snake; the bit
    transits the MF (HOP<31) and lands at the mapper) flow through Upsampler->RRC->
    IQUpconvert and egress x16_out — full-rate (sps/bit) and value-exact vs the
    composed TX reference, demuxed by the TX out_tag."""
    maxd, got, ref, counts = M.run_tx_direction(built)
    assert all(c == M.TX_SPS for c in counts), \
        f"TX not full-rate: per-bit counts {counts} (expect {M.TX_SPS}/bit)"
    assert len(got) == len(ref), \
        f"TX produced {len(got)} samples, ref has {len(ref)}"
    assert maxd <= M.TX_TOL, \
        f"TX max_abs_diff={maxd} (tol {M.TX_TOL}); got={got[:12]} ref={ref[:12]}"


def test_both_directions_one_bitstream(built):
    """Both directions on the SAME built bitstream, drive INTERLEAVED: RX bursts and
    TX bursts share the one x16_in / x16_out, demuxed by out_tag. Proves the duplex
    composition (not two separate bitstreams)."""
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415

    bres, ct_path = built["bres"], built["ct_path"]
    rxe, rxh = built["rx"]["entry"], built["rx"]["hop"]
    txe, txh, txda = built["tx"]["entry"], built["tx"]["hop"], built["tx"]["da"]

    # RX stimulus: a short RRC BPSK burst.
    import random  # noqa: PLC0415
    random.seed(7)
    rx_bits = [random.randint(0, 1) for _ in range(80)]
    sig, syms = M._tx_signal(rx_bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    tx_bits = [0, 1, 1, 0, 1, 0, 0, 1]

    chip = simkyt.Chip.from_yaml(ct_path)
    chip.load_bitstream_physical(bres.words(0))

    rx_out, tx_out = [], []
    ti = 0
    for n in range(len(sig)):
        # one RX sample
        chip.set_port_entry_address("x16_in", rxe)
        chip.inject_data_physical([M._fq(float(iq[n].real))], target_hop_cnt=rxh,
                                  target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([M._fq(float(iq[n].imag))], target_hop_cnt=rxh,
                                  target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=rxh, entry_addr=rxe)
        chip.run(max_events=90000)
        rx_out.extend(v & 1 for v in M._drain_tagged(chip, M.RX_TAG))
        # interleave a TX bit every 10 RX samples
        if n % 10 == 0 and ti < len(tx_bits):
            chip.set_port_entry_address("x16_in", txe)
            chip.inject_data_physical([tx_bits[ti] & 0xFFFF], target_hop_cnt=txh,
                                      target_addr=txda)
            chip.run(max_events=15000)
            chip.inject_jump_physical(target_hop_cnt=txh, entry_addr=txe)
            chip.run(max_events=1500000)
            tx_out.extend(M._s16(w) for w in M._drain_tagged(chip, M.TX_TAG))
            ti += 1

    # RX recovered at BER 0 (demuxed by tag, despite interleaved TX bursts).
    rx_ref = [0 if s > 0 else 1 for s in syms]
    e, m, lag = M._ber_with_lag(rx_out, rx_ref)
    assert m and e == 0, f"interleaved RX BER={e}/{m} (lag={lag})"
    # TX bursts came through tagged TX, full-rate.
    assert len(tx_out) == M.TX_SPS * len(tx_bits), \
        f"interleaved TX got {len(tx_out)} samples (expect {M.TX_SPS*len(tx_bits)})"
    ref = M._tx_reference(tx_bits)
    n = min(len(tx_out), len(ref))
    maxd = max((abs(tx_out[i] - ref[i]) for i in range(n)), default=99999)
    assert maxd <= M.TX_TOL, f"interleaved TX max_abs_diff={maxd} (tol {M.TX_TOL})"


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    b = M.build_modem()
    test_modem_builds_and_routes(b); print("[1] co-resident build+route: PASS")
    test_rx_direction_ber_zero(b); print("[2] RX BER 0 (direct-inject): PASS")
    test_tx_direction_value_exact(b); print("[3] TX value-exact (direct-inject): PASS")
    test_both_directions_one_bitstream(b); print("[4] both in ONE bitstream: PASS")
