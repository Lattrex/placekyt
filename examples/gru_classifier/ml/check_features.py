"""Feature-discriminance gate for the GRU modulation classifier.

Run FIRST, before any training: verifies that a 2-feature subset of the
candidate per-window features (mag, rms, zcr -- see features.py) actually
separates SSB / BPSK / 4-FSK / noise, across the SNR + frequency-offset
ranges the dataset will use.

Method: for every 2-feature pair, summarise each clip as
[mean(f1), std(f1), mean(f2), std(f2)] and fit a trivial multinomial
logistic baseline (numpy gradient descent) on a train split; report held-out
accuracy + confusion.  Also dumps per-window and per-clip scatter plots.

Usage:  .venv/bin/python check_features.py  [--out results]
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from features import ALL_FEATURES, WINDOW_N, compute_features
from signals import CLASS_NAMES, make_clip

SNRS = [None, 20.0, 10.0, 5.0]  # None = clean
CLIP_STEPS = 300                # feature timesteps per clip
CLIPS_PER_CLASS = 96            # per class, spread over SNRS
SEED = 20260823


def gen_check_set(rng: np.random.Generator):
    n_samp = CLIP_STEPS * WINDOW_N
    rows, labels, snrs = [], [], []
    for ci, cls in enumerate(CLASS_NAMES):
        for k in range(CLIPS_PER_CLASS):
            snr = SNRS[k % len(SNRS)]
            gain = rng.uniform(0.25, 0.7)
            foff = rng.uniform(-100.0, 100.0)
            x = make_clip(cls, n_samp, rng, snr, gain, foff)
            rows.append(compute_features(x))
            labels.append(ci)
            snrs.append(-1.0 if snr is None else snr)
    return rows, np.array(labels), np.array(snrs)


def softmax_fit(X, y, n_cls=4, iters=3000, lr=0.5, seed=0):
    """Trivial multinomial logistic regression, full-batch GD, numpy."""
    rng = np.random.default_rng(seed)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xn = (X - mu) / sd
    Xb = np.hstack([Xn, np.ones((len(Xn), 1))])
    W = 0.01 * rng.standard_normal((Xb.shape[1], n_cls))
    Y = np.eye(n_cls)[y]
    for _ in range(iters):
        Z = Xb @ W
        Z -= Z.max(1, keepdims=True)
        P = np.exp(Z)
        P /= P.sum(1, keepdims=True)
        W -= lr * (Xb.T @ (P - Y)) / len(Xb)
    return (mu, sd, W)


def softmax_predict(model, X):
    mu, sd, W = model
    Xb = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    return np.argmax(Xb @ W, axis=1)


def clip_summary(feats: dict, pair: tuple[str, str]) -> np.ndarray:
    v = []
    for name in pair:
        f = feats[name]
        v += [np.mean(f), np.std(f)]
    return np.array(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    out = Path(__file__).parent / args.out
    out.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SEED)
    rows, labels, snrs = gen_check_set(rng)

    # train/test split (stratified by construction: interleave)
    idx = np.arange(len(rows))
    rng.shuffle(idx)
    n_test = len(idx) // 3
    te, tr = idx[:n_test], idx[n_test:]

    report = {"window_n": WINDOW_N, "clip_steps": CLIP_STEPS,
              "clips_per_class": CLIPS_PER_CLASS,
              "snrs_db": ["clean" if s is None else s for s in SNRS],
              "pairs": {}}

    pairs = list(itertools.combinations(ALL_FEATURES, 2))
    fig, axes = plt.subplots(2, len(pairs), figsize=(5 * len(pairs), 9))
    colors = ["tab:blue", "tab:orange", "tab:green", "tab:red"]

    for pi, pair in enumerate(pairs):
        X = np.stack([clip_summary(rows[i], pair) for i in range(len(rows))])
        model = softmax_fit(X[tr], labels[tr])
        pred = softmax_predict(model, X[te])
        acc = float(np.mean(pred == labels[te]))
        conf = np.zeros((4, 4), dtype=int)
        for t, p in zip(labels[te], pred):
            conf[t, p] += 1
        report["pairs"]["+".join(pair)] = {
            "clip_level_logistic_test_acc": acc,
            "n_test_clips": int(n_test),
            "confusion_rows_true_cols_pred": conf.tolist(),
        }

        # scatter: per-window (row 0) and per-clip means (row 1)
        ax = axes[0, pi]
        for ci, cname in enumerate(CLASS_NAMES):
            sel = [i for i in range(len(rows)) if labels[i] == ci][:12]
            f1 = np.concatenate([rows[i][pair[0]] for i in sel])
            f2 = np.concatenate([rows[i][pair[1]] for i in sel])
            ax.scatter(f1, f2, s=2, alpha=0.25, color=colors[ci], label=cname)
        ax.set_xlabel(pair[0]); ax.set_ylabel(pair[1])
        ax.set_title(f"per-window: {pair[0]} vs {pair[1]}")
        ax.legend(markerscale=4)
        ax = axes[1, pi]
        for ci, cname in enumerate(CLASS_NAMES):
            sel = [i for i in range(len(rows)) if labels[i] == ci]
            m1 = [np.mean(rows[i][pair[0]]) for i in sel]
            s2 = [np.std(rows[i][pair[1]]) for i in sel]
            ax.scatter(m1, s2, s=8, alpha=0.6, color=colors[ci], label=cname)
        ax.set_xlabel(f"mean {pair[0]}"); ax.set_ylabel(f"std {pair[1]}")
        ax.set_title(f"per-clip: mean {pair[0]} vs std {pair[1]}"
                     f"  (logistic acc {acc:.3f})")
    fig.tight_layout()
    fig.savefig(out / "feature_check.png", dpi=110)

    best = max(report["pairs"], key=lambda k:
               report["pairs"][k]["clip_level_logistic_test_acc"])
    report["best_pair"] = best
    with open(out / "feature_check.json", "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))
    print(f"\nBEST PAIR: {best} "
          f"(acc {report['pairs'][best]['clip_level_logistic_test_acc']:.3f})")


if __name__ == "__main__":
    main()
