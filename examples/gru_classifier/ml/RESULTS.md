# Results — GRU modulation classifier (offline pipeline)

All numbers below were produced by the checked-in scripts on the checked-in
config (seed 20260817 dataset, seed 20260823 feature gate); the final
accuracy numbers come from the **numpy reference model**
(`gru_reference.py`), not the torch graph.

## Feature-discriminance gate (`check_features.py`)

Candidate per-window features: `mag` (mean |x|), `rms`, `zcr`
(zero-crossing rate of Re(x)); window N = 32 samples @ 32 kHz. 96
clips/class over {clean, 20, 10, 5} dB, ±100 Hz offsets, gain 0.25–0.7.
A trivial multinomial-logistic baseline on per-clip summaries
[mean f1, std f1, mean f2, std f2], 128 held-out clips:

| pair | held-out clip accuracy |
|---|---|
| `mag+rms` | 0.992 |
| `mag+zcr` | **1.000** |
| `rms+zcr` | **1.000** |

Two features are sufficient — but only as a *time series*: single windows
overlap heavily between classes (see `results/feature_check.png`, top row);
the separation lives in the temporal statistics (bottom row): noise sits in
a tight high-ZCR band, 4-FSK in a tight mid-ZCR band, BPSK shows high ZCR
variance (RRC envelope nulls), SSB shows large bursty RMS variance. That is
exactly what a stateful GRU can accumulate. **Chosen pair: `rms + zcr`**
(windowed RMS is the cleaner front-end primitive; `mag+zcr` would also
work).

## Train/test protocol

* Dataset: 600 clips/class (2400 total), 300 feature steps/clip (0.3 s),
  split 400 train / 80 val / 120 test per class, SNR levels cycled so every
  split is SNR-balanced. Held-out test = **480 clips** never seen in
  training or model selection.
* Training: truncated BPTT (chunk 120) over 40 parallel streams of
  concatenated clips with per-timestep labels; hidden state carried across
  chunks, h=0 only at stream start — identical to the deployed contract.
  100 epochs Adam (lr 3e-3, wd 1e-4), |w| ≤ 3.9 / |b| ≤ 0.99 clamps every
  step, last 12 epochs with straight-through fake-quantization of weights.
* Evaluation: stateful stream over concatenated test clips (8 streams,
  never reset); per-clip decision = majority vote of per-step argmax over
  steps 50..299 of the clip's segment (50-step burn-in after each class
  transition); per-step accuracy over the same region.

## Accuracy (held-out test, 480 clips, numpy reference)

| model | mode | clip acc | per-step acc |
|---|---|---|---|
| single (1×GRU H=4) | float | 0.9563 | 0.9327 |
| single (1×GRU H=4) | **Q15 int** | **0.9583** | 0.9326 |
| stacked (2×GRU H=4) | float | 1.0000 | 0.9922 |
| stacked (2×GRU H=4) | **Q15 int** | **1.0000** | 0.9923 |

Quantization does not collapse: Q15 matches float within noise (the single
model is marginally *better* quantized). Per-step float-vs-Q15 argmax
agreement: 99.86% (single) / 99.98% (stacked) over 36 000 steps. The Q15
forward is bit-deterministic (verified: repeated runs identical).

### Per-SNR clip accuracy (Q15)

| SNR | single | stacked | n |
|---|---|---|---|
| clean | 0.922 | 1.000 | 90 |
| 20 dB | 1.000 | 1.000 | 90 |
| 10 dB | 0.956 | 1.000 | 90 |
| 5 dB  | 0.900 | 1.000 | 90 |
| noise class | 1.000 | 1.000 | 120 |

### Confusion (Q15, rows = true, cols = predicted; order ssb/bpsk/fsk4/noise)

single:
```
ssb   [120   0   0   0]
bpsk  [  5 115   0   0]
fsk4  [  2  13 105   0]
noise [  0   0   0 120]
```
stacked: perfectly diagonal (120 per class).

The single model's residual error is almost entirely 4-FSK → BPSK
(13/120) plus a few → SSB; H=4 with a single layer is tight for tracking
mean *and* variance of two features. The stacked variant separates the
test set completely.

## Reproduce

```sh
.venv/bin/python check_features.py
.venv/bin/python gen_dataset.py
.venv/bin/python train.py --layers 1 --epochs 100 --qat-epochs 12 --out weights_single.json
.venv/bin/python train.py --layers 2 --epochs 100 --qat-epochs 12 --out weights_stacked.json
```

Raw metric dumps: `results/metrics_1layer.json`, `results/metrics_2layer.json`,
`results/feature_check.json`.
