# SPDX-License-Identifier: GPL-3.0-or-later
"""QuadratureDemodBlock — see :class:`QuadratureDemodBlock`."""
import math
from typing import Dict, List

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class QuadratureDemodBlock(KyttarBlock):
    """
    Quadrature (FM) Demodulator — drop-in for GNU Radio ``analog.quadrature_demod_cf``.

    GR computes ``out[n] = gain · arg(x[n]·conj(x[n-1]))`` (the FM discriminator, i.e.
    ``gain·Δphase``).  On this fabric we use the STANDARD 16-bit-DSP FM DISCRIMINATOR —
    the differentiator form that every real FM receiver uses — instead of a literal
    ``atan2``::

        d[n]   = x[n]·conj(x[n-1])                (complex product)
        di[n]  = Im(d[n]) = I[n]·Q[n-1] − Q[n]·I[n-1]      (= I·dQ − Q·dI, up to sign)
        out[n] = gain · di[n]                      (real)

    ``di`` is exactly the numerator of ``d/dt·atan2(Q,I)`` and, for the constant-|x|
    (limited / AGC'd) signal a real FM RX operates on, ``di = sin(Δphase) ≈ Δphase`` —
    so ``gain·di`` tracks GR's ``gain·arg`` to first order.  This is ALL MAC/multiply/
    subtract (the fabric's strengths) and lands in TWO cells, versus the ~47 a literal
    on-chip ``atan2`` (CORDIC) would need on this accumulator ISA.

    ALGORITHM DEVIATION FROM GR (RULE #0, documented loudly):
      GR's block literally calls ``atan2``; we use the equivalent divide-free
      discriminator.  The two AGREE to first order in Δphase and are used
      interchangeably in practice.  The verification CONTRACT is therefore a
      CORRELATION gate (≥0.999 vs GR over the FM deviation range), NOT bit-exact
      equality to ``atan2`` — CM-approved (2026-07-05).  Correlation vs GR:
      ~0.99999 at fs/fdev typical (Δphase ≤ ~0.33 rad), degrading gracefully only
      past ~1 rad/sample (extreme deviation).  A hard-limiter/AGC ahead of this block
      (as in any FM RX) keeps |x| constant, which is the regime it matches GR in.

    Parameters mirror GRC's **Quadrature Demod** exactly:

      * ``gain`` — output scale (``gain = fs / (2π·f_dev)`` in a real FM RX).  Default
        1.0 (GR's default).

    Output scaling: ``K = gain`` is factored as ``2^p · Kp`` with ``Kp ∈ (0.5, 1]`` (so
    ``Kp_q15`` fits int16); ``out = (di · Kp_q15)>>15 << p`` (saturating), supporting any
    gain via a MULQ + a fixed p-bit saturating shift.

    Interface: COMPLEX input (xi@R0, xq@R1 — the proven complex-burst fan-in), ONE real
    output.  Two cells: ``conjmult`` (the conjugate product, emitting ``di``) → ``gain``.
    """
    CATEGORY = "demodulators"
    TAGS = ["fm", "quadrature_demod", "discriminator", "demodulator", "demodulators"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0])

    _CELL_IDS = ["conjmult", "gain"]

    def __init__(self, name: str, gain: float = 1.0):
        super().__init__(name, gain=gain)
        self._gain = float(gain)
        # Output scale K = gain, factored as 2^p * Kp, Kp in (0.5, 1] (so Kp_q15 fits
        # int16). out = (di*Kp_q15)>>15 << p, saturating. Supports any gain.
        K = self._gain
        self._out_sign = 1 if K >= 0 else -1
        Kp = abs(K)
        p = 0
        if Kp != 0.0:
            while Kp > 1.0:
                Kp /= 2.0
                p += 1
        self._out_shift = p
        self._kp_q15 = float_to_q15(self._out_sign * Kp) if Kp != 0.0 else 0

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def gain(self) -> float:
        return self._gain

    # ------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        cells = {}

        # (1) conjmult — di = Im(x[n]·conj(x[n-1])) = cur_q·pv_i − cur_i·pv_q, then update
        # the held previous sample.  di IS the FM discriminator numerator.  (dr is not
        # needed by the discriminator, so we don't emit it.)  x[-1]=0 -> di[0]=0 (matches
        # GR's out[0]=gain·arg(0)=0).
        cells["conjmult"] = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("di"), Port("trig")],
            entries=[EntryPoint("default")],
            state=[StateVar("pv_i", initial_value=0, register=2),
                   StateVar("pv_q", initial_value=0, register=3),
                   StateVar("cur_i", register=4), StateVar("cur_q", register=5),
                   StateVar("acc", register=6)],
            data=[],
            assembly_template="""\
start:
    MOVE R{state:cur_i}, R{in:xi}
    MOVE R{state:cur_q}, R{in:xq}
    MOVE R{state:acc}, R{state:cur_q}
    MULQ R{state:acc}, R{state:pv_i}
    MOVE R{state:acc}, R0
    MOVE R0, R{state:cur_i}
    MULQ R0, R{state:pv_q}
    SUB R{state:acc}, R0
    {write:di}
    MOVE R{state:pv_i}, R{state:cur_i}
    MOVE R{state:pv_q}, R{state:cur_q}
    {jump:trig}
""",
        )

        # (2) gain — out = (di·Kp_q15)>>15 << p (saturating).  A single MULQ + p saturating
        # doublings supports ANY gain (incl. GR's default 1.0).
        shl_block = ""
        for j in range(self._out_shift):
            shl_block += "    ADD R{state:a}, R{state:a}\n"
            if j < self._out_shift - 1:
                shl_block += "    MOVE R{state:a}, R0\n"
        cells["gain"] = CellProgram(
            inputs=[Port("di", register=0)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kp", self._kp_q15, address=1)],
            state=[StateVar("a")],
            assembly_template="""\
start:
    MOVE R{state:a}, R{in:di}
    MULQ R{state:a}, R{data:kp}
    MOVE R{state:a}, R0
""" + shl_block + """\
    {write:out}
    {jump:trig}
""",
        )
        return cells

    def _chain(self):
        return ["conjmult", "gain"]

    def internal_connections(self):
        return [("conjmult", "di", "gain", "di")]

    def internal_jumps(self):
        return [("conjmult", "trig", "gain", "default")]

    def output_cell_ids(self):
        return ["gain"]

    def default_layout(self):
        return {"conjmult": (0, 0, "east"), "gain": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    def _sample_q15(self, pv_i, pv_q, xi, xq):
        """One output word: gain·di, op-for-op with the on-chip cells.
        di = (xq·pv_i)>>15 − (xi·pv_q)>>15 (two Q15 MULQ truncations, then subtract)."""
        di = ((xq * pv_i) >> 15) - ((xi * pv_q) >> 15)
        Kp = _s16(self._kp_q15)
        y = (di * Kp) >> 15
        return int(np.clip(y << self._out_shift, -32768, 32767))

    def process_reference(self, input_samples) -> np.ndarray:
        """Real gain·di(x[n]·conj(x[n-1])), the discriminator, op-for-op (float view)."""
        x = np.asarray(input_samples, dtype=np.complex128).reshape(-1)
        out = np.zeros(len(x), dtype=np.float64)
        pv_i = pv_q = 0
        for k in range(len(x)):
            xi = int(np.clip(round(x[k].real * 32768.0), -32768, 32767))
            xq = int(np.clip(round(x[k].imag * 32768.0), -32768, 32767))
            out[k] = self._sample_q15(pv_i, pv_q, xi, xq) / 32768.0
            pv_i, pv_q = xi, xq
        return out

    def process_reference_q15(self, input_samples) -> List[int]:
        """Bit-exact on-chip predictor: one signed Q15 word per input sample."""
        x = np.asarray(input_samples, dtype=np.complex128).reshape(-1)
        out = []
        pv_i = pv_q = 0
        for k in range(len(x)):
            xi = int(np.clip(round(x[k].real * 32768.0), -32768, 32767))
            xq = int(np.clip(round(x[k].imag * 32768.0), -32768, 32767))
            out.append(self._sample_q15(pv_i, pv_q, xi, xq) & 0xFFFF)
            pv_i, pv_q = xi, xq
        return out

    def reset(self):
        pass
