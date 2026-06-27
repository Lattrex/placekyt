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
    fold/table cells::

        phase | sin_fold | cos_fold | table_sin | table_cos | upmix

      * phase:    holds phase; phase += freq; emit ph_sin (= phase) and
                  ph_cos (= phase + pi/2); forward I, Q. FREE-RUNNING (no feedback).
      * sin_fold/cos_fold: quarter-wave fold (phase -> table index + neg flag).
      * table_sin/table_cos: quarter-wave LUT (index + neg -> Q15 value).
      * upmix:    out = I*cos - Q*sin (the real passband sample).

    The NCO increments phase BEFORE the first sample (phase = freq at n=0) and
    uses the quantized quarter-wave table — match that in any reference.

    Interface: COMPLEX input (I at R0, Q at R1 of the phase landing cell); the
    output is the real passband sample.
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
        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("ph_sin"), Port("ph_cos"),
                     Port("xi_fwd"), Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("freq", self._freq_word, address=3),
                  DataWord("quarter", 16384, address=4)],
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

        # --- upmix cell: out = xi*cosv - xq*sinv (the real passband sample). ---
        upmix_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)],
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
        """Linear execution chain (each cell triggers the next)."""
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "upmix", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Linear datapath on one row, all cells facing east (the upmix exit
        cell's output is the block output, routed downstream by the build)."""
        return {cid: (i, 0, "east") for i, cid in enumerate(self._CELL_IDS)}

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
