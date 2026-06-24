"""DecimatorBlock — see :class:`DecimatorBlock`."""
import numpy as np
from typing import Dict, List
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from .fir_filter_block import FIRFilterBlock


class DecimatorBlock(FIRFilterBlock):
    """
    Decimator — drop-in for GNU Radio ``filter.fir_filter_fff(decimation, taps)``.

    A decimating FIR: it runs the anti-alias FIR every input sample but emits only
    every ``M``-th output. GR's ``fir_filter_fff(M, taps)`` produces exactly the
    full FIR output sampled at phase 0 — ``y_full[0::M]`` (verified) — so the block
    emits on input samples ``0, M, 2M, …`` and its emitted stream aligns with GR's
    output at delay 0.

    Because a decimator IS an FIR plus an emit-every-``M`` counter, this SUBCLASSES
    the verified :class:`FIRFilterBlock`: every cell of the wavefront runs each
    sample exactly as the FIR does (so the delay line / partial-sum forwarding and
    the COEFFICIENT-HEADROOM saturation are all inherited unchanged), and only the
    LAST cell's OUTPUT is gated by a modulo-``M`` counter. The non-last cells are
    reused verbatim from the FIR builder.

    Parameters:
      * ``coefficients``: the anti-alias FIR taps (same as FIRFilterBlock — GR's
        ``taps``). Use a low-pass to suppress aliasing before downsampling.
      * ``decimation``: the decimation factor ``M`` (GR's first ``fir_filter_fff``
        argument / the GRC ``decim``). Output rate = input rate / M.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["decimator", "downsample", "fir", "filtering"]

    # The last cell ALWAYS carries the mod-M counter (decim/one data + counter
    # state + ~6 instrs ≈ 9 words), so its tap segment is capped tighter than the
    # plain FIR — at S=0 (counter only) and tighter still at S>0 (counter + the
    # saturating-shift restore). Re-derived against the resolver's allocator
    # (probed real builds): a 5-tap single cell + counter overflows, a 4-tap fits;
    # a multi-cell last cell fits 3 taps + counter (S=0) / 2 taps + counter +
    # restore (S>0). Non-last cells keep the full TAPS_PER_CELL (no counter).
    MAX_SINGLE_CELL_TAPS = 4            # S=0 single cell + counter (FIR: 6)
    MAX_SINGLE_CELL_TAPS_WITH_SHIFT = 2  # S>0 single cell + counter + restore (FIR: 4)
    LAST_CELL_TAPS_DECIM = 3           # S=0 multi-cell last cell + counter
    LAST_CELL_TAPS_WITH_SHIFT = 2      # S=1 multi-cell last cell + counter + restore (FIR: 3)
    MAX_HEADROOM_SHIFT = 2             # Σ|h| ≤ 4; beyond this the restore+counter overflow one cell

    def _segment_offsets(self) -> List[int]:
        """Like FIRFilterBlock, but the LAST cell is ALWAYS capped (it carries the
        mod-M counter, present at every S) — so the decimator always takes the
        capped-tail path, with a tighter cap when the headroom restore is also on
        the last cell (S>0)."""
        import math
        N, K = self._num_taps, self.TAPS_PER_CELL
        if N <= self._single_cell_max():
            return [0, N]
        # The last cell carries the counter (always) + the doubling restore (S>0,
        # 4 instrs PER doubling), so its tap room shrinks as S grows: S=0 → 3,
        # S=1 → 2, S≥2 → 1.
        S = self._head_shift
        last_max = (self.LAST_CELL_TAPS_DECIM if S == 0
                    else self.LAST_CELL_TAPS_WITH_SHIFT if S == 1 else 1)
        c = math.ceil((N - last_max) / K) + 1
        segs = [K] * (c - 1) + [N - K * (c - 1)]
        while segs[-1] < 1:
            j = max(range(c - 1), key=lambda i: segs[i])
            segs[j] -= 1
            segs[-1] += 1
        while segs[-1] > last_max:
            j = next((i for i in range(c - 1) if segs[i] < K), None)
            if j is None:
                break
            segs[j] += 1
            segs[-1] -= 1
        offs = [0]
        for s in segs:
            offs.append(offs[-1] + s)
        return offs

    def __init__(self, name: str, coefficients: List[float], decimation: int = 2):
        """
        Initialize the decimator.

        Args:
            name: Block name.
            coefficients: anti-alias FIR taps (GR ``taps``).
            decimation: decimation factor M (GR ``decim``), output = input / M.
        """
        if int(decimation) < 1:
            raise ValueError(f"decimation must be >= 1, got {decimation}")
        self._decimation = int(decimation)
        super().__init__(name, coefficients=coefficients)
        # The mod-M counter shares the last cell with the COEFFICIENT-HEADROOM
        # restore; the restore is done as S doublings (4 instrs each), so its cost
        # grows with S and at S≥3 it no longer fits beside the counter on one cell.
        # S = ceil(log2 Σ|h|), so S≤2 means Σ|h| ≤ 4 — every realistic anti-alias
        # decimation low-pass (normalized, or up to ~4× gain) is covered. A filter
        # needing more headroom is a documented limit: scale the taps down (then
        # apply the gain elsewhere) or split into FIR + decimate-by-[1.0]. Raise
        # clearly rather than silently fail to build.
        if self._head_shift > self.MAX_HEADROOM_SHIFT:
            raise ValueError(
                f"decimation filter needs {self._head_shift} bits of coefficient "
                f"headroom (Σ|h|={sum(abs(c) for c in coefficients):.2f}); the "
                f"decimator supports up to {self.MAX_HEADROOM_SHIFT} (Σ|h|≤"
                f"{1 << self.MAX_HEADROOM_SHIFT}) because the saturating restore "
                f"shares the last cell with the mod-M counter. Scale the taps down "
                f"(Σ|h|≤{1 << self.MAX_HEADROOM_SHIFT}) or use a separate FIR+gain "
                f"stage ahead of a decimate-by-[1.0].")

    @property
    def decimation(self) -> int:
        return self._decimation

    # --- datapath: FIR wavefront, last cell's emit gated by a mod-M counter ----

    def _counter_data(self, base_addr: int):
        """The two counter DataWords (``decim`` = M, ``one`` = 1) at explicit
        addresses just past ``base_addr`` (the last coeff/bias/satpos word)."""
        return [DataWord("decim", self._decimation, address=base_addr + 1),
                DataWord("one", 1, address=base_addr + 2)]

    @staticmethod
    def _counter_gate_open():
        """Lines that run EVERY sample after the delay shift: bump the mod-M
        counter and, when it has not yet reached M, branch past the MAC+emit to a
        HALT (state already updated; no output this sample). Targets a REAL
        instruction label (``_decim_skip`` on a HALT), never a {write}/{jump}
        placeholder (the build-engine GOTO miscompile, INV-13)."""
        return [
            "    ADD R{state:counter}, R{data:one}",
            "    MOVE R{state:counter}, R0",
            "    CMP R{state:counter}, R{data:decim}",
            "    BR.NZ _decim_skip",
            "    XOR R{state:counter}, R{state:counter}",
            "    MOVE R{state:counter}, R0",
        ]

    # Saturating-rail constant for the doubling restore: 0x8000. The clamp
    # computes 0x8000 - sign = +0x7FFF (positive overflow) or -0x8000 (negative).
    SAT_NEG_Q15 = 0x8000

    def _decim_satshift_and_emit(self, S: int, emit_lines):
        """Decimator gain restore: a SATURATING left shift by ``S`` done as ``S``
        DOUBLINGS (``ADD R0,R0`` + a V-flag clamp each), then emit.

        Equivalent to ``clamp(acc·2^S)`` — bit-identical to the FIR's bias-and-shift
        restore (:meth:`FIRFilterBlock._sat_shl`), so the inherited
        ``process_reference_q15`` still predicts the DUT exactly. The decimator
        uses the doubling form (not the FIR's bias-shift) because it is CHEAPER in
        fixed overhead (1 data word + 4 instrs per doubling vs 2 data + ~9
        instrs), which is what lets the saturating restore COEXIST with the mod-M
        counter on one cell for the small S a decimation filter needs (S∈{0,1,2}:
        Σ|h| of a normalized anti-alias low-pass is ~1..2). On overflow an
        ``ADD R0,R0`` sets V and inverts the wrapped sign N, so
        ``BR.NV +2 ; SHR R0,#15 ; SUB satneg,R0`` pins to ``0x8000 - sign`` = the
        right rail; chaining S doublings re-clamps each time, equalling
        ``clamp(acc·2^S)``. ``BR.NV +2`` targets a real instruction (the emit),
        never a {write}/{jump} placeholder (the build-engine GOTO miscompile)."""
        if S <= 0:
            return list(emit_lines)
        lines = []
        for _ in range(S):
            lines.append("    ADD R0, R0")
            lines.append("    BR.NV +2")
            lines.append("    SHR R0, #15")
            lines.append("    SUB R{data:satneg}, R0")
        lines.extend(emit_lines)
        return lines

    @staticmethod
    def _counter_gate_close():
        """Terminate the emit path and provide the skip target. The emit path must
        HALT before falling into the skip block (a remote {jump} does NOT stop
        local execution)."""
        return ["    HALT", "_decim_skip:", "    HALT"]

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """FIR wavefront with the LAST cell's emit gated by a mod-M counter.

        Non-last cells are reused verbatim from FIRFilterBlock; only the last cell
        is rebuilt to add the counter (so register allocation accounts for it)."""
        programs = super().build_cell_programs()
        last = max(programs.keys())
        n_cells = len(programs)
        S = self._head_shift
        N = self._num_taps

        if n_cells == 1:
            # Single cell: the whole FIR + counter live here. Mirror FIR's
            # single-cell layout, then add counter data/state and the gate.
            coeffs = self._coeff_q15
            data = [DataWord(f"c{i}", c, address=i + 1) for i, c in enumerate(coeffs)]
            base = N
            if S > 0:
                data.append(DataWord("satneg", self.SAT_NEG_Q15, address=N + 1))
                base = N + 1
            data += self._counter_data(base)
            state = [StateVar(f"d{i}") for i in range(N)]
            if S > 0:
                state.append(StateVar("acc_save"))
            state.append(StateVar("counter", initial_value=self._decimation - 1))

            lines = []
            for i in range(N - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{N-1}}}, R{{in:sample}}")
            lines += self._counter_gate_open()
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, N):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            lines.extend(self._decim_satshift_and_emit(
                S, ["    {write:out}", "    {jump:out}"]))
            lines += self._counter_gate_close()
            programs[0] = CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=data, state=state,
                assembly_template="start:\n" + "\n".join(lines) + "\n")
            return programs

        # Multi-cell: rebuild ONLY the last cell with the counter (the non-last
        # cells from super() are unchanged). Mirror FIRFilterBlock's last-cell
        # layout exactly, then splice in the counter data/state + gate.
        offsets = self._segment_offsets()
        start, end = offsets[last], offsets[last + 1]
        L = end - start
        cell_coeffs = self._coeff_q15[N - end:N - start]
        data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1) for i in range(L)]
        base = L
        if S > 0:
            data.append(DataWord("satneg", self.SAT_NEG_Q15, address=L + 1))
            base = L + 1
        data += self._counter_data(base)

        state = [StateVar(f"d{i}") for i in range(L)]
        if S > 0:
            state.append(StateVar("acc_save"))
        state.append(StateVar("counter", initial_value=self._decimation - 1))

        last_data_addr = max(dw.address for dw in data)
        partial_reg = last_data_addr + len(state) + 1
        inputs = [Port("sample", register=0),
                  Port("partial", register=partial_reg)]

        lines = []
        for i in range(L - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        lines.append(f"    MOVE R{{state:d{L-1}}}, R{{in:sample}}")
        lines += self._counter_gate_open()
        lines.append("    MULQ R{state:d0}, R{data:c0}")
        for i in range(1, L):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
        lines.append("    ADD R0, R{in:partial}")
        lines.extend(self._decim_satshift_and_emit(
            S, ["    {write:out}", "    {jump:out}"]))
        lines += self._counter_gate_close()
        programs[last] = CellProgram(
            inputs=inputs,
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template="start:\n" + "\n".join(lines) + "\n")
        return programs

    # --- references -----------------------------------------------------------

    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 reference: the inherited FIR Q15 datapath, then decimate
        at phase 0 (emit on input samples 0, M, 2M, …). Returns one Q15 word per
        EMITTED sample (length = ceil(len(input)/M))."""
        full = super().process_reference_q15(input_q15)
        return full[::self._decimation]

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference: full FIR convolution decimated at phase 0."""
        filtered = np.convolve(
            input_samples, self._coefficients, mode="full")[:len(input_samples)]
        return filtered[::self._decimation].astype(np.float32)
