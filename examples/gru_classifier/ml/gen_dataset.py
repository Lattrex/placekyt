"""Dataset generator for the GRU modulation classifier.

Reads config.json, generates labelled clips for the 4 classes through the
channel model, computes ALL candidate windowed features (mag/rms/zcr -- the
training pair is selected by config "features"), and writes
dataset/gru_dataset.npz with deterministic per-clip seeding:

  X       float32 (n_clips, T, 3)   features in candidate order [mag,rms,zcr]
  y       int64   (n_clips,)        class index into config "classes"
  snr     float32 (n_clips,)        SNR in dB, -1 for clean, nan for noise cls
  split   uint8   (n_clips,)        0=train 1=val 2=test
  feature_names                     ["mag","rms","zcr"]

Each clip's RNG is np.random.default_rng([seed, class_idx, clip_idx]) so any
clip regenerates identically in isolation.  Regenerate with:

  .venv/bin/python gen_dataset.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from features import ALL_FEATURES, feature_matrix
from signals import make_clip

HERE = Path(__file__).parent


def main():
    cfg = json.loads((HERE / "config.json").read_text())
    classes = cfg["classes"]
    T = cfg["clip_feature_steps"]
    wn = cfg["window_n"]
    n_samp = T * wn
    per = cfg["clips_per_class"]
    sp = cfg["split_per_class"]
    assert sp["train"] + sp["val"] + sp["test"] == per
    snrs = [None if s == "clean" else float(s) for s in cfg["snrs_db"]]
    g0, g1 = cfg["gain_range"]
    f0, f1 = cfg["freq_offset_hz"]
    seed = cfg["seed"]

    X = np.zeros((per * len(classes), T, len(ALL_FEATURES)), dtype=np.float32)
    y = np.zeros(per * len(classes), dtype=np.int64)
    snr_out = np.zeros(per * len(classes), dtype=np.float32)
    split = np.zeros(per * len(classes), dtype=np.uint8)

    t0 = time.time()
    i = 0
    for ci, cls in enumerate(classes):
        for k in range(per):
            rng = np.random.default_rng([seed, ci, k])
            snr = snrs[k % len(snrs)]
            gain = rng.uniform(g0, g1)
            foff = rng.uniform(f0, f1)
            x = make_clip(cls, n_samp, rng, snr, gain, foff)
            X[i] = feature_matrix(x, ALL_FEATURES, wn)
            y[i] = ci
            snr_out[i] = (np.nan if cls == "noise"
                          else (-1.0 if snr is None else snr))
            # deterministic split: SNR levels cycle with k, so contiguous
            # ranges of k stay SNR-balanced in every split
            split[i] = (0 if k < sp["train"]
                        else 1 if k < sp["train"] + sp["val"] else 2)
            i += 1
        print(f"{cls}: {per} clips done ({time.time()-t0:.1f}s)")

    outdir = HERE / "dataset"
    outdir.mkdir(exist_ok=True)
    np.savez_compressed(outdir / "gru_dataset.npz", X=X, y=y, snr=snr_out,
                        split=split,
                        feature_names=np.array(ALL_FEATURES))
    print(f"wrote {outdir/'gru_dataset.npz'}  X={X.shape} "
          f"({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
