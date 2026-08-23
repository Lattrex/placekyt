"""Float + Q15-integer reference forward pass for the GRU modulation
classifier.  Pure numpy, bit-deterministic: this module is the verification
golden for the on-chip block build.

Model (per layer; gate order r, z, n everywhere):

    r_t = sigmoid(Wxr x_t + Whr h_{t-1} + br)
    z_t = sigmoid(Wxz x_t + Whz h_{t-1} + bz)
    n_t = tanh  (Wxn x_t + r_t * (Whn h_{t-1}) + bn)
    h_t = (1 - z_t) * n_t + z_t * h_{t-1}

Head:  logits = Wo h_T + bo, class = argmax(logits).
Stacked variant: layer 2 receives h^{(1)}_t as its input.

Q15 integer semantics (the on-chip contract, mirrored exactly here):

  * Activations and hidden state are Q15 int16 (value = q / 32768); the
    input features live in [0, 1) so their q is in [0, 32767].
  * Each weight matrix M has an int16 Q15 mantissa Mq and a small
    non-negative scale exponent e:  M ~= Mq * 2**e / 32768.
  * matvec: acc(int64) = sum(Mq * vq)  (value * 2**30 / 2**e), then
    preact_q15 = sat32(rshift_round(acc, 15 - e))         [value * 2**15]
  * rshift_round(v, s) = (v + 2**(s-1)) >> s   (round half up, arithmetic
    shift; s >= 1 always holds because e <= 14 is enforced).
  * Gate biases are plain Q15 ints added to the Q15 preactivation.
  * sigmoid/tanh are 1024-entry Q15 lookup tables over [-8, 8) / [-4, 4)
    (bin-center sampling, input clamped into range; see make_luts()).
  * Elementwise Q15 multiply:  sat16(rshift_round(a * b, 15)).
  * State update: h' = sat16(rshift_round((32768 - z) * n + z * h, 15)).
  * Head: acc = sum(Wo_q * h_q) + round(bo * 2**30 / 2**eo)  (int64, one
    shared exponent eo for the whole head so argmax is scale-consistent);
    class = argmax(acc).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

LUT_BITS = 10
LUT_SIZE = 1 << LUT_BITS
SIG_RANGE = 8.0   # sigmoid LUT domain [-8, 8)
TANH_RANGE = 4.0  # tanh LUT domain [-4, 4)

GATES = ("r", "z", "n")


# ----------------------------------------------------------------------------
# fixed-point helpers
# ----------------------------------------------------------------------------
def rshift_round(v, s: int):
    """Arithmetic right shift by s with round-half-up.  s >= 1."""
    v = np.asarray(v, dtype=np.int64)
    return (v + (1 << (s - 1))) >> s


def sat16(v):
    return np.clip(np.asarray(v, dtype=np.int64), -32768, 32767)


def sat32(v):
    return np.clip(np.asarray(v, dtype=np.int64), -(1 << 31), (1 << 31) - 1)


def make_luts():
    """Deterministic Q15 sigmoid/tanh tables (bin-center sampling)."""
    i = np.arange(LUT_SIZE)
    xs = -SIG_RANGE + 2 * SIG_RANGE * (i + 0.5) / LUT_SIZE
    xt = -TANH_RANGE + 2 * TANH_RANGE * (i + 0.5) / LUT_SIZE
    sig = np.clip(np.round(1.0 / (1.0 + np.exp(-xs)) * 32768), 0,
                  32767).astype(np.int64)
    tnh = np.clip(np.round(np.tanh(xt) * 32768), -32768,
                  32767).astype(np.int64)
    return sig, tnh


_SIG_LUT, _TANH_LUT = make_luts()


def lut_sigmoid(p_q15):
    """p_q15: int preactivation (value*2**15) -> Q15 sigmoid via LUT."""
    idx = np.clip((np.asarray(p_q15, dtype=np.int64)
                   + int(SIG_RANGE * 32768)) >> (int(math.log2(
                       2 * SIG_RANGE * 32768)) - LUT_BITS), 0, LUT_SIZE - 1)
    return _SIG_LUT[idx]


def lut_tanh(p_q15):
    idx = np.clip((np.asarray(p_q15, dtype=np.int64)
                   + int(TANH_RANGE * 32768)) >> (int(math.log2(
                       2 * TANH_RANGE * 32768)) - LUT_BITS), 0, LUT_SIZE - 1)
    return _TANH_LUT[idx]


def quantize_matrix(M: np.ndarray):
    """int16 Q15 mantissa + minimal exponent e >= 0 with |Mq| <= 32767."""
    m = float(np.max(np.abs(M))) if M.size else 0.0
    e = 0
    while m * 32768.0 / (1 << e) > 32767.0:
        e += 1
    if e > 14:
        raise ValueError(f"weight magnitude too large to quantize: {m}")
    Mq = np.clip(np.round(M * 32768.0 / (1 << e)), -32768,
                 32767).astype(np.int64)
    return Mq, e


def q15(v: np.ndarray):
    return np.clip(np.round(np.asarray(v) * 32768.0), -32768,
                   32767).astype(np.int64)


def matvec_q15(Mq: np.ndarray, e: int, vq: np.ndarray):
    acc = Mq.astype(np.int64) @ np.asarray(vq, dtype=np.int64)
    return sat32(rshift_round(acc, 15 - e))


# ----------------------------------------------------------------------------
# model
# ----------------------------------------------------------------------------
class GRULayer:
    """One GRU layer; float params + derived quantized params."""

    def __init__(self, p: dict):
        # float params: Wx/Wh are (3H, I)/(3H, H) with rows [r; z; n]
        self.Wx = np.asarray(p["Wx"], dtype=np.float64)
        self.Wh = np.asarray(p["Wh"], dtype=np.float64)
        self.b = np.asarray(p["b"], dtype=np.float64)
        self.H = self.Wh.shape[1]
        assert self.Wx.shape[0] == 3 * self.H == self.Wh.shape[0]
        # quantized (from file if present, else derived)
        if "quant" in p:
            q = p["quant"]
            self.Wxq = {g: np.asarray(q["Wx"][g]["q"], dtype=np.int64)
                        for g in GATES}
            self.Wxe = {g: int(q["Wx"][g]["e"]) for g in GATES}
            self.Whq = {g: np.asarray(q["Wh"][g]["q"], dtype=np.int64)
                        for g in GATES}
            self.Whe = {g: int(q["Wh"][g]["e"]) for g in GATES}
            self.bq = {g: np.asarray(q["b"][g], dtype=np.int64)
                       for g in GATES}
        else:
            H = self.H
            sl = {"r": slice(0, H), "z": slice(H, 2 * H),
                  "n": slice(2 * H, 3 * H)}
            self.Wxq, self.Wxe, self.Whq, self.Whe, self.bq = {}, {}, {}, {}, {}
            for g in GATES:
                self.Wxq[g], self.Wxe[g] = quantize_matrix(self.Wx[sl[g]])
                self.Whq[g], self.Whe[g] = quantize_matrix(self.Wh[sl[g]])
                self.bq[g] = q15(self.b[sl[g]])

    def quant_dict(self):
        return {
            "Wx": {g: {"q": self.Wxq[g].tolist(), "e": self.Wxe[g]}
                   for g in GATES},
            "Wh": {g: {"q": self.Whq[g].tolist(), "e": self.Whe[g]}
                   for g in GATES},
            "b": {g: self.bq[g].tolist() for g in GATES},
        }

    # ---- float ----
    def step_float(self, x: np.ndarray, h: np.ndarray) -> np.ndarray:
        H = self.H
        px = self.Wx @ x
        ph = self.Wh @ h
        r = 1.0 / (1.0 + np.exp(-(px[:H] + ph[:H] + self.b[:H])))
        z = 1.0 / (1.0 + np.exp(-(px[H:2 * H] + ph[H:2 * H]
                                  + self.b[H:2 * H])))
        n = np.tanh(px[2 * H:] + r * ph[2 * H:] + self.b[2 * H:])
        return (1.0 - z) * n + z * h

    # ---- Q15 integer ----
    def step_q15(self, xq: np.ndarray, hq: np.ndarray) -> np.ndarray:
        pr = (matvec_q15(self.Wxq["r"], self.Wxe["r"], xq)
              + matvec_q15(self.Whq["r"], self.Whe["r"], hq) + self.bq["r"])
        pz = (matvec_q15(self.Wxq["z"], self.Wxe["z"], xq)
              + matvec_q15(self.Whq["z"], self.Whe["z"], hq) + self.bq["z"])
        r = lut_sigmoid(pr)
        z = lut_sigmoid(pz)
        u = matvec_q15(self.Whq["n"], self.Whe["n"], hq)
        pn = (matvec_q15(self.Wxq["n"], self.Wxe["n"], xq)
              + rshift_round(r * u, 15) + self.bq["n"])
        n = lut_tanh(pn)
        hp = rshift_round((32768 - z) * n + z * hq, 15)
        return sat16(hp)


class GRUClassifier:
    """Full model: stacked GRU layers + linear argmax head."""

    def __init__(self, params: dict):
        self.params = params
        self.layers = [GRULayer(lp) for lp in params["layers"]]
        self.Wo = np.asarray(params["head"]["Wo"], dtype=np.float64)
        self.bo = np.asarray(params["head"]["bo"], dtype=np.float64)
        hq = params["head"].get("quant")
        if hq is not None:
            self.Woq = np.asarray(hq["Wo_q"], dtype=np.int64)
            self.Woe = int(hq["e"])
            self.bo_acc = np.asarray(hq["bo_acc"], dtype=np.int64)
        else:
            self.Woq, self.Woe = quantize_matrix(self.Wo)
            self.bo_acc = np.round(
                self.bo * (1 << 30) / (1 << self.Woe)).astype(np.int64)

    # ---- persistence ----
    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def save(self, path, extra: dict | None = None):
        out = {
            "format_version": 1,
            "model": self.params.get("model", "gru_classifier"),
            "classes": self.params["classes"],
            "feature_config": self.params["feature_config"],
            "state_contract": self.params.get(
                "state_contract",
                "h=0 at stream start only; never reset while streaming"),
            "layers": [],
            "head": {
                "Wo": self.Wo.tolist(), "bo": self.bo.tolist(),
                "quant": {"Wo_q": self.Woq.tolist(), "e": self.Woe,
                          "bo_acc": self.bo_acc.tolist()},
            },
            "lut": {"bits": LUT_BITS, "sigmoid_range": SIG_RANGE,
                    "tanh_range": TANH_RANGE,
                    "note": "bin-center sampled; see gru_reference.make_luts"},
        }
        for L in self.layers:
            out["layers"].append({"Wx": L.Wx.tolist(), "Wh": L.Wh.tolist(),
                                  "b": L.b.tolist(),
                                  "quant": L.quant_dict()})
        if extra:
            out.update(extra)
        Path(path).write_text(json.dumps(out, indent=1))

    # ---- float forward ----
    def init_state_float(self):
        return [np.zeros(L.H) for L in self.layers]

    def forward_float(self, X: np.ndarray, state=None):
        """X: (T, I) float features.  Returns (logits (T, C), state)."""
        if state is None:
            state = self.init_state_float()
        T = X.shape[0]
        logits = np.zeros((T, self.Wo.shape[0]))
        for t in range(T):
            inp = X[t]
            for li, L in enumerate(self.layers):
                state[li] = L.step_float(inp, state[li])
                inp = state[li]
            logits[t] = self.Wo @ inp + self.bo
        return logits, state

    # ---- Q15 integer forward ----
    def init_state_q15(self):
        return [np.zeros(L.H, dtype=np.int64) for L in self.layers]

    def forward_q15(self, Xq: np.ndarray, state=None):
        """Xq: (T, I) int Q15 features.  Returns (head accs (T, C) int64,
        state).  argmax(axis=1) of the accs is the class decision."""
        if state is None:
            state = self.init_state_q15()
        T = Xq.shape[0]
        accs = np.zeros((T, self.Woq.shape[0]), dtype=np.int64)
        for t in range(T):
            inp = np.asarray(Xq[t], dtype=np.int64)
            for li, L in enumerate(self.layers):
                state[li] = L.step_q15(inp, state[li])
                inp = state[li]
            accs[t] = self.Woq @ inp + self.bo_acc
        return accs, state


def features_to_q15(X: np.ndarray) -> np.ndarray:
    """Float features in [0,1) -> Q15 ints (the on-chip front-end grid)."""
    return np.clip(np.round(np.asarray(X) * 32768.0), 0, 32767).astype(
        np.int64)
