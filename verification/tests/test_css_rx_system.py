# SPDX-License-Identifier: GPL-3.0-or-later
"""CSS receive-spine SYSTEM gate — the ConjChirpMixer + ChirpSync pair's
reason to exist.

End-to-end integration: mapper → generator → channel → dechirp → FFT16 →
|·|² → align → argmax → sync, with the WHOLE RECEIVE SPINE placed, routed,
built and run as ONE 10x12 chip:

    x16_in → ConjChirpMixerBlock(n=16) → FFT16Block → ComplexToMagSquared
           → DelayBlock(1) → BinArgmaxBlock(16) [→ ChirpSyncBlock(4)] → x16_out

WHERE THE ON-CHIP / NUMPY BOUNDARY SITS (honest statement):
  * ON-CHIP (one placed+routed chip, real corridors and hand-offs): the whole
    receive spine — dechirp, FFT16, magnitude, alignment delay, argmax, and
    (chain B) sync. Driven BOTH per-sample and SATURATED (the whole burst via
    queue_words_physical, one continuous run — the real streaming condition).
  * MODEL-SIDE (numpy / bit-exact integer goldens): the TRANSMITTER
    (ChirpSymbolMapperBlock + ChirpGeneratorBlock process_reference_q15 —
    both blocks independently chip-bit-exact-verified in their own suites)
    and the CHANNEL (attenuation + AWGN, then Q15 quantization — a channel is
    numpy by nature).
  The chip output is asserted BIT-EXACT against the COMPOSED integer goldens
  of the five RX blocks, so the golden chain is chip-proven on every stimulus
  it shares with the chip.

FRAME ALIGNMENT (the system-level insight this file pins): FFT16's streaming
latency is N-1 = 15 ≡ -1 (mod 16), so BinArgmax's frames would STRADDLE two
FFT frames; ONE extra real-rail sample of delay (DelayBlock(1)) lands every
argmax frame exactly on one FFT frame (slots j = 0..15, bins brev4(j)). The
decode map is then s = brev4(argmax index); argmax output frame 0 is the
deterministic zero-startup frame (index 0), and frame f+1 carries symbol f.
A no-delay mutant golden mis-frames and must FAIL (gated).

SYMBOL ERROR RATE (the headline number): ≥1000 random symbols at 10 dB SNR
(attenuation 0.5), full RX spine ON-CHIP under saturated drive — measured
SER reported in the emitted JSON; the gate bound (SER ≤ 0.005) is derived,
not tuned: post-FFT SNR ≈ 10 + 10·log10(16) ≈ 22 dB puts the 16-ary
noncoherent error probability far below it. A -10 dB negative control (SER
must be HIGH) proves the metric is live (INV-26 spirit).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_css_rx_system.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_session_report  # noqa: E402

from gr_kyttar.placement.blocks.conj_chirp_mixer_block import (  # noqa: E402
    ConjChirpMixerBlock)
from gr_kyttar.placement.blocks.chirp_generator_block import (  # noqa: E402
    ChirpGeneratorBlock)
from gr_kyttar.placement.blocks.chirp_symbol_mapper_block import (  # noqa: E402
    ChirpSymbolMapperBlock)
from gr_kyttar.placement.blocks.chirp_sync_block import ChirpSyncBlock  # noqa: E402
from gr_kyttar.placement.blocks.bin_argmax_block import BinArgmaxBlock  # noqa: E402
from gr_kyttar.placement.blocks.complex_mag_block import (  # noqa: E402
    ComplexToMagSquaredBlock)
from gr_kyttar.placement.blocks.fft16_block import (  # noqa: E402
    fft16_streaming_reference, bit_reverse_4)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")

N = 16          # samples per symbol == FFT size == alphabet size (n = m = 16)
K = 4           # sync run length


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


# --- the one-chip RX spine (place + route + build) ----------------------------

def _build_chain(with_sync: bool):
    """Place, route and build the whole RX spine on ONE 10x12 chip. The
    placement is the probed hand layout (mixer col 0-1, FFT16 6x8 block in the
    middle, the four 1-cell tail blocks along the bottom rows)."""
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("css_rx", key)
    pl = [("mix", "ConjChirpMixerBlock", 0, 1, {"n": N}),
          ("fft", "FFT16Block", 2, 2, {}),
          ("mag", "ComplexToMagSquaredBlock", 0, 9, {}),
          ("dly", "DelayBlock", 0, 11, {"delay": 1}),
          ("amx", "BinArgmaxBlock", 2, 11, {"n": N})]
    conns = [("CHIP", "x16_in", "mix", "xi"),
             ("mix", "yi", "fft", "xi"),
             ("fft", "out_i", "mag", "re"),
             ("mag", "out", "dly", "sample"),
             ("dly", "out", "amx", "sample")]
    if with_sync:
        pl.append(("syn", "ChirpSyncBlock", 4, 11, {"k": K}))
        conns += [("amx", "out", "syn", "idx"),
                  ("syn", "out", "CHIP", "x16_out")]
    else:
        conns += [("amx", "out", "CHIP", "x16_out")]
    ids = {}
    for bname, btype, x, y, params in pl:
        ids[bname] = ctrl.place_block(btype, 0, x, y,
                                      library="lattrex.official", params=params)
    for (src, sport, dst, dport) in conns:
        s = (ChipPortEndpoint(chip=0, port=sport) if src == "CHIP"
             else BlockEndpoint(block=ids[src], port=sport))
        d = (ChipPortEndpoint(chip=0, port=dport) if dst == "CHIP"
             else BlockEndpoint(block=ids[dst], port=dport))
        ctrl.add_logical_connection(s, d, name=f"{src}_{sport}_{dst}")
    rep = ctrl.auto_route_all({key: ct})
    assert rep.ok, ("route failed: "
                    + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, [str(e) for e in getattr(bres, "errors", [])][:3]
    return bres


def _landing(bres):
    cb = (getattr(bres, "chips", {}) or {}).get(0)
    land = (getattr(cb, "input_landings", {}) or {}).get("CHIP_x16_in_mix")
    assert land, "input landing missing"
    return (int(land["hop"]) & 0x1F, int(land["entry"]),
            list(land["data_addrs"]))


def _drive_per_sample(bres, samples):
    import simkyt  # noqa: PLC0415
    hop, entry, addrs = _landing(bres)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    out = []
    for (xi, xq) in samples:
        chip.inject_data_physical([xi], target_hop_cnt=hop,
                                  target_addr=addrs[0])
        chip.run(max_events=6000)
        chip.inject_data_physical([xq], target_hop_cnt=hop,
                                  target_addr=addrs[1])
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=400000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return out


def _drive_saturated(bres, samples):
    """The whole burst queued back-to-back (queue_words_physical), ONE bounded
    continuous run (INV-19 harness safety) — the real streaming condition."""
    import simkyt  # noqa: PLC0415
    hop, entry, addrs = _landing(bres)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    w_op = lambda a: (0x6 << 12) | ((hop & 0x1F) << 5) | (a & 0x1F)  # noqa: E731
    j_op = (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)
    stream = []
    for (xi, xq) in samples:
        stream += [w_op(addrs[0]), xi, w_op(addrs[1]), xq, j_op]
    chip.queue_words_physical("x16_in", stream)
    res = chip.run(max_events=max(2000000, 60000 * len(samples)))
    assert res.get("completed", False), (
        f"saturated run did not complete: {res.get('stop_reason')} after "
        f"{res.get('events_processed')} events")
    return [int(v) & 0xFFFF for (v, _d, _t) in
            chip.read_port_words_timed("x16_out")]


# --- the composed bit-exact golden (the five RX blocks' own references) -------

def _golden_rx(samples, with_sync: bool):
    mix = ConjChirpMixerBlock("m", n=N)
    y = mix.process_reference_q15(np.array(samples, dtype=np.uint16))
    f = fft16_streaming_reference(y)
    mag = ComplexToMagSquaredBlock("g").process_reference_q15(
        [a for a, _ in f], [b for _, b in f])
    aligned = [0] + list(mag[:-1])                    # DelayBlock(1)
    idxs = BinArgmaxBlock("a", n=N).process_reference_q15(aligned)
    if not with_sync:
        return [w & 0xFFFF for w in idxs]
    return [w & 0xFFFF
            for w in ChirpSyncBlock("s", k=K).process_reference_q15(idxs)]


def _tx(syms):
    """TX side (model, chip-bit-exact-verified in its own suite): the
    generator's integer golden as (xi, xq) word pairs."""
    return list(ChirpGeneratorBlock("g", n=N, m=N).process_reference_q15(syms))


def _channel(word_pairs, snr_db, atten, seed):
    """numpy channel: attenuation + complex AWGN at snr_db, re-quantized to
    Q15 words (what the chip port actually receives)."""
    rng = np.random.default_rng(seed)
    x = np.array([complex(_s16(a), _s16(b)) for a, b in word_pairs]) / 32768.0
    sig = x * atten
    p_sig = float(np.mean(np.abs(sig) ** 2))
    sigma = np.sqrt(p_sig / (10 ** (snr_db / 10.0)) / 2.0)
    noise = sigma * (rng.standard_normal(len(sig))
                     + 1j * rng.standard_normal(len(sig)))
    rx = sig + noise
    return [(_q15(c.real), _q15(c.imag)) for c in rx]


def _decode(idx_words, n_syms):
    """Argmax words -> symbols: frame 0 is the zero-startup frame; frame f+1
    carries symbol f as brev4(index). NOTE the +1 framing latency: symbol f's
    argmax word emerges one frame after its samples end, so decoding the LAST
    transmitted symbol requires one FLUSH symbol behind it (the tests append
    a trailing s=0)."""
    return [bit_reverse_4(i) if i < N else -1
            for i in idx_words[1:1 + n_syms]]


def _bits_to_syms(bits):
    """ChirpSymbolMapperBlock's verified reference: log2(m)=4 bits MSB-first
    -> one raw symbol word."""
    return [int(w) & 0xFFFF for w in
            ChirpSymbolMapperBlock("map", m=N).process_reference(
                np.asarray(bits, dtype=np.uint8))]


# --- clean-channel: every symbol decodes, chip == composed golden -------------

def test_chain_all_symbols_decode_clean():
    """All 16 symbols (preceded by the K-run preamble) through the ON-CHIP RX
    spine, clean channel: chip output BIT-EXACT vs the composed golden, and
    every symbol decodes to its expected (bit-reversed) bin."""
    syms = [0] * K + list(range(N))
    samples = _tx(syms + [0])                          # +1 flush symbol
    bres = _build_chain(with_sync=False)
    got = _drive_per_sample(bres, samples)
    exp = _golden_rx(samples, with_sync=False)
    assert got == exp, "chip argmax stream != composed integer golden"
    assert len(got) == len(syms) + 1                   # one word per symbol
    assert _decode(got, len(syms)) == syms, "decode map s=brev4(idx) broken"


def test_chain_symbols_from_mapper_bits():
    """The mapper front (bits -> symbols): a random bit stream mapped by the
    verified ChirpSymbolMapperBlock reference, transmitted, and recovered
    END-TO-END on-chip (saturated drive) — recovered symbols == mapped
    symbols, so the full mapper→generator→…→argmax path round-trips."""
    rng = np.random.default_rng(3)
    bits = list(rng.integers(0, 2, 4 * 24))
    syms = _bits_to_syms(bits)
    assert len(syms) == 24
    samples = _tx([0] * K + syms + [0])                # +1 flush symbol
    bres = _build_chain(with_sync=False)
    got = _drive_saturated(bres, samples)
    assert got == _golden_rx(samples, with_sync=False)
    assert _decode(got, K + 24)[K:] == syms


# --- the preamble lock (chain B, ChirpSync on-chip) ---------------------------

def test_preamble_locks_on_chip():
    """Chain B (…→ argmax → ChirpSync → x16_out), clean channel: the K-run
    preamble asserts sync ON-CHIP with locked bin 0, de-asserts on the first
    data symbol, and the whole packed-word stream is bit-exact vs the
    composed golden. (The deterministic zero-startup argmax frame also reads
    index 0, so it contributes one frame of run credit — lock asserts within
    the preamble; documented.)"""
    syms = [0] * 6 + [5, 11, 3]
    samples = _tx(syms)
    bres = _build_chain(with_sync=True)
    got = _drive_per_sample(bres, samples)
    exp = _golden_rx(samples, with_sync=True)
    assert got == exp, "chip sync stream != composed golden"
    locked = [w for w in got if w != 0xFFFF]
    assert locked and all(w == 0 for w in locked), \
        "preamble must lock with bin 0 reported"
    # frames 7, 8 carry data symbols 5 and 11 (frame 6 is still the last
    # preamble frame, locked) — sync must have de-asserted there
    assert got[-2:] == [0xFFFF] * 2, "data symbols must not stay locked"


def test_no_preamble_no_lock_on_chip():
    """Chain B negative control: distinct consecutive data symbols (no K-run)
    never assert sync on-chip."""
    syms = [1, 5, 9, 2, 14, 7, 3, 12]
    samples = _tx(syms)
    bres = _build_chain(with_sync=True)
    got = _drive_saturated(bres, samples)
    assert got == _golden_rx(samples, with_sync=True)
    assert all(w == 0xFFFF for w in got), "sync asserted with no preamble!"


# --- the alignment delay is LOAD-BEARING (mutation) ---------------------------

def test_alignment_delay_mutation_fails():
    """FFT16's latency 15 ≡ -1 (mod 16): WITHOUT the DelayBlock(1) the argmax
    frames straddle two FFT frames and the decode map breaks — the no-delay
    mutant golden must DISAGREE with the chip-proven aligned golden on data
    symbols (INV-4 for the system-level framing choice)."""
    syms = [0] * K + [5, 11, 3, 0, 15, 8]
    samples = _tx(syms + [0])                          # +1 flush symbol
    mix = ConjChirpMixerBlock("m", n=N)
    y = mix.process_reference_q15(np.array(samples, dtype=np.uint16))
    f = fft16_streaming_reference(y)
    mag = ComplexToMagSquaredBlock("g").process_reference_q15(
        [a for a, _ in f], [b for _, b in f])
    amx = BinArgmaxBlock("a", n=N)
    aligned = amx.process_reference_q15([0] + list(mag[:-1]))
    misframed = amx.process_reference_q15(list(mag))   # the DEFECT: no delay
    assert aligned != misframed, \
        "gate failed to detect the missing alignment delay!"
    dec_ok = [bit_reverse_4(i) for i in aligned[1:1 + len(syms)]]
    assert dec_ok == syms


# --- SER at 10 dB over >= 1000 symbols, full RX spine ON-CHIP -----------------

def test_system_ser_10db_1000_symbols():
    """THE HEADLINE GATE: >= 1000 random symbols + the K-preamble through
    attenuation 0.5 + AWGN at 10 dB SNR, the ENTIRE RX spine on ONE chip
    under SATURATED drive. Asserts (a) the chip stream is BIT-EXACT vs the
    composed golden (the golden chain is chip-proven at 10 dB), (b) the
    preamble locks in the golden sync view, (c) SER <= 0.005 (derived:
    post-FFT SNR ≈ 22 dB). The measured SER is reported."""
    n_data = 1000
    rng = np.random.default_rng(2026)
    syms = list(int(s) for s in rng.integers(0, N, n_data))
    tx = _tx([0] * K + syms + [0])                     # +1 flush symbol
    samples = _channel(tx, snr_db=10.0, atten=0.5, seed=99)
    bres = _build_chain(with_sync=False)
    got = _drive_saturated(bres, samples)
    exp = _golden_rx(samples, with_sync=False)
    assert got == exp, "10 dB saturated chip stream != composed golden"
    assert len(got) == K + n_data + 1
    dec = _decode(got, K + n_data)[K:]
    errs = sum(1 for d, s in zip(dec, syms) if d != s)
    ser = errs / n_data
    print(f"\nCSS system SER @ 10 dB (atten 0.5, {n_data} symbols, "
          f"full RX spine on-chip, saturated): {errs}/{n_data} = {ser:.4f}")
    assert ser <= 0.005, f"SER collapsed at 10 dB: {ser:.4f}"
    # the preamble still locks at 10 dB (golden sync view over the chip-proven
    # index stream)
    sync = ChirpSyncBlock("s", k=K).process_reference_q15(got)
    assert any(w != 0xFFFF for w in sync[:K + 1]), \
        "preamble failed to lock at 10 dB"
    report = {
        "example": "css_rx_system", "n_symbols": n_data, "snr_db": 10.0, "attenuation": 0.5,
        "symbol_errors": errs, "ser": ser,
        "onchip": "full RX spine (dechirp+FFT16+mag2+delay+argmax[+sync]) on "
                  "one 10x12 chip, saturated queue_words drive",
        "model_side": "TX (mapper+generator integer goldens, chip-verified in "
                      "their own suites) and the numpy channel",
    }
    write_session_report("CssRxSystem", report)


def test_system_ser_negative_control():
    """INV-26 spirit: the SER metric must be LIVE — at -10 dB SNR the
    (chip-proven) golden chain must FAIL badly (SER > 0.2). Golden-side (the
    composed references are bit-exact to the chip on every shared stimulus)."""
    n_data = 200
    rng = np.random.default_rng(7)
    syms = list(int(s) for s in rng.integers(0, N, n_data))
    samples = _channel(_tx([0] * K + syms + [0]), snr_db=-10.0, atten=0.5,
                       seed=11)
    idxs = _golden_rx(samples, with_sync=False)
    dec = _decode(idxs, K + n_data)[K:]
    errs = sum(1 for d, s in zip(dec, syms) if d != s)
    assert errs / n_data > 0.2, (
        f"negative control too clean ({errs}/{n_data}) — the SER metric "
        f"would be vacuous")
