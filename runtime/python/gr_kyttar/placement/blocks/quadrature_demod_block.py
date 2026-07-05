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

    _CELL_IDS = (["conjmult", "cordic_init"]
                 + [f"cordic{ab}{i}" for i in range(14) for ab in ("A", "B")]
                 + ["cordic_quad", "cordic_gain"])

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
        # conjmult + cordic_init + 2*NITER (A/B per iteration) + cordic_quad + cordic_gain
        return 2 + 2 * self.NITER + 2

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

        # (3..) each CORDIC iteration = TWO cells (cordicA[i], cordicB[i]).  A single step
        # is ~33 instr — PROVEN over the 31-word budget by the resolver (instr+data<=31) even
        # with emit-from-R0, because the accumulator ISA taxes every value with a
        # MOVE-through-R0.  Split so each half fits:
        #   cordicA[i]: read X,Y; compute xsh=X>>i and SIGNED ysh; update X (+/-ysh); relay Y
        #     and a; pass sxsh = +xsh if Y>0 else -xsh (sign folded in).  Emits 4 words
        #     (X',Y,a,sxsh) -> ~24 instr, fits.
        #   cordicB[i]: read X',Y,a,sxsh; Y -= sxsh (branchless: Y>0->Y-xsh, Y<=0->Y+xsh);
        #     a += satc where satc has the SAME sign as the branch — derived from sign(sxsh)?
        #     No: sxsh can be 0.  So B also needs the branch sign.  Instead A folds the atc
        #     sign too by passing satc as a 5th word — but that pushes A to 5 emits.  RESOLVE:
        #     A does the a-update ITSELF (a += atc or -= atc, cheap: 2 instr) and emits the
        #     UPDATED a; then B only touches Y.  A emits 4 (X',Y,a',sxsh); B emits 3 (X',Y',a').
        # ARITHMETIC-SHIFT: X>=0 always -> plain SHR; the Y<0 arm sign-fills ysh via OR mask.
        for i in range(self.NITER):
            sh = i  # shift amount (per-cell constant)
            smask = (0xFFFF << (16 - sh)) & 0xFFFF if sh > 0 else 0  # ASR sign-fill mask
            # ---- cordicA[i]: UNSIGNED shifts + neg flag (NO negations).  Emits Xo,Yo,ao
            # (relay) + xsh(=X>>i) + ysh(=ASR(Y,i)) + neg(=1 if Y<=0 else 0).  All the
            # sign logic is deferred to cordicB, so A does no `0-x` negations (saves ~9
            # instr) -> ~22 instr, fits.  X>=0 always so xsh is a plain SHR; ysh is ASR
            # (sign-fill OR only when Y<0).
            if sh == 0:
                ysh_asr = "    MOVE R{state:ysh}, R{in:Y}\n"                  # i==0: ysh = Y
            else:
                # ysh = ASR(Y,i): logical SHR then OR sign bits ONLY when Y is STRICTLY
                # negative (Y<0).  Gate on Y's real sign bit, NOT `neg` (which is 1 for
                # Y<=0): for Y==0 the shift is 0 and MUST stay 0 (no sign-fill), else the
                # OR would corrupt it to a large negative value.  ysgn = signbit(Y).
                ysh_asr = (
                    "    SHR R{in:Y}, #15\n    MOVE R{state:ysgn}, R0\n"       # ysgn = Y<0 ? 1:0
                    f"    SHR R{{in:Y}}, #{sh}\n    MOVE R{{state:ysh}}, R0\n"
                    "    CMP R{state:ysgn}, R{data:zero}\n    BR.Z yshdone\n"
                    "    MOVE R0, R{state:ysh}\n    OR R0, R{data:smask}\n    MOVE R{state:ysh}, R0\n"
                    "yshdone:\n"
                )
            a_body = (
                "start:\n"
                # neg = 1 if Y<=0 else 0.  Y<0 -> signbit=1; Y==0 -> handle as neg (Y<=0).
                # Compute via: if Y>0 neg=0 else neg=1.
                "    CMP R{in:Y}, R{data:zero}\n"
                "    MOVE R{state:neg}, R{data:one}\n"      # assume Y<=0
                "    BR.NP negset\n"
                "    MOVE R{state:neg}, R{data:zero}\n"     # Y>0 -> neg=0
                "negset:\n"
                f"    SHR R{{in:X}}, #{sh}\n    MOVE R{{state:xsh}}, R0\n"   # xsh = X>>i (X>=0)
                + ysh_asr +
                "emit:\n"
                "    MOVE R0, R{in:X}\n    {write:Xo}\n"      # SHR wrote R0, in:X reg intact
                "    MOVE R0, R{in:Y}\n    {write:Yo}\n"
                "    MOVE R0, R{in:a}\n    {write:ao}\n"
                "    MOVE R0, R{state:xsh}\n    {write:xsh}\n"
                "    MOVE R0, R{state:ysh}\n    {write:ysh}\n"
                "    MOVE R0, R{state:neg}\n    {write:neg}\n"
                "    {jump:trig}\n"
            )
            cells[f"cordicA{i}"] = CellProgram(
                inputs=[Port("X", register=0), Port("Y", register=1),
                        Port("a", register=2)],
                outputs=[Port("Xo"), Port("Yo"), Port("ao"), Port("xsh"),
                         Port("ysh"), Port("neg"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("zero", 0, address=3), DataWord("one", 1, address=4),
                      DataWord("smask", _s16(smask), address=5)],
                state=[StateVar("neg"), StateVar("xsh"), StateVar("ysh"),
                       StateVar("ysgn")],
                assembly_template=a_body,
            )
            # ---- cordicB[i]: branch on neg, do all 3 updates with correct sign ----
            #   neg==0 (Y>0):  X+=ysh ; Y-=xsh ; a+=atc
            #   neg==1 (Y<=0): X-=ysh ; Y+=xsh ; a-=atc
            b_body = (
                "start:\n"
                "    MOVE R{state:xx}, R{in:Xo}\n"
                "    MOVE R{state:ys}, R{in:Yo}\n"
                "    MOVE R{state:aa}, R{in:ao}\n"
                "    CMP R{in:neg}, R{data:zero}\n"
                "    BR.NP bneg\n"
                # neg==0 (Y>0)
                "    ADD R{state:xx}, R{in:ysh}\n    MOVE R{state:xx}, R0\n"
                "    SUB R{state:ys}, R{in:xsh}\n    MOVE R{state:ys}, R0\n"
                "    ADD R{state:aa}, R{data:atc}\n    MOVE R{state:aa}, R0\n"
                "    BR.NN emit\n"
                "bneg:\n"
                # neg==1 (Y<=0)
                "    SUB R{state:xx}, R{in:ysh}\n    MOVE R{state:xx}, R0\n"
                "    ADD R{state:ys}, R{in:xsh}\n    MOVE R{state:ys}, R0\n"
                "    SUB R{state:aa}, R{data:atc}\n    MOVE R{state:aa}, R0\n"
                "emit:\n"
                "    MOVE R0, R{state:xx}\n    {write:X}\n"
                "    MOVE R0, R{state:ys}\n    {write:Y}\n"
                "    MOVE R0, R{state:aa}\n    {write:a}\n"
                "    {jump:trig}\n"
            )
            cells[f"cordicB{i}"] = CellProgram(
                inputs=[Port("Xo", register=0), Port("Yo", register=1),
                        Port("ao", register=2), Port("xsh", register=3),
                        Port("ysh", register=4), Port("neg", register=5)],
                outputs=[Port("X"), Port("Y"), Port("a"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("zero", 0, address=6), DataWord("atc", atan_c[i], address=7)],
                state=[StateVar("xx"), StateVar("ys"), StateVar("aa")],
                assembly_template=b_body,
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
        # conjmult -> cordic_init -> [A0,B0, ... A13,B13] -> cordic_quad -> cordic_gain.
        chain = ["conjmult", "cordic_init"]
        for i in range(self.NITER):
            chain += [f"cordicA{i}", f"cordicB{i}"]
        chain += ["cordic_quad", "cordic_gain"]
        return chain

    def internal_connections(self):
        C = [("conjmult", "dr", "cordic_init", "dr"),
             ("conjmult", "di", "cordic_init", "di")]
        # cordic_init -> A0 (X,Y,a); A[i]->B[i] (X,Yo,a,sxsh); B[i]->A[i+1] (X,Y,a).
        # flags bypasses the whole CORDIC chain (cordic_init -> cordic_quad).
        prev = "cordic_init"
        for i in range(self.NITER):
            A, B = f"cordicA{i}", f"cordicB{i}"
            C += [(prev, "X", A, "X"), (prev, "Y", A, "Y"), (prev, "a", A, "a")]
            C += [(A, "Xo", B, "Xo"), (A, "Yo", B, "Yo"), (A, "ao", B, "ao"),
                  (A, "xsh", B, "xsh"), (A, "ysh", B, "ysh"), (A, "neg", B, "neg")]
            prev = B
        C += [(prev, "a", "cordic_quad", "a"),
              ("cordic_init", "flags", "cordic_quad", "flags"),
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
        """CORDIC vectoring atan2(y,x) as signed Q15 fraction of pi (op-for-op with the
        on-chip cordic cells, INCLUDING the logical-SHR / ASR-emulation).  Returns 0 for
        (0,0).

        The on-chip SHR is LOGICAL (fill with 0).  In the Y>0 branch both shifted operands
        (X,Y) are >=0 so logical == arithmetic.  In the Y<=0 branch Y is negative, so the
        cell emulates ASR (`SHR` then `OR sign-mask`); this reference mirrors that exactly
        with :meth:`_asr16` so the built DUT is BIT-EXACT to it."""
        if x == 0 and y == 0:
            return 0
        atan_c = self._atan_consts()
        sx = 1 if x >= 0 else -1
        sy = 1 if y >= 0 else -1
        X = abs(x); Y = abs(y); a = 0
        for i in range(self.NITER):
            xs = X
            xsh = (xs & 0xFFFF) >> i          # X always >=0 -> logical exact
            if Y > 0:
                ysh = (Y & 0xFFFF) >> i        # Y>=0 -> logical exact
                X = X + ysh
                Y = Y - xsh
                a = a + atan_c[i]
            else:
                ysh = self._asr16(Y, i)        # Y<0 -> arithmetic (emulated on chip)
                X = X - ysh
                Y = Y + xsh
                a = a - atan_c[i]
        a = _s16(a & 0xFFFF)
        if sx < 0:
            a = _s16((0x8000 - a) & 0xFFFF)
        if sy < 0:
            a = -a
        return a

    @staticmethod
    def _asr16(v, n):
        """Arithmetic shift right of a signed 16-bit value by n (0..15), matching the
        on-chip logical-SHR + sign-mask-OR emulation."""
        if n == 0:
            return _s16(v)
        r = (v & 0xFFFF) >> n
        if v < 0:
            r |= (0xFFFF << (16 - n)) & 0xFFFF
        return _s16(r)

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
