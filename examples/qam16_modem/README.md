<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Coherent 16-QAM receiver (decision-directed Costas)

A coherent **16-QAM** receiver running on the Kyttar cell array: a decision-directed
complex **Costas loop** recovers the carrier, then a **16-QAM hard-decision slicer**
emits the 4-bit symbol index. The input is a stream of GNU-Radio
`digital.constellation_16qam()` symbols (**4 bits/symbol**) carrying a carrier frequency
offset; the chip recovers the symbols at **BER 0** once the loop locks.

```
RX:  16-QAM I/Q ──▶ QAM16ComplexCostasLoop (DD carrier recovery) ──▶ QAM16Slicer ──▶ 4-bit symbols
```

16-QAM is the next step up from the [QPSK modem](../qpsk_modem/) (4 bits on a 16-point
square constellation vs. 2 bits on a QPSK circle). Because 16-QAM is **not**
constant-modulus, the order-4 (QPSK) and sign (BPSK) Costas phase detectors fail — this
runs a **decision-directed** loop: derotate, slice each axis to the nearest 4-PAM grid
level, and form the phase error from the decision (`err = Im{y·conj(decision)}`), the
standard `digital.constellation_receiver_cb(constellation_16qam())` carrier-recovery
path. Like QPSK it keeps a 90° 4-fold phase ambiguity, so the BER check tries the four
constellation rotations.

> **Open the `.kyt` — don't import the `.grc`.** Like the [FSK4 modem](../fsk4_modem/)
> and [SSB Weaver](../ssb_weaver/), this is a dense hand-placed design. The
> `QAM16ComplexCostasLoop` is a 10-cell feedback block that **must sit at the top-left
> corner** so its carrier-NCO landing cell abuts the `x16_in` port (else the injected
> samples never reach the loop and it can't lock), and its recovered-I/Q tap plus the
> 3-cell slicer congest the auto-router from a fresh import. The shipped
> `qam16_modem.kyt` is a DRC-clean placement + routing of exactly this receiver — so
> **open the `.kyt`** to host the chip. Importing the `.grc` will place the blocks
> wrong and the loop won't lock.

## The chain

| GRC block (`kyttar_qam16_*`)   | placeKYT block                 | What it does |
|--------------------------------|--------------------------------|--------------|
| `kyttar_qam16_costas_loop`     | `QAM16ComplexCostasLoopBlock`  | Decision-directed carrier recovery — recovers (I, Q) from a carrier-offset 16-QAM stream. |
| `kyttar_qam16_slicer`          | `QAM16SlicerBlock`             | Hard-decision demapper — recovered (I, Q) → the 4-bit `constellation_16qam().decision_maker()` symbol index (0..15). |

Every internal handoff is a **complex `yi`/`yq` pair** (two WRITEs + one trigger) until
the slicer, which emits the 4-bit symbol. The recovered pair egresses the Costas from a
mid-block `tap` cell (dual-face, like the order-4 QPSK Costas) so the derotated
constellation reaches the slicer without disturbing the loop.

**GR equivalence.** The slicer mirrors `digital.constellation_decoder_cb(
constellation_16qam())` (verified bit-for-bit against `decision_maker()` —
`verification/tests/test_qam16_slicer.py`), and the companion
`QAM16SymbolMapperBlock` mirrors `constellation_16qam().points()` for the transmit side
(`verification/tests/test_qam16_mapper.py`). The constellation is GR's exact (idiosyncratic,
non-separable) bit→point map, re-derived from GNU Radio and pinned in those tests.

**Design point.** The DD loop locks a carrier offset up to ~±0.003 cycles/sample with the
shipped gains (`alpha_q15=0x0400`, `beta_q15=0x0020`); the default burst uses `foff=0.002`.

## Files

| File | What it is |
|------|------------|
| `qam16_modem.kyt` | The pre-placed, pre-routed receiver — **open this**. |
| `qam16_modem.grc` | The GNU Radio flowgraph (reference; the design is hand-placed — open the `.kyt`). |
| `qam16_modem.py`  | The flowgraph compiled to Python (`grcc` output). |
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

Expected: `Symbol BER = 0.0000` after the loop locks (the first ~40 symbols are the
loop warm-up and are guarded). Pass `--no-plot` for stats only; `--foff` / `--n` /
`--seed` tune the burst.

## Acceptance test

`placekyt/tests/test_qam16_modem_ber.py` builds the receiver through the real
place + route + build pipeline and drives it on simKYT, asserting **BER 0** — including
`test_shipped_kyt_recovers_ber_zero`, which loads the shipped `qam16_modem.kyt` exactly
as a user opens it.

## Key parameters

- 16-QAM symbols per burst: `n_syms = 400`
- Carrier offset: `foff = 0.002` cycles/sample (locks to ~±0.003)
- Costas loop gains: `alpha_q15 = 0x0400` (proportional), `beta_q15 = 0x0020` (integral)
