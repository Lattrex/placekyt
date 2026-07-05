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

    _CELL_IDS = ["conjmult", "fold_abs", "fold_mm", "norm_sh1", "norm_sh2", "norm_idx",
                 "recip_lut", "recip_t", "atan_lut", "atan_out"]

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
        return 10

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
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        rinv = self._rinv_table()
        atbl = self._atan_table()

        # (1) conjmult — landing cell.  Holds prev sample (pv_i, pv_q), computes the
        # conjugate product dr,di, updates prev, emits dr,di.
        conjmult = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("dr"), Port("di"), Port("trig")],
            entries=[EntryPoint("default")],
            # EXPLICIT state registers R2..R6 — the inputs land xi@R0 (the ALU
            # accumulator!) / xq@R1, and with no `data` the resolver would auto-
            # allocate state from R0 upward, aliasing pv_i onto R0 (clobbered by
            # every ALU op) and pv_q onto R1 (=xq).  Pin state above the inputs.
            state=[StateVar("pv_i", initial_value=0, register=2),
                   StateVar("pv_q", initial_value=0, register=3),
                   StateVar("cur_i", register=4), StateVar("cur_q", register=5),
                   StateVar("acc", register=6)],
            data=[],
            # dr = (xi*pv_i + xq*pv_q)>>15 ; di = (xq*pv_i - xi*pv_q)>>15.
            # Snapshot the inputs FIRST (MULQ clobbers R0 and the input regs survive,
            # but we need xi,xq twice each), then compute, emit, update prev.
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

        # (2) fold_abs — ax=|dr|, ay=|di|; sdr=(dr<0), sdi=(di<0) as 0/1 flags.
        fold_abs = CellProgram(
            inputs=[Port("dr", register=0), Port("di", register=1)],
            outputs=[Port("ax"), Port("ay"), Port("sdr"), Port("sdi"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2)],
            # dr lands in R0 (the accumulator) — SNAPSHOT dr,di into state FIRST, else
            # the sign-bit SHR on R0 clobbers dr before ax=|dr| reads it.  Work from
            # the snapshots (dd,ii) thereafter.
            state=[StateVar("dd"), StateVar("ii")],
            # Snapshot dr,di into dd,ii; emit signs; then ABS in-place (dd=|dd|,
            # ii=|ii|) and emit as ax,ay.
            assembly_template="""\
start:
    MOVE R{state:dd}, R{in:dr}
    MOVE R{state:ii}, R{in:di}
    MOVE R0, R{state:dd}
    SHR R0, #15
    {write:sdr}
    MOVE R0, R{state:ii}
    SHR R0, #15
    {write:sdi}
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
    {write:ax}
    MOVE R0, R{state:ii}
    {write:ay}
    {jump:trig}
""",
        )

        # (3) fold_mm — num=min(ax,ay), den=max, swap=(ay>ax) as 0/1; relay signs.
        fold_mm = CellProgram(
            inputs=[Port("ax", register=0), Port("ay", register=1),
                    Port("sdr", register=2), Port("sdi", register=3)],
            outputs=[Port("num"), Port("den"), Port("swap"),
                     Port("sdr_o"), Port("sdi_o"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4), DataWord("one", 1, address=5)],
            state=[StateVar("nmin"), StateVar("dmax"), StateVar("sw")],
            # Branch only sets nmin/dmax/sw; a single shared tail emits everything.
            assembly_template="""\
start:
    MOVE R{state:nmin}, R{in:ay}
    MOVE R{state:dmax}, R{in:ax}
    MOVE R{state:sw}, R{data:zero}
    CMP R{in:ax}, R{in:ay}
    BR.NN emit
    MOVE R{state:nmin}, R{in:ax}
    MOVE R{state:dmax}, R{in:ay}
    MOVE R{state:sw}, R{data:one}
emit:
    MOVE R0, R{state:nmin}
    {write:num}
    MOVE R0, R{state:dmax}
    {write:den}
    MOVE R0, R{state:sw}
    {write:swap}
    MOVE R0, R{in:sdr}
    {write:sdr_o}
    MOVE R0, R{in:sdi}
    {write:sdi_o}
    {jump:trig}
""",
        )

        # (4) norm_sh1 — coarse normalise (<<8, <<4) on den and num together;
        # relay swap/sdr/sdi.
        norm_sh1 = CellProgram(
            inputs=[Port("num", register=0), Port("den", register=1)],
            outputs=[Port("d"), Port("n"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("c80", 0x0080, address=2),
                  DataWord("c800", 0x0800, address=3)],
            state=[StateVar("d"), StateVar("n")],
            # Capture num@R0 FIRST — R0 is the accumulator, so `MOVE d,den` (which
            # routes through R0) would clobber num before `MOVE n,num` reads it.
            assembly_template="""\
start:
    MOVE R{state:n}, R{in:num}
    MOVE R{state:d}, R{in:den}
    CMP R{state:d}, R{data:c80}
    BR.NN s8
    SHL R{state:d}, #8
    MOVE R{state:d}, R0
    SHL R{state:n}, #8
    MOVE R{state:n}, R0
s8:
    CMP R{state:d}, R{data:c800}
    BR.NN s4
    SHL R{state:d}, #4
    MOVE R{state:d}, R0
    SHL R{state:n}, #4
    MOVE R{state:n}, R0
s4:
    MOVE R0, R{state:d}
    {write:d}
    MOVE R0, R{state:n}
    {write:n}
    {jump:trig}
""",
        )

        # (5) norm_sh2 — fine normalise (<<2, <<1) on d and n together; relay signs.
        norm_sh2 = CellProgram(
            inputs=[Port("d", register=0), Port("n", register=1)],
            outputs=[Port("d_o"), Port("n_o"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("c2000", 0x2000, address=2),
                  DataWord("c4000", 0x4000, address=3)],
            state=[StateVar("dd"), StateVar("nn")],
            assembly_template="""\
start:
    MOVE R{state:dd}, R{in:d}
    MOVE R{state:nn}, R{in:n}
    CMP R{state:dd}, R{data:c2000}
    BR.NN s2
    SHL R{state:dd}, #2
    MOVE R{state:dd}, R0
    SHL R{state:nn}, #2
    MOVE R{state:nn}, R0
s2:
    CMP R{state:dd}, R{data:c4000}
    BR.NN s1
    SHL R{state:dd}, #1
    MOVE R{state:dd}, R0
    SHL R{state:nn}, #1
    MOVE R{state:nn}, R0
s1:
    MOVE R0, R{state:dd}
    {write:d_o}
    MOVE R0, R{state:nn}
    {write:n_o}
    {jump:trig}
""",
        )

        # (5) norm_idx — from normalised d: ridx=(d-16384)>>r_ishift,
        # rfrac=((d-16384)&r_fmask)<<r_fshift; relay n + swap/sdr/sdi.
        norm_idx = CellProgram(
            inputs=[Port("d", register=0), Port("n", register=1),
                    Port("swap", register=2), Port("sdr", register=3),
                    Port("sdi", register=4)],
            outputs=[Port("n_o"), Port("ridx"), Port("rfrac"),
                     Port("swap_o"), Port("sdr_o"), Port("sdi_o"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("c16384", 16384, address=5),
                  DataWord("fmask", self._r_fmask, address=6)],
            state=[StateVar("dd")],
            assembly_template="""\
start:
    MOVE R0, R{in:n}
    {write:n_o}
    MOVE R{state:dd}, R{in:d}
    SUB R{state:dd}, R{data:c16384}
    MOVE R{state:dd}, R0
    MOVE R0, R{state:dd}
    SHR R0, #%(rish)d
    {write:ridx}
    MOVE R0, R{state:dd}
    AND R0, R{data:fmask}
    MOVE R0, R0
    SHL R0, #%(rfsh)d
    {write:rfrac}
    MOVE R0, R{in:swap}
    {write:swap_o}
    MOVE R0, R{in:sdr}
    {write:sdr_o}
    MOVE R0, R{in:sdi}
    {write:sdi_o}
    {jump:trig}
""" % {"rish": self._r_ishift, "rfsh": self._r_fshift},
        )

        NT = len(rinv)
        # (5) recip_lut — inv = P + ((Q-P)*rfrac)>>15 from rinv LUT (addr 1..NT);
        # relay n, swap/sdr/sdi.
        recip_lut = CellProgram(
            inputs=[Port("ridx", register=0), Port("rfrac", register=1)],
            outputs=[Port("inv"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord(f"r{k}", v, address=2 + k) for k, v in enumerate(rinv)]
            + [DataWord("one", 1, address=2 + NT),
               DataWord("tbase", 2, address=3 + NT)],
            state=[StateVar("adr"), StateVar("P"), StateVar("Q")],
            assembly_template="""\
start:
    MOVE R{state:adr}, R{in:ridx}
    ADD R{state:adr}, R{data:tbase}
    MOVE R{state:adr}, R0
    LOAD R{state:adr}
    MOVE R{state:P}, R0
    ADD R{state:adr}, R{data:one}
    MOVE R{state:adr}, R0
    LOAD R{state:adr}
    MOVE R{state:Q}, R0
    SUB R{state:Q}, R{state:P}
    MOVE R{state:Q}, R0
    MOVE R0, R{state:P}
    MACQ R{state:Q}, R{in:rfrac}
    {write:inv}
    {jump:trig}
""",
        )

        # (6) recip_t — t=(n*inv)>>14 (=MULQ<<1); aidx=t>>a_ishift,
        # afrac=(t & a_fmask) << a_fshift.
        recip_t = CellProgram(
            inputs=[Port("inv", register=0), Port("n", register=1),
                    Port("swap", register=2), Port("sdr", register=3),
                    Port("sdi", register=4)],
            outputs=[Port("aidx"), Port("afrac"), Port("swap_o"),
                     Port("sdr_o"), Port("sdi_o"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("afmask", self._a_fmask, address=5)],
            state=[StateVar("t"), StateVar("tmp")],
            assembly_template="""\
start:
    MOVE R{state:t}, R{in:n}
    MULQ R{state:t}, R{in:inv}
    SHL R0, #1
    MOVE R{state:t}, R0
    MOVE R{state:tmp}, R{state:t}
    SHR R{state:tmp}, #%(aish)d
    MOVE R0, R{state:tmp}
    {write:aidx}
    MOVE R{state:tmp}, R{state:t}
    AND R{state:tmp}, R{data:afmask}
    MOVE R{state:tmp}, R0
    SHL R{state:tmp}, #%(afsh)d
    MOVE R0, R{state:tmp}
    {write:afrac}
    MOVE R0, R{in:swap}
    {write:swap_o}
    MOVE R0, R{in:sdr}
    {write:sdr_o}
    MOVE R0, R{in:sdi}
    {write:sdi_o}
    {jump:trig}
""" % {"aish": self._a_ishift, "afsh": self._a_fshift},
        )

        AT = len(atbl)
        # (7) atan_lut — a = P + ((Q-P)*afrac)>>15 from atan LUT (addr 1..AT);
        # relay swap/sdr/sdi.
        atan_lut = CellProgram(
            inputs=[Port("aidx", register=0), Port("afrac", register=1)],
            outputs=[Port("a"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord(f"a{k}", v, address=2 + k) for k, v in enumerate(atbl)]
            + [DataWord("one", 1, address=2 + AT),
               DataWord("tbase", 2, address=3 + AT)],
            state=[StateVar("adr"), StateVar("P"), StateVar("Q")],
            assembly_template="""\
start:
    MOVE R{state:adr}, R{in:aidx}
    ADD R{state:adr}, R{data:tbase}
    MOVE R{state:adr}, R0
    LOAD R{state:adr}
    MOVE R{state:P}, R0
    ADD R{state:adr}, R{data:one}
    MOVE R{state:adr}, R0
    LOAD R{state:adr}
    MOVE R{state:Q}, R0
    SUB R{state:Q}, R{state:P}
    MOVE R{state:Q}, R0
    MOVE R0, R{state:P}
    MACQ R{state:Q}, R{in:afrac}
    {write:a}
    {jump:trig}
""",
        )

        # (8) atan_out — quadrant fixups (swap: 16384-a; sdr: 32768-a==-a mod; sdi: -a)
        # -> ang=arg/pi (Q15); then y=(ang*kp)>>15 << p (saturating doublings); emit.
        # Saturating doublings for the <<p output scale.  The LAST op leaves the
        # result in R0 (which {write:out} sends), so the final MOVE-back is omitted.
        shl_block = ""
        for i in range(self._out_shift):
            shl_block += "    ADD R{state:a}, R{state:a}\n"
            if i < self._out_shift - 1:
                shl_block += "    MOVE R{state:a}, R0\n"
        atan_out = CellProgram(
            inputs=[Port("a", register=0), Port("swap", register=1),
                    Port("sdr", register=2), Port("sdi", register=3)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4), DataWord("c16384", 16384, address=5),
                  DataWord("kp", self._kp_q15, address=6)],
            state=[StateVar("a")],
            # Work in-place in `a`; the gain MULQ+saturating doublings reuse `a`.
            assembly_template="""\
start:
    MOVE R{state:a}, R{in:a}
    CMP R{in:swap}, R{data:zero}
    BR.Z nsw
    MOVE R0, R{data:c16384}
    SUB R0, R{state:a}
    MOVE R{state:a}, R0
nsw:
    CMP R{in:sdr}, R{data:zero}
    BR.Z nsd
    MOVE R0, R{data:zero}
    SUB R0, R{state:a}
    MOVE R{state:a}, R0
nsd:
    CMP R{in:sdi}, R{data:zero}
    BR.Z nsi
    MOVE R0, R{data:zero}
    SUB R0, R{state:a}
    MOVE R{state:a}, R0
nsi:
    MULQ R{state:a}, R{data:kp}
    MOVE R{state:a}, R0
""" + shl_block + """\
    {write:out}
    {jump:trig}
""",
        )

        return {"conjmult": conjmult, "fold_abs": fold_abs, "fold_mm": fold_mm,
                "norm_sh1": norm_sh1, "norm_sh2": norm_sh2, "norm_idx": norm_idx, "recip_lut": recip_lut, "recip_t": recip_t,
                "atan_lut": atan_lut, "atan_out": atan_out}

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        return [
            ("conjmult", "dr", "fold_abs", "dr"),
            ("conjmult", "di", "fold_abs", "di"),
            ("fold_abs", "ax", "fold_mm", "ax"),
            ("fold_abs", "ay", "fold_mm", "ay"),
            ("fold_abs", "sdr", "fold_mm", "sdr"),
            ("fold_abs", "sdi", "fold_mm", "sdi"),
            ("fold_mm", "num", "norm_sh1", "num"),
            ("fold_mm", "den", "norm_sh1", "den"),
            ("fold_mm", "swap", "norm_idx", "swap"),
            ("fold_mm", "sdr_o", "norm_idx", "sdr"),
            ("fold_mm", "sdi_o", "norm_idx", "sdi"),
            ("norm_sh1", "d", "norm_sh2", "d"),
            ("norm_sh1", "n", "norm_sh2", "n"),
            ("norm_sh2", "d_o", "norm_idx", "d"),
            ("norm_sh2", "n_o", "norm_idx", "n"),
            ("norm_idx", "n_o", "recip_t", "n"),
            ("norm_idx", "ridx", "recip_lut", "ridx"),
            ("norm_idx", "rfrac", "recip_lut", "rfrac"),
            ("norm_idx", "swap_o", "recip_t", "swap"),
            ("norm_idx", "sdr_o", "recip_t", "sdr"),
            ("norm_idx", "sdi_o", "recip_t", "sdi"),
            ("recip_lut", "inv", "recip_t", "inv"),
            ("recip_t", "aidx", "atan_lut", "aidx"),
            ("recip_t", "afrac", "atan_lut", "afrac"),
            ("recip_t", "swap_o", "atan_out", "swap"),
            ("recip_t", "sdr_o", "atan_out", "sdr"),
            ("recip_t", "sdi_o", "atan_out", "sdi"),
            ("atan_lut", "a", "atan_out", "a"),
        ]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        chain = ["conjmult", "fold_abs", "fold_mm", "norm_sh1", "norm_sh2", "norm_idx", "recip_lut",
                 "recip_t", "atan_lut", "atan_out"]
        return [(chain[i], "trig", chain[i + 1], "default")
                for i in range(len(chain) - 1)]

    def output_cell_ids(self) -> List[str]:
        return ["atan_out"]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # Two-row serpentine: row 0 EAST (conjmult..norm_idx turns SOUTH), row 1 WEST.
        top = ["conjmult", "fold_abs", "fold_mm", "norm_sh1", "norm_sh2", "norm_idx"]
        bot = ["recip_lut", "recip_t", "atan_lut", "atan_out"]
        layout = {}
        for i, cid in enumerate(top):
            face = "south" if cid == "norm_idx" else "east"
            layout[cid] = (i, 0, face)
        for k, cid in enumerate(bot):
            layout[cid] = (5 - k, 1, "west")
        return layout

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples) -> np.ndarray:
        """Real ``gain·arg(x[n]·conj(x[n-1]))`` via the on-chip divide-free LUT
        atan2 + Q15 output scale, op-for-op.  ``input_samples`` is complex."""
        rinv = self._rinv_table()
        atbl = self._atan_table()
        Kp = _s16(self._kp_q15)
        p = self._out_shift
        x = np.asarray(input_samples, dtype=np.complex128).reshape(-1)
        out = np.zeros(len(x), dtype=np.float64)
        pv_i = pv_q = 0
        for k in range(len(x)):
            xi = int(np.clip(round(x[k].real * 32768.0), -32768, 32767))
            xq = int(np.clip(round(x[k].imag * 32768.0), -32768, 32767))
            ang = self._atan2_q15(pv_i, pv_q, xi, xq, rinv, atbl)
            y = (ang * Kp) >> 15
            y = int(np.clip(y << p, -32768, 32767))
            out[k] = y / 32768.0
            pv_i, pv_q = xi, xq
        return out

    def process_reference_q15(self, input_samples) -> List[int]:
        """Bit-exact on-chip predictor: one signed Q15 word per input sample."""
        rinv = self._rinv_table()
        atbl = self._atan_table()
        Kp = _s16(self._kp_q15)
        p = self._out_shift
        x = np.asarray(input_samples, dtype=np.complex128).reshape(-1)
        out: List[int] = []
        pv_i = pv_q = 0
        for k in range(len(x)):
            xi = int(np.clip(round(x[k].real * 32768.0), -32768, 32767))
            xq = int(np.clip(round(x[k].imag * 32768.0), -32768, 32767))
            ang = self._atan2_q15(pv_i, pv_q, xi, xq, rinv, atbl)
            y = (ang * Kp) >> 15
            y = int(np.clip(y << p, -32768, 32767)) & 0xFFFF
            out.append(y)
            pv_i, pv_q = xi, xq
        return out

    def _atan2_q15(self, pv_i, pv_q, xi, xq, rinv, atbl) -> int:
        """arg(x·conj(xprev)) as a signed Q15 FRACTION OF π — the exact chip datapath
        (conj-mult, octant fold, binary normalise, half-reciprocal LUT divide, atan
        LUT, quadrant fix-ups)."""
        # The chip does two SEPARATE Q15 MULQs (each truncates >>15) then adds/
        # subtracts — NOT one shift of the combined product.  Model that exactly so
        # the reference is bit-identical (a single combined >>15 differs by 1 LSB).
        dr = ((xi * pv_i) >> 15) + ((xq * pv_q) >> 15)
        di = ((xq * pv_i) >> 15) - ((xi * pv_q) >> 15)
        ay, ax = abs(di), abs(dr)
        if ax == 0 and ay == 0:
            return 0
        swap = ay > ax
        num = ax if swap else ay
        den = ay if swap else ax
        d, sh = den, 0
        if d < 0x0080:
            d <<= 8; sh += 8
        if d < 0x0800:
            d <<= 4; sh += 4
        if d < 0x2000:
            d <<= 2; sh += 2
        if d < 0x4000:
            d <<= 1; sh += 1
        n = num << sh
        N = self.TABLE_SIZE - 1
        k = (d - 16384) >> self._r_ishift
        frac = ((d - 16384) & self._r_fmask) << self._r_fshift
        inv = rinv[k] + (((rinv[min(k + 1, N)] - rinv[k]) * frac) >> 15)
        t = (n * inv) >> 14
        if t > 32768:
            t = 32768
        idx = t >> self._a_ishift
        tf = (t & self._a_fmask) << self._a_fshift
        a = atbl[idx] + (((atbl[min(idx + 1, N)] - atbl[idx]) * tf) >> 15)
        if swap:
            a = 16384 - a
        if dr < 0:
            a = 32768 - a
        if di < 0:
            a = -a
        return a

    def reset(self):
        pass
