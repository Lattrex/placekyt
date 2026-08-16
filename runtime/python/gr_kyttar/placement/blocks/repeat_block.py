# SPDX-License-Identifier: GPL-3.0-or-later
"""RepeatBlock — hold-upsampler (GNU Radio ``blocks.repeat``). See the class."""
import numpy as np

from ..block import CellProgram, Port, EntryPoint, StateVar
from ._base import KyttarBlock, BlockInterface


class RepeatBlock(KyttarBlock):
    """Hold-upsampling rate expander — each input sample emitted ``interp`` times.

    The EXACT GNU Radio counterpart is ``blocks.repeat(gr.sizeof_float, interp)``:
    one input -> ``interp`` copies of that input, a pure pass-through (no Q15
    arithmetic), so the comparison is bit-exact. This is the SYMBOL-HOLD a
    shaped-envelope transmitter needs between its symbol mapper and an
    amplitude-envelope stage: the RaisedCosineEnvelopeBlock (PSK31) documents its
    input as the **held** ±A symbol stream at ``samples_per_symbol`` — i.e. the
    output of THIS block at ``interp = samples_per_symbol`` — where the
    zero-stuffing :class:`UpsamplerBlock` (the pulse-shaper front half) would
    feed it zeros between symbols and collapse the envelope.

    One input -> ``interp`` outputs (rate-EXPANDING): a single trigger emits a
    burst of ``interp`` WRITE+JUMP pairs, paced by the single-outstanding output
    handshake. ``interp`` is small and fixed, so the emit is UNROLLED (same
    single-cell construction as UpsamplerBlock; the unroll is 3 words per emitted
    copy, capping ``interp`` at 8 in one 32-word cell — larger factors RAISE,
    never silently truncate).

    Params mirror GNU Radio's GRC binding verbatim: ``interp`` (the repeat count).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["repeat", "hold", "upsample", "interpolate", "envelope"]

    MAX_INTERP = 8  # unrolled emit: 3 words/copy + prologue in a 32-word cell

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, interp: int = 4):
        if int(interp) < 1:
            raise ValueError(f"interp must be >= 1, got {interp}")
        if int(interp) > self.MAX_INTERP:
            raise ValueError(
                f"interp > {self.MAX_INTERP} not supported in a single cell "
                f"(unrolled emit); got {interp}")
        super().__init__(name, interp=int(interp))
        self._interp = int(interp)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interp(self) -> int:
        return self._interp

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        # Latch the input, then emit it interp times — one WRITE+JUMP per copy,
        # paced by the single-outstanding output handshake (cf. UpsamplerBlock,
        # which emits zeros after the first copy; here every copy is the sample).
        lines = ["start:", "    MOVE R{state:xs}, R{in:x}"]
        for _ in range(self._interp):
            lines += ["    MOVE R0, R{state:xs}", "    {write:out}",
                      "    {jump:out}"]
        lines.append("    HALT")
        template = "\n".join(lines) + "\n"
        return {0: CellProgram(
            inputs=[Port("x", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("xs")],
            assembly_template=template,
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Each input repeated interp times (uint16 Q15 words)."""
        out = []
        for w in x_q15:
            out.extend([int(w) & 0xFFFF] * self._interp)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        out = []
        for v in input_samples:
            out.extend([float(v)] * self._interp)
        return np.asarray(out, dtype=np.float32)

    def reset(self):
        pass
