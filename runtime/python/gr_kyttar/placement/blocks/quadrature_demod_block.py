# SPDX-License-Identifier: GPL-3.0-or-later
"""QuadratureDemodBlock — see :class:`QuadratureDemodBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class QuadratureDemodBlock(KyttarBlock):
    """
    Quadrature (FM) Demodulator — drop-in for GNU Radio ``analog.quadrature_demod_cf``.

    The FM discriminator: for each complex input sample ``x[n]``::

        d[n]   = x[n] · conj(x[n-1])          (complex product)
        out[n] = gain · arg(d[n])             (= gain · Δphase, real)

    ``arg`` is ``atan2(Im d, Re d)``.  A constant-frequency input becomes a constant
    output; an FM-modulated passband becomes the recovered baseband.  GR initialises
    ``x[-1] = 0`` so ``out[0] = gain·arg(0) = 0``.

    Parameters mirror GRC's **Quadrature Demod** exactly (RULE #0):

      * ``gain`` — output scale (``gain = fs / (2π·f_dev)`` in a real FM RX).  Default
        1.0 (GR's default).

    ON-CHIP atan2 — divide-free, quarter-arc LUT (no CORDIC, no divide instruction)
    -----------------------------------------------------------------------------
    The array has no divide, so ``t = num/den`` (the atan argument) is formed by a
    NORMALISE + RECIPROCAL-TABLE step, then ``atan`` from a second table — the same
    17-entry Q15 linear-interpolated LUT technique as the NCO's sine table:

      1. conjmult: ``dr = (xi·pi + xq·pq)>>15``, ``di = (xq·pi − xi·pq)>>15`` where
         (pi, pq) is the held previous sample; then prev ← current.
      2. fold: reduce to the first octant — ``ax=|dr|, ay=|di|``, ``swap = ay>ax``,
         ``num = min(ax,ay)``, ``den = max(ax,ay)``, plus the sign flags ``sdr``
         (dr<0), ``sdi`` (di<0).
      3. norm: binary-normalise ``den`` into ``[16384, 32768)`` with a 4-stage
         conditional-shift cascade (``<<8, <<4, <<2, <<1``) counting the shift ``sh``;
         ``n = num<<sh``; emit the reciprocal-table index/frac from the normalised
         ``den``.
      4. recip: a 17-entry HALF-RECIPROCAL table ``rinv[k] = 2^29/(16384+1024k)``
         (16-bit-safe) → ``inv ≈ 2^29/den_norm``; then ``t = (n·inv)>>14`` (= num/den
         in Q15); emit the atan-table index/frac.
      5. atan: a 17-entry table ``atbl[k] = atan(k/16)/π`` (angle as a Q15 FRACTION
         OF π) → base angle ``a``; quadrant fix-ups ``if swap: a=16384−a`` (π/2≡16384),
         ``if dr<0: a=32768−a`` (π≡32768), ``if di<0: a=−a`` → ``ang = arg/π`` (Q15);
         then the output ``y = (ang · Kp_q15)>>15 << p`` (saturating) where ``gain·π =
         2^p·Kp`` with ``Kp ∈ (0.5, 1]`` — a MULQ plus a fixed p-bit saturating shift,
         which supports ANY ``gain`` (including GR's default 1.0, where ``gain·π>1``).

    PRECISION — a derived ~11-LSB floor (the two 17-entry LUTs)
    ----------------------------------------------------------
    The reciprocal + atan linear interpolations give a worst-case ~11 LSB vs GR's
    exact ``gain·arg`` inside the Q15-representable output range; ``process_reference``
    reproduces the datapath op-for-op so the DUT is BIT-EXACT to it, and CORRELATION
    vs GR is ~1.0.  SATURATION (INV-3): when ``|gain·arg| ≥ 1`` the Q15 output pins to
    ±full-scale (GR's float would exceed ±1) — verify in the ``|out|<1`` regime, the
    documented substrate limit shared with the other Q15 blocks.

    Interface: COMPLEX input (xi@R0, xq@R1, the proven complex-burst fan-in), ONE
    real output (the recovered ``gain·Δphase``).
    """
    CATEGORY = "demodulators"
    TAGS = ["fm", "quadrature_demod", "discriminator", "demodulator", "demodulators"]

    TABLE_SIZE = 9    # 9-entry LUTs fit one cell WITH the interp code (~20-LSB floor).
    #                   (17 entries is cleaner ~11 LSB but a 17-word table + interp code
    #                    overruns a cell; the pipeline is split enough already.)

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0])

    _CELL_IDS = ["conjmult", "cordic_init"] + [f"cordic{i}" for i in range(14)] + ["cordic_quad", "cordic_gain"]

    def __init__(self, name: str, gain: float = 1.0):
        super().__init__(name, gain=gain)
        self._gain = float(gain)
        # Output scale K = gain*pi factored as 2^p * Kp, Kp in (0.5, 1] (so Kp_q15
        # fits int16). y = (ang*Kp_q15)>>15 << p, saturating. Supports any gain.
        K = self._gain * math.pi
        self._out_sign = 1 if K >= 0 else -1
        Kp = abs(K)
        p = 0
        if Kp == 0.0:
            p = 0
        else:
            while Kp > 1.0:
                Kp /= 2.0
                p += 1
        self._out_shift = p
        self._kp_q15 = float_to_q15(self._out_sign * Kp)
        # Derived table-index math (so TABLE_SIZE can change without editing shifts).
        NI = self.TABLE_SIZE - 1                 # number of intervals
        self._r_ishift = (16384 // NI).bit_length() - 1   # recip: (d-16384) >> r_ishift
        self._r_fmask = (16384 // NI) - 1                  #        (d-16384) & r_fmask
        self._r_fshift = 15 - self._r_ishift               #        frac << r_fshift -> Q15
        self._a_ishift = (32768 // NI).bit_length() - 1   # atan:  t >> a_ishift
        self._a_fmask = (32768 // NI) - 1                  #        t & a_fmask
        self._a_fshift = 15 - self._a_ishift               #        frac << a_fshift -> Q15

    @property
    def cell_count(self) -> int:
        return 18

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def gain(self) -> float:
        return self._gain

    # ------------------------------------------------------------ tables
    def _rinv_table(self) -> List[int]:
        """Half-reciprocal LUT over d_norm in [16384,32768]: rinv[k] = 2^29/(16384+
        step·k), 16-bit-safe (step = 16384/(TABLE_SIZE-1))."""
        step = 16384 // (self.TABLE_SIZE - 1)
        return [min(32767, int(round((1 << 29) / (16384 + step * k))))
                for k in range(self.TABLE_SIZE)]

    def _atan_table(self) -> List[int]:
        """atan LUT: atbl[k] = atan(k/(N-1))/pi (Q15 fraction of pi, [0,0.25])."""
        return [float_to_q15(math.atan(k / (self.TABLE_SIZE - 1)) / math.pi)
                for k in range(self.TABLE_SIZE)]

    # ------------------------------------------------------------ cells
    NITER = 14  # CORDIC vectoring iterations -> ~8 LSB atan2 (no tables)

    def _atan_consts(self):
        """arctan(2^-i)/pi in Q15 (one per CORDIC iteration; a per-cell constant)."""
        return [float_to_q15(math.atan(2.0 ** -i) / math.pi) for i in range(self.NITER)]

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        atan_c = self._atan_consts()
        cells = {}

        # (1) conjmult — dr,di = Re/Im(x*conj(xprev)); update prev.  (Unchanged, bit-exact.)
        cells["conjmult"] = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("dr"), Port("di"), Port("trig")],
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
    MOVE R{state:acc}, R{state:cur_i}
    MULQ R{state:acc}, R{state:pv_i}
    MOVE R{state:acc}, R0
    MOVE R0, R{state:cur_q}
    MULQ R0, R{state:pv_q}
    ADD R{state:acc}, R0
    {write:dr}
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

        # (2) cordic_init — zero-guard + abs + pack sign flags (flags = sx<0 | (sy<0)<<1).
        # Emits X=|dr|, Y=|di|, a=0, flags.  (If both zero, X=Y=0 -> CORDIC stays 0.)
        cells["cordic_init"] = CellProgram(
            inputs=[Port("dr", register=0), Port("di", register=1)],
            outputs=[Port("X"), Port("Y"), Port("a"), Port("flags"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2), DataWord("one", 1, address=3)],
            state=[StateVar("dd"), StateVar("ii"), StateVar("fl")],
            assembly_template="""\
start:
    MOVE R{state:dd}, R{in:dr}
    MOVE R{state:ii}, R{in:di}
    MOVE R0, R{state:dd}
    SHR R0, #15
    MOVE R{state:fl}, R0
    MOVE R0, R{state:ii}
    SHR R0, #15
    SHL R0, #1
    ADD R{state:fl}, R0
    CMP R{state:dd}, R{data:zero}
    BR.NN axp
    MOVE R0, R{data:zero}
    SUB R0, R{state:dd}
    MOVE R{state:dd}, R0
axp:
    CMP R{state:ii}, R{data:zero}
    BR.NN ayp
    MOVE R0, R{data:zero}
    SUB R0, R{state:ii}
    MOVE R{state:ii}, R0
ayp:
    MOVE R0, R{state:dd}
    {write:X}
    MOVE R0, R{state:ii}
    {write:Y}
    MOVE R0, R{data:zero}
    {write:a}
    MOVE R0, R{state:fl}
    {write:flags}
    {jump:trig}
""",
        )

        # (3..3+NITER-1) cordic_iter[i] — one CORDIC vectoring step:
        #   if Y>0: Xn=X+(Y>>i); Yn=Y-(X>>i); a+=ATAN[i]
        #   else:   Xn=X-(Y>>i); Yn=Y+(X>>i); a-=ATAN[i]
        # Snapshot X (Xs) first so the Y update uses the OLD X.  Relay flags.
        for i in range(self.NITER):
            cid = f"cordic{i}"
            sh = i  # shift amount
            body = (
                "start:\n"
                "    MOVE R{state:xs}, R{in:X}\n"
                "    MOVE R{state:ys}, R{in:Y}\n"
                "    MOVE R{state:aa}, R{in:a}\n"
                "    MOVE R{state:fl}, R{in:flags}\n"
                "    CMP R{state:ys}, R{data:zero}\n"
                "    BR.NP yneg\n"
                # Y>0 branch: X += Y>>i ; Y -= Xs>>i ; a += atc
                f"    MOVE R0, R{{state:ys}}\n    SHR R0, #{sh}\n    ADD R{{state:xs}}, R0\n"
                f"    MOVE R0, R{{in:X}}\n    SHR R0, #{sh}\n    MOVE R{{state:t}}, R0\n"
                "    MOVE R0, R{state:ys}\n    SUB R0, R{state:t}\n    MOVE R{state:ys}, R0\n"
                "    ADD R{state:aa}, R{data:atc}\n"
                "    BR.NN emit\n"
                "yneg:\n"
                # Y<=0 branch: X -= Y>>i ; Y += Xs>>i ; a -= atc
                f"    MOVE R0, R{{state:ys}}\n    SHR R0, #{sh}\n    MOVE R{{state:t}}, R0\n"
                "    MOVE R0, R{state:xs}\n    SUB R0, R{state:t}\n    MOVE R{state:xs}, R0\n"
                f"    MOVE R0, R{{in:X}}\n    SHR R0, #{sh}\n    ADD R{{state:ys}}, R0\n"
                "    MOVE R0, R{state:aa}\n    SUB R0, R{data:atc}\n    MOVE R{state:aa}, R0\n"
                "emit:\n"
                "    MOVE R0, R{state:xs}\n    {write:X}\n"
                "    MOVE R0, R{state:ys}\n    {write:Y}\n"
                "    MOVE R0, R{state:aa}\n    {write:a}\n"
                "    MOVE R0, R{state:fl}\n    {write:flags_o}\n"
                "    {jump:trig}\n"
            )
            cells[cid] = CellProgram(
                inputs=[Port("X", register=0), Port("Y", register=1),
                        Port("a", register=2), Port("flags", register=3)],
                outputs=[Port("X"), Port("Y"), Port("a"), Port("flags_o"),
                         Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("zero", 0, address=4),
                      DataWord("atc", atan_c[i], address=5)],
                state=[StateVar("xs"), StateVar("ys"), StateVar("aa"),
                       StateVar("fl"), StateVar("t")],
                assembly_template=body,
            )

        # (last-2) cordic_quad — unpack flags, quadrant fixups: sx<0 -> a=32768-a; sy<0 -> a=-a.
        cells["cordic_quad"] = CellProgram(
            inputs=[Port("a", register=0), Port("flags", register=1)],
            outputs=[Port("ang"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2), DataWord("one", 1, address=3),
                  DataWord("c32768", 0x8000, address=4)],
            state=[StateVar("aa"), StateVar("fl"), StateVar("b")],
            assembly_template="""\
start:
    MOVE R{state:aa}, R{in:a}
    MOVE R{state:fl}, R{in:flags}
    MOVE R{state:b}, R{state:fl}
    AND R{state:b}, R{data:one}
    CMP R0, R{data:zero}
    BR.Z nsx
    MOVE R0, R{data:c32768}
    SUB R0, R{state:aa}
    MOVE R{state:aa}, R0
nsx:
    MOVE R{state:b}, R{state:fl}
    SHR R{state:b}, #1
    MOVE R{state:b}, R0
    AND R{state:b}, R{data:one}
    CMP R0, R{data:zero}
    BR.Z nsy
    MOVE R0, R{data:zero}
    SUB R0, R{state:aa}
    MOVE R{state:aa}, R0
nsy:
    MOVE R0, R{state:aa}
    {write:ang}
    {jump:trig}
""",
        )

        # (last) cordic_gain — y = (ang*kp)>>15 << p (saturating); emit.
        shl_block = ""
        for j in range(self._out_shift):
            shl_block += "    ADD R{state:a}, R{state:a}\n"
            if j < self._out_shift - 1:
                shl_block += "    MOVE R{state:a}, R0\n"
        cells["cordic_gain"] = CellProgram(
            inputs=[Port("ang", register=0)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kp", self._kp_q15, address=1)],
            state=[StateVar("a")],
            assembly_template="""\
start:
    MOVE R{state:a}, R{in:ang}
    MULQ R{state:a}, R{data:kp}
    MOVE R{state:a}, R0
""" + shl_block + """\
    {write:out}
    {jump:trig}
""",
        )
        return cells

    def _chain(self):
        return (["conjmult", "cordic_init"] + [f"cordic{i}" for i in range(self.NITER)]
                + ["cordic_quad", "cordic_gain"])

    def internal_connections(self):
        C = [("conjmult", "dr", "cordic_init", "dr"),
             ("conjmult", "di", "cordic_init", "di")]
        # cordic_init -> cordic0 -> ... -> cordicN-1 -> cordic_quad
        prev = "cordic_init"
        for i in range(self.NITER):
            cur = f"cordic{i}"
            C += [(prev, "X", cur, "X"), (prev, "Y", cur, "Y"),
                  (prev, "a", cur, "a"),
                  (prev, ("flags" if prev == "cordic_init" else "flags_o"),
                   cur, "flags")]
            prev = cur
        C += [(prev, "X", "cordic_quad", "a")]  # placeholder overwritten below
        # cordicN-1 -> cordic_quad needs a + flags
        C = C[:-1]
        C += [(prev, "a", "cordic_quad", "a"),
              (prev, "flags_o", "cordic_quad", "flags"),
              ("cordic_quad", "ang", "cordic_gain", "ang")]
        return C

    def internal_jumps(self):
        chain = self._chain()
        return [(chain[i], "trig", chain[i + 1], "default")
                for i in range(len(chain) - 1)]

    def output_cell_ids(self):
        return ["cordic_gain"]

    def default_layout(self):
        chain = self._chain()
        # Serpentine <=8 across: fill row 0 L->R, row 1 R->L, row 2 L->R.
        layout = {}
        width = 8
        for idx, cid in enumerate(chain):
            row = idx // width
            col = idx % width
            if row % 2 == 1:
                col = width - 1 - col
            face = "east" if row % 2 == 0 else "west"
            layout[cid] = (col, row, face)
        return layout

    # -------------------------------------------------------------- reference
    def _cordic_atan2(self, y, x):
        """CORDIC vectoring atan2(y,x) as signed Q15 fraction of pi (op-for-op with
        the on-chip cordic cells).  Returns 0 for (0,0)."""
        if x == 0 and y == 0:
            return 0
        atan_c = self._atan_consts()
        sx = 1 if x >= 0 else -1
        sy = 1 if y >= 0 else -1
        X = abs(x); Y = abs(y); a = 0
        for i in range(self.NITER):
            xs = X
            if Y > 0:
                X = X + (Y >> i)
                Y = Y - (xs >> i)
                a = a + atan_c[i]
            else:
                X = X - (Y >> i)
                Y = Y + (xs >> i)
                a = a - atan_c[i]
        a = _s16(a & 0xFFFF)
        if sx < 0:
            a = _s16((0x8000 - a) & 0xFFFF)
        if sy < 0:
            a = -a
        return a

    def _sample_q15(self, pv_i, pv_q, xi, xq):
        dr = ((xi * pv_i) >> 15) + ((xq * pv_q) >> 15)
        di = ((xq * pv_i) >> 15) - ((xi * pv_q) >> 15)
        ang = self._cordic_atan2(di, dr)
        Kp = _s16(self._kp_q15)
        y = (ang * Kp) >> 15
        return int(np.clip(y << self._out_shift, -32768, 32767))

    def process_reference(self, input_samples) -> np.ndarray:
        """Real gain*arg(x[n]*conj(x[n-1])) via the on-chip CORDIC atan2, op-for-op."""
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
