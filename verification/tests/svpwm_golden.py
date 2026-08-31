# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone SVPWM golden — written directly from the specification,
INDEPENDENT of the block implementation, so the suite can cross-check the
block's own reference against it before either gates anything.

The contract (see ``SVPWMBlock``):

  inverse Clarke:  va = v_alpha
                   vb = -v_alpha/2 + (sqrt(3)/2) * v_beta
                   vc = -v_alpha/2 - (sqrt(3)/2) * v_beta
  min-max injection:  m = (max + min) / 2 ;  duty_i = v_i - m

Exact integer arithmetic of the shipped datapath (every step one instruction
sequence of the block; MULQ is a FLOOR shift, measured on chip):

  nh = -((v_alpha * 16384) >> 15)          # -v_alpha/2
  t  = (v_beta * 28378) >> 15              # (sqrt(3)/2) * v_beta
  pa = v_alpha ; pb = sat(nh + t) ; pc = sat(nh - t)
  m  = ((max * 16384) >> 15) + ((min * 16384) >> 15)
  duty_i = sat(p_i - m)

Output: 3 words per sample, FIXED order a, b, c (the packet convention).
"""
import math

SQRT3_2_Q15 = 28378          # round(sqrt(3)/2 * 2^15)
HALF_Q15 = 16384             # 0.5 in Q15
assert SQRT3_2_Q15 == round(math.sqrt(3) / 2 * 32768)


def _s16(w: int) -> int:
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


def _sat16(v: int) -> int:
    return max(-32768, min(32767, int(v)))


def svpwm_phases(v_alpha: int, v_beta: int) -> tuple:
    """The pre-injection integer three-phase set (pa, pb, pc)."""
    va = _s16(v_alpha)
    vb = _s16(v_beta)
    nh = -((va * HALF_Q15) >> 15)
    t = (vb * SQRT3_2_Q15) >> 15
    return (va, _sat16(nh + t), _sat16(nh - t))


def svpwm_duties(v_alpha: int, v_beta: int) -> tuple:
    """One (v_alpha, v_beta) Q15 pair in, the three SIGNED duty words out."""
    pa, pb, pc = svpwm_phases(v_alpha, v_beta)
    mx = max(pa, pb, pc)
    mn = min(pa, pb, pc)
    m = ((mx * HALF_Q15) >> 15) + ((mn * HALF_Q15) >> 15)
    return (_sat16(pa - m), _sat16(pb - m), _sat16(pc - m))


def svpwm_sector(v_alpha: int, v_beta: int) -> tuple:
    """(argmax, argmin) of the pre-injection phases — the six-sector identity,
    with the block's exact tie-break (first strictly-greater/smaller wins)."""
    pa, pb, pc = svpwm_phases(v_alpha, v_beta)
    mx, imx = pa, 0
    if pb > mx:
        mx, imx = pb, 1
    if pc > mx:
        mx, imx = pc, 2
    mn, imn = pa, 0
    if pb < mn:
        mn, imn = pb, 1
    if pc < mn:
        mn, imn = pc, 2
    return (imx, imn)


def svpwm_stream(alpha_words, beta_words) -> list:
    """N complete pairs in, the flat 3-word-per-sample uint16 packet stream
    ``[a0, b0, c0, a1, b1, c1, ...]`` out — truncated to the shortest arm (a
    packet is emitted only when BOTH arms have supplied their word); the
    arrival order is deliberately absent from the signature."""
    n = min(len(alpha_words), len(beta_words))
    out: list = []
    for i in range(n):
        da, db, dc = svpwm_duties(alpha_words[i], beta_words[i])
        out.extend((da & 0xFFFF, db & 0xFFFF, dc & 0xFFFF))
    return out


def svpwm_duties_float(v_alpha: float, v_beta: float) -> tuple:
    """The textbook float reference (inverse Clarke + min-max injection),
    clipped to the Q15 domain."""
    s = math.sqrt(3.0) / 2.0
    pa = v_alpha
    pb = -v_alpha / 2.0 + s * v_beta
    pc = -v_alpha / 2.0 - s * v_beta
    m = (max(pa, pb, pc) + min(pa, pb, pc)) / 2.0

    def clip(x):
        return max(-1.0, min(32767.0 / 32768.0, x))
    return (clip(pa - m), clip(pb - m), clip(pc - m))
