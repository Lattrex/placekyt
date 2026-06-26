# SPDX-License-Identifier: GPL-3.0-or-later
"""UpsamplerBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class UpsamplerBlock(KyttarBlock):
    """Zero-stuffing rate expander — the front half of an interpolating filter.

    On each input sample it emits ``sps`` outputs: the sample itself followed by
    ``sps - 1`` ZEROS. This is the standard pulse-shaping upsampler: feed its output
    to an RRC pulse shaper and the pair is GNU Radio's
    ``filter.interp_fir_filter_fff(sps, rrc_taps)`` (insert sps−1 zeros, then filter).
    The on-chip RRC pulse shaper expects an ALREADY-upsampled stream, so this block
    sits between the symbol mapper and the RRC in the TX chain.

    One input -> ``sps`` outputs (rate-EXPANDING), so a single trigger emits a burst
    of ``sps`` WRITE+JUMP pairs. ``sps`` is small and fixed (default 4, matching the
    RRC pulse shaper's SAMPLES_PER_SYMBOL), so the emit is UNROLLED. Single cell.

    Params mirror the interpolation factor: ``sps`` (samples per symbol). Exact
    pass-through of the kept sample (no Q15 arithmetic); the stuffed samples are 0.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["upsample", "interpolate", "zero_stuff", "pulse_shaping"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, sps: int = 4):
        if int(sps) < 1:
            raise ValueError(f"sps must be >= 1, got {sps}")
        if int(sps) > 8:
            raise ValueError(f"sps > 8 not supported (unrolled emit); got {sps}")
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
        # Emit the sample, then sps-1 zeros — one WRITE+JUMP per output. The output
        # port handshake paces each emission (single-outstanding), so the burst is
        # delivered in order. The LAST emit carries the jump that closes the entry.
        lines = ["start:", "    MOVE R{state:xs}, R{in:x}",
                 "    MOVE R0, R{state:xs}", "    {write:out}", "    {jump:out}"]
        for _ in range(self._sps - 1):
            lines += ["    MOVE R0, R{data:zero}", "    {write:out}", "    {jump:out}"]
        lines.append("    HALT")
        template = "\n".join(lines) + "\n"
        return {0: CellProgram(
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1)],
            state=[StateVar("xs")],
            assembly_template=template,
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Each input followed by sps-1 zeros (uint16 Q15 words)."""
        out = []
        for w in x_q15:
            out.append(int(w) & 0xFFFF)
            out.extend([0] * (self._sps - 1))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        out = []
        for v in input_samples:
            out.append(float(v))
            out.extend([0.0] * (self._sps - 1))
        return np.asarray(out, dtype=np.float32)
