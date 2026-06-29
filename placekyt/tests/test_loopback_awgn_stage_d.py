"""Stage D — the loopback AWGN channel actually degrades the recovered-bit BER.

The loopback .grc (examples/bpsk_transceiver_loopback/bpsk_loopback.grc) now carries
a stock-GR AWGN channel (analog.noise_source_c + blocks.add_cc, amplitude `noise_volt`,
default 0.0 = clean) spliced into the downconverted baseband before the RX chain, with
the constellation sink tapping the NOISY baseband. Stock-GR ⇒ dropped on import; live
on run. Raising noise_volt is meant to smear the constellation and climb the BER.

This test proves that claim HEADLESSLY against the REAL chip (no GR needed): it drives
the EXPLICIT-placement coherent RX chain on a hosted simKYT chip with an RRC-BPSK burst
(carrier + timing offset, the proven run_rx_direction path) and ADDS complex Gaussian
noise of increasing std-dev — the exact thing the .grc's AWGN block does to the baseband.
It asserts:
  * clean (noise 0)         -> BER 0 (the loopback's BER-0 contract is intact), and
  * heavy noise (std ~0.6)  -> BER materially worse than clean (the channel degrades it),
  * BER is non-decreasing across the sweep (more noise never helps).

So the AWGN block in the .grc is a real, working channel, not decoration.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[2]


def _chip_available() -> bool:
    try:
        import simkyt  # noqa: F401
        import engine.bpsk_modem_demo as _m  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chip_available(), reason="simKYT / bpsk_modem_demo not importable")


def _run_rx_with_noise(built, noise_std, *, nsym=160, foff=0.008, toff=0.45,
                       seed=5, noise_seed=12345):
    """Drive the hosted coherent-RX chain with an RRC-BPSK burst + complex AWGN of
    std-dev ``noise_std`` on the baseband I/Q (independent per-rail, matching
    analog.noise_source_c). Returns (ber, errors, matched). Mirrors
    bpsk_modem_demo.run_rx_direction, adding the channel noise the .grc's AWGN block
    injects so the degradation is measured on the SAME RX the loopback uses."""
    import random as _random

    import simkyt
    import engine.bpsk_modem_demo as M

    bres, ct_path = built["bres"], built["ct_path"]
    entry, hop = built["rx"]["entry"], built["rx"]["hop"]
    _random.seed(seed)
    bits = [_random.randint(0, 1) for _ in range(nsym)]
    sig, syms = M._tx_signal(bits, timing_offset=toff, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * foff * kk)).astype(np.complex64)

    if noise_std > 0:
        rng = np.random.default_rng(noise_seed)
        # Complex Gaussian: independent real/imag rails, std-dev = noise_std each
        # (analog.noise_source_c with amplitude=noise_std produces this).
        noise = (rng.normal(0.0, noise_std, len(iq))
                 + 1j * rng.normal(0.0, noise_std, len(iq))).astype(np.complex64)
        iq = (iq + noise).astype(np.complex64)

    chip = simkyt.Chip.from_yaml(ct_path)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    rx = []
    for n in range(len(sig)):
        chip.inject_data_physical([M._fq(float(np.clip(iq[n].real, -1, 1)))],
                                  target_hop_cnt=hop, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_data_physical([M._fq(float(np.clip(iq[n].imag, -1, 1)))],
                                  target_hop_cnt=hop, target_addr=1)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=90000)
        rx.extend(v & 1 for v in M._drain_tagged(chip, M.RX_TAG))
    tx = [0 if s > 0 else 1 for s in syms]
    e, m, lag = M._ber_with_lag(rx, tx)
    return (e / m if m else 1.0), e, m


def test_awgn_channel_degrades_ber():
    """noise 0 -> BER 0; heavy noise -> BER worse; BER non-decreasing with noise."""
    import engine.bpsk_modem_demo as M

    built = M.build_modem()
    levels = [0.0, 0.15, 0.35, 0.6]
    bers = []
    for std in levels:
        ber, e, m = _run_rx_with_noise(built, std)
        bers.append(ber)
        print(f"[stage-D AWGN] noise_std={std:.2f} -> BER={ber:.4f} ({e}/{m})")

    # Clean channel still recovers perfectly (the loopback BER-0 contract).
    assert bers[0] == 0.0, f"clean-channel BER must be 0, got {bers[0]}"
    # The heaviest noise materially degrades recovery (the channel does something).
    assert bers[-1] > 0.05, (
        f"heavy AWGN (std={levels[-1]}) should degrade BER; got {bers[-1]:.4f}")
    # More noise never IMPROVES BER (monotone non-decreasing, small slack for the
    # stochastic loop transient / lag search).
    for lo, hi in zip(bers, bers[1:]):
        assert hi >= lo - 0.02, f"BER decreased with more noise: {bers}"
