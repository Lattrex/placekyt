# SPDX-License-Identifier: GPL-3.0-or-later
"""CHIP-EXACT integer reference for the GRU modulation classifier.

This is the bit-exact golden for ``GRUCellBlock`` (the on-chip composite).
It differs from ``gru_reference.py`` (the training-side Q15 reference) in
that every operation is the EXACT arithmetic of the landed cell programs:

* Gate/head rows are DotProductMACBlock rows: coefficients requantized by
  the landed ``scale_schedule`` (headroom shift S + post-rounding guard),
  bias PRELOADED, then a TRUNCATING ``MULQ``/accumulate walk in address
  order — NOT the round-half-up ``matvec_q15`` of the training reference.
  Each gate uses ONE COMMON scale S (max over its 4 rows, guard re-checked
  per row at the common S); the head uses ONE COMMON scale across all 4
  class rows (per-row head scales would corrupt the argmax).
* Activations are the landed 17-entry table + interpolation engine
  (``activation_blocks.activation_ref_word``), consuming the RAW
  (2^-S-scaled) row word with the scale folded into the activation's
  ``dshift`` immediates: sigmoid ``dshift = S_gate - 3``, tanh
  ``dshift = S_n - 2`` (zero-instruction scale restore).
* The blend is ``h' = sat(MULQ(0x7FFF - z, n) + MULQ(z, h))`` — both
  partials in range, ONE saturating add (the pinned form; ``n + z*(h-n)``
  overflows and is NOT used on-chip).
* The class decision is the BinArgmax signed compare (first occurrence
  wins) over the 4 RAW head accumulator words.

Weights come from the training pipeline's JSON (``weights_single.json``,
schema in README.md): the on-chip coefficients are derived from the FILE'S
quantized ``{q, e}`` mantissas (dequantized exactly, then requantized by the
scale schedule), so the chip constants are a deterministic function of the
shipped weights file.

MEASURED (held-out 480-clip test, seeded dataset regenerated,
``weights_single.json``; recorded training-reference baseline 0.9583):

* this chip-exact model:        clip acc 0.9458, step acc 0.9139
  (−1.25 points vs the baseline — inside the 2-point activation budget).
* activation-swap-only control (training-ref MACs + 17-entry activations):
  clip acc 0.9438 — the residual is dominated by the activation tables,
  not the MAC quantization (weight-grid sweep: insensitive to 10-bit
  mantissas).
* the ``Whn.(r⊙h)`` mis-ordering (reset gate applied BEFORE the matmul)
  collapses to clip acc 0.57 — the gate-ordering mutation this golden's
  test suite must prove fatal.

Run as a script to evaluate the chip model on the held-out 480-clip test
set (regenerate the dataset first with ``gen_dataset.py``):

    .venv/bin/python gru_reference_chip.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO / "runtime" / "python"))

from gr_kyttar.placement.blocks.activation_blocks import (  # noqa: E402
    SIGMOID_TABLE_Q15, TANH_TABLE_Q15, activation_ref_word)
from gr_kyttar.placement.blocks.dot_product_mac_block import (  # noqa: E402
    scale_schedule)

GATES = ("r", "z", "n")
SIG_K = 3   # sigmoid canonical half-domain 2^3 = 8
TANH_K = 2  # tanh canonical half-domain 2^2 = 4


def _s16(v: int) -> int:
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _mulq(a: int, b: int) -> int:
    """Truncating Q15 product: (a*b) >> 15, arithmetic (the cell MULQ)."""
    return (_s16(a) * _s16(b)) >> 15


def _sat_add16(a: int, b: int) -> int:
    """16-bit saturating add (ADD + V-flag rail pin: 0x7FFF / -0x8000)."""
    t = _s16(a) + _s16(b)
    return 32767 if t > 32767 else (-32768 if t < -32768 else t)


def _quantize_at(vals, S):
    """round(v * 2^-S * 32768) clipped to int16 — the stored-word rule."""
    return [max(-32768, min(32767, round(float(v) * (2.0 ** -S) * 32768.0)))
            for v in vals]


def common_scale_rows(rows):
    """One COMMON headroom scale for a set of MAC rows.

    ``rows`` is a list of ``(coefficients, bias)`` float pairs. Returns
    ``(S, [(coeff_q, bias_q), ...])``: S = max of the landed per-row
    ``scale_schedule`` S over the set, then every row requantized at that
    common S with the POST-ROUNDING GUARD re-verified per row (S bumps until
    every row satisfies ``sum|q| <= 32767`` — the no-wrap invariant).
    """
    S = 0
    for c, b in rows:
        S = max(S, scale_schedule(c, b)[0])
    while True:
        out = []
        ok = True
        for c, b in rows:
            qs = _quantize_at(list(c) + [b], S)
            if sum(abs(q) for q in qs) > 32767:
                ok = False
                break
            out.append((qs[:-1], qs[-1]))
        if ok:
            return S, out
        S += 1


def mac_row_words(coeff_q, bias_q, xs):
    """The DotProductMAC accumulation, bit-exact: bias preload + truncating
    MULQ walk in address order; 16-bit accumulator (guard ⇒ never wraps).
    Returns the RAW emitted word (signed int16 value, = y / 2^S)."""
    acc = int(bias_q)
    for c, x in zip(coeff_q, xs):
        acc = _s16(acc + ((int(c) * _s16(x)) >> 15))
    return acc


class GRUChipModel:
    """Bit-exact model of the GRUCellBlock datapath (H=4, I=2, 4 classes).

    Loads the training pipeline's weights JSON and derives the exact on-chip
    constants (per-gate common-scale MAC rows + activation dshifts + common-
    scale head rows). ``step(x_q, h)`` advances one timestep and returns
    ``(h_next, head_words, cls)`` — everything the two-level gate compares.
    """

    def __init__(self, params: dict):
        lp = params["layers"][0]["quant"]
        H = len(lp["b"]["r"])
        I = len(lp["Wx"]["r"]["q"][0])
        if H != 4 or I != 2:
            raise ValueError(f"GRUCellBlock is pinned at H=4, I=2; file has "
                             f"H={H}, I={I}")
        self.H, self.I = H, I

        def deq_mat(m):
            q = np.asarray(m["q"], dtype=np.float64)
            return q * (2.0 ** int(m["e"])) / 32768.0

        # r/z gates: full 7-term rows [Wx_g[i,:] (I), Wh_g[i,:] (H)] + b_g[i]
        # (the reference computes r/z = sigmoid(Wx.x + Wh.h + b) — one row).
        self.gate_S = {}
        self.gate_rows = {}
        for g in ("r", "z"):
            Wx = deq_mat(lp["Wx"][g])
            Wh = deq_mat(lp["Wh"][g])
            b = np.asarray(lp["b"][g], dtype=np.float64) / 32768.0
            rows = [(list(Wx[i]) + list(Wh[i]), float(b[i]))
                    for i in range(H)]
            self.gate_S[g], self.gate_rows[g] = common_scale_rows(rows)

        # n gate: the reference applies the reset gate AFTER the matmul —
        # n = tanh(Wxn.x + r ⊙ (Whn.h) + bn) (the standard/PyTorch GRU form;
        # Whn.(r⊙h) is a DIFFERENT function and measurably collapses
        # accuracy). On-chip split per hidden unit: a 4-term u-row (Whn.h,
        # no bias), a 2-term x-row (Wxn.x with bias preload), then
        # word_n = MULQ(r, u) + xw. ALL THREE quantized at ONE common S_n
        # (the full 7-term row schedule) so the combine needs no shifts and
        # the no-wrap guard covers the summed parts:
        #   |MULQ(r,u)| + |xw| <= sum|Whn_q| + sum|Wxn_q| + |bn_q| <= 32767.
        Wx = deq_mat(lp["Wx"]["n"])
        Wh = deq_mat(lp["Wh"]["n"])
        b = np.asarray(lp["b"]["n"], dtype=np.float64) / 32768.0
        full = [(list(Wx[i]) + list(Wh[i]), float(b[i])) for i in range(H)]
        S = common_scale_rows(full)[0]
        while True:
            u_rows = [( _quantize_at(list(Wh[i]), S), 0) for i in range(H)]
            x_rows = [( _quantize_at(list(Wx[i]), S),
                        _quantize_at([b[i]], S)[0]) for i in range(H)]
            if all(sum(abs(q) for q in u_rows[i][0])
                   + sum(abs(q) for q in x_rows[i][0])
                   + abs(x_rows[i][1]) <= 32767 for i in range(H)):
                break
            S += 1
        self.gate_S["n"] = S
        self.n_u_rows = u_rows       # 4-term Whn rows, zero bias
        self.n_x_rows = x_rows       # 2-term Wxn rows, bn bias preload

        self.dshift_r = self.gate_S["r"] - SIG_K
        self.dshift_z = self.gate_S["z"] - SIG_K
        self.dshift_n = self.gate_S["n"] - TANH_K

        # head: ONE COMMON scale across all class rows (argmax consistency)
        hq = params["head"]["quant"]
        eo = int(hq["e"])
        Wo = np.asarray(hq["Wo_q"], dtype=np.float64) * (2.0 ** eo) / 32768.0
        bo = np.asarray(hq["bo_acc"], dtype=np.float64) * (2.0 ** eo) / (
            1 << 30)
        rows = [(list(Wo[j]), float(bo[j])) for j in range(Wo.shape[0])]
        self.head_S, self.head_rows = common_scale_rows(rows)
        self.n_classes = Wo.shape[0]

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    # ------------------------------------------------------------------ step
    def step(self, x_q, h):
        """One timestep. ``x_q``: I signed Q15 words; ``h``: H signed Q15
        words. Returns ``(h_next, head_words, cls)``."""
        xin = [_s16(v) for v in x_q]
        rz_in = xin + list(h)
        r = [_s16(activation_ref_word(
                mac_row_words(cq, bq, rz_in) & 0xFFFF,
                SIGMOID_TABLE_Q15, 0x8000, self.dshift_r))
             for cq, bq in self.gate_rows["r"]]
        z = [_s16(activation_ref_word(
                mac_row_words(cq, bq, rz_in) & 0xFFFF,
                SIGMOID_TABLE_Q15, 0x8000, self.dshift_z))
             for cq, bq in self.gate_rows["z"]]
        # n gate: u = Whn.h row, xw = Wxn.x + bn row, word = MULQ(r,u) + xw
        n = []
        for i in range(self.H):
            u = mac_row_words(*self.n_u_rows[i], list(h))
            xw = mac_row_words(*self.n_x_rows[i], xin)
            word = _s16(_mulq(r[i], u) + xw)   # guard ⇒ no wrap
            n.append(_s16(activation_ref_word(
                word & 0xFFFF, TANH_TABLE_Q15, 0x0000, self.dshift_n)))
        hp = [_sat_add16(_mulq(0x7FFF - z[i], n[i]), _mulq(z[i], h[i]))
              for i in range(self.H)]
        head = [mac_row_words(cq, bq, hp) for cq, bq in self.head_rows]
        # BinArgmax signed compare, FIRST occurrence wins
        cls_i, best = 0, head[0]
        for j in range(1, self.n_classes):
            if head[j] > best:
                cls_i, best = j, head[j]
        return hp, head, cls_i

    # --------------------------------------------------------------- forward
    def init_state(self):
        return [0] * self.H

    def forward(self, Xq, h=None, want_h=False):
        """``Xq``: (T, I) int Q15 features. Returns ``(cls (T,), h)`` and,
        with ``want_h``, also the (T, H) h-trajectory + (T, C) head words."""
        if h is None:
            h = self.init_state()
        T = len(Xq)
        cls = np.zeros(T, dtype=np.int64)
        hs = np.zeros((T, self.H), dtype=np.int64) if want_h else None
        heads = (np.zeros((T, self.n_classes), dtype=np.int64)
                 if want_h else None)
        for t in range(T):
            h, head, c = self.step([int(v) for v in Xq[t]], h)
            cls[t] = c
            if want_h:
                hs[t] = h
                heads[t] = head
        if want_h:
            return cls, h, hs, heads
        return cls, h


# ---------------------------------------------------------------------------
# held-out evaluation (the train.py protocol: 8 stateful streams, per-clip
# majority vote over steps 50..299, burn-in 50)
# ---------------------------------------------------------------------------
def _load_dataset(feat_names):
    cfg = json.loads((_HERE / "config.json").read_text())
    d = np.load(_HERE / "dataset" / "gru_dataset.npz")
    all_names = [str(s) for s in d["feature_names"]]
    fidx = [all_names.index(f) for f in feat_names]
    X = d["X"][:, :, fidx].astype(np.float32)
    return cfg, X, d["y"], d["split"]


def _make_streams(X, y, idx, rows, rng):
    idx = np.array(idx)
    rng.shuffle(idx)
    L = len(idx) // rows
    idx = idx[:L * rows].reshape(rows, L)
    T = X.shape[1]
    Xs = X[idx].reshape(rows, L * T, X.shape[2])
    ys = np.repeat(y[idx], T).reshape(rows, L * T)
    return Xs, ys, L, T


def evaluate(model: "GRUChipModel", burn=50, rows=8):
    _, X, y, split = _load_dataset(["rms", "zcr"])
    te = np.where(split == 2)[0]
    rng = np.random.default_rng(99)
    Xs, ys, L, T = _make_streams(X, y, te, rows, rng)
    conf = np.zeros((4, 4), dtype=int)
    step_ok, step_n = 0, 0
    for r in range(rows):
        Xq = np.clip(np.round(Xs[r] * 32768.0), 0, 32767).astype(np.int64)
        pred, _ = model.forward(Xq)
        for si in range(L):
            s0 = si * T
            seg = pred[s0 + burn:s0 + T]
            true = int(ys[r, s0])
            vote = int(np.argmax(np.bincount(seg, minlength=4)))
            conf[true, vote] += 1
            step_ok += int(np.sum(seg == true))
            step_n += len(seg)
    return {
        "clip_acc": float(conf.trace() / conf.sum()),
        "step_acc": float(step_ok / step_n),
        "n_clips": int(conf.sum()),
        "confusion_rows_true_cols_pred": conf.tolist(),
    }


if __name__ == "__main__":
    model = GRUChipModel.load(_HERE / "weights_single.json")
    print("chip constants: gate S =",
          {g: model.gate_S[g] for g in GATES},
          f"head S = {model.head_S}",
          f"dshifts: r={model.dshift_r} z={model.dshift_z} "
          f"n={model.dshift_n}")
    m = evaluate(model)
    print(json.dumps(m, indent=1))


