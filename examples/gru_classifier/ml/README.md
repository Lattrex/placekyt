# GRU modulation classifier — offline ML pipeline

A small GRU modulation classifier example: 4-class classification of
**SSB voice / BPSK / 4-FSK / noise** from two windowed features of a complex
baseband stream. This directory is the *offline* side: dataset generation,
training, quantization, and the numpy reference model that serves as the
verification golden for an on-chip implementation.

Everything here is numpy-only at inference time; PyTorch is needed only for
`train.py`.

## Pipeline

```
signals.py      complex-baseband clip generators (reuses the repo's modem math:
                fsk4_modem FM sensitivity pi/2 @ sps=2, bpsk_modem RRC alpha
                0.35 @ sps=4, ssb_weaver 300..2700 Hz USB voice band)
features.py     windowed feature front end (N=32 samples/window)
check_features.py   feature-discriminance gate — run FIRST
gen_dataset.py  labelled feature dataset -> dataset/gru_dataset.npz
train.py        TBPTT training (stateful streams) + QAT + export
gru_reference.py    float + Q15-integer forward pass (the golden)
```

Reproduce end to end:

```sh
.venv/bin/python check_features.py                       # discriminance gate
.venv/bin/python gen_dataset.py                          # dataset (~2 s)
.venv/bin/python train.py --layers 1 --out weights_single.json
.venv/bin/python train.py --layers 2 --out weights_stacked.json
```

## Features and windowing (the on-chip front-end contract)

Over non-overlapping windows of **N = 32** complex samples (sample rate
32 kHz → feature rate 1 kHz), computable with existing library blocks:

* `rms` — sqrt(mean |x|²) over the window
* `zcr` — zero-crossing count of Re(x) divided by N

Model input is `[rms, zcr]` per window, both in [0, 1), quantized to Q15
(`q = round(f · 32768)`). The candidate set also included `mag`
(mean |x|); the discriminance gate (`results/feature_check.json/.png`)
shows `rms+zcr` and `mag+zcr` both separate the 4 classes perfectly at the
clip level while `mag+rms` alone (no frequency feature) is slightly worse —
`rms+zcr` was chosen.

## Model

Single-layer GRU, H=4 hidden units, I=2 inputs, plus a linear 4-class
readout (argmax at inference). Gate order is **r, z, n** everywhere:

```
r_t = sigmoid(Wxr x_t + Whr h_{t-1} + br)
z_t = sigmoid(Wxz x_t + Whz h_{t-1} + bz)
n_t = tanh  (Wxn x_t + r_t * (Whn h_{t-1}) + bn)
h_t = (1 - z_t) * n_t + z_t * h_{t-1}
logits_t = Wo h_t + bo
```

A stacked 2-layer variant (same H; layer 2 input = layer 1 hidden state) is
trained by the same pipeline for a later multi-die configuration.

**State contract:** `h = 0` at stream start only; the model is stateful and
is never reset while streaming. Training matches this: TBPTT over long
streams made of concatenated clips (class changes mid-stream, per-timestep
labels), hidden state carried across chunk boundaries.

## Q15 integer semantics (mirrored bit-exactly by `gru_reference.py`)

* Activations/hidden state: Q15 int16 (`value = q/32768`).
* Each weight matrix `M`: int16 Q15 mantissa `Mq` + scale exponent `e ≥ 0`,
  `M ≈ Mq · 2^e / 32768` (minimal `e`; training clamps |w| ≤ 3.9 so e ≤ 2).
* matvec: `acc = Σ Mq·vq` (int64), then `preact = sat32((acc + 2^(14-e)) >> (15-e))`
  (round-half-up arithmetic shift back to Q15).
* Gate biases: plain Q15 ints added to the Q15 preactivation (|b| ≤ 0.99).
* sigmoid/tanh: 1024-entry Q15 LUTs over [−8, 8) / [−4, 4), bin-center
  sampled, input clamped into range (`gru_reference.make_luts`).
* Elementwise Q15 multiply: `sat16((a·b + 2^14) >> 15)`.
* Update: `h' = sat16(((32768−z)·n + z·h + 2^14) >> 15)`.
* Head: `acc_c = Σ Wo_q[c]·h_q + bo_acc[c]` (int64; one shared exponent for
  the whole head so argmax is scale-consistent); class = argmax over c.

## Weights file schema (`weights_single.json`, `weights_stacked.json`)

This JSON is the interface the on-chip block build consumes:

```jsonc
{
  "format_version": 1,
  "model": "gru_classifier_1layer",
  "classes": ["ssb", "bpsk", "fsk4", "noise"],
  "feature_config": {
    "features": ["rms", "zcr"],       // input order
    "window_n": 32,
    "sample_rate_hz": 32000,
    "feature_rate_hz": 1000,
    "input_format": "Q15 in [0,1): q = round(f * 32768)"
  },
  "state_contract": "h=0 at stream start only; never reset while streaming",
  "layers": [                          // one entry per GRU layer
    {
      "Wx": [[...]],                   // float (3H x I), rows [r; z; n]
      "Wh": [[...]],                   // float (3H x H), rows [r; z; n]
      "b":  [...],                     // float (3H)
      "quant": {
        "Wx": {"r": {"q": [[int16]], "e": 0}, "z": {...}, "n": {...}},
        "Wh": {"r": {"q": [[int16]], "e": 0}, "z": {...}, "n": {...}},
        "b":  {"r": [int Q15], "z": [...], "n": [...]}
      }
    }
  ],
  "head": {
    "Wo": [[...]], "bo": [...],        // float (4 x H), (4)
    "quant": {"Wo_q": [[int16]], "e": 0, "bo_acc": [int64]}
                                       // bo_acc = round(bo * 2^30 / 2^e)
  },
  "lut": {"bits": 10, "sigmoid_range": 8.0, "tanh_range": 4.0}
}
```

Load and run with:

```python
from gru_reference import GRUClassifier, features_to_q15
m = GRUClassifier.load("weights_single.json")
logits, state = m.forward_float(X, state)          # float golden
accs, qstate = m.forward_q15(features_to_q15(X), qstate)  # integer golden
cls = accs[-1].argmax()
```

## Dataset

`gen_dataset.py` + `config.json`: 600 clips/class (400 train / 80 val /
120 test), 300 feature steps per clip (0.3 s), SNR ∈ {clean, 20, 10, 5} dB,
random frequency offset ±100 Hz, random gain 0.25–0.7 (the noise class is
generated at matched total power so absolute level cannot cheat).
Deterministic: each clip's RNG is `default_rng([seed, class_idx, clip_idx])`.
The `.npz` is not checked in — regenerate it (~2 s).

Results: see [RESULTS.md](RESULTS.md).
