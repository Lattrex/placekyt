"""ComplexGainBlock — see :class:`ComplexGainBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ComplexGainBlock(KyttarBlock):
    """Complex fixed-gain scaler — mirrors GNU Radio ``blocks.multiply_const_cc(gain)``.

    Multiplies a complex (I, Q) stream by the SAME real constant ``gain`` on BOTH
    rails (out = gain * in), so the constellation is scaled WITHOUT rotation or
    distortion — the receiver gain-staging stage a matched filter needs before a
    fixed-threshold decision-directed loop (the 16-QAM RX: the MF output is
    attenuated ~2.8x by the MF's Q15 headroom pre-scale, and the DD Costas + slicer
    have FIXED decision thresholds that assume the constellation at its nominal scale,
    so the MF output MUST be scaled back up).

    Q15 gain > 1: the datapath is Q15 [-1, 1), but ``gain`` may exceed 1 (a receiver
    amplifies). The block applies gain as an INTEGER doubling plus a Q15 fractional
    MULQ so any ``gain`` in (0, 4) is exact-to-Q15::

        n = floor(gain)                      # integer part (0..3)
        frac = gain - n                      # fractional part in [0,1)
        out = n * in + MULQ(in, frac_q15)    # n doublings via ADD + one MULQ

    (For n <= 1 this is a single MULQ; the receiver uses n=2, frac≈0.6.) Each rail
    (I, Q) is scaled by the identical gain, one cell, two MULQ/ADD chains + a saturate.

    Interface: complex (xi @R0, xq @R1) in, complex (yi, yq) out. One cell.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["gain", "complex", "scaler", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0, 1])

    def __init__(self, name: str, gain: float = 1.0):
        """Args: name; gain (real multiplier applied to both I and Q, 0 < gain < 4)."""
        g = float(gain)
        if not (0.0 < g < 4.0):
            raise ValueError(f"ComplexGainBlock gain must be in (0, 4); got {gain}")
        super().__init__(name, gain=g)
        self._gain = g
        self._n = int(g)                      # integer doublings (0..3)
        self._frac_q15 = float_to_q15(g - self._n) & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _scale_rail(self, src: str) -> str:
        """Emit ``R0 = gain * R{state:src}`` (saturating): MULQ(src, frac) then add
        ``src`` n times. ``src`` is a STATE reg holding the sample (survives R0 use)."""
        parts = [f"    MULQ R{{state:{src}}}, R{{data:frac}}",
                 f"    MOVE R{{state:acc}}, R0"]
        for _ in range(self._n):
            parts.append(f"    ADD R{{state:acc}}, R{{state:{src}}}")
            parts.append(f"    MOVE R{{state:acc}}, R0")
        parts.append(f"    MOVE R0, R{{state:acc}}")
        return "\n".join(parts)

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        return {0: CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("frac", self._frac_q15, address=2)],
            state=[StateVar("xis"), StateVar("xqs"), StateVar("acc")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
""" + self._scale_rail("xis") + """
    {write:yi}
""" + self._scale_rail("xqs") + """
    {write:yq}
    {jump:trig}
""",
        )}

    def process_reference(self, input_samples: np.ndarray):
        """Q15-exact: out = n*in + MULQ(in, frac), saturating (matches the cell)."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):
            return s16((s16(a) * s16(b) + (1 << 14)) >> 15)

        def sat(v):
            return max(-32768, min(32767, v))

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(a) & 0xFFFF, int(b) & 0xFFFF) for a, b in arr]
        else:
            iq = [(float_to_q15(float(v)), 0) for v in arr]
        out = []
        for (xi, xq) in iq:
            row = []
            for x in (xi, xq):
                acc = mulq(x, self._frac_q15)
                for _ in range(self._n):
                    acc = sat(acc + s16(x))
                row.append(acc & 0xFFFF)
            out.append((row[0], row[1]))
        return np.array(out, dtype=np.int32)

    def reset(self):
        pass
