# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus generators for the FEC protocol-link demo flowgraph
(examples/fec_link/fec_link.grc). Imported as a plain Python module (like
modem_demo_stim) so the .grc has no fragile inline source. Pure Python — this
module runs under the GNU Radio interpreter and must not import gr_kyttar.

THE STORY the numbers tell: a 2-bit consecutive channel burst that would be
UNCORRECTABLE inside one Hamming(7,4) codeword (distance 3 — two errors
mis-correct a third bit) is DISPERSED by the 4x3 block interleaver so its two
bits land in TWO DIFFERENT codewords, each with a single correctable error;
the frame then proves itself by a CRC-16 match.

Three streams share one chip (each stream = one injection landing + one
tagged egress, the GR client contract):

  * 'tx'    : message+pad bytes -> UnpackKBits(8) -> HammingEncoder(4:7)
              -> BlockInterleaver(4x3). Egress = the interleaved coded bits.
  * 'txcrc' : the SAME message+pad bytes -> Crc16(frame_len=12). Egress = the
              on-chip TX CRC word (CRC-16/CCITT-FALSE over the 12 message
              bytes; the 6 pad bytes are a dropped partial frame).
  * 'rx'    : the burst-corrupted channel bits -> BlockInterleaver(4x3,
              deinterleave) -> HammingDecoder(7:4) -> PackKBits(8). Egress =
              the recovered bytes.

BURST/ALIGNMENT ARITHMETIC (all derived, asserted at import time):

  * Streaming interleaver contract (BlockInterleaverBlock): strict 1:1 with a
    group delay of N = rows*cols = 12; output block b carries input block b-1
    read column-major: y[b*12 + i] = x[(b-1)*12 + sigma(i)],
    sigma(i) = (i mod 4)*3 + (i div 4). The first 12 outputs are zeros.
  * TX flush: 12 message bytes -> 96 bits -> 168 coded bits (14 whole 12-bit
    interleaver blocks). 6 zero pad bytes append 84 coded bits so the message
    part clears BOTH interleaver stages (one block of delay each) and the
    coded stream stays block-aligned (252 = 21 blocks).
  * RX codeword alignment: the decoder frames 7-bit codewords from stream
    start, but the two interleaver stages put 12 + 12 = 24 zeros ahead of the
    coded stream — and 24 mod 7 != 0 would misframe EVERY codeword. The host
    channel therefore prepends 60 more zeros: 12 + 60 + 12 = 84 = 12 whole
    zero codewords (84 mod 7 == 0), which decode to 48 zero bits = 6 whole
    zero bytes (48 mod 8 == 0). The recovered stream is byte-aligned:
    6 zero bytes, the 12 message bytes, then 4 pad-derived zero bytes.
  * The burst: channel offset BURST_AT = 28 (in TX-egress coordinates; the
    shipped channel vector carries it at 60 + 28 = 88). Consecutive
    interleaved positions g, g+1 within one column walk deinterleave to coded
    positions EXACTLY cols = 3 apart:  g = b*12 + i  carries coded bit
    (b-1)*12 + sigma(i), and sigma(i+1) - sigma(i) = 3 for i mod 4 < 3. Two
    coded positions o, o+3 straddle a 7-bit codeword boundary iff
    o mod 7 in {4, 5, 6}. Here o = 13 (13 mod 7 = 6): the burst lands on
    codeword 1 (its p0) and codeword 2 (its d1) — one error each, both
    corrected. WITHOUT the interleaver the same two consecutive channel bits
    hit coded positions 28, 29 (28 mod 7 = 0): d3 and d2 of ONE codeword —
    a double error the (7,4) code mis-corrects, and the CRC catches it.
"""

# ----------------------------------------------------------- the parameters
MESSAGE = "KYTTAR FEC73"     # 12 bytes = one CRC frame
ROWS, COLS = 4, 3            # interleaver matrix (N = 12 = MAX_DEPTH)
N = ROWS * COLS
PAD_BYTES = 6                # flushes both interleaver stages, keeps 12|bits
PREFIX_ZEROS = 60            # host-prepended zeros: 12+60+12 = 84 = 12 codewords
BURST_AT = 28                # burst offset, TX-egress coordinates
BURST_LEN = 2                # consecutive corrupted channel bits
CRC_POLY = 0x1021            # CRC-16/CCITT-FALSE
CRC_INIT = 0xFFFF


# ------------------------------------------------------------- chain mirrors
def _sigma(i):
    """Interleaver read permutation (write row-major, read column-major)."""
    return (i % ROWS) * COLS + (i // ROWS)


def _sigma_d(i):
    """Deinterleaver read permutation (the exact inverse of _sigma)."""
    return (i % COLS) * ROWS + (i // COLS)


def _interleave_stream(x, perm):
    """The STREAMING block-interleaver form (BlockInterleaverBlock contract):
    one output per input, output block b = input block b-1 permuted, block 0
    = zeros; the final input block stays in the buffer (hence the pads)."""
    out = []
    for g in range(len(x)):
        b, i = divmod(g, N)
        out.append(0 if b == 0 else x[(b - 1) * N + perm(i)])
    return out


def _unpack8(byts):
    """GR unpack_k_bits_bb(8): MSB-first bits of each byte."""
    return [(b >> (7 - k)) & 1 for b in byts for k in range(8)]


def _henc(bits):
    """HammingEncoderBlock convention pin: wire = d3 d2 d1 d0 p2 p1 p0,
    even parity p2=d3^d2^d1, p1=d3^d2^d0, p0=d3^d1^d0."""
    out = []
    for j in range(len(bits) // 4):
        d3, d2, d1, d0 = bits[4 * j:4 * j + 4]
        out += [d3, d2, d1, d0, d3 ^ d2 ^ d1, d3 ^ d2 ^ d0, d3 ^ d1 ^ d0]
    return out


def crc16(byts):
    """CRC-16/CCITT-FALSE (MSB-first, non-reflected) — mirrors Crc16Block."""
    crc = CRC_INIT
    for b in byts:
        crc ^= (int(b) & 0xFF) << 8
        for _ in range(8):
            crc = (((crc << 1) ^ CRC_POLY) if crc & 0x8000
                   else (crc << 1)) & 0xFFFF
    return crc


# --------------------------------------------------------------- the streams
def message_bytes():
    """The 12 payload bytes (one CRC frame)."""
    return [ord(c) for c in MESSAGE]


def tx_bytes():
    """The TX chip input: message + flush pads (fed to BOTH 'tx' and 'txcrc';
    the pads are a dropped partial CRC frame, so the CRC covers the message
    exactly)."""
    return message_bytes() + [0] * PAD_BYTES


def coded_bits():
    """The Hamming-coded bit stream S (before interleaving), 252 bits."""
    return _henc(_unpack8(tx_bytes()))


def tx_bits():
    """GOLDEN chip 'tx' egress: the interleaved coded stream (streaming form,
    252 bits: one startup zero block, then the permuted coded blocks)."""
    return _interleave_stream(coded_bits(), _sigma)


def error_pattern():
    """The deterministic channel error pattern over the channel vector: ones
    at the BURST_LEN consecutive burst positions, zeros elsewhere."""
    e = [0] * (PREFIX_ZEROS + len(tx_bits()))
    for k in range(BURST_LEN):
        e[PREFIX_ZEROS + BURST_AT + k] = 1
    return e


def channel_bits():
    """The RX chip input: alignment prefix + the TX egress bits XOR the burst
    (what a receiver sees after the bursty channel)."""
    t = [0] * PREFIX_ZEROS + tx_bits()
    return [b ^ e for b, e in zip(t, error_pattern())]


def chip_crc():
    """GOLDEN 'txcrc' egress: the single CRC word the chip emits (over the 12
    message bytes; frame_len=12 drops the pad partial frame)."""
    return crc16(message_bytes())


def rx_bytes_expected():
    """GOLDEN 'rx' egress: 6 alignment zero bytes, the recovered message,
    then 4 pad-derived zero bytes (derived, not assumed — the full RX mirror
    is run over channel_bits())."""
    d = _interleave_stream(channel_bits(), _sigma_d)
    bits = []
    for g in range(len(d) // 7):
        w = 0
        for b in d[g * 7:(g + 1) * 7]:
            w = ((w << 1) | b) & 0x7F
        s = 0
        for j, col in enumerate((7, 6, 5, 3, 4, 2, 1)):
            if (w >> (6 - j)) & 1:
                s ^= col
        c = w ^ (0, 1, 2, 8, 4, 16, 32, 64)[s]
        nib = (c >> 3) & 0xF
        bits += [(nib >> k) & 1 for k in (3, 2, 1, 0)]
    return [int("".join(map(str, bits[8 * j:8 * j + 8])), 2)
            for j in range(len(bits) // 8)]


# ------------------------------------------------- sizes for the .grc params
def n_tx_bytes():
    """Burst length of the 'tx' and 'txcrc' sources."""
    return len(tx_bytes())


def n_channel_bits():
    """Burst length of the 'rx' source (and the channel scope size)."""
    return len(channel_bits())


def n_tx_bits():
    """'tx' egress word count (the TX-bits scope size)."""
    return len(tx_bits())


def n_rx_bytes():
    """'rx' egress byte count (the recovered-bytes scope size)."""
    return len(rx_bytes_expected())


def rx_msg_offset():
    """Index of the first message byte in the recovered stream (after the
    alignment zero bytes)."""
    return (2 * N + PREFIX_ZEROS) // 7 * 4 // 8


def crc_frame_len():
    """Crc16Block frame_len — the CRC covers exactly the message."""
    return len(message_bytes())


# ------------------------------------------------ import-time self-assertion
def _selfcheck():
    lead = 2 * N + PREFIX_ZEROS
    assert lead % 7 == 0, "alignment prefix must complete whole codewords"
    assert (lead // 7 * 4) % 8 == 0, "prefix must decode to whole bytes"
    assert len(coded_bits()) % N == 0, "coded stream must be block-aligned"
    # The burst disperses across two codewords...
    b, i = divmod(BURST_AT, N)
    assert i % ROWS != ROWS - 1, "burst must sit inside one column walk"
    o = (b - 1) * N + _sigma(i)
    o2 = (b - 1) * N + _sigma(i + 1)
    assert o2 - o == COLS and o // 7 != o2 // 7 and o2 < 168
    # ... but the SAME offsets hit ONE codeword without the interleaver.
    assert BURST_AT // 7 == (BURST_AT + BURST_LEN - 1) // 7
    # And the full RX mirror recovers the message through the burst.
    got = rx_bytes_expected()
    off = rx_msg_offset()
    assert got[off:off + len(MESSAGE)] == message_bytes(), \
        "stim self-check: RX mirror failed to recover the message"


_selfcheck()
