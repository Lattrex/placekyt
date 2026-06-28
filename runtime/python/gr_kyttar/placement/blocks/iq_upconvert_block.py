"""IQUpconvertBlock — see :class:`IQUpconvertBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class IQUpconvertBlock(KyttarBlock):
    """
    I/Q passband upconverter — production 6-cell implementation.

    Produces a REAL passband signal from a complex baseband (I, Q)::

        s[n] = I[n]*cos(phase) - Q[n]*sin(phase)        phase += freq (free-run)

    Unlike :class:`ComplexMixerBlock` (single-axis: ``out = input*cos`` only),
    this combines BOTH quadrature arms with the I/Q carrier — exactly what
    QAM16 / PSK passband TX needs. Validated bit-exact on-chip and to corr=1.0
    / 1-LSB vs an ideal continuous ``I*cos - Q*sin`` in
    the internal reference implementation.

    Cells (free-running NCO + dual mixer), reusing the proven quarter-wave NCO
    fold/table cells, on a COMPACT 4x2 serpentine so the upmix->phase UNLOCK
    return is a short authored corridor::

        col:     0            1             2            3
        row 0:  phase(E)     sin_fold(E)   cos_fold(E)  table_sin(S)
        row 1:  trans(N)     trans(W)      upmix(W)     table_cos(W)

      * phase:    holds phase; phase += freq; emit ph_sin (= phase) and
                  ph_cos (= phase + pi/2); forward I, Q. FREE-RUNNING (no
                  algorithmic feedback). After emitting, it LOCKs its own
                  arbiter to the SOUTH face (the unlock corridor) so the NEXT
                  sample on its input face is HELD until upmix has consumed the
                  current one — see "Fan-in serialization" below.
      * sin_fold/cos_fold: quarter-wave fold (phase -> table index + neg flag).
      * table_sin/table_cos: quarter-wave LUT (index + neg -> Q15 value).
      * upmix:    out = I*cos - Q*sin (the real passband sample). After emitting
                  it CLEARS phase's LOCK via a backward WRITE.CFG down the
                  row-1 corridor (@3), releasing the held next sample.

    The NCO increments phase BEFORE the first sample (phase = freq at n=0) and
    uses the quantized quarter-wave table — match that in any reference.

    Interface: COMPLEX input (I at R0, Q at R1 of the phase landing cell); the
    output is the real passband sample.

    Fan-in serialization (the burst race + the LOCK fix)
    ====================================================
    ``upmix`` is a RECONVERGENT FAN-IN: it gets xi/xq from ``phase`` (a SHORT
    path) and sinv/cosv from the table cells (a LONGER path), both derived from
    the SAME ``phase`` sample. Fed one sample per trigger this is fine, but fed a
    BACK-TO-BACK BURST (e.g. an upsampler emitting ``sps`` samples in one
    program), ``phase`` runs sample N+1 and re-drives upmix's xi/xq onto the
    shared bus BEFORE sample N's sinv/cosv (still walking the long path) have
    been consumed by upmix — so upmix mixes xi from one sample with the carrier
    of another and the ``sps`` outputs collapse to one wrong value.

    All of upmix's inputs arrive on a SINGLE bus face (verified from the built
    bitstream), so the race cannot be separated AT upmix by a per-face LOCK. It
    is fixed by SERIALIZING at the source: ``phase`` processes exactly one
    sample, then LOCKs its own arbiter to the SOUTH face (``LOCK_FACE`` = the
    unlock corridor, the one face with no input traffic). The arbiter then gates
    OFF every OTHER face, so the next sample's data+JUMP sit cleanly
    backpressured on phase's input face (no data loss). When ``upmix`` finishes
    the current sample it CLEARS phase's LOCK with a ``WRITE.CFG`` routed back up
    the row-1 corridor; phase's input face re-opens and the held sample flows in,
    re-triggering phase — which locks again. This is RATE-GENERAL (independent of
    ``sps`` / the upstream burst length): exactly one sample is in the
    phase..upmix pipeline at a time. The LOCK semantics (gate-all-but-LOCK_FACE,
    held req released on clear) are the same arbiter LOCK the DFE decision cell
    uses; here it gates the block's SOURCE rather than a fan-in consumer.
    """
    CATEGORY = "modulation"
    TAGS = ["upconvert", "passband", "mixer", "iq", "modulation"]

    QUARTER_SIZE = 17

    # Landing cell is the phase cell; complex input lands at R0 (I) and R1 (Q).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0]
    )

    _CELL_IDS = [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos", "upmix",
    ]

    def __init__(self, name: str, sample_rate: float = 32000.0,
                 frequency: float = 2000.0):
        """
        Args:
            name: Block name.
            sample_rate: sample rate in Hz (GR sig_source ``sampling_freq``).
            frequency: carrier frequency in Hz (GR sig_source ``frequency``). The
                16-bit NCO phase increment is derived internally:
                ``freq_word = round(frequency/sample_rate · 65536)``.

        Parameters mirror GR's mixing **Signal Source** in DSP units — NOT the raw
        hardware ``freq_word`` (the sibling NCO/ComplexMixer expose Hz the same
        way; the GR equivalent is ``multiply_cc(baseband, sig_source_c(samp_rate,
        COS, frequency, 1, 0, ph0)) -> complex_to_real`` with
        ``ph0 = 2π·frequency/sample_rate`` for the increment-before-emit NCO).
        """
        super().__init__(name, sample_rate=sample_rate, frequency=frequency)
        self._sample_rate = float(sample_rate)
        self._frequency = float(frequency)
        self._freq_word = round(self._frequency / self._sample_rate * 65536) & 0xFFFF
        self._phase = 0  # reference-model state

    @property
    def cell_count(self) -> int:
        return 6

    @property
    def frequency(self) -> float:
        """Carrier frequency in Hz (as requested)."""
        return self._frequency

    @property
    def freq_word(self) -> int:
        """The derived 16-bit NCO phase increment per sample."""
        return self._freq_word

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _quarter_wave_table(self) -> List[int]:
        return [
            int(round(math.sin(k / 16 * math.pi / 2) * 32767)) & 0xFFFF
            for k in range(self.QUARTER_SIZE)
        ]

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The 6 cells (NCO fold/table proven in the Costas redesign + a dual
        mixer). Keyed by the string cell ids in ``_CELL_IDS`` order."""
        qt = self._quarter_wave_table()

        # --- phase cell: free-running NCO; emit ph_sin (= phase), ph_cos
        # (= phase + pi/2); forward I, Q. ph_sin = phase directly (the upconvert
        # uses +sin, unlike Costas's -sin derotation). ---
        # ``lock_face`` = the SOUTH face code (0) the unlock corridor arrives on;
        # it is an ``is_face`` constant so it transforms with the block's
        # orientation. ``one`` enables the LOCK. After phase emits its sample it
        # sets LOCK_FACE=SOUTH and LOCK=1, gating off EVERY OTHER face (its input
        # face included) so the next sample is HELD until upmix clears the lock.
        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("ph_sin"), Port("ph_cos"),
                     Port("xi_fwd"), Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("freq", self._freq_word, address=3),
                  DataWord("quarter", 16384, address=4),
                  DataWord("lock_face", 0, address=5, is_face=True),
                  DataWord("one", 1, address=6)],
            state=[StateVar("phase"), StateVar("xis"), StateVar("xqs")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    ADD R{state:phase}, R{data:freq}
    MOVE R{state:phase}, R0
    {write:ph_sin}
    ADD R0, R{data:quarter}
    {write:ph_cos}
    MOVE R0, R{state:xis}
    {write:xi_fwd}
    MOVE R0, R{state:xqs}
    {write:xq_fwd}
    {jump:trig}
    MOVE R0, R{data:lock_face}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
""",
        )

        def _fold_cell():
            return CellProgram(
                inputs=[Port("phase", register=0)],
                outputs=[Port("neg"), Port("idx"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("thirtytwo", 32, address=1),
                      DataWord("fifteen", 15, address=2),
                      DataWord("sixteen", 16, address=3),
                      DataWord("zero", 0, address=4)],
                state=[StateVar("ph"), StateVar("fidx"), StateVar("loc")],
                assembly_template="""\
start:
    MOVE R{state:ph}, R{in:phase}
    SHR R{state:ph}, #10
    MOVE R{state:fidx}, R0
    AND R{state:fidx}, R{data:thirtytwo}
    {write:neg}
    AND R{state:fidx}, R{data:fifteen}
    MOVE R{state:loc}, R0
    AND R{state:fidx}, R{data:sixteen}
    CMP R0, R{data:zero}
    BR.Z nomir
    SUB R{data:sixteen}, R{state:loc}
    MOVE R{state:loc}, R0
nomir:
    MOVE R0, R{state:loc}
    {write:idx}
    {jump:trig}
""",
            )

        def _table_cell():
            data = [DataWord(f"qt{i}", v, address=2 + i)
                    for i, v in enumerate(qt)]
            data += [DataWord("tbase", 2, address=19),
                     DataWord("zero", 0, address=20)]
            return CellProgram(
                inputs=[Port("idx", register=0), Port("neg", register=1)],
                outputs=[Port("val"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data, state=[StateVar("v")],
                assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    MOVE R{state:v}, R0
    CMP R{in:neg}, R{data:zero}
    BR.Z out
    SUB R{data:zero}, R{state:v}
out:
    {write:val}
    {jump:trig}
""",
            )

        # --- upmix cell: out = xi*cosv - xq*sinv (the real passband sample).
        # DUAL-FACE + LOCK release: after computing out, upmix (1) flips FACE to
        # ``face_internal`` (its resting WEST, toward the row-1 unlock corridor)
        # and CLEARS phase's arbiter LOCK with a backward ``WRITE.CFG @3, 4`` (R0
        # = 0 -> phase CONFIG[4]=LOCK), releasing the next sample HELD at phase;
        # then (2) flips FACE to ``face_tap`` and emits ``out`` as its LAST WRITE
        # (so the build's ``_patch_last_write_handoff`` points it at the
        # downstream route). ``out`` is saved in ``acc`` because the WRITE.CFG's
        # ``MOVE R0, zero`` clobbers R0. ``@3`` is the fixed authored corridor
        # distance upmix(2,1) -> trans(1,1) -> trans(0,1) -> phase(0,0); the
        # corridor faces are authored in ``default_layout`` and orientation-
        # transformed with the block, so the hop is layout-invariant. The
        # face_internal/face_tap constants are ``is_face`` DataWords the build
        # sets from the actual placement (``_apply_rotate_tap_face``), exactly as
        # the Costas ``rotate`` cell does. ---
        upmix_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4),
                  DataWord("face_internal", 2, address=5, is_face=True),
                  DataWord("face_tap", 2, address=6, is_face=True)],
            state=[StateVar("xis"), StateVar("xqs"), StateVar("sv"),
                   StateVar("cv"), StateVar("acc")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    MOVE R{state:sv}, R{in:sinv}
    MOVE R{state:cv}, R{in:cosv}
    MULQ R{state:xis}, R{state:cv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:sv}
    SUB R{state:acc}, R0
    MOVE R{state:acc}, R0
    MOVE [FACE], R{data:face_internal}
    MOVE R0, R{data:zero}
    WRITE.CFG @3, 4
    MOVE [FACE], R{data:face_tap}
    MOVE R0, R{state:acc}
    {write:out}
    {jump:trig}
""",
        )

        return {
            "phase": phase_cell,
            "sin_fold": _fold_cell(),
            "cos_fold": _fold_cell(),
            "table_sin": _table_cell(),
            "table_cos": _table_cell(),
            "upmix": upmix_cell,
        }

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Feed-forward data handoffs (no feedback)."""
        return [
            ("phase", "ph_sin", "sin_fold", "phase"),
            ("phase", "ph_cos", "cos_fold", "phase"),
            ("phase", "xi_fwd", "upmix", "xi"),
            ("phase", "xq_fwd", "upmix", "xq"),
            ("sin_fold", "idx", "table_sin", "idx"),
            ("sin_fold", "neg", "table_sin", "neg"),
            ("cos_fold", "idx", "table_cos", "idx"),
            ("cos_fold", "neg", "table_cos", "neg"),
            ("table_sin", "val", "upmix", "sinv"),
            ("table_cos", "val", "upmix", "cosv"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """Linear execution chain (each cell triggers the next). upmix is the
        last cell AND the output cell: its ``trig`` SELF-TERMINATES
        (``__terminate__``) so the build does NOT default that JUMP through the
        row-1 unlock corridor (which would loop back into phase). Same idiom as
        the ComplexCostasLoop pd_pi ``trig``."""
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "upmix", "default"),
            ("upmix", "trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """COMPACT 4x2 serpentine (replaces the old straight line) so the upmix
        -> phase UNLOCK return is a short authored row-1 corridor::

            col:     0            1             2            3
            row 0:  phase(E)     sin_fold(E)   cos_fold(E)  table_sin(S)
            row 1:  trans(N)     trans(W)      upmix(W)     table_cos(W)

        Forward datapath (each cell's fwd_face followed to the next):
          phase(0,0,E) -> sin_fold(1,0,E) -> cos_fold(2,0,E) -> table_sin(3,0,S)
            -> table_cos(3,1,W) -> upmix(2,1,W).
        table_cos ABUTS upmix (@1), and every phase->upmix forwarded handoff
        (xi/xq) traces along this single connected face-path, exactly as the
        ComplexCostasLoop layout (upmix sits where the Costas ``rotate`` does).

        UNLOCK corridor (upmix's backward WRITE.CFG @3 to phase):
          upmix(2,1,W) -> trans(1,1,W) -> trans(0,1,N) -> phase(0,0), so the
          unlock lands on phase's SOUTH face (phase's ``LOCK_FACE``). The two
          FACE-only transit cells carry NO program (ids start with ``transit``).
        upmix's resting (``face_internal``) face is WEST = the first corridor
        hop; its ``out`` egresses on the route-overridden ``face_tap``."""
        return {
            "phase": (0, 0, "east"),
            "sin_fold": (1, 0, "east"),
            "cos_fold": (2, 0, "east"),
            "table_sin": (3, 0, "south"),
            "table_cos": (3, 1, "west"),
            "upmix": (2, 1, "west"),          # out exits here; abuts table_cos (@1)
            "transit_unlock_0": (1, 1, "west"),   # upmix -> phase unlock corridor
            "transit_unlock_1": (0, 1, "north"),  # corner up into phase (SOUTH face)
        }

    def output_cell_id(self) -> Any:
        """The real passband sample (``out``) leaves from the UPMIX cell, which
        sits in the MIDDLE of the compact layout (the row-1 unlock transit cells
        follow it). placeKYT routes the block output from here, not the last
        placed cell."""
        return "upmix"

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference Q15 I/Q upconvert. ``input_samples`` is complex (or (N,2)
        real [I,Q]). Returns the real passband sample as Q15 int16."""
        def s16(v): return v - 0x10000 if v & 0x8000 else v
        def u16(v): return v & 0xFFFF
        def mq(a, b): return u16((s16(a) * s16(b)) >> 15)
        qt = self._quarter_wave_table()

        def qw(ph16):
            fi = (ph16 >> 10) & 0x3F
            neg = (fi & 32) != 0
            mir = (fi & 16) != 0
            lo = fi & 15
            if mir:
                lo = 16 - lo
            v = qt[lo]
            return u16(-s16(v)) if neg else v

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(x) & 0xFFFF, int(y) & 0xFFFF) for x, y in arr]
        else:
            iq = [(float_to_q15(float(x)), 0) for x in arr]

        phase = 0
        out = []
        for (xi, xq) in iq:
            phase = u16(phase + self._freq_word)  # increment BEFORE emit
            cosv = qw(u16(phase + 16384))
            sinv = qw(phase)
            out.append(s16(u16(s16(mq(xi, cosv)) - s16(mq(xq, sinv)))))
        return np.array(out, dtype=np.int16)

    def reset(self):
        """Reset the reference-model phase accumulator."""
        self._phase = 0
