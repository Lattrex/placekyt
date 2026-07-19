# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexUpsamplerBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class ComplexUpsamplerBlock(KyttarBlock):
    """Complex (I/Q) zero-stuffing rate expander — the front half of a complex
    interpolating pulse-shaper.

    The 2-rail twin of :class:`UpsamplerBlock`. On each complex input sample
    ``(xi, xq)`` it emits ``sps`` complex outputs: the sample itself followed by
    ``sps - 1`` ZERO pairs ``(0, 0)``. Feed its output to a complex RRC filter
    (:class:`ComplexRRCMatchedFilterBlock`, reused here as a pulse shaper) and the
    pair is GNU Radio's::

        filter.interp_fir_filter_ccc(sps, rrc_taps)

    (insert sps-1 complex zeros, then complex-filter). This block sits between the
    QPSK symbol mapper (which emits a complex constellation point per symbol) and
    the complex RRC shaper in the QPSK modem's TX chain — the QPSK analog of the
    BPSK modem's real ``PSKSymbolMapper -> Upsampler -> RRCPulseShaper`` front end.

    Each complex output is a yi/yq PACKET (``WRITE yi; WRITE yq; JUMP`` — two
    operands into the downstream cell's R0/R1, ONE trigger), the same complex-packet
    contract the ComplexCostasLoop / matched filter emit ([[INV-17]]). One input ->
    ``sps`` complex outputs (rate-EXPANDING), so a single trigger emits a burst of
    ``sps`` yi/yq packets; ``sps`` is small and fixed (default 2 for a QPSK modem,
    up to 8), so the emit is UNROLLED. Single cell.

    Params mirror the interpolation factor: ``sps`` (samples per symbol). Exact
    pass-through of the kept sample (no Q15 arithmetic); the stuffed samples are 0.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["upsample", "interpolate", "zero_stuff", "pulse_shaping", "complex"]

    # Complex landing: xi@R0, xq@R1 (the ComplexCostasLoop / MF complex-input
    # convention). Output is the interpolated complex stream (yi, yq).
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    # HARDWARE LIMIT: the unrolled complex emit is 3 words per output packet
    # (WRITE yi; WRITE yq; JUMP), so a single cell holds at most sps=4 (sps=5
    # overflows the ~31-word cell; verified test_complex_upsampler ceiling). The
    # real (single-rail) UpsamplerBlock reaches sps=8 because its packet is only
    # 2 words (WRITE; JUMP); the complex block's 3-word packet halves the ceiling.
    # A QPSK modem runs at sps=2 (comfortably within); larger complex upsampling
    # would need a multi-cell burst emitter (not built — a documented substrate cap).
    MAX_SPS = 4

    def __init__(self, name: str, sps: int = 2):
        if int(sps) < 1:
            raise ValueError(f"sps must be >= 1, got {sps}")
        if int(sps) > self.MAX_SPS:
            # HARDWARE LIMIT (raise, never silently clamp — INV-0): the unrolled
            # 3-word complex packet caps a single-cell complex upsampler at sps=4.
            raise ValueError(
                f"HARDWARE LIMIT: ComplexUpsampler sps > {self.MAX_SPS} does not "
                f"fit one cell (the 3-word yi/yq packet emit overflows ~31 words); "
                f"got {sps}. A larger factor needs a multi-cell burst emitter.")
        super().__init__(name, sps=int(sps))
        self._sps = int(sps)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def sps(self) -> int:
        return self._sps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # Save xi/xq to state (R0 is clobbered by the first emit), then emit the
        # kept complex sample as a yi/yq PACKET, then sps-1 zero packets. Each
        # packet is WRITE yi; WRITE yq; JUMP (two operands, one trigger) — the
        # complex-packet contract a downstream complex block consumes (yi->R0,
        # yq->R1). The output port handshake paces each packet (single-outstanding),
        # so the burst is delivered in order. The LAST packet's jump closes the entry.
        lines = ["start:",
                 "    MOVE R{state:xis}, R{in:xi}",
                 "    MOVE R{state:xqs}, R{in:xq}",
                 # kept sample: (yi, yq) = (xi, xq)
                 "    MOVE R0, R{state:xis}",
                 "    {write:yi}",
                 "    MOVE R0, R{state:xqs}",
                 "    {write:yq}",
                 "    {jump:trig}"]
        for _ in range(self._sps - 1):
            lines += ["    MOVE R0, R{data:zero}",
                      "    {write:yi}",
                      "    MOVE R0, R{data:zero}",
                      "    {write:yq}",
                      "    {jump:trig}"]
        lines.append("    HALT")
        template = "\n".join(lines) + "\n"
        return {0: CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2)],
            state=[StateVar("xis"), StateVar("xqs")],
            assembly_template=template,
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, iq_q15) -> list:
        """Each complex input followed by sps-1 zero pairs, as (yi, yq) uint16
        Q15 pairs. ``iq_q15`` is a list of (xi, xq) uint16 pairs."""
        out = []
        for (i, q) in iq_q15:
            out.append((int(i) & 0xFFFF, int(q) & 0xFFFF))
            out.extend([(0, 0)] * (self._sps - 1))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Zero-stuff a complex stream: each sample followed by sps-1 complex zeros.
        Accepts a complex array or an (N,2) [i,q] real array; returns a complex
        numpy array (the interpolated stream)."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            syms = [complex(c) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            syms = [complex(float(i), float(q)) for i, q in arr]
        else:
            syms = [complex(float(v), 0.0) for v in arr]
        out = []
        for s in syms:
            out.append(s)
            out.extend([0.0 + 0.0j] * (self._sps - 1))
        return np.asarray(out, dtype=np.complex64)
