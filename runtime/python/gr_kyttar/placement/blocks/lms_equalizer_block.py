# SPDX-License-Identifier: GPL-3.0-or-later
"""LMSEqualizerBlock — see :class:`LMSEqualizerBlock`."""
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface, float_to_q15


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _mq(a: int, b: int) -> int:
    """Chip MULQ: (a*b) >> 15, signed, truncating (arithmetic shift)."""
    return ((_s16(a) * _s16(b)) >> 15) & 0xFFFF


def _satc(v: int) -> int:
    return max(-32768, min(32767, v))


class LMSEqualizerBlock(KyttarBlock):
    """
    Decision-directed complex LMS adaptive equalizer (linear FFE) — the GNU
    Radio counterpart is ``digital.linear_equalizer(num_taps, 1,
    adaptive_algorithm_lms(constellation_qpsk(), step_size), ...)``.

    Algorithm (per complex sample x, all arithmetic Q15/MULQ-truncating)::

        h[k] = h[k-1], h[0] = x                    # complex history shift
        acc  = sum_k w_half[k] * h[k]              # complex MACQ chain
        y    = sat(acc << 1)                       # INV-15: taps stored HALVED
        d    = (+-dp, +-dp) by sign(y)             # DD decision, dp = 0.7071
        e    = sat(d - y)
        g    = mq(mu_half, e)                      # mu_half = step_size / 2
        w_half[k] += sat(mq(g_r,h_r[k]) + mq(g_i,h_i[k]))          (saturating)
                   + j*sat(mq(g_i,h_r[k]) - mq(g_r,h_i[k]))

    HW-DEVIATIONS from GNU Radio (each verified scale-covariant — see the gate):

    * **Unit-circle decision constellation.** GR's ``constellation_qpsk()``
      points are ``+-1.414 +- 1.414j`` — components OUTSIDE Q15. The on-chip
      decisions live at ``+-0.7071`` components (alpha = 1/2 of GR's). LMS is
      scale-covariant, so the chip's whole trajectory equals ``alpha *`` GR's
      (outputs AND taps) — the verification gate proves it (RMS ~0.006 to
      ``alpha*GR``, 100% decision agreement, BER 0).
    * **DD-only, spike cold start.** A training sequence needs sample memory
      the fabric doesn't have (INV-29); the chip cold-starts decision-directed
      from a delay-0 center spike (``w_eff[0] = 1.0``), which reaches the SAME
      steady state as GR-with-training on open-eye channels (proven).
    * **Taps stored HALVED** (INV-15/INV-13): effective tap range +-2, tap-gain
      envelope ``sum|w_eff| <= 2`` (a channel needing more is outside the
      operating envelope — documented, not silently wrong: the MAC chain and
      tap accumulators SATURATE, they never wrap).

    Cell architecture (14 program cells + 1 transit, 8x2 fold, I/O co-located
    on the WEST edge)::

        row0:  IN(E)  F0 .. F4 (tap filter cells, E)  SAT(E)  ERR(S)
        row1:  OUT    U0 .. U4 (tap update cells, W)  tw(W)   BCAST(W)

    * IN: input marshal — (xi, xq) land here; forwards to F0.
    * F_k: holds h[k] + a MIRROR of w[k]; shifts history east, mirrors h down
      to U_k, accumulates its complex partial product into the eastbound acc.
    * SAT: y = sat(acc << 1) per component (ADD-with-V clamp); sends y east.
    * ERR: DD decision, e = d - y, g = mq(mu_half, e); sends (y, g) south.
    * BCAST: single WEST face — relays y to OUT, jumps OUT, then broadcasts
      g into every U cell and triggers them FARTHEST-FIRST (a transiting word
      follows each cell's CURRENT fwd_face, so a jump must never transit an
      already-triggered cell whose face is mid-flip — the router-proven
      farthest-sibling rule).
    * U_k: master w[k] + a MIRROR of h[k]; applies the tap update with
      saturating adds, then flips FACE north, writes the updated w mirror back
      into F_k, and RESTORES its westbound face (the resting face is the g/y
      transit corridor for the next sample).
    * OUT: the output cell — emits the recovered complex (yi, yq).

    The update runs at the END of each sample (after the output), exactly like
    ``process_reference`` — the on-chip result is BIT-EXACT to it.

    Parameters mirror GR verbatim: ``num_taps``, ``step_size``. ``sps`` exists
    for parity and only 1 is supported (validated).
    """

    CATEGORY = "recovery"
    TAGS = ["lms", "equalizer", "adaptive", "dd", "complex", "recovery"]

    DECISION_Q15 = float_to_q15(0.70710678)   # unit-circle QPSK component

    # Complex input lands at IN (xi@R0, xq@R1); complex output leaves OUT
    # (yi, yq — two rails, INV-17).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1]
    )

    GRC_UNSUPPORTED_PARAMS = ()

    def __init__(self, name: str, num_taps: int = 5, step_size: float = 0.03,
                 sps: int = 1):
        if int(sps) != 1:
            raise ValueError("LMSEqualizerBlock supports sps=1 only "
                             f"(got {sps}) — decimating equalization is host-side")
        if not (2 <= int(num_taps) <= 5):
            raise ValueError("LMSEqualizerBlock supports 2..5 taps on the "
                             f"10x12 chip (got {num_taps})")
        super().__init__(name, num_taps=int(num_taps),
                         step_size=float(step_size), sps=1)
        self._nt = int(num_taps)
        self._mu = float(step_size)
        self._mu_half_q15 = float_to_q15(self._mu / 2.0)
        self.reset()

    # ------------------------------------------------------------------ misc
    @property
    def cell_count(self) -> int:
        # IN + N*(F+U) + SAT + ERR + BCAST + OUT
        return 4 + 2 * self._nt

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        """The block's output leaves the OUT cell (a NON-last cell — the router
        and the build's egress patching must tap (0,1), not the placement-order
        last cell)."""
        return "out"

    def _cell_ids(self) -> List[str]:
        return (["in"] + [f"f{k}" for k in range(self._nt)] + ["sat"]
                + ["out"] + [f"u{k}" for k in range(self._nt)]
                + ["err", "bcast"])

    # ------------------------------------------------------------ reference
    def reset(self):
        """Cold start: spike at tap 0 (w_eff[0] = 1.0 -> half word 0x4000)."""
        self._wr = [0] * self._nt
        self._wi = [0] * self._nt
        self._wr[0] = float_to_q15(0.5)
        self._hr = [0] * self._nt
        self._hi = [0] * self._nt

    def process_reference(self, samples) -> np.ndarray:
        """Bit-exact Q15 model of the cell programs (the DUT gate compares the
        chip against THIS; GR equivalence is proven against the scale-covariant
        golden in the verification test). ``samples``: iterable of (i, q) Q15
        word pairs. Returns interleaved (yi, yq) Q15 words."""
        out = []
        dp = self.DECISION_Q15
        for (xr, xi) in samples:
            self._hr = [int(xr) & 0xFFFF] + self._hr[:-1]
            self._hi = [int(xi) & 0xFFFF] + self._hi[:-1]
            accr = 0
            acci = 0
            for k in range(self._nt):
                accr = _satc(accr + _s16(_mq(self._wr[k], self._hr[k]))
                             - _s16(_mq(self._wi[k], self._hi[k])))
                acci = _satc(acci + _s16(_mq(self._wr[k], self._hi[k]))
                             + _s16(_mq(self._wi[k], self._hr[k])))
            yr = _satc(accr + accr)
            yi = _satc(acci + acci)
            out.append(yr & 0xFFFF)
            out.append(yi & 0xFFFF)
            dr = _s16(dp) if yr >= 0 else -_s16(dp)
            di = _s16(dp) if yi >= 0 else -_s16(dp)
            er = _satc(dr - yr) & 0xFFFF
            ei = _satc(di - yi) & 0xFFFF
            g_r = _mq(self._mu_half_q15, er)
            g_i = _mq(self._mu_half_q15, ei)
            for k in range(self._nt):
                ur = _satc(_s16(_mq(g_r, self._hr[k]))
                           + _s16(_mq(g_i, self._hi[k])))
                ui = _satc(_s16(_mq(g_i, self._hr[k]))
                           - _s16(_mq(g_r, self._hi[k])))
                self._wr[k] = _satc(_s16(self._wr[k]) + ur) & 0xFFFF
                self._wi[k] = _satc(_s16(self._wi[k]) + ui) & 0xFFFF
        return np.array(out, dtype=np.uint16)

    # ------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        nt = self._nt
        progs: Dict[str, CellProgram] = {}

        # --- IN: input marshal (xi@R0, xq@R1) -> F0, trigger F0. -----------
        progs["in"] = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("h_r"), Port("h_i"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[],
            assembly_template="""\
start:
    MOVE R0, R{in:xi}
    {write:h_r}
    MOVE R0, R{in:xq}
    {write:h_i}
    {jump:trig}
""",
        )

        # --- F_k: tap filter cell. --------------------------------------
        # Resting face EAST (the forward corridor). Per sample: shift the OLD
        # h east (except the last tap), store the new h, mirror h SOUTH into
        # U_k (face flip + restore), then accumulate this tap's complex
        # partial product into the eastbound acc. F0 STARTS the accumulators.
        for k in range(nt):
            last = (k == nt - 1)
            first = (k == 0)
            outs = [Port("hm_r"), Port("hm_i"),
                    Port("acc_r"), Port("acc_i"), Port("trig")]
            if not last:
                outs = [Port("h_r"), Port("h_i")] + outs
            # NO input on R0 (the accumulator): every ALU op clobbers R0, so an
            # R0-landed input is destroyed before the program reads it (the
            # first DUT run mirrored hm_r=0 for exactly this).
            # Register map (resolver contract: inputs < data < state, state is
            # auto-allocated ABOVE the max data address): inputs 1..6, data 3,4
            # (the wm cold-start inits SHARE the wm port addresses) + faces 7,8;
            # state lands at 9+.
            ins = [Port("h_r_in", register=1), Port("h_i_in", register=2)]
            if not first:
                # acc_r is DELIVERED INTO R0 (the accumulator) and consumed by
                # this cell's FIRST instructions; it is also WRITTEN first, so
                # the DOWNSTREAM R0 delivery is never disturbed by the later
                # acc_i write (which targets a normal register). The saved seed
                # MOVE is what fits the middle taps in 30 words.
                ins += [Port("acc_r_in", register=0), Port("acc_i_in", register=5)]
            # w mirror registers are written by U_k (declared as inputs so the
            # resolver reserves them; U_k's WRITEs land here). The COLD-START
            # values are emitted as same-address DataWords — without them the
            # first sample filters with w=0 (the mirrors only sync after the
            # first update) and the chip diverges from process_reference.
            ins += [Port("wm_r", register=3), Port("wm_i", register=4)]

            shift = "" if last else """\
    MOVE R0, R{state:hr}
    {write:h_r}
    MOVE R0, R{state:hi}
    {write:h_i}
"""
            # f0 seeds both accumulators itself; later taps get acc_r in R0
            # (no seed instruction) and acc_i from a register.
            acc_r_head = ("    SUB R0, R0\n" if first else "")
            acc_i_src = ("    SUB R0, R0\n" if first
                         else "    MOVE R0, R{in:acc_i_in}\n")
            progs[f"f{k}"] = CellProgram(
                inputs=ins,
                outputs=outs,
                entries=[EntryPoint("default")],
                data=[DataWord("wm_r_init", float_to_q15(0.5) if first else 0,
                               address=3, reset_per_batch=True),
                      DataWord("wm_i_init", 0, address=4, reset_per_batch=True),
                      DataWord("face_fwd", 1, address=7, is_face=True),
                      DataWord("face_down", 0, address=8, is_face=True)],
                # h[k] survives across samples (the delay line) but must COLD-
                # START per packet like every loop memory (INV-19 packet rule).
                # f0 has one extra instruction (the acc_r seed) and no acc
                # input registers — PIN its state into those free addresses
                # (the allocator only scans the post-data gap, not holes).
                state=([StateVar("hr", register=5, reset_per_batch=True),
                        StateVar("hi", register=6, reset_per_batch=True)]
                       if first else
                       [StateVar("hr", reset_per_batch=True),
                        StateVar("hi", reset_per_batch=True)]),
                assembly_template=(acc_r_head + """\
    MACQ R{in:wm_r}, R{in:h_r_in}
    MSUQ R{in:wm_i}, R{in:h_i_in}
    {write:acc_r}
""" + acc_i_src + """\
    MACQ R{in:wm_r}, R{in:h_i_in}
    MACQ R{in:wm_i}, R{in:h_r_in}
    {write:acc_i}
""" + shift + """\
    MOVE R{state:hr}, R{in:h_r_in}
    MOVE R{state:hi}, R{in:h_i_in}
    MOVE [FACE], R{data:face_down}
    MOVE R0, R{state:hr}
    {write:hm_r}
    MOVE R0, R{state:hi}
    {write:hm_i}
    MOVE [FACE], R{data:face_fwd}
    {jump:trig}
"""),
            )

        # --- SAT: y = sat(acc + acc) per component; send y SOUTH to ERR. ---
        # ADD sets V on signed overflow; on overflow pin to the rail of the
        # ORIGINAL sign (SHR #15 gives 0/1; + 0x7FFF -> 0x7FFF / 0x8000).
        progs["sat"] = CellProgram(
            inputs=[Port("acc_r", register=1), Port("acc_i", register=2)],
            outputs=[Port("y_r"), Port("y_i"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("rail", 0x7FFF, address=3)],
            state=[],
            assembly_template="""\
start:
    ADD R{in:acc_r}, R{in:acc_r}
    BR.NV okr
    SHR R{in:acc_r}, #15
    ADD R0, R{data:rail}
okr:
    {write:y_r}
    ADD R{in:acc_i}, R{in:acc_i}
    BR.NV oki
    SHR R{in:acc_i}, #15
    ADD R0, R{data:rail}
oki:
    {write:y_i}
    {jump:trig}
""",
        )

        # --- ERR: DD decision, error, gradient; forward (y, g) EAST. -------
        progs["err"] = CellProgram(
            inputs=[Port("y_r", register=1), Port("y_i", register=2)],
            outputs=[Port("yf_r"), Port("yf_i"),
                     Port("g_r"), Port("g_i"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("dp", self.DECISION_Q15, address=3),
                  DataWord("zero", 0, address=4),
                  DataWord("mu", self._mu_half_q15, address=5)],
            state=[StateVar("t")],
            assembly_template="""\
start:
    MOVE R0, R{in:y_r}
    {write:yf_r}
    MOVE R0, R{in:y_i}
    {write:yf_i}
    MOVE R0, R{data:dp}
    CMP R{in:y_r}, R{data:zero}
    BR.NN posr
    SUB R{data:zero}, R{data:dp}
posr:
    MOVE R{state:t}, R0
    SUB R{state:t}, R{in:y_r}
    MOVE R{state:t}, R0
    MULQ R{data:mu}, R{state:t}
    {write:g_r}
    MOVE R0, R{data:dp}
    CMP R{in:y_i}, R{data:zero}
    BR.NN posi
    SUB R{data:zero}, R{data:dp}
posi:
    MOVE R{state:t}, R0
    SUB R{state:t}, R{in:y_i}
    MOVE R{state:t}, R0
    MULQ R{data:mu}, R{state:t}
    {write:g_i}
    {jump:trig}
""",
        )


        # --- BCAST: single WEST face. Order is LOAD-BEARING: (1) y to OUT +
        # OUT's trigger (transits idle U cells), (2) g into every U (data,
        # all idle), (3) U triggers FARTHEST-FIRST so no jump ever transits an
        # already-running U whose face is mid-flip. ------------------------
        g_r_writes = "".join(
            "    {write:g_r_u%d}\n" % k for k in range(nt - 1, -1, -1))
        g_i_writes = "".join(
            "    {write:g_i_u%d}\n" % k for k in range(nt - 1, -1, -1))
        u_jumps = "".join(
            "    {jump:trig_u%d}\n" % k for k in range(nt))  # u0 = farthest
        bcast_outs = ([Port("y_r"), Port("y_i"), Port("out_trig")]
                      + [Port(f"g_r_u{k}") for k in range(nt)]
                      + [Port(f"g_i_u{k}") for k in range(nt)]
                      + [Port(f"trig_u{k}") for k in range(nt)])
        progs["bcast"] = CellProgram(
            inputs=[Port("y_r_in", register=1), Port("y_i_in", register=2),
                    Port("g_r_in", register=3), Port("g_i_in", register=4)],
            outputs=bcast_outs,
            entries=[EntryPoint("default")],
            data=[],
            state=[],
            assembly_template=("""\
start:
    MOVE R0, R{in:y_r_in}
    {write:y_r}
    MOVE R0, R{in:y_i_in}
    {write:y_i}
    {jump:out_trig}
    MOVE R0, R{in:g_r_in}
""" + g_r_writes + """\
    MOVE R0, R{in:g_i_in}
""" + g_i_writes + u_jumps),
        )

        # --- OUT: the block's complex output (yi, yq) + downstream trigger. -
        progs["out"] = CellProgram(
            inputs=[Port("yi_in", register=0), Port("yq_in", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[],
            assembly_template="""\
start:
    MOVE R0, R{in:yi_in}
    {write:yi}
    MOVE R0, R{in:yq_in}
    {write:yq}
    {jump:trig}
""",
        )

        # --- U_k: master taps + update. Resting face WEST (the transit
        # corridor); flips NORTH only to write the w mirror up, restores. ---
        for k in range(nt):
            init_wr = float_to_q15(0.5) if k == 0 else 0
            progs[f"u{k}"] = CellProgram(
                inputs=[Port("g_r", register=1), Port("g_i", register=2),
                        Port("hm_r", register=3), Port("hm_i", register=4)],
                outputs=[Port("wm_r"), Port("wm_i")],
                entries=[EntryPoint("default")],
                data=[DataWord("rail", 0x7FFF, address=5),
                      DataWord("face_up", 3, address=6, is_face=True),
                      DataWord("face_rest", 2, address=7, is_face=True)],
                # Master taps: packet-reset to the cold-start spike.
                state=[StateVar("wr", initial_value=init_wr,
                                reset_per_batch=True),
                       StateVar("wi", initial_value=0,
                                reset_per_batch=True)],
                assembly_template="""\
start:
    SUB R0, R0
    MACQ R{in:g_r}, R{in:hm_r}
    MACQ R{in:g_i}, R{in:hm_i}
    ADD R0, R{state:wr}
    BR.NV okr
    SHR R{state:wr}, #15
    ADD R0, R{data:rail}
okr:
    MOVE R{state:wr}, R0
    SUB R0, R0
    MACQ R{in:g_i}, R{in:hm_r}
    MSUQ R{in:g_r}, R{in:hm_i}
    ADD R0, R{state:wi}
    BR.NV oki
    SHR R{state:wi}, #15
    ADD R0, R{data:rail}
oki:
    MOVE R{state:wi}, R0
    MOVE [FACE], R{data:face_up}
    {write:wm_i}
    MOVE R0, R{state:wr}
    {write:wm_r}
    MOVE [FACE], R{data:face_rest}
""",
            )

        return progs

    # ------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        nt = self._nt
        conns: List[Tuple[Any, str, Any, str]] = [
            ("in", "h_r", "f0", "h_r_in"),
            ("in", "h_i", "f0", "h_i_in"),
        ]
        for k in range(nt):
            if k < nt - 1:
                conns += [
                    (f"f{k}", "h_r", f"f{k+1}", "h_r_in"),
                    (f"f{k}", "h_i", f"f{k+1}", "h_i_in"),
                    (f"f{k}", "acc_r", f"f{k+1}", "acc_r_in"),
                    (f"f{k}", "acc_i", f"f{k+1}", "acc_i_in"),
                ]
            conns += [
                (f"f{k}", "hm_r", f"u{k}", "hm_r"),
                (f"f{k}", "hm_i", f"u{k}", "hm_i"),
                (f"u{k}", "wm_r", f"f{k}", "wm_r"),
                (f"u{k}", "wm_i", f"f{k}", "wm_i"),
            ]
        conns += [
            (f"f{nt-1}", "acc_r", "sat", "acc_r"),
            (f"f{nt-1}", "acc_i", "sat", "acc_i"),
            ("sat", "y_r", "err", "y_r"),
            ("sat", "y_i", "err", "y_i"),
            ("err", "yf_r", "bcast", "y_r_in"),
            ("err", "yf_i", "bcast", "y_i_in"),
            ("err", "g_r", "bcast", "g_r_in"),
            ("err", "g_i", "bcast", "g_i_in"),
            ("bcast", "y_r", "out", "yi_in"),
            ("bcast", "y_i", "out", "yq_in"),
        ]
        for k in range(nt):
            conns += [
                ("bcast", f"g_r_u{k}", f"u{k}", "g_r"),
                ("bcast", f"g_i_u{k}", f"u{k}", "g_i"),
            ]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        nt = self._nt
        jumps: List[Tuple[Any, str, Any, str]] = [("in", "trig", "f0", "default")]
        for k in range(nt - 1):
            jumps.append((f"f{k}", "trig", f"f{k+1}", "default"))
        jumps += [
            (f"f{nt-1}", "trig", "sat", "default"),
            ("sat", "trig", "err", "default"),
            ("err", "trig", "bcast", "default"),
            ("bcast", "out_trig", "out", "default"),
        ]
        for k in range(nt):
            jumps.append(("bcast", f"trig_u{k}", f"u{k}", "default"))
        # OUT's trig fires the downstream consumer (patched by the build);
        # standalone it terminates.
        jumps.append(("out", "trig", "__terminate__", "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """8x2 fold, I/O co-located on the WEST edge (layout conventions 1-3)::

            col:   0     1    2    3    4    5    6       7
            row0:  IN(E) F0   F1   F2   F3   F4   SAT(E)  ERR(S)
            row1:  OUT   U0   U1   U2   U3   U4   tw(W)   BCAST(W)

        Resting faces ARE the corridors: F cells EAST (forward h/acc), U cells
        WEST (the y/g/trigger transit lane from BCAST), SAT EAST (to ERR),
        ERR SOUTH (to BCAST), BCAST WEST (everything it sends), ``tw`` a
        face-only transit continuing that corridor, OUT set by the build's
        egress patch. Every cell keeps ONE face except F (flips south for the
        h mirror) and U (flips north for the w mirror) — both flip-and-restore
        while nothing can be transiting them. ``num_taps < 5`` compacts."""
        nt = self._nt
        lay: Dict[Any, Tuple[int, int, str]] = {"in": (0, 0, "east")}
        for k in range(nt):
            lay[f"f{k}"] = (1 + k, 0, "east")
        lay["sat"] = (1 + nt, 0, "east")
        lay["err"] = (2 + nt, 0, "south")
        # BCAST precedes OUT and the U cells in BOTH the program-dict and the
        # layout order (they are paired POSITIONALLY): with bcast EARLIER, all
        # its broadcast edges are FORWARD in program order, so the internal-
        # feedback pass never "patches" them — its dest-register matching is
        # ambiguous across the g fan-out and clobbered the y_i hop (the
        # first-DUT zero-yq bug).
        lay["bcast"] = (2 + nt, 1, "west")
        lay["out"] = (0, 1, "east")
        for k in range(nt):
            lay[f"u{k}"] = (1 + k, 1, "west")
        # Face-only transit LAST (the program<->layout assignment is POSITIONAL
        # — program-dict order must equal layout order, transits after): keeps
        # BCAST's westbound corridor west across the column right of U(nt-1).
        lay["transit_w"] = (1 + nt, 1, "west")
        return lay
