<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Coherent QPSK receiver demo

A complete coherent **QPSK** receiver running on the Kyttar cell array: an RRC
matched filter, an **order-4 (QPSK) Costas loop** for carrier recovery, **complex
(I/Q) Gardner** timing recovery, and a **QPSK hard-decision slicer**. The input is an
RRC pulse-shaped QPSK signal carrying **both** a carrier frequency offset and a
fractional timing offset; the chip recovers the 2-bit symbols with **BER 0**.

This is the QPSK analog of the [coherent BPSK receiver](../coherent_bpsk_rx/): the
same four-stage recovery chain, but end-to-end complex (both I and Q rails carried
through every internal handoff) and with a 2-bit-per-symbol slicer.

## The chain

```
x16_in (I/Q)
  → ComplexRRCMatchedFilter(span=8, decimation=1)     matched filter, 2 sps
  → ComplexCostasLoop(order=4)                          QPSK carrier recovery
  → GardnerTimingRecovery(complex=True)                 I/Q symbol-timing recovery
  → QPSKSlicer                                          (I,Q) → 2-bit Gray symbol
  → x16_out (0..3)
```

Every internal handoff between the complex blocks is a **yi/yq pair** (two WRITEs +
one trigger): the MF emits `(yi, yq)` → Costas; the order-4 Costas `qpd` output cell
emits `(yi_tap, yq_tap)` → the complex Gardner; the Gardner `qout` emits `(yi_e,
yq_e)` → the slicer, which emits one 2-bit symbol per symbol. QPSK has a 90° carrier
phase ambiguity, so a recovered stream may be rotated by a multiple of 90° — the BER
check tries all four constellation rotations (plus a small lag) before declaring a
match. The recovered symbol index follows GNU Radio `digital.constellation_qpsk()`:
`symbol = (Q≥0 ? 2 : 0) | (I≥0 ? 1 : 0)`.

**Design point.** The transmitter runs at **2 samples/symbol** and the matched
filter uses **decimation = 1**, so the carrier and timing loops run at 2 sps — the
same operating point at which the complex Gardner is proven bit-exact on-chip. (MF
decimation > 1 is for an *oversampled* input, e.g. a 4-sps TX with MF decimation = 2
→ 2 sps into the loops; this demo keeps it simple with 2 sps throughout.)

## Files

| File | What it is |
|------|------------|
| `qpsk_modem.grc` | The GNU Radio flowgraph: QPSK stimulus → the four Kyttar receiver blocks → QT GUI plots. Imports into placeKYT (auto-place + bus-route) and, with the Kyttar OOT blocks installed, drives the hosted chip from `gnuradio-companion`. |
| `qpsk_modem.kyt` | The pre-built placeKYT design (the four real catalog blocks auto-placed and bus/broker-routed). Open this directly if you'd rather not import the `.grc`. |
| `batch_check.py` | A headless verifier: streams the QPSK burst through the hosted chip and prints the recovered symbols + symbol BER (with a plot). No GNU Radio needed. |

## Headless check (no GNU Radio GUI)

**1. Host the chip** — open the pre-built receiver design directly (from the repo
root, `placekyt/`):

```bash
.venv/bin/python placekyt/main.py examples/qpsk_modem/qpsk_modem.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** (port **58950**). Leave
placeKYT running.

**2. Drive it** — stream the burst through the hosted chip and report the BER:

```bash
.venv/bin/python examples/qpsk_modem/batch_check.py --port 58950
# random QPSK -> RRC 2 sps + carrier + timing offset -> chip -> recovered symbols
# prints:  Symbol BER = 0.0000  (0 errors / N symbols, best rot=R*90deg, lag=L)
```

Expect **BER 0** after the loops lock. `--no-plot` prints stats only.

## The GRC-server path (live GUI)

The `.grc` imports into placeKYT today: **File → Import GNURadio Flowgraph…** →
`qpsk_modem.grc` auto-places the four blocks and bus-routes all nine nets. To also
**drive** it from `gnuradio-companion` (Run as GNURadio Server in placeKYT, then ▶
Run the flowgraph), the Kyttar out-of-tree GNU Radio module needs the QPSK block
bindings installed. The importer maps these flowgraph blocks by id + params:

| GRC block id | placeKYT block | key params |
|--------------|----------------|-----------|
| `kyttar_complex_rrc_matched_filter` | ComplexRRCMatchedFilterBlock | `span=8`, `decimation=1` |
| `kyttar_costas_loop` | ComplexCostasLoopBlock | **`order=4`** (QPSK) |
| `kyttar_gardner_ted` | GardnerTimingRecovery | **`complex=True`** |
| `kyttar_qpsk_slicer` | QPSKSlicerBlock | — |

> **GUI-companion follow-up.** Opening the `.grc` in `gnuradio-companion` (not
> placeKYT) additionally requires the OOT `.block.yml` + `make` bindings for the
> `order` param on `kyttar_costas_loop`, the `complex` param on `kyttar_gardner_ted`,
> and a new `kyttar_qpsk_slicer` block — a small addition to `gr-kyttar/grc/`. The
> **placeKYT import + headless `batch_check.py` paths work today**; the companion GUI
> bindings are the remaining follow-up. The burst generator
> `gr-kyttar/python/kyttar/qpsk_demo_stim.py` is already in place (imported by the
> `.grc`).

## Acceptance test

The programmatic acceptance test — the four blocks placed via the controller,
auto-placed + bus-routed + built, then a full-scale RRC QPSK burst (160 symbols,
carrier offset 0.008 cyc/sample, timing offset 0.45 samples) driven through simKYT
and recovered at **BER 0** — lives at
[`placekyt/tests/test_qpsk_modem_ber.py`](../../placekyt/tests/test_qpsk_modem_ber.py)
and runs in the suite (`test_qpsk_rx_ber_zero`, plus the GRC-import gate
`test_qpsk_grc_ber_zero`).
