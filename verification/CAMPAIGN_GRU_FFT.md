<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Campaign: GRU classifier + streaming FFT + CSS modem

**Loaded into the manifest 2026-08-23.** 15 blocks, `planned`, tier-ordered. Run with the
normal factory loop (`verification/FACTORY.md`). All design constraints are already in each
block's manifest `notes`, so `factory_dispatch.py` injects them into every builder prompt.
**No extra prompt engineering is needed per block.**

---

## Run order

Tier order is already correct. Just loop `factory_queue.py claim` and dispatch.

**Two HARD GATES that override tier order:**

1. **`ComplexDelayLineBlock` (B1) must go before any FFT block.** It decides whether a
   multi-cell on-fabric delay line works. If it can only be done with the host-side SRAM
   panel, say so in the lessons log, because it narrows the FFT claim from "on the chip"
   to "delay line in host SRAM."
2. **`FFT16Block` (B4) is a STOP GATE.** If N=16 does not verify against `numpy.fft.fft`,
   do NOT attempt FFT64Block or FFT128Block. Set both to `needs_human`, write the exact
   wall in the lessons log, and report. An honest negative here is a publishable result;
   burning tokens on N=128 after N=16 failed is not.

Suggested sequence:

```
tier 1 (cheap, warm up the flow):
  ZeroCrossingRateBlock, R2ButterflyBlock, TwiddleMultiplyBlock, ChirpSymbolMapperBlock

tier 2 (the risky numerics, do SigmoidBlock FIRST):
  SigmoidBlock, TanhBlock, DotProductMACBlock, ComplexDelayLineBlock,
  ChirpGeneratorBlock, BinArgmaxBlock

tier 3 (composites):
  FFT16Block   <-- STOP GATE, evaluate before continuing
  GRUCellBlock <-- the classifier deliverable, highest value
  FFT64Block, FFT128Block, ChirpSyncBlock
```

**If token budget is limited, priority order is: SigmoidBlock, TanhBlock,
DotProductMACBlock, GRUCellBlock.** Those 4 are the classifier deliverable. Everything else is the
follow-on campaign.

---

## Numeric spikes (do these in numpy BEFORE dispatching the block)

Half a day each. Dispatching a block whose numerics are unpinned is how these quarantine.

| Spike | Before which block | Output needed |
|---|---|---|
| S1 sigmoid/tanh approximation | SigmoidBlock | table size + interpolation form + symmetry folding + max error vs float |
| S2 GRU per-gate Q15 scaling | DotProductMACBlock | per-gate scale factors; the fold-prescale-into-the-nonlinearity mapping |
| S3 FFT twiddle + per-stage scaling | FFT16Block | scale-by-2-per-stage vs block-floating-point; SNR floor vs `numpy.fft` |

**S2's key trick:** a sigmoid/tanh approximation absorbs an arbitrary fixed INPUT SCALE for
free (it only shifts the table/poly domain). So scale the MAC accumulation into safe
headroom and let the nonlinearity eat the compensation. Costs zero instructions. Both
SigmoidBlock and TanhBlock take an `in_scale` param for exactly this.

---

## The ML track (parallel, NOT part of the factory loop)

`GRUCellBlock` needs trained weights and a float golden before it can be dispatched.

1. **Task:** 4-class classification, SSB vs BPSK vs 4-FSK vs noise.
2. **Dataset:** generate with GNU Radio flowgraphs (reuses the shipped modem examples).
3. **Features:** magnitude/RMS + zero-crossing rate. **Confirm the 2-feature set actually
   separates the 4 classes BEFORE training.**
4. **Train:** PyTorch, H=4, I=2, 4-class readout head trained JOINTLY (it is the model's
   final Linear layer).
5. **Train the way you infer:** the fabric runs stateful and unresetting on a continuous
   stream. Use truncated-BPTT with carried state, or an explicit periodic h-reset applied
   identically in training AND on the fabric. Do NOT train windowed with h0=0 and deploy
   stateful.
6. **Quantization-aware** (weight clipping or QAT-lite), not pure post-training rounding:
   quantization error in `h` feeds back and compounds.
7. **Emit `gru_weights.npz`** in the layout `GRUCellBlock`'s `weights_file` param expects.
8. **Write the float golden** as a `gr-kyttar` Python block. It must exist before the
   GRUCellBlock dispatch, since it is what the factory gate compares against.
9. Also train the STACKED (2-layer) config now if the multi-chip configuration is wanted, not later.

---

## What to measure and record

Beyond the normal factory cost record:

- Cells used per block, and for the composed classifier.
- Throughput. **Report FEATURE rate AND the I/Q rate the full chain sustains.** The GRU
  runs at feature rate, not sample rate; saying so converts an apparent weakness into
  "the fabric spends its cycles where the workload is."
- Power via `chip.performance_report()` `total_power_mw`, same method as
  `verification/simplex_rates.py`.
- Classification accuracy vs the float reference, AND quantized-vs-float divergence as a
  function of SEQUENCE LENGTH (recurrent error compounds).
- For the FFT: SNR vs `numpy.fft`, and the scaling schedule actually used.

---

## Reporting rules (non-negotiable)

- Everything runs on the **SHIPPING v0.11 cell**, exactly as it exists. Never modify
  the ISA or the cell parameters.
- Report the ugly numbers. A big cell count or a low rate is fine and expected; it is the
  measured floor that makes any future-improvement estimate credible.
- Provenance sentence for any power/throughput figure: workload activity from the
  SDF-annotated run x SPICE-characterized per-op energy, on a known-good PDK, pre-silicon,
  typical corner. Never just "measured."
- Quarantine honestly after 2 real attempts. A written wall beats a faked pass — and a
  well-written wall is what lets a later attempt clear it. Gardner was quarantined twice
  and then SHIPPED on the third attempt (BER 0, bit-exact); each quarantine record named
  the failure precisely enough for the next attempt to disprove it and move on.
