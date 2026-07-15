# SPDX-License-Identifier: GPL-3.0-or-later
"""FrequencyModulatorBlock — see :class:`FrequencyModulatorBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, float_to_q15
from .nco_block import NCOBlock


class FrequencyModulatorBlock(NCOBlock):
    """
    Frequency Modulator (VCO) — drop-in for GNU Radio ``analog.frequency_modulator_fc``.

    A voltage-controlled oscillator: a REAL input drives the instantaneous phase of
    a unit-amplitude complex exponential.  For each input sample ``x[n]``::

        phase   += sensitivity · x[n]        (phase in RADIANS)
        out[n]   = cos(phase) + j·sin(phase)

    This is the FM modulator — the phase ACCUMULATES the (scaled) input, so a
    constant input becomes a fixed-frequency tone and an audio input becomes an
    FM-modulated passband signal.  It is the NCO's cos/sin table pipeline with ONE
    change: the phase increment is the RUNTIME INPUT (``sensitivity·x``) instead of
    a constant ``freq_word``.

    Parameters mirror GRC's **Frequency Mod** exactly (RULE #0):

      * ``sensitivity`` — radians of phase advance per unit input.  This is GR's
        ``frequency_modulator_fc(sensitivity)`` argument, in the SAME units.  In a
        real modem it is ``2π · f_dev / sample_rate``.

    There is NO amplitude/frequency/offset/phase parameter: GR's modulator emits on
    the UNIT circle (amplitude 1.0) starting at phase 0, exactly like this block.

    DATAPATH (10 cells) — identical to :class:`NCOBlock` except the phase cell
    -------------------------------------------------------------------------
    ``phase | {fold even odd interp}_sin | {fold even odd interp}_cos | emit``

    The phase cell now READS the input sample (Q15, ``R0``), scales it by the
    fixed Q15 constant ``kscale = sensitivity/π`` (so a Q15 input maps to
    16-bit phase-words: ``incr = (x_q15 · kscale) >> 15``), and ACCUMULATES it into
    the phase — instead of adding a constant ``freq_word``.  Every other cell (the
    quarter-wave table fold/even/odd/interp and the emit) is inherited verbatim.

    kscale derivation
    -----------------
    GR phase advance ``dphi = sensitivity · x`` radians.  On-chip the phase
    accumulator is 16-bit with ``2π ≡ 65536``, so ``dphi_word = dphi · 65536/2π =
    sensitivity · x · 65536/2π``.  The input arrives as Q15 (``x_q15 = x · 32768``),
    so ``dphi_word = (x_q15/32768) · sensitivity · 65536/2π = x_q15 · sensitivity/π``.
    Thus the on-chip multiplier is ``kscale = sensitivity/π`` applied via MULQ
    (``incr = (x_q15 · kscale_q15) >> 15``).  For the multiply to stay in range,
    ``kscale`` must be ≤ 1.0 in magnitude, i.e. ``sensitivity ≤ π`` — a full-scale
    input then advances a full radian per sample, the fastest meaningful FM.

    PRECISION — inherits the NCO's 33-entry-table floor (≈ 11 LSB)
    -------------------------------------------------------------
    The cos/sin reconstruction is the NCO's linear-interpolated quarter-wave table,
    so the amplitude/quadrature error floor is the same ≈ 11 LSB.  The phase-word
    quantisation (fs/65536 rad resolution) adds a small accumulating phase drift vs
    GR's float64 accumulator; verify against the Q15 reference (``process_reference``)
    which models BOTH effects op-for-op.
    """
    CATEGORY = "modulation"
    TAGS = ["fm", "vco", "frequency_modulator", "modulator", "modulators"]

    # Input is a REAL sample (drives phase); output is complex (unit-circle exp).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0, 1])

    def __init__(self, name: str, sensitivity: float = 1.0):
        # Reuse the NCO plumbing: unit amplitude, no offset/initial-phase, cos wave.
        # frequency=0 -> freq_word 0 (unused: the phase cell adds the INPUT, not a
        # constant), but keep the NCO ctor happy.  amplitude MUST be 1.0 (GR emits
        # on the unit circle).
        super().__init__(name, sample_rate=1.0, frequency=0.0, amplitude=1.0,
                         offset=0.0, phase=0.0, waveform="cos")
        self._sensitivity = float(sensitivity)
        # HARDWARE DEVIATION: sensitivity is limited to |s| <= pi so that
        # kscale = s/pi fits a Q15 MULQ multiplier (|kscale| <= 1.0).  GR accepts
        # any sensitivity; larger values would overflow the on-chip phase-scale
        # multiply.  In a real modem sensitivity = 2*pi*f_dev/fs is well under pi.
        if abs(self._sensitivity) > math.pi:
            raise ValueError(
                f"FrequencyModulatorBlock: |sensitivity| must be <= pi "
                f"(got {sensitivity}); the on-chip phase-scale multiplier is Q15. "
                f"HW-DEVIATION vs analog.frequency_modulator_fc.")
        # kscale = sensitivity/pi  (Q15).  incr_word = (x_q15 * kscale_q15) >> 15.
        self._kscale = self._sensitivity / math.pi
        self._kscale_q15 = float_to_q15(self._kscale)

    @property
    def sensitivity(self) -> float:
        return self._sensitivity

    @property
    def kscale_q15(self) -> int:
        """The derived Q15 phase-scale multiplier (sensitivity/pi)."""
        return self._kscale_q15

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        # Inherit every cell from the NCO, then REPLACE the phase cell with the
        # input-driven accumulator (phase += (x_q15 * kscale) >> 15).
        cells = super().build_cell_programs()
        cells["phase"] = CellProgram(
            # The REAL input sample lands in R0 (the block's single input port).
            inputs=[Port("x", register=0)],
            outputs=[Port("ph_sin"), Port("ph_cos"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kscale", self._kscale_q15, address=3),
                  DataWord("quarter", 16384, address=4)],
            state=[StateVar("phase", initial_value=0),
                   StateVar("incr")],
            # GR's frequency_modulator_fc ACCUMULATES FIRST, then emits:
            # phase += sensitivity*x[n];  out[n] = exp(j*phase).  So scale+add the
            # input BEFORE emitting the folds (the n=0 output is at phase =
            # sensitivity*x[0], NOT 0).  Snapshot x into `incr`, scale by kscale,
            # accumulate into phase, THEN emit ph_sin (phase) and ph_cos (phase+90).
            assembly_template="""\
start:
    MOVE R{state:incr}, R{in:x}
    MULQ R{state:incr}, R{data:kscale}
    MOVE R{state:incr}, R0
    ADD R{state:phase}, R{state:incr}
    MOVE R{state:phase}, R0
    {write:ph_sin}
    ADD R{state:phase}, R{data:quarter}
    MOVE R{state:phase}, R0
    {write:ph_cos}
    SUB R{state:phase}, R{data:quarter}
    MOVE R{state:phase}, R0
    {jump:trig}
""",
        )
        return cells

    # ------------------------------------------------------------ interface
    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples) -> np.ndarray:
        """Complex reference ``exp(j·phase_n)`` with ``phase += sensitivity·x`` — via
        the on-chip interpolated table + Q15 phase-scale, op-for-op.  ``input_samples``
        is the REAL modulating signal (float in [-1, 1])."""
        tbl = self._quarter_table()
        amp = self._s16(self._amp_q15)            # == 32767 (unit circle)
        kscale = self._s16(self._kscale_q15)
        x = np.asarray(input_samples, dtype=np.float64).reshape(-1)
        n = len(x)
        out = np.zeros(n, dtype=np.complex64)
        phase = 0
        for i in range(n):
            # GR accumulates FIRST, then emits: out[n] = exp(j*(phase += sens*x[n])).
            x_q15 = int(np.clip(round(x[i] * 32768.0), -32768, 32767))
            incr = self._s16(((x_q15 * kscale) >> 15) & 0xFFFF)
            phase = (phase + incr) & 0xFFFF
            cos = self._channel_q15((phase + 16384) & 0xFFFF, tbl, amp)
            sin = self._channel_q15(phase & 0xFFFF, tbl, amp)
            out[i] = complex(cos / 32768.0, sin / 32768.0)
        return out

    def process_reference_q15(self, input_samples) -> List[Tuple[int, int]]:
        """Bit-exact ``(yi, yq)`` unsigned Q15 pairs per input sample (I=cos, Q=sin),
        phase driven by ``phase += (x_q15 · kscale) >> 15``."""
        tbl = self._quarter_table()
        amp = self._s16(self._amp_q15)
        kscale = self._s16(self._kscale_q15)
        x = np.asarray(input_samples, dtype=np.float64).reshape(-1)
        out = []
        phase = 0
        for i in range(len(x)):
            x_q15 = int(np.clip(round(x[i] * 32768.0), -32768, 32767))
            incr = self._s16(((x_q15 * kscale) >> 15) & 0xFFFF)
            phase = (phase + incr) & 0xFFFF
            cos = self._channel_q15((phase + 16384) & 0xFFFF, tbl, amp) & 0xFFFF
            sin = self._channel_q15(phase & 0xFFFF, tbl, amp) & 0xFFFF
            out.append((cos, sin))
        return out

    def reset(self):
        self._phase = 0
