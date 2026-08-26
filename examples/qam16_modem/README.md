<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Full-duplex 16-QAM modem (TX + coherent RX on one array)

A full-duplex **16-QAM** modem running on the Kyttar cell array: **4 bits/symbol** on a
16-point square constellation. The transmit chain and the coherent receiver live on
**one** 10×12 array, sharing one input port (`x16_in`) and one output port (`x16_out`).
The receiver takes an RRC-shaped 16-QAM baseband at **2 samples/symbol** and recovers the
4-bit symbol indices (0..15) at **BER 0**:

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

## The receive chain

- **Complex RRC matched filter** — matched to the TX pulse shape (β 0.35, span 8).
- **Complex Gain (2.4)** — restores the constellation to its nominal scale (outer symbols
  at **0.949**), which the decision-directed loops downstream expect.
- **Mueller & Müller timing recovery** — decision-directed symbol timing at 2 sps, the
  `digital.symbol_sync_cc` M&M path (on-chip `MMTimingRecoveryBlock`, verified bit-exact
  to that reference).
- **16-QAM Costas loop** — decision-directed carrier phase: slice to the nearest grid
  point, then `err = Im{y · conj(decision)}` (the
  `digital.constellation_receiver_cb(constellation_16qam())` path).
- **16-QAM slicer** — hard-decision to the 4-bit symbol index (0..15).

The recovered symbols keep a 90° four-fold phase ambiguity, so the BER check tries the four
constellation rotations plus a small lag before scoring. The transmitter and receiver run
on the same chip and clock, so there is no carrier frequency offset (`foff = 0`); the
Costas handles the static phase and the 90° ambiguity.

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

## Performance

**Simplex**, driven at **saturation** (whole burst back-to-back), recovering at
**BER 0**. Each direction runs alone at its compute-bound ceiling; the rate is the
sink (output) sample rate — the "Settled rate" the Stream Summary panel shows.

| Direction | Sink rate | Power |
|-----------|----------:|------:|
| **RX** (demod) | 146 kSa/s | 8.9 mW  |
| **TX** (mod)   | 460 kSa/s | 11.5 mW |

Power is total draw (active + idle) while that direction runs alone; **idle ~0.4 mW**.
The array is asynchronous — only active cells draw power. To reproduce: open the `.kyt`,
Run as GNURadio Server, set the Kyttar
Source **Full-speed (saturated) = Yes** and **Duplex schedule = Sequential**, Run, and
read each direction's Settled rate. (Set schedule = Interleaved for the full-duplex
rate.)

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
.venv/bin/python placekyt/main.py examples/qam16_modem/qam16_modem.kyt
# then: Simulation → "Run as GNURadio Server"; note the printed port (default 58950).
```

**Terminal 2 — drive the recovered-symbol BER check:**

```
.venv/bin/python examples/qam16_modem/batch_check.py --port 58950
```

Expected: `Symbol BER = 0.0000` after the loops lock (the first ~60 symbols are the warm-up
and are guarded). Pass `--no-plot` for stats only; `--n` sets the burst length.

Alternatively, run the flowgraph itself (open `qam16_modem.grc` in GRC / *Run as GNURadio Server*): the
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
