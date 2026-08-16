# SPDX-License-Identifier: GPL-3.0-or-later
"""RaisedCosineEnvelopeBlock — see :class:`RaisedCosineEnvelopeBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import KyttarBlock, BlockInterface, float_to_q15, q15_to_float


# --- The NCO quarter-wave sine machinery, reused VERBATIM (INV-31) -------------
# The envelope env[n]=sin((n+0.5)*pi/N) is a SINE of a linearly-advancing phase.
# The PROVEN NCOBlock reconstructs sin() from a 33-entry quarter-wave Q15 table +
# linear interpolation — a table whose size is INDEPENDENT of N. That is exactly
# the mechanism that lets THIS block generate the envelope ON THE FLY for ANY sps
# (including the PSK31 default sps=256) with NO sps-entry table (PATH B). The
# helpers below mirror NCOBlock._quarter_table / _sine_mag_neg / _channel_q15
# op-for-op so process_reference is bit-exact to the on-fabric NCO sine column.
_TABLE_SIZE = 33


def _quarter_table() -> List[int]:
    """The 33-entry quarter-wave Q15 sine table sin(0deg)..sin(90deg) (NCOBlock)."""
    return [min(32767, int(round(math.sin((math.pi / 2) * k / 32) * 32768))) & 0xFFFF
            for k in range(_TABLE_SIZE)]


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _sine_mag_neg(phase16: int, tbl: List[int]) -> Tuple[int, int]:
    """Op-for-op copy of NCOBlock._sine_mag_neg: the interpolated POSITIVE
    magnitude + sign flag for a 16-bit phase (angle-fold + forward interp)."""
    phase16 &= 0xFFFF
    within = phase16 & 0x3FFF
    neg = phase16 >> 15
    mir = (phase16 >> 14) & 1
    q = (16384 - within) if mir else within
    idx = q >> 9
    frac = (q & 0x1FF) << 6
    P = _s16(tbl[idx])
    Q = _s16(tbl[idx + 1]) if idx < 32 else P
    mag = P + ((_s16((Q - P) & 0xFFFF) * frac) >> 15)
    return mag, neg


def nco_sine_q15(phase16: int, tbl: List[int] = None) -> int:
    """Signed Q15 sine of a 16-bit phase via the NCO quarter-wave interp table.

    This is the value the on-fabric fold/even/odd/interp sine column produces
    (identical to NCOBlock's sin channel BEFORE any amplitude MULQ)."""
    if tbl is None:
        tbl = _quarter_table()
    mag, neg = _sine_mag_neg(phase16, tbl)
    return -mag if neg else mag


class RaisedCosineEnvelopeBlock(KyttarBlock):
    """PSK31 (G3PLX) transmit AMPLITUDE-envelope shaper — NO stock GNU Radio block.

    PSK31, designed by Peter Martinez (G3PLX), shapes the BPSK carrier's 180 deg
    phase reversals with a **raised-cosine amplitude envelope** so the occupied
    bandwidth stays ~31 Hz. The amplitude is driven to (near) zero *at the instant
    of a phase reversal* and back to full over the symbol; where the phase does NOT
    reverse it stays at full. This is exactly ``cos-shaped envelope x BPSK carrier``
    reduced to baseband (the RF carrier multiply is a downstream NCO mix).

    SPEC / GOLDEN (cited)
    ---------------------
    The reference PSK31 synthesis (swharden, "Experiments in PSK-31 Synthesis",
    2022-10-16, faithful to the G3PLX design; QSL.net "What is PSK31?") computes a
    per-symbol amplitude envelope of ``samples_per_symbol`` (``N``) samples::

        env[n] = sin((n + 0.5) * pi / N)      for n = 0 .. N-1

    a raised-cosine HALF-BUMP that is ~0 at BOTH symbol boundaries and 1.0 at the
    symbol centre. It is applied CONDITIONALLY, gated by whether the phase reverses
    at each boundary of the symbol (swharden's ``rampUp`` / ``rampDown``):

      * first half  (n < N/2):  env[n] if this symbol REVERSES vs the PREVIOUS one
                                (a rising taper 0->1), else full amplitude 1.0.
      * second half (n >= N/2): env[n] if this symbol REVERSES vs the NEXT one
                                (a falling taper 1->0), else full amplitude 1.0.

    Output sample = ``symbol_value(+/-1) * amplitude``. The dip therefore straddles a
    phase reversal (...->1->0->1->... through the boundary), which is precisely the
    PSK31 "amplitude minimum at the phase shift" that halves the occupied bandwidth.

    INTERFACE (documented, self-contained)
    --------------------------------------
    * INPUT  — one real Q15 sample per output sample: the **upsampled BPSK symbol
      stream** (``+A`` for a ``+1`` symbol, ``-A`` for ``-1``, held constant across
      each symbol of ``N`` samples — the same "upsampled symbols" a pulse-shaper
      consumes; cf. RRCPulseShaperBlock).
    * OUTPUT — one real Q15 sample per input sample: the amplitude-envelope-shaped
      value ``symbol_value * amplitude``. (Real baseband envelope, NOT the RF
      passband — the carrier mix is a separate NCO/mixer stage.)
    * PARAM  — ``samples_per_symbol`` (default 256, the PSK31 norm at a typical
      audio sample rate: 8000 Hz / 31.25 baud ~= 256).

    BUILT (INV-31) — was quarantined; now an ON-THE-FLY (NCO-cosine) block
    ---------------------------------------------------------------------
    This block was PREVIOUSLY QUARANTINED (needs_human): the per-symbol envelope was
    modelled as an ``sps``-entry LOAD table (129 entries even folded for sps=256 >
    the LOAD & 0x1F 32-entry ceiling), and the reversal test appeared to need a full
    ``sps``-sample lookahead delay line. BOTH walls are removed WITHOUT any table and
    WITHOUT a deep buffer:

    * **Table wall -> on-the-fly NCO cosine (PATH B).** ``env[n]=sin((n+0.5)*pi/N)``
      is the SINE of a phase that advances by ``pi/N`` per sample from ``pi/(2N)``.
      The PROVEN NCO quarter-wave table (33 entries) + linear interpolation
      reconstructs that sine for ANY ``N`` with a table size INDEPENDENT of ``sps`` —
      so the PSK31 default sps=256 fits (no 129-entry table). The block holds a
      16-bit phase accumulator: ``phase0 = round(16384/N)``, increment
      ``round(32768/N)`` per within-symbol sample; the sine column (fold / even /
      odd / interp, reused from NCOBlock) turns that phase into the Q15 envelope.
      Envelope error vs the ideal ``sin`` is the NCO's DERIVED linear-interpolation
      floor (<= 11 LSB; :attr:`ENV_TOL_LSB`), NOT a tuned tolerance.

    * **Lookahead wall -> 1-symbol PIPELINE LATENCY, sign-only state.** A reversal is
      a SIGN CHANGE of the held symbol, detectable at SYMBOL granularity. The block
      emits with exactly ONE symbol of pipeline latency: while it streams out the
      shaped samples of symbol ``k`` it has ALREADY observed symbol ``k+1`` (the
      current input symbol), so ``rev_end(k)=sign(k)!=sign(k+1)`` is known WITHOUT a
      per-sample delay line. State is just three signs (``s_{k-1}``, ``s_k``,
      ``s_{k+1}``) + the within-symbol counter — NOT an ``sps``-deep FIFO. Documented
      group delay = ``sps`` samples (like GR ``blocks.delay``'s group delay).

    DATAPATH (7 cells): ``ingest -> phasegen -> sin_fold -> sin_even -> sin_odd ->
    sin_interp -> shape``, where ``ingest`` tracks the sign pipeline and within-symbol
    position, ``phasegen`` turns the position into the envelope phase, the sine column
    (fold/even/odd/interp, reused from NCOBlock) reconstructs ``env[pos]``, and
    ``shape`` selects the per-half envelope-vs-full amplitude by the reversal flags and
    does the final Q15 envelope multiply ``held_symbol * amp``.

    ``process_reference`` implements the EXACT op-for-op Q15 datapath (accumulated
    phase -> NCO-interp sine -> sign-pipeline select -> MULQ), verified faithful to
    the cited PSK31 golden within :attr:`ENV_TOL_LSB` across sps in {2,4,6,8,...,256}
    and mixed-reversal bit patterns. See lessons_log 2026-08-07.
    """
    CATEGORY = "modulators"
    TAGS = ["psk31", "envelope", "raised_cosine", "pulse_shaper", "modulators", "nco"]

    _interface = BlockInterface(entry_address=1, input_registers=[31],
                                output_registers=[31])

    # DERIVED tolerance vs the IDEAL sin() golden (NOT tuned): the on-the-fly NCO
    # cosine reconstructs the envelope from a 33-entry quarter-wave table + linear
    # interpolation, whose worst-case error vs the exact sine is the NCO's analytic
    # ~11 LSB floor (NCOBlock docstring); the final envelope MULQ adds <=1 LSB. So
    # the on-fabric envelope is within 12 LSB of the ideal env[n] for every sps.
    ENV_TOL_LSB = 12

    def __init__(self, name: str, samples_per_symbol: int = 256):
        """Args:
            name: block instance name.
            samples_per_symbol: N samples per PSK31 symbol (default 256, the PSK31
                norm at ~8 kHz / 31.25 baud). Any even N >= 2 is representable (the
                envelope is generated ON THE FLY; no sps-entry table).
        """
        sps = int(samples_per_symbol)
        if sps < 2 or sps % 2 != 0:
            raise ValueError(
                f"RaisedCosineEnvelopeBlock samples_per_symbol must be an even "
                f"integer >= 2 (the half-symbol taper split needs it); "
                f"got {samples_per_symbol}")
        super().__init__(name, samples_per_symbol=sps)
        self._sps = sps
        # 16-bit phase accumulator constants for the on-the-fly envelope sine.
        # theta_n = (n+0.5)*pi/N ; phase_word = round(theta/(2pi)*65536)
        #         = round((n+0.5)*32768/N). phase0 = round(16384/N); inc=round(32768/N).
        self._phase0 = round(16384.0 / sps) & 0xFFFF
        self._phase_inc = round(32768.0 / sps) & 0xFFFF
        self._tbl = _quarter_table()
        # env[n] as the on-fabric accumulated-phase NCO sine produces it (Q15).
        self._env_q15 = self._envelope_q15()

    # ------------------------------------------------------------------ envelope
    def _envelope_q15(self) -> List[int]:
        """The Q15 envelope the on-fabric NCO sine column emits, accumulated-phase.

        ``env[n] = nco_sine(phase0 + n*inc)`` — the SAME phase accumulator + quarter-
        wave interp the datapath runs, so this is bit-exact to the hardware sine."""
        out = []
        ph = self._phase0
        for _ in range(self._sps):
            out.append(nco_sine_q15(ph & 0xFFFF, self._tbl) & 0xFFFF)
            ph = (ph + self._phase_inc) & 0xFFFF
        return out

    @property
    def cell_count(self) -> int:
        # ingest (sign pipeline + counter) + phasegen (envelope phase accumulator)
        # + the NCO sine column (fold, even, odd, interp) + shape (select + MULQ).
        return 7

    @property
    def samples_per_symbol(self) -> int:
        return self._sps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def envelope(self) -> List[float]:
        """The generated raised-cosine envelope (float, one bump of sps samples)."""
        return [q15_to_float(v) for v in self._env_q15]

    @property
    def envelope_q15(self) -> List[int]:
        """The Q15 envelope the on-fabric NCO sine column emits (accumulated phase)."""
        return list(self._env_q15)

    # -------------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """The on-the-fly (PATH B) datapath: sign-pipeline + phase counter feeding
        the reused NCO quarter-wave sine column, then the envelope-select multiply.

        Cells (dict order = chain order):
          * ``ingest``  — per input sample: update the within-symbol counter ``pos``
            and, at each symbol boundary (pos wraps), shift the sign pipeline
            (s_prev<-s_held, s_held<-s_next=sign(input)). Emit the envelope phase
            ``phase0 + pos*inc`` to the sine column and the shaping operands
            (held symbol value, rev_start, rev_end, pos<N/2) to ``shape``.
          * ``sin_fold`` / ``sin_even`` / ``sin_odd`` / ``sin_interp`` — the NCO
            quarter-wave interpolated-sine column (reused VERBATIM from NCOBlock), it
            turns the phase into the Q15 envelope value ``env[pos]``.
          * ``shape`` — pick ``amp = env[pos]`` on a tapering half (rev_start on the
            1st half, rev_end on the 2nd) else full 0x7FFF, then emit
            ``held_symbol * amp`` (Q15 MULQ).

        This mirrors the NCO's proven fold/even/odd/interp reconvergence; only the
        phase SOURCE (a within-symbol counter, not a free-running frequency word) and
        the final envelope-select multiply differ. The sign pipeline gives the block
        its documented 1-symbol group delay.
        """
        even_tbl = [self._tbl[2 * j] for j in range(17)]
        odd_tbl = [self._tbl[2 * j + 1] for j in range(16)] + [0]
        N = self._sps
        half = N // 2

        # --- ingest: within-symbol counter + sign pipeline + shaping flags -------
        # The block emits with 1-symbol PIPELINE LATENCY: while the input streams a
        # new symbol it EMITS the PREVIOUS symbol (s_prev), for which the NEXT symbol
        # (s_held, now arriving) is already known. Three raw-sample stages:
        #   s_pp (2 symbols old) | s_prev (EMITTED) | s_held (arriving/next)
        #   rev_start = (s_prev - s_pp)   nonzero => reversal INTO s_prev (1st half)
        #   rev_end   = (s_prev - s_held) nonzero => reversal OUT of s_prev (2nd half)
        # At a symbol boundary (pos==0) the pipeline shifts: s_pp<-s_prev,
        # s_prev<-s_held, s_held<-cur. Values are the RAW held Q15 samples (so symv
        # keeps the +/-A amplitude; a "reversal" is a nonzero sample difference).
        # firsthalf = pos - half (NEGATIVE => 1st half). ingest emits symv/rs/re/
        # firsthalf to `shape` and pos to `phasegen`. Cold start s_*=0 => the first
        # sps outputs are the pipeline FILL (leading zero symbol).
        ingest = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("pos"), Port("symv"), Port("rs"), Port("re"),
                     Port("firsthalf"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=1),
                  DataWord("nsym", N, address=2),
                  DataWord("half", half, address=3),
                  DataWord("zero", 0, address=4)],
            state=[StateVar("pos", initial_value=0),
                   StateVar("s_pp", initial_value=0),
                   StateVar("s_prev", initial_value=0),
                   StateVar("s_held", initial_value=0)],
            assembly_template="""\
start:
    CMP R{state:pos}, R{data:one}
    BR.GE nobound
    MOVE R{state:s_pp}, R{state:s_prev}
    MOVE R{state:s_prev}, R{state:s_held}
    MOVE R{state:s_held}, R{in:sample}
nobound:
    MOVE R0, R{state:pos}
    {write:pos}
    MOVE R0, R{state:s_prev}
    {write:symv}
    SUB R{state:s_prev}, R{state:s_pp}
    {write:rs}
    SUB R{state:s_prev}, R{state:s_held}
    {write:re}
    SUB R{state:pos}, R{data:half}
    {write:firsthalf}
    ADD R{state:pos}, R{data:one}
    MOVE R{state:pos}, R0
    CMP R{state:pos}, R{data:nsym}
    BR.LT done
    MOVE R{state:pos}, R{data:zero}
done:
    {jump:trig}
""",
        )

        # --- phasegen: within-symbol phase accumulator (no multiply) -------------
        # Receives pos; a within-symbol phase accumulator: at pos==0 reset
        # phase<-phase0, else phase<-phase+inc; emits phase to the sine column. Kept
        # separate from ingest so each cell fits one 32-word cell (ingest's sign
        # pipeline + counter is already register-dense).
        phasegen = CellProgram(
            inputs=[Port("pos", register=0)],
            outputs=[Port("ph"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("phase0", self._phase0, address=1),
                  DataWord("inc", self._phase_inc, address=2),
                  DataWord("zero", 0, address=3),
                  DataWord("one", 1, address=4)],
            state=[StateVar("phase", initial_value=self._phase0)],
            assembly_template="""\
start:
    CMP R{in:pos}, R{data:zero}
    BR.NZ accum
    MOVE R{state:phase}, R{data:phase0}
    CMP R{data:zero}, R{data:one}
    BR.N emit
accum:
    ADD R{state:phase}, R{data:inc}
    MOVE R{state:phase}, R0
emit:
    MOVE R0, R{state:phase}
    {write:ph}
    {jump:trig}
""",
        )

        # --- NCO sine column (reused verbatim from NCOBlock) --------------------
        def _fold_cell():
            return CellProgram(
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
            # Emit the SIGNED interpolated envelope magnitude (env is always >=0 for
            # a half-bump, but keep the general NCO sign path for exactness).
            return CellProgram(
                inputs=[Port("eval", register=0), Port("oval", register=1),
                        Port("par", register=2), Port("frac", register=3),
                        Port("neg", register=4)],
                outputs=[Port("env"), Port("trig")],
                entries=[EntryPoint("default")],
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
    MOVE R{state:d}, R0
    CMP R{in:neg}, R{data:zero}
    BR.Z out
    SUB R{data:zero}, R{state:d}
    MOVE R{state:d}, R0
out:
    MOVE R0, R{state:d}
    {write:env}
    {jump:trig}
""",
            )

        # --- shape: select env vs full by the reversal flags, then MULQ ---------
        # shape receives env[pos] (from the sine column) plus the shaping operands
        # forwarded from ingest (symv=held symbol, rs=rev_start diff, re=rev_end diff,
        # firsthalf = pos-half : NEGATIVE (N flag) on the FIRST half). amp = full
        # (0x7FFF) unless the relevant-half reversal (nonzero diff) is set, then env.
        # out = symv * amp (Q15). rev/env are chosen with forward BR + fall-through
        # (no intra-cell JUMP): pick rev = firsthalf ? rs : re into R{state:rev}, then
        # if rev==0 keep full else use env.
        shape = CellProgram(
            inputs=[Port("env", register=0), Port("symv", register=1),
                    Port("rs", register=2), Port("re", register=3),
                    Port("firsthalf", register=4)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("full", 0x7FFF, address=5),
                  DataWord("zero", 0, address=6)],
            state=[StateVar("amp"), StateVar("rev")],
            assembly_template="""\
start:
    MOVE R{state:amp}, R{data:full}
    MOVE R{state:rev}, R{in:re}
    CMP R{in:firsthalf}, R{data:zero}
    BR.NN checkrev
    MOVE R{state:rev}, R{in:rs}
checkrev:
    CMP R{state:rev}, R{data:zero}
    BR.Z emit
    MOVE R{state:amp}, R{in:env}
emit:
    MOVE R0, R{in:symv}
    MULQ R0, R{state:amp}
    {write:out}
    {jump:out}
""",
        )

        return {
            "ingest": ingest,
            "phasegen": phasegen,
            "sin_fold": _fold_cell(), "sin_even": _even_cell(),
            "sin_odd": _odd_cell(), "sin_interp": _interp_cell(),
            "shape": shape,
        }

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        return [
            ("ingest", "pos", "phasegen", "pos"),
            ("phasegen", "ph", "sin_fold", "phase"),
            ("sin_fold", "idx_e", "sin_even", "idx"),
            ("sin_fold", "idx_o", "sin_odd", "idx"),
            ("sin_fold", "frac", "sin_interp", "frac"),
            ("sin_fold", "neg", "sin_interp", "neg"),
            ("sin_even", "eval", "sin_interp", "eval"),
            ("sin_even", "par", "sin_interp", "par"),
            ("sin_odd", "oval", "sin_interp", "oval"),
            ("sin_interp", "env", "shape", "env"),
            # ingest forwards the shaping operands directly to shape.
            ("ingest", "symv", "shape", "symv"),
            ("ingest", "rs", "shape", "rs"),
            ("ingest", "re", "shape", "re"),
            ("ingest", "firsthalf", "shape", "firsthalf"),
        ]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        chain = ["ingest", "phasegen", "sin_fold", "sin_even", "sin_odd",
                 "sin_interp", "shape"]
        return [(chain[i], "trig", chain[i + 1], "default")
                for i in range(len(chain) - 1)]

    def output_cell_ids(self) -> List[str]:
        return ["shape"]

    # 1-symbol pipeline latency (documented group delay), in samples.
    @property
    def latency(self) -> int:
        return self._sps

    # ------------------------------------------------------------------
    # Q15-exact predictor — op-for-op with the on-fabric datapath: the sign
    # PIPELINE (ingest), the accumulated-phase NCO envelope (phasegen + sine
    # column), and the envelope-select MULQ (shape). BIT-EXACT to simKYT.
    # ------------------------------------------------------------------
    def process_reference_q15(self, input_samples) -> List[int]:
        """The EXACT per-trigger on-fabric output (signed Q15 ints), op-for-op.

        Models the datapath cell-for-cell:
          * ingest keeps ``s_pp, s_prev, s_held`` (three symbol signs) and the
            within-symbol counter ``pos``; at each symbol boundary (pos wraps to 0)
            it shifts the pipeline (``s_pp<-s_prev, s_prev<-s_held, s_held<-cur``).
            It EMITS the symbol ``s_prev`` — one symbol OLD — so ``rev_end`` (does the
            emitted symbol reverse vs the NEXT one, ``s_held``) is already known: this
            is the block's 1-symbol PIPELINE LATENCY (group delay = sps samples), NOT
            a per-sample delay line. ``rev_start = s_prev != s_pp``,
            ``rev_end = s_prev != s_held``.
          * phasegen + the sine column produce ``env[pos]`` via the accumulated-phase
            NCO quarter-wave interp (:func:`nco_sine_q15`).
          * shape: ``amp = env[pos]`` on a tapering half (rev_start on the 1st half,
            rev_end on the 2nd) else full 0x7FFF; out = ``s_prev*A * amp`` (Q15 MULQ).

        Cold start: ``s_pp=s_prev=s_held=0`` so the first ``sps`` outputs are the
        pipeline FILL (the leading zero symbol) — exactly what simKYT emits."""
        def mulq(a, b):
            # Hardware MULQ = arithmetic ``(A*B) >> 15`` (floor toward -inf, NO
            # rounding add), per PROGRAMMING_GUIDE 4.4 — bit-exact to simKYT incl. the
            # -1 LSB on negatives.
            return _s16((_s16(a) * _s16(b)) >> 15)

        arr = np.asarray(input_samples).ravel()
        N = self._sps
        half = N // 2
        q = [float_to_q15(float(v)) if not np.issubdtype(arr.dtype, np.integer)
             else _s16(int(v)) for v in arr]
        env = self._env_q15
        pos = 0
        # The pipeline holds RAW Q15 sample values (exactly as the ingest cell does);
        # a "reversal" is a nonzero DIFFERENCE of adjacent held samples (a sign flip
        # for a +/-A BPSK stream), so rev_start/rev_end are those differences != 0.
        s_pp = s_prev = s_held = 0
        out: List[int] = []
        for x in q:
            cur = _s16(x)
            if pos == 0:
                s_pp, s_prev, s_held = s_prev, s_held, cur
            rev_start = ((s_prev - s_pp) != 0)
            rev_end = ((s_prev - s_held) != 0)
            if pos < half:
                amp = env[pos] if rev_start else 0x7FFF
            else:
                amp = env[pos] if rev_end else 0x7FFF
            out.append(mulq(s_prev, amp))
            pos = (pos + 1) % N
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float view of :meth:`process_reference_q15` — the EXACT on-fabric output
        (1-symbol pipeline latency included), Q15-quantized to float32."""
        return np.asarray([q15_to_float(v) for v in
                           self.process_reference_q15(input_samples)],
                          dtype=np.float32)

    def process_reference_ideal(self, input_samples: np.ndarray) -> np.ndarray:
        """The CITED PSK31 golden (swharden/G3PLX), directly gated, NO pipeline
        latency — ``env[n]=sin((n+0.5)*pi/N)`` (IDEAL sine) applied on the 1st half
        iff the symbol reverses vs the PREVIOUS, on the 2nd iff vs the NEXT, else
        full. This is the spec target; :meth:`process_reference` (the on-fabric
        datapath) matches it within :attr:`ENV_TOL_LSB` in steady state, shifted by
        the 1-symbol group delay."""
        def mulq(a, b):
            return _s16((_s16(a) * _s16(b) + (1 << 14)) >> 15)
        arr = np.asarray(input_samples).ravel()
        N = self._sps
        n = len(arr)
        q = [float_to_q15(float(v)) if not np.issubdtype(arr.dtype, np.integer)
             else (int(v) & 0xFFFF) for v in arr]
        sgn = [1 if _s16(v) > 0 else (-1 if _s16(v) < 0 else 0) for v in q]
        env = [float_to_q15(math.sin((k + 0.5) * math.pi / N)) for k in range(N)]
        out = np.zeros(n, dtype=np.float32)
        for i in range(n):
            pos = i % N
            cur = sgn[i]
            prev = sgn[i - N] if i - N >= 0 else cur
            nxt = sgn[i + N] if i + N < n else cur
            rs = (cur != prev)
            re = (cur != nxt)
            amp = (env[pos] if (rs if pos < N // 2 else re) else 0x7FFF)
            out[i] = q15_to_float(mulq(q[i], amp))
        return out

    def reset(self):
        pass
