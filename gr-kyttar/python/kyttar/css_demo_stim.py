# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus for the CSS (chirp-spread-spectrum) demo flowgraph
(``examples/css_transceiver/css_transceiver.grc``).

The transmitter is MODEL-SIDE here (numpy, driven by the two CSS TX blocks'
own chip-verified integer goldens) and the RECEIVE SPINE is the thing placed
on the array; see the example README for the honest on-chip/host boundary.

What this module builds, in the order the demo tells it:

  1. a text message -> 4-bit nibbles (MSB-first) -> raw CSS symbols, exactly
     ``ChirpSymbolMapperBlock(m=16).process_reference``;
  2. a ``K``-symbol s=0 PREAMBLE in front of the message (the run the on-chip
     ``ChirpSyncBlock`` locks to) and ONE trailing flush symbol (the RX spine
     emits symbol f's index one frame late, so the last data symbol needs a
     symbol behind it to be pushed out);
  3. the cyclic-shifted up-chirp waveform, exactly
     ``ChirpGeneratorBlock(n=16, m=16).process_reference_q15``;
  4. a channel: attenuation + complex AWGN at a chosen SNR, re-quantized to
     the Q15 words the chip port actually receives.

The shipped burst is TWO SEGMENTS back to back through the SAME on-chip
chain: a GOOD segment at +10 dB SNR (which decodes exactly) followed by a
NEGATIVE-CONTROL segment at -10 dB (which must not). One chain, one stream,
one run — the control is on the chip, not a host-side story.

The operating point (n = m = 16, K = 4, atten 0.5, SNR 10 dB) is the one the
CSS receive-spine system gate measured on the placed chip
(``verification/tests/test_css_rx_system.py``: SER 0/1000 at 10 dB with a
-10 dB control).
"""

import numpy as np

# --- the pinned operating point ---------------------------------------------
N = 16                 # samples per chirp symbol == FFT size == alphabet size
M = 16                 # symbol alphabet (m == n, the classic CSS configuration)
K = 4                  # preamble run length the ChirpSync block locks to
ATTEN = 0.5            # channel attenuation
SNR_GOOD_DB = 10.0     # the decoding segment
SNR_BAD_DB = -10.0     # the on-chip negative control segment
SEED_GOOD = 99
SEED_BAD = 11

MESSAGE = "KYTTAR CSS"   # 10 chars -> 20 nibbles -> 20 raw symbols


# --- word helpers (the Q15 port convention) ---------------------------------

def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# --- 1. message -> bits -> symbols ------------------------------------------

def message_bits(message=MESSAGE):
    """The message as a flat MSB-first bit list (8 bits per ASCII char)."""
    bits = []
    for ch in message:
        b = ord(ch) & 0xFF
        bits.extend((b >> i) & 1 for i in range(7, -1, -1))
    return bits


def message_symbols(message=MESSAGE, m=M):
    """The message's raw CSS symbols — ``ChirpSymbolMapperBlock``'s pinned
    MSB-first pack of log2(m) bits per symbol, recomputed here with the same
    recurrence (the gate asserts this equals the block's own reference)."""
    k = int(round(np.log2(m)))
    bits = message_bits(message)
    out = []
    for j in range(len(bits) // k):
        w = 0
        for i in range(k):
            w = ((w << 1) | (bits[j * k + i] & 1)) & 0xFFFF
        out.append(w)
    return out


def framed_symbols(message=MESSAGE, m=M, k_pre=K):
    """preamble (k_pre zeros) + message symbols + ONE flush symbol.

    The flush symbol is load-bearing, not padding: the receive spine emits the
    index of symbol f during frame f+1, so without a symbol behind it the LAST
    data symbol's index never leaves the chip."""
    return [0] * int(k_pre) + message_symbols(message, m) + [0]


def n_data_symbols(message=MESSAGE, m=M, k_pre=K):
    """Symbols carried per segment EXCLUDING the trailing flush symbol
    (preamble + message) — the count the decoder reads back."""
    return int(k_pre) + len(message_symbols(message, m))


# --- 2. symbols -> chirp waveform -------------------------------------------

# The generator's datapath is the verified NCO pipeline (33-entry quarter-wave
# table, angle-fold + forward interpolation, sign-before-amplitude emit). This
# module runs under the GNU Radio interpreter and so cannot import gr_kyttar —
# the three helpers below are a FAITHFUL transcription of
# ``NCOBlock._quarter_table`` / ``_sine_mag_neg`` / ``_channel_q15``, and the
# example's gate asserts word-for-word equality against the block's own
# ``process_reference_q15`` (so a drift here is a test failure, not a silent
# divergence).

_TABLE_SIZE = 33          # quarter-wave entries 0..32 = sin(0 deg)..sin(90 deg)


def _quarter_table():
    return [min(32767, int(round(np.sin((np.pi / 2) * k / 32) * 32768))) & 0xFFFF
            for k in range(_TABLE_SIZE)]


def _sine_mag_neg(phase16, tbl):
    """The positive interpolated magnitude + the sign flag for a 16-bit phase
    word (angle-fold + forward interp, op for op)."""
    phase16 &= 0xFFFF
    within = phase16 & 0x3FFF
    neg = phase16 >> 15
    mir = (phase16 >> 14) & 1
    q = (16384 - within) if mir else within
    idx = q >> 9
    frac = (q & 0x1FF) << 6
    p = _s16(tbl[idx])
    qq = _s16(tbl[idx + 1]) if idx < 32 else p
    mag = p + ((_s16((qq - p) & 0xFFFF) * frac) >> 15)
    return mag, neg


def _channel_q15(phase16, tbl, amp):
    """One channel's signed Q15 output — sign applied FIRST, then amplitude
    (the datapath's op order; amp-before-sign differs by up to 1 LSB)."""
    mag, neg = _sine_mag_neg(phase16, tbl)
    return ((-mag if neg else mag) * amp) >> 15


def chirp_words(symbols, n=N, m=M, amp=32767):
    """``ChirpGeneratorBlock(n, m).process_reference_q15`` re-derived: n
    ``(yi, yq)`` Q15 word pairs per symbol, phase CARRIED across symbols (the
    generator never resets it — the pinned, gated convention)."""
    tbl = _quarter_table()
    rate = 65536 // int(n)
    shift = 16 - int(round(np.log2(m)))
    out = []
    phase = 0
    for s in symbols:
        freq = (((int(s) << shift) & 0xFFFF) + 0x8000) & 0xFFFF
        for _ in range(int(n)):
            cos = _channel_q15((phase + 16384) & 0xFFFF, tbl, amp) & 0xFFFF
            sin = _channel_q15(phase & 0xFFFF, tbl, amp) & 0xFFFF
            out.append((cos, sin))
            phase = (phase + freq) & 0xFFFF
            freq = (freq + rate) & 0xFFFF
    return out


# --- 3. channel --------------------------------------------------------------

def channel(word_pairs, snr_db, atten=ATTEN, seed=0):
    """Attenuation + complex AWGN at ``snr_db``, re-quantized to Q15 words."""
    rng = np.random.default_rng(int(seed))
    x = np.array([complex(_s16(a), _s16(b)) for a, b in word_pairs]) / 32768.0
    sig = x * float(atten)
    p_sig = float(np.mean(np.abs(sig) ** 2))
    sigma = np.sqrt(p_sig / (10 ** (float(snr_db) / 10.0)) / 2.0)
    noise = sigma * (rng.standard_normal(len(sig))
                     + 1j * rng.standard_normal(len(sig)))
    return [(_q15(c.real), _q15(c.imag)) for c in sig + noise]


# --- 4. the shipped two-segment burst ----------------------------------------

def segment(snr_db, seed, message=MESSAGE, n=N, m=M, k_pre=K, atten=ATTEN):
    """One framed message through the channel, as complex samples."""
    words = chirp_words(framed_symbols(message, m, k_pre), n, m)
    rx = channel(words, snr_db, atten, seed)
    return [complex(_s16(a) / 32768.0, _s16(b) / 32768.0) for a, b in rx]


def rx_burst(message=MESSAGE, n=N, m=M, k_pre=K):
    """The SHIPPED stimulus: the +10 dB segment then the -10 dB control
    segment, back to back, one continuous complex stream into the chip."""
    return (segment(SNR_GOOD_DB, SEED_GOOD, message, n, m, k_pre)
            + segment(SNR_BAD_DB, SEED_BAD, message, n, m, k_pre))


def seg_samples(message=MESSAGE, n=N, m=M, k_pre=K):
    """Complex samples in ONE segment."""
    return len(framed_symbols(message, m, k_pre)) * int(n)


def burst_len(message=MESSAGE, n=N, m=M, k_pre=K):
    """Complex samples in the whole shipped burst (the Source burst length)."""
    return 2 * seg_samples(message, n, m, k_pre)


def n_out_words(message=MESSAGE, n=N, m=M, k_pre=K):
    """Index words the chip emits for the whole burst: the spine is n:1, so one
    word per input frame of n samples."""
    return burst_len(message, n, m, k_pre) // int(n)


# --- 5. the decode map (what the display and the gate both use) --------------

def brev4(i):
    """Bit-reversed 4-bit index — the FFT16 DIF output order. The decode map
    is s = brev4(argmax index)."""
    i = int(i) & 0xF
    return ((i & 1) << 3) | ((i & 2) << 1) | ((i & 4) >> 1) | ((i & 8) >> 3)


def decode(index_words, n_syms=None, message=MESSAGE, m=M, k_pre=K):
    """Argmax index words -> symbols for ONE segment. Frame 0 of a segment is
    the deterministic zero-startup frame; frame f+1 carries symbol f."""
    if n_syms is None:
        n_syms = n_data_symbols(message, m, k_pre)
    return [brev4(i) for i in index_words[1:1 + int(n_syms)]]


def display_symbols(message=MESSAGE, n=N, m=M, k_pre=K):
    """The transmitted symbol sequence laid out ON THE OUTPUT WORD GRID, so a
    scope can draw it straight against the chip's decoded symbols.

    Per segment the chip emits one index word per n-sample frame: word 0 is
    the frame that carries no data symbol (the spine's +1 framing latency),
    then word f+1 carries symbol f. The trailing flush symbol's own word is
    the next segment's word 0. So one segment's reference trace is
    ``[-1] + (preamble + message symbols)``, and the shipped burst is two
    such segments back to back — the SAME message, so the SAME trace twice.
    """
    seg = [-1.0] + [float(s) for s in
                    framed_symbols(message, m, k_pre)[
                        :n_data_symbols(message, m, k_pre)]]
    return seg * 2


def symbols_to_text(symbols, m=M):
    """Inverse of the mapper: log2(m)-bit symbols -> ASCII (2 nibbles/char at
    m = 16). Non-printable bytes render as '.'."""
    k = int(round(np.log2(m)))
    per = 8 // k
    text = ""
    for j in range(len(symbols) // per):
        b = 0
        for i in range(per):
            b = (b << k) | (int(symbols[j * per + i]) & (m - 1))
        text += chr(b) if 32 <= b < 127 else "."
    return text
