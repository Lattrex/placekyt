<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# QPSK modem (full-duplex, on-chip)

A complete **QPSK modem** built from real Kyttar DSP blocks: a **transmit**
(modulator) chain and a coherent **receive** (demodulator) chain that share ONE
chip, demuxed by `stream_id` (`"tx"` / `"rx"`) — the same full-duplex transceiver
pattern as the [BPSK modem](../bpsk_modem/), here carrying **2 bits/symbol** and
**complex I/Q end-to-end**.

```
TX (stream 'tx'):  bits ─▶ PSKSymbolMapper(qpsk) ─▶ ComplexUpsampler ─▶ ComplexRRC(shaper) ─▶ IQUpconvert ─▶ QPSK passband
RX (stream 'rx'):  RRC-QPSK I/Q ─▶ ComplexRRCMatchedFilter ─▶ CostasLoop(order=4) ─▶ Gardner(complex) ─▶ QPSKSlicer ─▶ 2-bit symbols
```

- **TX (modulator):** maps 2-bit symbols to the QPSK constellation (±1/√2 per
  axis), zero-stuffs BOTH rails to `sps` samples per symbol, complex-RRC pulse-shapes,
  and upconverts to a passband — the transmit side of a QPSK link. Because QPSK carries
  genuine I *and* Q (unlike BPSK, where Q=0), the pulse-shaping path is complex: a
  **ComplexUpsampler** (2-rail zero-stuff) feeding a **ComplexRRCMatchedFilter** reused
  as the complex pulse shaper.
- **RX (demodulator):** a complex RRC matched filter, an **order-4 (QPSK) Costas
  loop** for carrier recovery, **complex (I/Q) Gardner** timing recovery, and a
  **QPSK hard-decision slicer** — recovers the 2-bit Gray symbols from an RRC-shaped
  QPSK burst carrying a carrier **and** a fractional timing offset, at **BER 0**.

The RX chain is the QPSK analog of [`../coherent_bpsk_rx/`](../coherent_bpsk_rx/);
this demo adds the matching **transmitter** on the same chip so you can see a whole
QPSK modem in one design. The channel (the carrier + fractional-timing offset) is
external — in the headless check the host loopback applies it, exactly as a real RF
channel would between a modulator and a demodulator.

## The chain

The importer maps these flowgraph blocks by id + params:

| GRC block id | placeKYT block | key params |
|--------------|----------------|-----------|
| `kyttar_psk_symbol_mapper` | PSKSymbolMapperBlock | `modulation="qpsk"` |
| `kyttar_complex_upsampler` | ComplexUpsamplerBlock | `sps=2` |
| `kyttar_complex_rrc_matched_filter` (TX shaper) | ComplexRRCMatchedFilterBlock | `span=8` |
| `kyttar_iq_upconvert` | IQUpconvertBlock | `sample_rate`, `frequency` |
| `kyttar_complex_rrc_matched_filter` (RX MF) | ComplexRRCMatchedFilterBlock | `span=8`, `decimation=1` |
| `kyttar_costas_loop` | ComplexCostasLoopBlock | **`order=4`** (QPSK) |
| `kyttar_gardner_ted` | GardnerTimingRecovery | **`complex=True`** |
| `kyttar_qpsk_slicer` | QPSKSlicerBlock | — |

Every internal handoff between the complex blocks is a **yi/yq pair** (two WRITEs +
one trigger). QPSK has a 90° carrier phase ambiguity, so a recovered stream may be
rotated by a multiple of 90° — the BER check tries all four constellation rotations
(plus a small lag) before declaring a match. The recovered symbol index follows GNU
Radio `digital.constellation_qpsk()`: `symbol = (Q≥0 ? 2 : 0) | (I≥0 ? 1 : 0)`.

**Design point.** The transmitter runs at **2 samples/symbol** and the matched
filter uses **decimation = 1**, so the carrier and timing loops run at 2 sps — the
operating point at which the complex Gardner is proven bit-exact on-chip.

## Files

| File | What it is |
|------|------------|
| `qpsk_modem.grc` | The GNU Radio flowgraph: a TX source/sink pair and an RX source/sink pair, both targeting the same placeKYT-hosted chip by `stream_id`. Open in **both** placeKYT (to host the chip) and `gnuradio-companion` (to drive it). |
| `batch_check.py` | A headless verifier: streams a QPSK burst through the hosted chip and reports the recovered symbols + symbol BER. No GNU Radio GUI needed. |

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1) — launch placeKYT and import the flowgraph:

```bash
.venv/bin/python placekyt/main.py
```

In placeKYT: **File → Import GNURadio Flowgraph…** → `examples/qpsk_modem/qpsk_modem.grc`
(placeKYT places and routes both chains onto one cell array), then **Simulation →
Run as GNURadio Server** (binds port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph and press **▶ Run** (F6):

```bash
gnuradio-companion examples/qpsk_modem/qpsk_modem.grc
```

You'll see the TX passband (the modulated QPSK) and the RX side's recovered
2-bit symbols, both coming back from the one hosted chip.

## Headless check (no GNU Radio GUI)

With placeKYT hosting the chip (step 1 above, **Run as GNURadio Server**, port 58950):

```bash
.venv/bin/python examples/qpsk_modem/batch_check.py --port 58950
# random QPSK -> RRC 2 sps + carrier + timing offset -> chip -> recovered symbols
# prints:  Symbol BER = 0.0000  (0 errors / N symbols, best rot=R*90deg, lag=L)
```

Expect **BER 0** after the loops lock. `--no-plot` prints stats only.

## Acceptance test

The acceptance test builds the coherent QPSK RX chain (MF → order-4 Costas → complex
Gardner → QPSK slicer) on-chip through the real placeKYT place+route+build pipeline
and recovers the 2-bit QPSK symbols at **BER 0 through simKYT** — both a programmatic
build and the GRC-import path. It lives in
[`placekyt/tests/test_qpsk_modem_ber.py`](../../placekyt/tests/test_qpsk_modem_ber.py).

## Key parameters

Set at the top of the flowgraph (GNU Radio variables): `sps = 2`, TX
`carrier`/`sample_rate` for the upconvert, and RRC `alpha = 0.35`, `span = 8` on both
the pulse shaper and the matched filter. The order-4 Costas loop runs at
`loop_bw = 0.05`.
