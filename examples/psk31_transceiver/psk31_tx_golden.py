# SPDX-License-Identifier: GPL-3.0-or-later
"""GOLDEN model of the PSK31 TRANSMITTER — the sample-exact reference the built
chip is gated against.

There is NO stock GNU Radio PSK31 transmitter block; this golden is composed from
the ALREADY-VERIFIED Kyttar block references (each proven bit/sample-exact against
GNU Radio or the cited spec on real simKYT — see the per-block suites in
``verification/tests/``), applied in the canonical PSK31 (G3PLX / Peter Martinez)
transmit order.

SPEC / SOURCES (cited)
----------------------
* PSK31 Varicode: G3PLX / Peter Martinez; canonical 128-entry table transcribed
  from fldigi ``src/psk/pskvaricode.cxx`` (via the ``ckoval7/pydigi``
  reimplementation), cross-checked vs the ARRL PSK31 spec
  (http://www.arrl.org/psk31-spec) and the Wikipedia "Varicode" article. Each code
  starts+ends with '1', has no '00' inside, and characters are separated by a '00'
  gap. Implemented + gated in ``verification/tests/varicode_golden.py`` and
  ``VaricodeEncoderBlock`` (SRAM-backed, bit-exact through the real panel).
* Differential BPSK precode (DBPSK): ``y[n] = (x[n] + y[n-1]) mod 2`` with cold
  start ``y[-1]=0`` — the exact GNU Radio ``digital.diff_encoder_bb(2)`` recurrence
  (``DiffEncoderBlock``, verified vs live GR). PSK31 carries the DIFFERENCE between
  successive bits so the RX needs no absolute phase reference.
* BPSK symbol map: bit -> +/-1 (``PSKSymbolMapperBlock`` modulation="bpsk").
* Raised-cosine AMPLITUDE envelope on the 31.25-baud phase reversals: swharden,
  "Experiments in PSK-31 Synthesis" (2022-10-16, faithful to G3PLX); QSL.net
  "What is PSK31?". ``env[n] = sin((n+0.5)*pi/N)``, applied on a symbol half iff the
  phase reverses across that boundary, else full amplitude. The dip straddles the
  180-deg reversal (that is what halves the occupied bandwidth). Implemented +
  gated (BIT-EXACT on real simKYT) in ``RaisedCosineEnvelopeBlock``.

CHAIN (authentic PSK31 TX)
--------------------------
    text bytes
      -> Varicode encode      (variable-length bits + '00' inter-char gap)
      -> DiffEncoder(mod 2)    (DBPSK precode)
      -> BPSK map (+/-1)       (PSKSymbolMapperBlock, bpsk_bit0_positive=BIT0_POS)
      -> hold-upsample x N     (each symbol held N samples; GR blocks.repeat)
      -> RaisedCosineEnvelope  (per-symbol amplitude shaping on reversals)
      -> shaped baseband samples (Q15)

The raised-cosine ENVELOPE block IS the PSK31 pulse-shaper: it consumes the
symbol stream HELD at N samples/symbol (its documented interface) and applies the
amplitude taper. (A generic BPSK/QPSK modem uses a zero-stuff upsampler + RRC
matched-filter pair instead; PSK31's shaping is the amplitude envelope, so this TX
uses the envelope block, not an RRC.)

CONVENTIONS
-----------
* ``BIT0_POS = True`` -> BPSK ``bit 0 -> +1, bit 1 -> -1`` (GR
  ``chunks_to_symbols_bf([1, -1])`` — the historical/verified default). The
  amplitude envelope depends only on the phase REVERSAL pattern (sign changes),
  which is identical under either sign convention, so the choice is documentation,
  not correctness — we pin the verified default explicitly.
* ``AMPLITUDE`` — the symbol magnitude fed to the envelope (Q15-safe headroom); the
  envelope multiplies it by the per-sample taper.

Everything here is Q15-exact to the on-fabric datapath: the envelope reference is
``process_reference_q15`` (proven 0-LSB vs real simKYT), the mapper/diff/varicode
references are the same ones gated bit-exact per block.
"""
from __future__ import annotations

from typing import List

import numpy as np

# The default PSK31-TX parameters for this example. sps=8 keeps the sample stream
# small AND is a value the envelope block is proven BIT-EXACT on-chip for
# (FITTABLE_SPS in test_raised_cosine_envelope_v2). AMPLITUDE 0.9 is Q15-safe.
DEFAULT_SPS = 8
DEFAULT_AMPLITUDE = 0.9
BIT0_POS = True          # bit 0 -> +1 (GR chunks_to_symbols_bf([1,-1]); verified default)

# A recognizable ham-radio message (CQ call) — the demo drives this end to end.
DEMO_TEXT = "CQ CQ DE"


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def varicode_bits(text: str) -> List[int]:
    """Stage 1 — PSK31 Varicode bits for an ASCII string (code bits + '00' gap)."""
    from gr_kyttar.placement.blocks.varicode_encoder_block import (
        varicode_bits as _vb,
    )
    return list(_vb([ord(c) & 0x7F for c in text]))


def dbpsk_bits(bits: List[int]) -> List[int]:
    """Stage 2 — differential (DBPSK) precode, modulus 2 (GR diff_encoder_bb(2))."""
    from gr_kyttar.placement.blocks.diff_encoder_block import DiffEncoderBlock
    de = DiffEncoderBlock("golden_de", modulus=2)
    return de.process_reference(np.asarray(bits, dtype=np.int32)).tolist()


def bpsk_symbols(dbits: List[int], *, bit0_pos: bool = BIT0_POS) -> List[float]:
    """Stage 3 — BPSK map: bit -> +/-1 (real; Q is always 0 for BPSK)."""
    from gr_kyttar.placement.blocks.psk_symbol_mapper_block import (
        PSKSymbolMapperBlock,
    )
    m = PSKSymbolMapperBlock("golden_map", modulation="bpsk",
                             bpsk_bit0_positive=bit0_pos)
    i, _q = m.process_reference(np.asarray(dbits, dtype=np.int32))
    return i.tolist()


def hold_upsample_q15(symbols: List[float], sps: int, amplitude: float) -> List[int]:
    """Stage 4 — hold-upsample: each +/-1 symbol -> N held +/-AMPLITUDE Q15 samples.

    This is GR ``blocks.repeat(sizeof_float, N)`` on the +/-amplitude symbol stream:
    the envelope block's documented input (the symbol HELD across N samples), NOT a
    zero-stuff. Matches ``_upsample_q15`` in the envelope's own verification suite.
    """
    from gr_kyttar.placement.blocks._base import float_to_q15
    a = float(amplitude)
    out: List[int] = []
    for s in symbols:
        out.extend([float_to_q15(a if s >= 0 else -a)] * int(sps))
    return out


def envelope_q15(held_q15: List[int], sps: int) -> List[int]:
    """Stage 5 — the PSK31 raised-cosine amplitude envelope (signed Q15 ints).

    ``process_reference_q15`` is the EXACT op-for-op on-fabric datapath (accumulated-
    phase NCO envelope + sign-pipeline select + Q15 MULQ), proven 0-LSB vs real
    simKYT. Carries the block's documented 1-symbol pipeline latency (the first
    ``sps`` outputs are the leading-zero fill).
    """
    from gr_kyttar.placement.blocks.raised_cosine_envelope_block import (
        RaisedCosineEnvelopeBlock,
    )
    env = RaisedCosineEnvelopeBlock("golden_env", samples_per_symbol=int(sps))
    return [_s16(v) for v in
            env.process_reference_q15(np.asarray(held_q15, dtype=np.int32))]


def golden_tx_q15(text: str = DEMO_TEXT, *, sps: int = DEFAULT_SPS,
                  amplitude: float = DEFAULT_AMPLITUDE,
                  bit0_pos: bool = BIT0_POS) -> List[int]:
    """The full PSK31-TX golden: text -> shaped baseband samples (signed Q15 ints).

    Composed from the verified per-block references in canonical PSK31 TX order.
    """
    vc = varicode_bits(text)
    db = dbpsk_bits(vc)
    syms = bpsk_symbols(db, bit0_pos=bit0_pos)
    held = hold_upsample_q15(syms, sps, amplitude)
    return envelope_q15(held, sps)


def golden_stages(text: str = DEMO_TEXT, *, sps: int = DEFAULT_SPS,
                  amplitude: float = DEFAULT_AMPLITUDE,
                  bit0_pos: bool = BIT0_POS) -> dict:
    """Every intermediate stream (for the demo's per-stage assertions)."""
    vc = varicode_bits(text)
    db = dbpsk_bits(vc)
    syms = bpsk_symbols(db, bit0_pos=bit0_pos)
    held = hold_upsample_q15(syms, sps, amplitude)
    env = envelope_q15(held, sps)
    return {"varicode": vc, "dbpsk": db, "symbols": syms,
            "held_q15": held, "tx_q15": env}


if __name__ == "__main__":
    g = golden_stages()
    print(f"text        : {DEMO_TEXT!r}")
    print(f"varicode    : {len(g['varicode'])} bits")
    print(f"dbpsk       : {len(g['dbpsk'])} bits")
    print(f"symbols     : {len(g['symbols'])} (+/-1)")
    print(f"held @sps={DEFAULT_SPS}: {len(g['held_q15'])} samples")
    print(f"tx samples  : {len(g['tx_q15'])} shaped Q15 samples")
    print(f"first 16 tx : {g['tx_q15'][:16]}")
