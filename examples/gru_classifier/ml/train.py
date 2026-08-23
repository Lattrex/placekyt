"""Training pipeline for the GRU modulation classifier (single + stacked).

Trains THE WAY IT INFERS: the deployed model is stateful and unresetting on
a continuous feature stream, so training runs truncated BPTT over long
streams built by concatenating labelled clips (class changes at clip
boundaries, per-timestep labels), with the hidden state carried across TBPTT
chunks and never reset inside a stream.  h=0 only at stream start -- that is
the deployed contract recorded in the weights file.

Quantization-aware: weights/biases are clamped into the Q15-friendly range
after every optimizer step, and the last --qat-epochs epochs run with
straight-through fake-quantization matching gru_reference.quantize_matrix
semantics.  Post-training the model is exported as float + Q15-integer
params in one JSON (see README.md for the schema).

Usage:
  .venv/bin/python train.py --layers 1 --out weights_single.json
  .venv/bin/python train.py --layers 2 --out weights_stacked.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

import gru_reference as ref

HERE = Path(__file__).parent

W_CLAMP = 3.9   # |weight| bound  -> quantize exponent e <= 2
B_CLAMP = 0.99  # |bias| bound    -> biases are plain Q15


def fake_quant(w: torch.Tensor) -> torch.Tensor:
    """Straight-through fake quantization matching quantize_matrix: int16
    Q15 mantissa with the minimal power-of-two exponent."""
    m = w.detach().abs().max().item()
    e = 0
    while m * 32768.0 / (1 << e) > 32767.0:
        e += 1
    s = (1 << e) / 32768.0
    wq = torch.clamp(torch.round(w.detach() / s), -32768, 32767) * s
    return w + (wq - w).detach()


class GRUCellRef(nn.Module):
    """GRU cell matching gru_reference.GRULayer.step_float exactly.
    Gate order r, z, n;  n = tanh(Wxn x + r * (Whn h) + bn);
    h' = (1-z)*n + z*h."""

    def __init__(self, I: int, H: int):
        super().__init__()
        self.H = H
        self.Wx = nn.Parameter(torch.randn(3 * H, I) * 0.4)
        self.Wh = nn.Parameter(torch.randn(3 * H, H) * 0.3)
        self.b = nn.Parameter(torch.zeros(3 * H))

    def forward(self, x, h, fq: bool = False):
        # x: (B, I), h: (B, H)
        H = self.H
        Wx = fake_quant(self.Wx) if fq else self.Wx
        Wh = fake_quant(self.Wh) if fq else self.Wh
        b = self.b
        px = x @ Wx.T
        ph = h @ Wh.T
        r = torch.sigmoid(px[:, :H] + ph[:, :H] + b[:H])
        z = torch.sigmoid(px[:, H:2 * H] + ph[:, H:2 * H] + b[H:2 * H])
        n = torch.tanh(px[:, 2 * H:] + r * ph[:, 2 * H:] + b[2 * H:])
        return (1.0 - z) * n + z * h


class GRUNet(nn.Module):
    def __init__(self, I: int, H: int, layers: int, C: int):
        super().__init__()
        self.cells = nn.ModuleList(
            [GRUCellRef(I if li == 0 else H, H) for li in range(layers)])
        self.head = nn.Linear(H, C)

    def forward(self, X, state, fq: bool = False):
        # X: (B, T, I); state: list of (B, H).  Returns (B, T, C), state.
        outs = []
        for t in range(X.shape[1]):
            inp = X[:, t]
            for li, cell in enumerate(self.cells):
                state[li] = cell(inp, state[li], fq)
                inp = state[li]
            outs.append(self.head(inp))
        return torch.stack(outs, dim=1), state

    def clamp_(self):
        with torch.no_grad():
            for cell in self.cells:
                cell.Wx.clamp_(-W_CLAMP, W_CLAMP)
                cell.Wh.clamp_(-W_CLAMP, W_CLAMP)
                cell.b.clamp_(-B_CLAMP, B_CLAMP)
            self.head.weight.clamp_(-W_CLAMP, W_CLAMP)
            self.head.bias.clamp_(-W_CLAMP, W_CLAMP)


def load_dataset(feat_names):
    cfg = json.loads((HERE / "config.json").read_text())
    d = np.load(HERE / "dataset" / "gru_dataset.npz")
    all_names = [str(s) for s in d["feature_names"]]
    fidx = [all_names.index(f) for f in feat_names]
    X = d["X"][:, :, fidx].astype(np.float32)
    return cfg, X, d["y"], d["snr"], d["split"]


def make_streams(X, y, idx, rows, rng):
    """Arrange the clips in idx into `rows` parallel streams.
    Returns Xs (rows, L*T, I), ys (rows, L*T), seg_id (rows, L*T)."""
    idx = np.array(idx)
    rng.shuffle(idx)
    L = len(idx) // rows
    idx = idx[:L * rows].reshape(rows, L)
    T = X.shape[1]
    Xs = X[idx].reshape(rows, L * T, X.shape[2])
    ys = np.repeat(y[idx], T).reshape(rows, L * T)
    seg = np.repeat(np.arange(L * rows).reshape(rows, L), T).reshape(
        rows, L * T)
    return Xs, ys, seg


def train(args):
    torch.manual_seed(1234)
    rng = np.random.default_rng(5678)
    cfg, X, y, snr, split = load_dataset(args.features)
    tr = np.where(split == 0)[0]
    va = np.where(split == 1)[0]

    net = GRUNet(len(args.features), args.hidden, args.layers, 4)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
    lossf = nn.CrossEntropyLoss()

    B, chunk = args.batch_rows, args.chunk
    for ep in range(args.epochs):
        fq = ep >= args.epochs - args.qat_epochs
        Xs, ys, _ = make_streams(X, y, tr, B, rng)
        Xt = torch.from_numpy(Xs)
        yt = torch.from_numpy(ys)
        state = [torch.zeros(B, args.hidden) for _ in range(args.layers)]
        tot, totn = 0.0, 0
        for c0 in range(0, Xt.shape[1], chunk):
            xb = Xt[:, c0:c0 + chunk]
            yb = yt[:, c0:c0 + chunk]
            logits, state = net(xb, state, fq)
            loss = lossf(logits.reshape(-1, 4), yb.reshape(-1))
            opt.zero_grad()
            loss.backward()
            opt.step()
            net.clamp_()
            state = [s.detach() for s in state]
            tot += loss.item() * yb.numel()
            totn += yb.numel()
        # quick val (stateful stream over val clips, majority vote per clip)
        acc = eval_torch(net, X, y, va, args)
        print(f"epoch {ep:2d}{' qat' if fq else '    '} "
              f"loss {tot/totn:.4f}  val clip acc {acc:.4f}")
    return net, cfg, (X, y, snr, split)


def eval_torch(net, X, y, idx, args, burn=50):
    """Stateful stream eval in torch; per-clip majority vote after burn-in."""
    rng = np.random.default_rng(99)
    Xs, ys, seg = make_streams(X, y, idx, 8, rng)
    with torch.no_grad():
        state = [torch.zeros(8, args.hidden) for _ in range(args.layers)]
        logits, _ = net(torch.from_numpy(Xs), state)
        pred = logits.argmax(-1).numpy()
    T = X.shape[1]
    ok, n = 0, 0
    for r in range(pred.shape[0]):
        for s0 in range(0, pred.shape[1], T):
            votes = np.bincount(pred[r, s0 + burn:s0 + T], minlength=4)
            ok += int(np.argmax(votes) == ys[r, s0])
            n += 1
    return ok / n


def export(net, cfg, args, path):
    layers = []
    for cell in net.cells:
        layers.append({"Wx": cell.Wx.detach().numpy().tolist(),
                       "Wh": cell.Wh.detach().numpy().tolist(),
                       "b": cell.b.detach().numpy().tolist()})
    params = {
        "model": f"gru_classifier_{args.layers}layer",
        "classes": cfg["classes"],
        "feature_config": {
            "features": args.features,
            "window_n": cfg["window_n"],
            "sample_rate_hz": cfg["sample_rate_hz"],
            "feature_rate_hz": cfg["feature_rate_hz"],
            "input_format": "Q15 in [0,1): q = round(f * 32768)",
        },
        "layers": layers,
        "head": {"Wo": net.head.weight.detach().numpy().tolist(),
                 "bo": net.head.bias.detach().numpy().tolist()},
    }
    model = ref.GRUClassifier(params)
    model.save(path)
    return model


def eval_reference(model: ref.GRUClassifier, X, y, snr, idx, mode: str,
                   burn=50, rows=8):
    """Final numbers from the numpy reference (float or q15), stateful
    streams, per-clip majority vote.  Returns dict of metrics."""
    rng = np.random.default_rng(99)
    Xs, ys, _ = make_streams(X, y, idx, rows, rng)
    # rebuild the per-clip snr in stream order
    idx2 = np.array(idx)
    rng2 = np.random.default_rng(99)
    rng2.shuffle(idx2)
    L = len(idx2) // rows
    snr_grid = snr[idx2[:L * rows].reshape(rows, L)]
    T = X.shape[1]
    conf = np.zeros((4, 4), dtype=int)
    per_snr = {}
    step_ok, step_n = 0, 0
    for r in range(rows):
        if mode == "float":
            logits, _ = model.forward_float(Xs[r])
        else:
            accs, _ = model.forward_q15(ref.features_to_q15(Xs[r]))
            logits = accs
        pred = np.argmax(logits, axis=1)
        for si in range(L):
            s0 = si * T
            seg_pred = pred[s0 + burn:s0 + T]
            true = int(ys[r, s0])
            vote = int(np.argmax(np.bincount(seg_pred, minlength=4)))
            conf[true, vote] += 1
            step_ok += int(np.sum(seg_pred == true))
            step_n += len(seg_pred)
            s = snr_grid[r, si]
            key = "noise" if np.isnan(s) else (
                "clean" if s < 0 else f"{int(s)}dB")
            a = per_snr.setdefault(key, [0, 0])
            a[0] += int(vote == true)
            a[1] += 1
    n_clips = int(conf.sum())
    return {
        "mode": mode,
        "n_test_clips": n_clips,
        "clip_acc": float(np.trace(conf) / n_clips),
        "step_acc_after_burnin": float(step_ok / step_n),
        "burn_in_steps": burn,
        "confusion_rows_true_cols_pred": conf.tolist(),
        "per_snr_clip_acc": {k: [v[0] / v[1], v[1]]
                             for k, v in sorted(per_snr.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", type=int, default=1)
    ap.add_argument("--hidden", type=int, default=4)
    ap.add_argument("--features", nargs=2, default=["rms", "zcr"])
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--qat-epochs", type=int, default=6)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--batch-rows", type=int, default=40)
    ap.add_argument("--chunk", type=int, default=120)
    ap.add_argument("--out", default="weights_single.json")
    args = ap.parse_args()

    net, cfg, (X, y, snr, split) = train(args)
    model = export(net, cfg, args, HERE / args.out)
    te = np.where(split == 2)[0]

    print("\n== numpy reference eval (held-out test) ==")
    res = {}
    for mode in ("float", "q15"):
        m = eval_reference(model, X, y, snr, te, mode)
        res[mode] = m
        print(f"{mode:6s}: clip acc {m['clip_acc']:.4f} "
              f"({m['n_test_clips']} clips), "
              f"step acc {m['step_acc_after_burnin']:.4f}")
        print(f"        per-SNR: {m['per_snr_clip_acc']}")
    (HERE / "results").mkdir(exist_ok=True)
    out = HERE / "results" / f"metrics_{args.layers}layer.json"
    out.write_text(json.dumps(res, indent=2))
    print(f"wrote {out} and {HERE / args.out}")


if __name__ == "__main__":
    main()
