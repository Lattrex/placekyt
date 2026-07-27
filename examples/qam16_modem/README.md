<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Full-duplex 16-QAM modem (TX + coherent RX on one array)

A full-duplex **16-QAM** modem running on the Kyttar cell array — the step up from the
[QPSK modem](../qpsk_modem/) (4 bits on a 16-point square constellation vs. 2 bits on a
QPSK circle). The transmit chain and the coherent receiver live on **one** 10×12 array,
sharing one input port (`x16_in`) and one output port (`x16_out`), exactly like the QPSK
and [FSK4](../fsk4_modem/) modems. The receiver takes an RRC-shaped 16-QAM baseband at
**2 samples/symbol** and recovers the 4-bit symbol indices (0..15) at **BER 0**:

```
TX:  bits ─▶ QAM16 Symbol Mapper (4 bits → constellation point)
          ─▶ Complex Upsampler (sps 2, zero-stuff)
          ─▶ Complex RRC Pulse Shaper (β 0.35, span 8)
          ─▶ I/Q Upconvert (carrier 4 kHz, samp_rate 32 kHz) ─▶ real passband

RX:  RRC 16-QAM I/Q ─▶ Complex RRC Matched Filter
                    ─▶ Complex Gain (2.4)            ← restore the 0.949 outer level
                    ─▶ M&M Timing Recovery           ← decision-directed symbol timing
                    ─▶ QAM16 Costas Loop             ← decision-directed carrier phase
                    ─▶ QAM16 Slicer ─▶ 4-bit symbols
```

The receiver is a real demodulator front-to-back — **not** the 2-block `Costas → slicer`
stub. The one new block this modem required is `MMTimingRecoveryBlock`; everything else
already existed in the library.

## Why this cascade (and not 2-sps Gardner + PSK Costas)

16-QAM is **not** constant-modulus, so the receiver the QPSK/BPSK modems use does not work
here. Three independently-documented reasons, each fixed by this chain:

1. **Raw Gardner is a BPSK/QPSK timing-error detector.** On 16-QAM's 4-level-per-axis
   signal its S-curve is shallow and self-noisy (~3 % residual jitter — enough to misslice
   the tight inner levels). The correct TED is **Mueller & Müller** — decision-directed,
   GNU Radio's mainstream `digital.symbol_sync_cc` M&M path — which locks 16-QAM cleanly at
   2 sps. The on-chip `MMTimingRecoveryBlock` is verified **bit-exact** to that reference.
2. **A plain Costas is PSK-only.** GNU Radio's `costas_loop_cc` supports orders 2/4/8 only.
   QAM fine carrier phase uses a **decision-directed** phase-error detector (slice to the
   nearest grid point, then `err = Im{y · conj(decision)}`) — the
   `digital.constellation_receiver_cb(constellation_16qam())` path, here the
   `QAM16ComplexCostasLoopBlock`.
3. **The decision-directed loops are scale-sensitive.** The matched filter pre-scales its
   taps ÷2 for Q15 headroom, so its output is ~2.8× compressed. The **Complex Gain** stage
   restores the constellation so the outer symbols reach the nominal **0.949** level the
   M&M slicer and the DD Costas expect (their thresholds are fixed at ±0.316/±0.632/±0.949).
   `gain = 2.4` is the centre of the robust BER-0 window `[2.3, 2.45]` (higher saturates the
   outer symbols; lower collapses them toward the inner levels — either way the DD decisions
   go wrong and the constellation degenerates to a handful of symbols).

The recovered symbols keep a 90° four-fold phase ambiguity (like QPSK), so the BER check
tries the four constellation rotations plus a small lag before scoring.

## No carrier frequency offset — by construction, not a limitation

The hosted `.kyt` runs the transmitter and receiver on the **same chip, same clock**, so
there is genuinely **no carrier frequency offset** (`foff = 0`) — the same same-chip
loopback the QPSK and FSK4 shipped modems use. The decision-directed M&M TED sits *before*
the Costas and so has no carrier tracking; a same-chip TX guarantees the `foff = 0` it
needs. The Costas still handles the static phase (the 90° ambiguity + any fixed rotation).
Over a real channel with a frequency offset you would add a coarse-frequency (FLL /
FFT-based) stage ahead of the M&M block — the standard industry cascade — but that is not
needed for the same-chip modem and is out of scope here.

## The chain

**TX (transmit / modulator):**

| GRC block (`kyttar_*`)              | placeKYT block                  | What it does |
|-------------------------------------|---------------------------------|--------------|
| `kyttar_qam16_symbol_mapper`        | `QAM16SymbolMapperBlock`        | 4 payload bits → the `constellation_16qam().points()` (I, Q) point (complex egress). |
| `kyttar_complex_upsampler`          | `ComplexUpsamplerBlock`         | Zero-stuff to 2 samples/symbol (both rails). |
| `kyttar_complex_rrc_matched_filter` | `ComplexRRCMatchedFilterBlock`  | Root-raised-cosine **pulse shaper** (β 0.35, span 8) — the matched-filter block reused on the TX side. |
| `kyttar_iq_upconvert`               | `IQUpconvertBlock`              | Free-running NCO carrier + dual mixer: `s[n] = I·cos(φ) − Q·sin(φ)` → a **single real passband** stream. |

**RX (coherent receiver):**

| GRC block (`kyttar_*`)              | placeKYT block                  | What it does |
|-------------------------------------|---------------------------------|--------------|
| `kyttar_complex_rrc_matched_filter` | `ComplexRRCMatchedFilterBlock`  | Root-raised-cosine matched filter (β 0.35, span 8), the RX front end. |
| `kyttar_complex_gain`               | `ComplexGainBlock`              | Scale both I/Q rails by 2.4 — restore the 0.949 outer constellation level the DD loops need. |
| `kyttar_mm_timing_recovery`         | `MMTimingRecoveryBlock`         | Mueller & Müller decision-directed symbol-timing recovery at 2 sps (cubic-Farrow interpolator + modulo-1 counter + PI loop). |
| `kyttar_qam16_costas_loop`          | `QAM16ComplexCostasLoopBlock`   | Decision-directed carrier-phase recovery for 16-QAM. |
| `kyttar_qam16_slicer`               | `QAM16SlicerBlock`              | Hard-decision demapper — recovered (I, Q) → the 4-bit `constellation_16qam().decision_maker()` index (0..15). |

Every internal baseband handoff is a **complex `yi`/`yq` pair** (two WRITEs + one trigger).
The two ends are asymmetric on purpose: the **TX output is a single real passband** stream
(`I·cos − Q·sin` — the quadrature information is already carried by the one real waveform,
exactly like a real transmitter and like the QPSK modem's `time_sink_f`), while the RX
**baseband is complex** I/Q until the slicer emits the single real 4-bit symbol index.

**GR equivalence.** The matched filter, the M&M timing block (bit-exact to
`digital.symbol_sync_cc(TED_MUELLER_AND_MULLER)`), the DD Costas, the I/Q upconvert
(corr 1.0 vs `sig_source_c → multiply_cc → complex_to_real`), the symbol mapper
(`constellation_16qam().points()`), and the slicer (bit-for-bit vs
`constellation_16qam().decision_maker()`) are each verified against GNU Radio in
`verification/tests/`.

> **Open the `.kyt` — don't import the `.grc`.** Like the [FSK4 modem](../fsk4_modem/),
> this is a dense hand-placed design. `MMTimingRecoveryBlock` is a 14-cell feedback block
> (Farrow interpolator + M&M TED + PI loop + a routed feedback ring), and the whole duplex
> chain plus its long M&M → Costas corridor congests the auto-router from a fresh import.
> The shipped `qam16_modem.kyt` is a DRC-clean, hand-placed + routed **full-duplex** modem —
> so **open the `.kyt`** to host the chip. The `.grc` is the reference flowgraph (it imports
> with zero unknown blocks); the actual placement is the hand-authored `.kyt`.

## Files

| File | What it is |
|------|------------|
| `qam16_modem.kyt` | The pre-placed, pre-routed **full-duplex** modem — **open this**. |
| `qam16_modem.grc` | The GNU Radio flowgraph (full-duplex TX + RX; imports with zero unknown blocks — but the design is hand-placed, so open the `.kyt`). |
| `qam16_modem.py`  | The flowgraph compiled to Python (hand-maintained to match the `.grc`). |
| `pnr_trace.py`    | A runnable placeKYT command trace that reproduces the hand place + route of the `.kyt`. |
| `batch_check.py`  | Headless BER-0 check over the SimServer batch RPC. |

## Run it

Two terminals, from the repo root.

**Terminal 1 — placeKYT (host the chip):**

```
QT_QPA_PLATFORM=xcb .venv/bin/python -m placekyt.app examples/qam16_modem/qam16_modem.kyt
# then: Simulation → "Run as GNURadio Server"; note the printed port (default 58950).
```

**Terminal 2 — drive the recovered-symbol BER check:**

```
.venv/bin/python examples/qam16_modem/batch_check.py --port 58950
```

Expected: `Symbol BER = 0.0000` after the loops lock (the first ~60 symbols are the warm-up
and are guarded). Pass `--no-plot` for stats only; `--n` sets the burst length.

Alternatively, run the flowgraph itself (`qam16_modem.py` / *Run as GNURadio Server*): the
**TX passband** scope shows the real 4 kHz carrier modulated by the 16-QAM symbols, and the
**Recovered 16-QAM symbols** scope shows the decoded indices spanning the full 0..15 range.

## Acceptance test

`placekyt/tests/test_qam16_modem_ber.py` builds the receiver through the real
place + route + build pipeline and drives the full **MF → gain → M&M → Costas → slicer**
chain on simKYT, asserting **BER 0** — including `test_shipped_kyt_recovers_ber_zero`,
which loads the shipped `qam16_modem.kyt` exactly as a user opens it.

## Key parameters

- 16-QAM symbols per RX burst: `n_syms = 400`; TX bits per burst: `n_bits = 1600` (= 400 symbols)
- Samples/symbol: `sps = 2`; RRC β 0.35, span 8
- Carrier: `frequency = 4000` Hz at `samp_rate = 32000` Hz (1 cycle / 8 samples — a
  fixed, data-independent NCO increment; it *looks* faster under load only because the
  placeKYT chip timeline is wall-clock ns on an asynchronous array)
- Carrier offset: `foff = 0` (same-chip TX/RX — see above)
- Complex gain: `2.4` (robust BER-0 window `[2.3, 2.45]`)
- Costas loop gains: `alpha_q15 = 0x0400` (proportional), `beta_q15 = 0x0020` (integral)
