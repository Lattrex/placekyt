"""QAM16SoftDemapperBlock — see :class:`QAM16SoftDemapperBlock`."""
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class QAM16SoftDemapperBlock(KyttarBlock):
    """
    16-QAM Soft Demapper Block (1 cell) — per-axis approximate LLRs.

    Produces 4 soft bits (LLRs) from a received (I, Q) sample, using the standard
    separable max-log approximation for Gray-coded 16-QAM. The sign convention is
    "LLR >= 0  =>  bit 0", consistent with the QAM16SlicerBlock decisions
    (MSB bit = (v >= 0); LSB bit = (|v| < t)). So:

        I axis -> b3 (MSB), b2 (LSB);  Q axis -> b1 (MSB), b0 (LSB)
        LLR(MSB) = -v          (v >= 0 -> bit 1 -> LLR < 0)
        LLR(LSB) = |v| - t     (|v| <  t -> inner level -> bit 1 -> LLR < 0)

    where t = 2/sqrt(10). Both are scaled by an llr_gain to keep them in Q15 range
    for a downstream FEC decoder.

    Outputs the four LLRs in MSB-first order: llr_b3, llr_b2, llr_b1, llr_b0.

    Interface:
        - Entry: R1
        - Inputs: I (R0), Q (R1)
        - Outputs: four LLRs (Q15)
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "soft_demap", "llr", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[31, 30],
                                output_registers=[31])

    def __init__(self, name: str, llr_gain: float = 0.5):
        super().__init__(name, llr_gain=llr_gain)
        self._thresh_q15 = float_to_q15(2.0 / (10.0 ** 0.5))
        self._gain_q15 = float_to_q15(max(0.0, min(0.999, llr_gain)))

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _axis_cell(self, in_name, msb_chan, lsb_chan, fwd_chan,
                   fwd_value_chan=None):
        """One axis: emit MSB LLR = gain*(-v) and LSB LLR = gain*(|v|-t); then
        forward the OTHER axis value (if given) + a trigger to the next cell."""
        outs = [Port(msb_chan), Port(lsb_chan), Port(fwd_chan)]
        if fwd_value_chan:
            outs.insert(2, Port(fwd_value_chan))
        fwd_v = (f"    MOVE R0, R{{state:other}}\n    {{write:{fwd_value_chan}}}\n"
                 if fwd_value_chan else "")
        save_other = ("    MOVE R{state:other}, R{in:in_other}\n"
                      if fwd_value_chan else "")
        data = [DataWord("zero", 0, address=2),
                DataWord("thresh", self._thresh_q15, address=3),
                DataWord("gain", self._gain_q15, address=4)]
        state = [StateVar("v"), StateVar("av")]
        inputs = [Port(in_name, register=0)]
        if fwd_value_chan:
            state.append(StateVar("other"))
            inputs.append(Port("in_other", register=1))
        tmpl = ("start:\n"
                + save_other +
                "    MOVE R{state:v}, R{in:" + in_name + "}\n"
                "    SUB R{data:zero}, R{state:v}\n"
                "    MULQ R0, R{data:gain}\n"
                "    {write:" + msb_chan + "}\n"
                "    MOVE R{state:av}, R{state:v}\n"
                "    CMP R{state:v}, R{data:zero}\n"
                "    BR.NN pos\n"
                "    SUB R{data:zero}, R{state:v}\n"
                "    MOVE R{state:av}, R0\n"
                "pos:\n"
                "    SUB R{state:av}, R{data:thresh}\n"
                "    MULQ R0, R{data:gain}\n"
                "    {write:" + lsb_chan + "}\n"
                + fwd_v +
                "    {jump:" + fwd_chan + "}\n")
        return CellProgram(inputs=inputs, outputs=outs,
                           entries=[EntryPoint("default")],
                           data=data, state=state, assembly_template=tmpl)

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        # 2 cells: cell 0 does the I axis (b3,b2) and forwards Q; cell 1 does the
        # Q axis (b1,b0). Splitting keeps each cell within the register budget.
        cell0 = self._axis_cell("in_i", "llr_b3", "llr_b2", "fwd_trigger",
                                fwd_value_chan="q_fwd")
        cell1 = self._axis_cell("in_q", "llr_b1", "llr_b0", "out_trigger")
        return {0: cell0, 1: cell1}

    def process_reference(self, samples):
        """Reference: (I,Q) Q15 pairs -> [llr_b3, llr_b2, llr_b1, llr_b0] per sym."""
        def s16(v):
            return v - 0x10000 if v & 0x8000 else v

        def mulq(a, b):
            return ((s16(a) * s16(b)) >> 15)
        t = s16(self._thresh_q15)
        g = self._gain_q15
        out = []
        for (i, q) in samples:
            i, q = s16(i), s16(q)
            out.append([
                mulq((-i) & 0xFFFF, g),          # b3: LLR = gain * (-I)
                mulq((abs(i) - t) & 0xFFFF, g),  # b2: LLR = gain * (|I| - t)
                mulq((-q) & 0xFFFF, g),          # b1: LLR = gain * (-Q)
                mulq((abs(q) - t) & 0xFFFF, g),  # b0: LLR = gain * (|Q| - t)
            ])
        return out

    def reset(self):
        pass
