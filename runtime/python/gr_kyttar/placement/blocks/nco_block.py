# SPDX-License-Identifier: GPL-3.0-or-later
"""NCOBlock — see :class:`NCOBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15


class NCOBlock(KyttarBlock):
    """
    Signal Source — drop-in for GNU Radio ``analog.sig_source_c`` (complex cosine).

    A numerically-controlled oscillator that emits the complex exponential
    ``amplitude · exp(jθ_n)`` — ``I = amplitude·cos θ_n``, ``Q = amplitude·sin θ_n``
    — with ``θ_n = 2π · frequency/sample_rate · n``. Each input sample is a TRIGGER
    (its value is ignored); one complex output is produced per trigger.

    Parameters mirror GRC's **Signal Source** (the complex sig_source) in the
    user's units — NOT an internal fixed-point ``freq_word``:

      * ``sample_rate``  — sample rate in Hz.
      * ``frequency``    — tone frequency in Hz.
      * ``amplitude``    — output amplitude (0..1), applied as a Q15 gain.
      * ``offset``       — GR ``sig_source_c`` DC offset: a real bias added to the
        I (real) channel only (Q unchanged), matching GR's behaviour when given a
        real-valued offset. Default 0.
      * ``phase``        — initial phase θ₀ in RADIANS (GR ``sig_source_c`` phase);
        maps to the 16-bit phase accumulator's start value. Default 0.
      * ``waveform``     — ``"cos"`` (the complex exponential cos + j·sin; GR's
        ``GR_COS_WAVE`` for ``sig_source_c``). Only the complex cosine is built.

    The phase increment is derived internally:
    ``freq_word = round(frequency / sample_rate · 65536)`` (a 16-bit phase word).

    PRECISION — a 33-entry table + interpolation, ~10 LSB (a derived floor)
    ---------------------------------------------------------------------
    GNU Radio's ``sig_source_c`` is effectively EXACT (measured 0.002 LSB vs
    ``amp·exp(jθ)``). The Kyttar NCO reconstructs the sine from a **33-entry
    quarter-wave Q15 table** (``sin(0°)..sin(90°)`` at 2.8125° steps) with LINEAR
    INTERPOLATION on the phase fraction (``idx_bits = 7``: the top 7 phase bits
    pick the table interval, the low 9 interpolate). Worst-case error vs the exact
    tone is **≈ 11 LSB** (≤ 1 LSB on phase that lands on a table grid point) — the
    analytic linear-interpolation bound of a 33-point quarter table, a derived
    fixed-point limit (cf. the IIR pole-precision limit), NOT a tuned tolerance.

    Off-grid ``freq_word`` additionally DRIFTS vs GR's exact ``frequency`` (the
    16-bit phase word → fs/65536 Hz resolution; the drift grows with n). Verify on
    GRID-ALIGNED frequencies (integer ``freq_word``) to isolate the table floor.

    DATAPATH (10 cells)
    -------------------
    ``phase | {fold even odd interp}_sin | {fold even odd interp}_cos | emit``

      * phase: holds the 16-bit phase; emits the CURRENT phase to the sin fold and
        ``phase+90°`` (=phase+16384) to the cos fold, THEN increments by freq_word
        — so the n=0 output is at phase 0 = ``(amp, 0)``, matching GR.
      * fold: fold the quadrant symmetry **into the angle** (so interpolation is
        always FORWARD ``table[idx] → table[idx+1]``): within-quadrant angle ``w``,
        ``q_angle = mirror ? 16384−w : w``; emit ``idx = q_angle>>9`` (0..32),
        ``frac = (q_angle&0x1FF)<<6`` (Q15), and the ``neg`` sign (the upper
        semicircle).
      * even / odd: the 33-entry table is split by PARITY — even cell holds
        ``table[0,2,…,32]`` (17 entries), odd cell ``table[1,3,…,31]``. Since
        ``idx`` and ``idx+1`` always have OPPOSITE parity, each cell does exactly
        ONE unconditional LOAD (no range test, no straddle) — this is what keeps
        each table cell inside the 32-register/cell budget.
      * interp: re-pair the two looked-up samples by ``idx``'s parity into
        ``P=table[idx]``, ``Q=table[idx+1]`` and compute ``P + (Q−P)·frac``.
      * emit: apply the ``neg`` sign and the amplitude (Q15 MULQ), then write
        ``yi`` (=I=cos) and ``yq`` (=Q=sin) — two writes from one cell, ONE net
        wired out (the harness de-interleaves), per the complex-egress convention.

    Interface: complex TRIGGER input (R0/R1, ignored) so the block drives through
    the complex harness; complex output (yi, yq).
    """
    CATEGORY = "sources"
    TAGS = ["nco", "oscillator", "signal_source", "sig_source", "sources"]

    TABLE_SIZE = 33  # quarter-wave entries 0..32 = sin(0°)..sin(90°)

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    _CELL_IDS = ["phase",
                 "sin_fold", "sin_even", "sin_odd", "sin_interp",
                 "cos_fold", "cos_even", "cos_odd", "cos_interp",
                 "emit"]

    def __init__(self, name: str, sample_rate: float = 32000.0,
                 frequency: float = 2000.0, amplitude: float = 0.9,
                 offset: float = 0.0, phase: float = 0.0,
                 waveform: str = "cos"):
        super().__init__(name, sample_rate=sample_rate, frequency=frequency,
                         amplitude=amplitude, offset=offset, phase=phase,
                         waveform=waveform)
        self._sample_rate = float(sample_rate)
        self._frequency = float(frequency)
        self._amplitude = float(amplitude)
        self._offset = float(offset)
        self._init_phase = float(phase)
        if str(waveform).lower().replace("_wave", "").replace("gr_", "") not in (
                "cos", "complex", "exp"):
            raise ValueError(
                f"NCOBlock builds the complex cosine (GR_COS_WAVE); got "
                f"waveform={waveform!r}")
        self._waveform = waveform
        self._freq_word = round(self._frequency / self._sample_rate * 65536) & 0xFFFF
        self._amp_q15 = float_to_q15(self._amplitude)
        # GR sig_source_c params offset + phase (initial phase in radians).
        # phase -> the 16-bit phase accumulator's initial value; offset -> a Q15
        # DC bias added to the output (GR's offset is complex but is applied to
        # the real-valued sum per channel; the common case is a real offset).
        self._offset_q15 = float_to_q15(self._offset)
        self._phase0_word = round(self._init_phase / (2.0 * math.pi) * 65536) & 0xFFFF
        self._phase = self._phase0_word  # reference-model state

    @property
    def cell_count(self) -> int:
        return 10

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def frequency(self) -> float:
        """Tone frequency in Hz (as requested)."""
        return self._frequency

    @property
    def freq_word(self) -> int:
        """The derived 16-bit phase increment per sample."""
        return self._freq_word

    def _quarter_table(self) -> List[int]:
        return [min(32767, int(round(math.sin((math.pi / 2) * k / 32) * 32768))) & 0xFFFF
                for k in range(self.TABLE_SIZE)]

    def _even_odd_tables(self):
        t = self._quarter_table()
        even = [t[2 * j] for j in range(17)]          # table[0,2,..,32]
        odd = [t[2 * j + 1] for j in range(16)] + [0]  # table[1,3,..,31] + pad
        return even, odd

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        even_tbl, odd_tbl = self._even_odd_tables()

        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],  # trigger only
            outputs=[Port("ph_sin"), Port("ph_cos"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("freq", self._freq_word, address=3),
                  DataWord("quarter", 16384, address=4)],
            # Initial phase (GR sig_source_c `phase`, radians -> 16-bit word).
            state=[StateVar("phase", initial_value=self._phase0_word)],
            assembly_template="""\
start:
    MOVE R0, R{state:phase}
    {write:ph_sin}
    ADD R{state:phase}, R{data:quarter}
    {write:ph_cos}
    ADD R{state:phase}, R{data:freq}
    MOVE R{state:phase}, R0
    {jump:trig}
""",
        )

        def _fold_cell():
            return CellProgram(
                # idx is emitted as TWO separate writes (idx_e -> even, idx_o ->
                # odd): a single output port fanned out to multiple cells only
                # reliably delivers to the FIRST destination (the 2nd/3rd get 0).
                inputs=[Port("phase", register=0)],
                outputs=[Port("idx_e"), Port("idx_o"), Port("frac"), Port("neg"),
                         Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("mask3fff", 0x3FFF, address=1),
                      DataWord("one", 1, address=2),
                      DataWord("c16384", 16384, address=3),
                      DataWord("zero", 0, address=4)],
                state=[StateVar("ph"), StateVar("w"), StateVar("mir")],
                assembly_template="""\
start:
    MOVE R{state:ph}, R{in:phase}
    MOVE R{state:w}, R{state:ph}
    AND R{state:w}, R{data:mask3fff}
    MOVE R{state:w}, R0
    MOVE R{state:mir}, R{state:ph}
    SHR R{state:mir}, #14
    MOVE R{state:mir}, R0
    SHR R{state:mir}, #1
    {write:neg}
    AND R{state:mir}, R{data:one}
    CMP R0, R{data:zero}
    BR.Z nomir
    SUB R{data:c16384}, R{state:w}
    MOVE R{state:w}, R0
nomir:
    MOVE R{state:ph}, R{state:w}
    SHL R{state:ph}, #7
    MOVE R{state:ph}, R0
    SHR R{state:ph}, #1
    {write:frac}
    SHR R{state:w}, #9
    {write:idx_e}
    {write:idx_o}
    {jump:trig}
""",
            )

        def _even_cell():
            # Table at addresses 1..17 so the LOAD address is jE + 1 (tbase == one).
            data = [DataWord(f"e{j}", v, address=1 + j) for j, v in enumerate(even_tbl)]
            data += [DataWord("one", 1, address=1 + len(even_tbl))]
            return CellProgram(
                inputs=[Port("idx", register=0)],
                outputs=[Port("eval"), Port("par"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data, state=[StateVar("p")],
                assembly_template="""\
start:
    MOVE R{state:p}, R{in:idx}
    AND R{state:p}, R{data:one}
    {write:par}
    ADD R{state:p}, R0
    MOVE R{state:p}, R0
    SHR R{state:p}, #1
    MOVE R{state:p}, R0
    ADD R{state:p}, R{data:one}
    LOAD R0
    {write:eval}
    {jump:trig}
""",
            )

        def _odd_cell():
            data = [DataWord(f"o{j}", v, address=1 + j) for j, v in enumerate(odd_tbl)]
            data += [DataWord("one", 1, address=1 + len(odd_tbl))]
            return CellProgram(
                inputs=[Port("idx", register=0)],
                outputs=[Port("oval"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data, state=[StateVar("p")],
                assembly_template="""\
start:
    MOVE R{state:p}, R{in:idx}
    AND R{state:p}, R{data:one}
    SUB R{state:p}, R0
    MOVE R{state:p}, R0
    SHR R{state:p}, #1
    MOVE R{state:p}, R0
    ADD R{state:p}, R{data:one}
    LOAD R0
    {write:oval}
    {jump:trig}
""",
            )

        def _interp_cell():
            return CellProgram(
                # par (idx&1) comes from the even cell — the fold's idx fans only
                # to even/odd as separate writes, never to a 3rd cell.
                inputs=[Port("eval", register=0), Port("oval", register=1),
                        Port("par", register=2), Port("frac", register=3),
                        Port("neg", register=4)],
                outputs=[Port("mag"), Port("negf"), Port("trig")],
                entries=[EntryPoint("default")],
                # Data MUST sit past the 5 explicit input registers (R0..R4): the
                # resolver allocates state from gap = range(next_data_addr, base),
                # so data low at 1..2 would push the gap onto R3/R4 and collide
                # state with the frac/neg inputs.
                data=[DataWord("zero", 0, address=5)],
                state=[StateVar("p"), StateVar("Pe"), StateVar("Po"),
                       StateVar("d")],
                assembly_template="""\
start:
    MOVE R{state:Pe}, R{in:eval}
    MOVE R{state:Po}, R{in:oval}
    CMP R{in:par}, R{data:zero}
    BR.Z evencase
    MOVE R{state:p}, R{state:Pe}
    MOVE R{state:Pe}, R{state:Po}
    MOVE R{state:Po}, R{state:p}
evencase:
    SUB R{state:Po}, R{state:Pe}
    MOVE R{state:d}, R0
    MULQ R{state:d}, R{in:frac}
    MOVE R{state:d}, R0
    ADD R{state:d}, R{state:Pe}
    {write:mag}
    MOVE R0, R{in:neg}
    {write:negf}
    {jump:trig}
""",
            )

        emit_cell = CellProgram(
            inputs=[Port("cos_mag", register=0), Port("sin_mag", register=1),
                    Port("cos_neg", register=2), Port("sin_neg", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            # `off` = the GR sig_source_c `offset`, a Q15 DC bias added to each
            # channel after amplitude+sign (offset==0 -> ADD R0,off is a no-op).
            data=[DataWord("amp", self._amp_q15, address=4),
                  DataWord("zero", 0, address=5),
                  DataWord("off", self._offset_q15, address=6)],
            state=[StateVar("cv"), StateVar("sv")],
            assembly_template="""\
start:
    MOVE R{state:cv}, R{in:cos_mag}
    MOVE R{state:sv}, R{in:sin_mag}
    MULQ R{state:cv}, R{data:amp}
    MOVE R{state:cv}, R0
    MULQ R{state:sv}, R{data:amp}
    MOVE R{state:sv}, R0
    CMP R{in:cos_neg}, R{data:zero}
    BR.Z cpos
    SUB R{data:zero}, R{state:cv}
    MOVE R{state:cv}, R0
cpos:
    ADD R{state:cv}, R{data:off}
    {write:yi}
    CMP R{in:sin_neg}, R{data:zero}
    BR.Z spos
    SUB R{data:zero}, R{state:sv}
    MOVE R{state:sv}, R0
spos:
    MOVE R0, R{state:sv}
    {write:yq}
    {jump:trig}
""",
        )

        return {
            "phase": phase_cell,
            "sin_fold": _fold_cell(), "sin_even": _even_cell(),
            "sin_odd": _odd_cell(), "sin_interp": _interp_cell(),
            "cos_fold": _fold_cell(), "cos_even": _even_cell(),
            "cos_odd": _odd_cell(), "cos_interp": _interp_cell(),
            "emit": emit_cell,
        }

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        conns = [
            ("phase", "ph_sin", "sin_fold", "phase"),
            ("phase", "ph_cos", "cos_fold", "phase"),
        ]
        for ch in ("sin", "cos"):
            conns += [
                (f"{ch}_fold", "idx_e", f"{ch}_even", "idx"),
                (f"{ch}_fold", "idx_o", f"{ch}_odd", "idx"),
                (f"{ch}_fold", "frac", f"{ch}_interp", "frac"),
                (f"{ch}_fold", "neg", f"{ch}_interp", "neg"),
                (f"{ch}_even", "eval", f"{ch}_interp", "eval"),
                (f"{ch}_even", "par", f"{ch}_interp", "par"),
                (f"{ch}_odd", "oval", f"{ch}_interp", "oval"),
                (f"{ch}_interp", "mag", "emit", f"{ch}_mag"),
                (f"{ch}_interp", "negf", "emit", f"{ch}_neg"),
            ]
        return conns

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        chain = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp",
                 "cos_fold", "cos_even", "cos_odd", "cos_interp", "emit"]
        return [(chain[i], "trig", chain[i + 1], "default")
                for i in range(len(chain) - 1)]

    def output_cell_ids(self) -> List[str]:
        """The single external output cell (the complex emit)."""
        return ["emit"]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # Two-row serpentine modelled on the proven ComplexRRCMatchedFilter layout.
        # The FACE = each cell's egress direction matching the flow (this is what
        # makes egress work, not I/O co-location): row 0 (sin) flows EAST; its last
        # cell turns the corner SOUTH to the cos rail; row 1 (cos) flows WEST and
        # ends at emit, which egresses WEST to the bus.
        #
        #   col:    0          1        2        3        4
        #   row 0: phase ->  sin_fold sin_even sin_odd sin_interp(South)
        #   row 1: emit  <-  cos_interp cos_odd cos_even cos_fold
        # Column-major serpentine: col 0 flows SOUTH (phase down to sin_interp),
        # col 1 flows NORTH (cos_fold up to emit). emit (output) sits at the top of
        # col 1 and egresses EAST to the bus; faces match each cell's flow.
        col0 = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp"]
        col1_bottom_up = ["cos_fold", "cos_even", "cos_odd", "cos_interp", "emit"]
        layout = {}
        for j, cid in enumerate(col0):
            face = "east" if cid == "sin_interp" else "south"
            layout[cid] = (0, j, face)
        for k, cid in enumerate(col1_bottom_up):
            y = 4 - k
            face = "east" if cid == "emit" else "north"
            layout[cid] = (1, y, face)
        return layout

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(v):
        v &= 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def _sine_mag_neg(self, phase16: int, tbl: List[int]):
        """The on-chip POSITIVE interpolated magnitude + the sign flag for a 16-bit
        phase — mirrors the fold + even/odd + interp cells op-for-op (angle-fold,
        forward interp). The emit cell applies amplitude THEN the sign, so the
        reference returns them separately and the caller applies amp before neg."""
        s16 = self._s16
        phase16 &= 0xFFFF
        within = phase16 & 0x3FFF
        neg = phase16 >> 15
        mir = (phase16 >> 14) & 1
        q = (16384 - within) if mir else within
        idx = q >> 9
        frac = (q & 0x1FF) << 6
        P = s16(tbl[idx])
        Q = s16(tbl[idx + 1]) if idx < 32 else P
        mag = P + ((s16((Q - P) & 0xFFFF) * frac) >> 15)
        return mag, neg

    def _channel_q15(self, phase16, tbl, amp):
        """One channel's signed Q15 output: amplitude FIRST (Q15 MULQ), THEN the
        sign — exactly the emit cell's order (``neg ? -((mag·amp)>>15) : ...``)."""
        mag, neg = self._sine_mag_neg(phase16, tbl)
        v = (mag * amp) >> 15
        return -v if neg else v

    def process_reference(self, input_samples) -> np.ndarray:
        """Complex reference ``amplitude·(cos θ_n + j sin θ_n)`` via the on-chip
        interpolated table; ``input_samples`` is only a trigger count."""
        tbl = self._quarter_table()
        amp = self._s16(self._amp_q15)
        off = self._s16(self._offset_q15)
        n = len(input_samples)
        out = np.zeros(n, dtype=np.complex64)
        phase = self._phase0_word
        for i in range(n):
            cos = self._channel_q15((phase + 16384) & 0xFFFF, tbl, amp) + off
            sin = self._channel_q15(phase, tbl, amp)
            out[i] = complex(cos / 32768.0, sin / 32768.0)
            phase = (phase + self._freq_word) & 0xFFFF
        return out

    def process_reference_q15(self, input_samples) -> List[Tuple[int, int]]:
        """Bit-exact on-chip predictor: ``(yi, yq)`` unsigned Q15 pairs per trigger
        (I=cos, Q=sin). Includes the initial phase + DC offset (GR sig_source_c)."""
        tbl = self._quarter_table()
        amp = self._s16(self._amp_q15)
        off = self._s16(self._offset_q15)
        out = []
        phase = self._phase0_word
        for _ in range(len(input_samples)):
            cos = (self._channel_q15((phase + 16384) & 0xFFFF, tbl, amp) + off) & 0xFFFF
            sin = self._channel_q15(phase, tbl, amp) & 0xFFFF
            out.append((cos, sin))
            phase = (phase + self._freq_word) & 0xFFFF
        return out

    def reset(self):
        self._phase = 0
