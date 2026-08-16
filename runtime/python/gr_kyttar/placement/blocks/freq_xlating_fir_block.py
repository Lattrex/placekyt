# SPDX-License-Identifier: GPL-3.0-or-later
"""FreqXlatingFIRBlock — see :class:`FreqXlatingFIRBlock`.

Drop-in for GNU Radio ``filter.freq_xlating_fir_filter_ccf``: a frequency shift
(complex mixer with an NCO) FUSED with a decimating real-tap FIR — the workhorse
channelizer. Complex in, complex out.
"""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface
from .complex_mixer_block import ComplexMixerBlock


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class FreqXlatingFIRBlock(ComplexMixerBlock):
    """Frequency-translating decimating FIR — GR ``freq_xlating_fir_filter_ccf``.

    GR semantics (VERBATIM param names ``decimation``/``taps``/``center_freq``/
    ``sampling_freq``): multiply the complex input by ``exp(-j·2π·center_freq/
    sampling_freq·n)`` (a DOWN-shift), apply the real FIR ``taps``, and decimate by
    ``decimation``. GR folds the rotator into the taps for efficiency; this block is
    output-equivalent by the algebraically-exact decomposition

        out[m] = mixed_FIR[m·decimation]
        mixed[n] = x[n]·exp(-j·(fwT0·n + θ0)),   fwT0 = 2π·center_freq/sampling_freq
        θ0 = fwT0·(L-1)/2   (the FIR group-delay phase GR carries in its output rotator)

    i.e. an NCO DOWN-mixer whose initial phase absorbs GR's ``(L-1)/2`` tap-fold
    constant, feeding a real-tap complex FIR, then a phase-0 decimation gate. Verified
    bit-equivalent (algebra) and Q15-equivalent within the NCO table + FIR floor vs GR.

    ARCHITECTURE (fused, reuses the proven ComplexMixer NCO front):
    ================================================================
    The 11-cell interpolated quarter-wave NCO + complex mixer of
    :class:`ComplexMixerBlock` (phase→sin/cos columns→mixer, plus a signal relay) is
    inherited UNCHANGED except that the mixer's output ``yi``/``yq`` are handed
    INTERNALLY to a SERIALIZED complex FIR chain (the ComplexRRC serialized-rail
    design: a Q-rail filters yq while ferrying yi, hands off to an I-rail that filters
    yi while carrying yq, and the I-rail's last cell emits the filtered pair +
    ONE trigger). The FIR's last I-cell carries the phase-0 mod-M decimation gate (the
    proven FIRFilterBlock/ComplexRRC decimator). Real taps are shared by both rails.

    COEFFICIENT HEADROOM (INV-13): taps are pre-scaled by ``2**-head_shift`` (with
    ``head_shift = max(0, ceil(log2 Σ|taps|))``) so the wrapping MACQ never overflows
    mid-chain; the last cell restores the gain with a saturating left shift. A
    normalized filter (Σ|taps| ≤ 1) → head_shift = 0, a no-op.

    HARDWARE CONSTRAINT (loud): the fused block's last I-rail cell already runs the
    dual-rail emit + decimation gate; a saturating-restore (head_shift > 0) on top
    overflows its 32-word budget, so ``Σ|taps| ≤ 1`` is REQUIRED (head_shift == 0) and
    a violation RAISES (never silently rescaled — matches GR magnitude). Pass a
    normalized tap set (any firdes low/band-pass at gain ≤ 1 is Σ|h| ≤ 1).
    """
    CATEGORY = "filtering"
    TAGS = ["freq_xlating", "channelizer", "mixer", "fir", "decimation", "complex"]

    # 4 taps/cell on the FIR rails (the ComplexRRC serialized-rail density: the I-rail
    # cells carry a passenger forward too, so 4 not 5 to fit the 32-reg budget).
    TAPS_PER_CELL = 4

    def __init__(self, name: str,
                 decimation: int = 1,
                 taps: List[float] = None,
                 center_freq: float = 0.0,
                 sampling_freq: float = 32000.0,
                 pipeline_lock: bool = False):
        if taps is None:
            taps = [1.0]
        if int(decimation) < 1:
            raise ValueError(f"decimation must be >= 1, got {decimation}")
        taps = [float(t) for t in taps]
        L = len(taps)
        if L < 1:
            raise ValueError("taps must be non-empty")
        # HEADROOM: fused last cell has no room for a saturating restore on top of the
        # dual-rail emit + decimation gate. Require Σ|taps| ≤ 1 (head_shift == 0).
        sum_abs = sum(abs(t) for t in taps)
        if sum_abs > 1.0 + 1e-9:
            raise ValueError(
                f"FreqXlatingFIRBlock '{name}': Σ|taps|={sum_abs:.4f} > 1. The fused "
                f"last FIR cell (dual-rail emit + mod-M decimation gate) has no room "
                f"for a coefficient-headroom saturating restore. Normalize the taps so "
                f"Σ|taps| ≤ 1 (e.g. a firdes low/band-pass at gain ≤ 1). Correlation "
                f"is gain-invariant if you need a smaller gain.")

        fwT0 = 2.0 * math.pi * float(center_freq) / float(sampling_freq)
        theta0 = fwT0 * (L - 1) / 2.0
        # Reuse the ComplexMixer NCO as the DOWN-shift front: mixer computes
        # in·exp(j·(phase0 + 2π·frequency/fs·n)); we need exp(-j·(fwT0·n + θ0)),
        # so frequency = -center_freq and phase0 = -θ0. amplitude=1, offset=0.
        # SATURATION SERIALIZE-LOCK (INV-20). The inherited NCO/mixer front has a
        # RECONVERGENT fan-in (phase → sin/cos columns + relay → mixer); under
        # saturated drive a second sample's fast-path operands race into the mixer's
        # input regs before the first's slow-path operand arrives → DEADLOCK (0
        # output). ComplexMixer/NCO fix this with a serialize-LOCK whose UNLOCK
        # (a backward WRITE.CFG clearing `phase`'s arbiter LOCK) rides the BLOCK EXIT
        # cell. Here the mixer is MID-chain (it triggers the FIR head, not the port),
        # so the exit is the FIR's I-rail last cell, NOT the mixer.
        #
        # WALL (documented, verified 2026-08-06): porting the lock to the mid-chain
        # mixer does NOT release. The scaffolding below (phase locks; mixer dual-FACE
        # unlock via a transit_unlock corridor) BUILDS `ok=True` but produces EMPTY
        # output even per-sample — `_apply_internal_feedback`'s config-only unlock
        # branch assumes the unlock rides the block's OUTPUT cell (it records the
        # unlock cell for the exit-default and traces the corridor from the exit), so a
        # mid-chain mixer's forward yi/yq WRITEs + trig to the FIR head are lost when
        # the face-flip unlock is present. Making a mid-chain config-unlock work is a
        # build-engine change (support an unlock edge whose source is NOT output_cell_id)
        # beyond this block. So pipeline_lock RAISES rather than ship a silently-empty
        # locked variant; the block is SATURATION-BESPOKE (NEEDS_BESPOKE) and fully
        # verified per-sample. The scaffolding is retained (guarded) as the proven-far
        # starting point for the build-engine fix.
        if pipeline_lock:
            raise NotImplementedError(
                "FreqXlatingFIRBlock: pipeline_lock (saturation serialize-LOCK) is NOT "
                "yet functional — the mid-chain mixer's config-only unlock is not "
                "handled by _apply_internal_feedback (which assumes the unlock rides "
                "the block OUTPUT cell). The locked variant builds but emits nothing. "
                "The block is verified per-sample (bit-exact vs GR) and is "
                "SATURATION-BESPOKE; see lessons_log. Drive it un-saturated / at the "
                "channel rate, or extend the build engine for a mid-chain unlock.")
        self._fx_pipeline_lock = bool(pipeline_lock)
        super().__init__(name, sample_rate=float(sampling_freq),
                         frequency=-float(center_freq),
                         amplitude=1.0, offset=0.0, phase=-theta0,
                         pipeline_lock=bool(pipeline_lock))
        # Re-tag the block's exposed params to the GR names (the base stored the
        # mixer's derived ones). ``params`` is what the catalog resolves with.
        self._decimation = int(decimation)
        self._taps = taps
        self._center_freq = float(center_freq)
        self._sampling_freq = float(sampling_freq)
        self._num_taps = L
        self._theta0 = theta0
        self.params = {
            "decimation": int(decimation),
            "taps": taps,
            "center_freq": float(center_freq),
            "sampling_freq": float(sampling_freq),
            "pipeline_lock": bool(pipeline_lock),
        }
        # Real taps as signed Q15 (head_shift == 0, so no pre-scale). int(round(*32767))
        # matches the ComplexRRC/FIR convention (NOT float_to_q15's *32768/saturate).
        self._coeff_q15 = [int(round(t * 32767)) & 0xFFFF for t in taps]

        # INV-9: keep the block ≤ 8 cells across on this 10×12 chip (nothing enforces
        # it; a wider fold silently fails to route). The NCO front is 2 cols; the FIR
        # rails add cells_per_rail columns to the EAST (fhead + q rail on one row). The
        # widest row spans mixer_col(1) → fhead(2) → q rail (cells_per_rail cells), so
        # the fold width is 3 + (cells_per_rail - 1) = cells_per_rail + 2. Raise loudly
        # past 8 rather than build an un-routable strip (firdes channelizer taps can be
        # long; fold or decimate, or use a shorter design).
        fold_w = self.cells_per_rail + 2
        if fold_w > 8:
            raise ValueError(
                f"FreqXlatingFIRBlock '{name}': {L} taps fold to {self.cells_per_rail} "
                f"rail cells → a {fold_w}-cell-wide block, exceeding the ≤8-across "
                f"routing limit on this 10×12 chip (INV-9). Use ≤ "
                f"{(6 * self.TAPS_PER_CELL)} taps (or fewer with decimation), or a "
                f"coarser filter. This is a chip-size convention, not an algorithm limit.")

    # ---- geometry ----------------------------------------------------------
    @property
    def cells_per_rail(self) -> int:
        return len(self._segment_sizes())

    def _segment_sizes(self) -> List[int]:
        """Tap counts per rail cell (the wavefront partition).

        Plain (decimation == 1): compact ⌈L/TAPS_PER_CELL⌉ packing, TAPS_PER_CELL
        each with the remainder on the last cell. Decimating (decimation > 1): the
        LAST cell also carries the mod-M output gate (a dcnt state + 2 data words),
        which — with a full tap segment + the incoming partial + carried passenger —
        overflows the last cell's register/addressing budget (verified: L=7/decim=2
        with a 3-tap last cell reads garbage). So CAP the last (gated) cell to ONE
        tap, exactly like the RRC MF's natural 4+4+4+4+1 fold. The head taps still
        pack TAPS_PER_CELL each; only the gated tail is a lone tap."""
        L = self._num_taps
        K = self.TAPS_PER_CELL
        if self._decimation <= 1:
            n = math.ceil(L / K)
            segs = [K] * (n - 1) + [L - K * (n - 1)]
            return segs
        # Decimating: cap the last cell at 1 tap. Pack the first L-1 taps at K each.
        head = L - 1
        if head <= 0:
            return [L]            # L == 1: a single (gated) cell
        n_head = math.ceil(head / K)
        segs = [K] * (n_head - 1) + [head - K * (n_head - 1)] + [1]
        return segs

    @property
    def cell_count(self) -> int:
        # 11 NCO/mixer cells (inherited) + 1 FIR head (complex distributor) + two
        # FIR rails. The mixer emits a CLEAN complex packet (yi/yq) to the head — the
        # head, not the fan-in mixer, does the serialized-rail distribution (isolating
        # the reconvergent fan-in from the forward fan-out). +1 transit_unlock corridor
        # cell when the serialize-LOCK is on.
        n = 11 + 1 + 2 * self.cells_per_rail
        return n + 1 if self._fx_pipeline_lock else n

    def _rail_ids(self, rail: str) -> List[str]:
        return [f"{rail}{i}" for i in range(self.cells_per_rail)]

    # ---- cell programs -----------------------------------------------------
    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        cells = super().build_cell_programs()   # the 11 NCO/mixer cells
        # Re-wire the mixer: it emits a CLEAN complex packet (yi/yq) + trigger to the
        # FIR head (exactly as the stock mixer emits to Costas). The head — a plain
        # 1-complex-pair cell, NOT the reconvergent fan-in mixer — does the serialized
        # distribution into the Q rail. This isolates the mixer's fan-in from the FIR
        # fan-out (a mixer doing BOTH scrambles the rails).
        cells["mixer"] = self._build_mixer_emit()
        cells["fhead"] = self._build_head()
        cells.update(self._build_rail("q"))
        cells.update(self._build_rail("i"))
        return cells

    def _build_mixer_emit(self) -> CellProgram:
        """The ComplexMixer product cell, arithmetic byte-identical, emitting a clean
        complex packet (yi, yq, trig) to the FIR head (like the stock mixer→Costas).

        When the serialize-LOCK is on, the mixer ALSO clears `phase`'s arbiter LOCK
        (INV-20) with a DUAL-FACE flip: after emitting yi/yq on its ROUTED forward face
        (face_tap → fhead), it flips FACE to the unlock corridor (unlock_face=NORTH →
        transit_unlock → phase), does WRITE.CFG @N,4 (R0=0 → phase CONFIG[4]=LOCK), and
        flips FACE BACK to face_tap so the trailing `{jump:trig}` fires FORWARD to fhead
        (not up the unlock corridor). The mixer is MID-chain (not the block exit) and
        has no backward DATA edge, so `_set_cell_hop1`/`_patch_last_write_handoff` never
        clobber the WRITE.CFG (INV-19). The @N hop is re-patched to the real corridor
        distance by `_apply_internal_feedback` (config_only branch)."""
        lock = self._fx_pipeline_lock
        # fwd_face = the mixer's FORWARD datapath face to fhead. Unlike the stock
        # ComplexMixer (where the mixer is the block EXIT and its output face is the
        # ROUTE-overridden face_tap), here the mixer is MID-chain, so its forward face
        # is the LAYOUT face (EAST=1, mixer at (1,1) → fhead at (2,1)). Name it NOT
        # "face_tap"/"face_internal" so _apply_rotate_tap_face leaves it alone and only
        # _apply_orientation_face_words rotates it. unlock_face = NORTH(3) toward
        # transit_unlock. Both is_face so they transform with orientation.
        lock_data = ([DataWord("fwd_face", 1, address=5, is_face=True),
                      DataWord("unlock_face", 3, address=6, is_face=True)]
                     if lock else [])
        # No top-of-loop face restore (matches ComplexMixer): the tail below ends with
        # MOVE [FACE], face_tap, so FACE is ALREADY the forward face when the next
        # iteration's yi/yq egress — saving the extra instruction/register pressure.
        lock_set_out = ""
        lock_release = ("""\
    MOVE [FACE], R{data:unlock_face}
    MOVE R0, R{data:zero}
    WRITE.CFG @2, 4
    MOVE [FACE], R{data:fwd_face}
""" if lock else "")
        return CellProgram(
            inputs=[Port("cosv", register=0), Port("sinv", register=1),
                    Port("xi", register=2), Port("xq", register=3)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)] + lock_data,
            state=[StateVar("c"), StateVar("s"), StateVar("xi2"), StateVar("xq2"),
                   StateVar("acc")],
            assembly_template="""\
start:
""" + lock_set_out + """\
    MOVE R{state:c}, R{in:cosv}
    MOVE R{state:s}, R{in:sinv}
    MOVE R{state:xi2}, R{in:xi}
    MOVE R{state:xq2}, R{in:xq}
    MULQ R{state:xi2}, R{state:c}
    MOVE R{state:acc}, R0
    MULQ R{state:xq2}, R{state:s}
    SUB R{state:acc}, R0
    {write:yi}
    MULQ R{state:xi2}, R{state:s}
    MOVE R{state:acc}, R0
    MULQ R{state:xq2}, R{state:c}
    ADD R{state:acc}, R0
    {write:yq}
""" + lock_release + """\
    {jump:trig}
""",
        )

    def _build_head(self) -> CellProgram:
        """FIR head — the ComplexRRC serialized distributor. Lands the mixer's yi/yq
        packet (yi@R0, yq@R1) and feeds the Q-rail head q0: xq_out=yq→q0.sample (the Q
        rail filters yq), xi_pass=yi→q0.carry_in (the UNFILTERED yi ferried along the
        Q rail as a passenger), + trigger. Both emits go to q0 (EAST), no face flip."""
        return CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("xi_pass"), Port("xq_out"), Port("qtrig")],
            entries=[EntryPoint("default")],
            state=[StateVar("yqs", register=2)],
            assembly_template="""\
start:
    MOVE R{state:yqs}, R{in:yq}
    MOVE R0, R{in:yi}
    {write:xi_pass}
    MOVE R0, R{state:yqs}
    {write:xq_out}
    {jump:qtrig}
""",
        )

    def _build_rail(self, rail: str) -> Dict[str, CellProgram]:
        """One real-tap FIR rail as a chained-partial-sum wavefront, ferrying a
        passenger (yi on the Q rail, yq on the I rail) — the ComplexRRC serialized
        design. Bit-identical wrapping MACQ. Real taps shared by both rails."""
        sizes = self._segment_sizes()
        n_cells = len(sizes)
        offs = [0]
        for s in sizes:
            offs.append(offs[-1] + s)
        ids = self._rail_ids(rail)
        progs: Dict[str, CellProgram] = {}

        for cell_idx in range(n_cells):
            start_tap = offs[cell_idx]
            end_tap = offs[cell_idx + 1]
            n_taps = end_tap - start_tap
            is_first = (cell_idx == 0)
            is_last = (cell_idx == n_cells - 1)

            cell_coeffs = list(reversed(self._coeff_q15[start_tap:end_tap]))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(n_taps)]
            state = [StateVar(f"d{i}", reset_per_batch=True) for i in range(n_taps)]
            if not is_last:
                state.append(StateVar("old_save"))
            state.append(StateVar("cs"))   # carried passenger (yi on Q, yq on I)

            gated = (is_last and rail == "i" and self._decimation > 1)
            if gated:
                state.append(StateVar("dcnt",
                                      initial_value=self._decimation - 1))
                base = n_taps + 1
                data = data + [
                    DataWord("dg_decim", self._decimation, address=base),
                    DataWord("dg_one", 1, address=base + 1),
                ]

            n_state = len(state)
            data_top = max((dw.address for dw in data
                            if dw.address is not None), default=n_taps)
            partial_reg = data_top + n_state + 1
            carry_reg = partial_reg + 1
            if is_first:
                inputs = [Port("sample", register=0),
                          Port("carry_in", register=carry_reg)]
            else:
                inputs = [Port("sample", register=0),
                          Port("partial", register=partial_reg),
                          Port("carry_in", register=carry_reg)]

            outputs = []
            if not is_last:
                outputs.append(Port("partial"))
                outputs.append(Port("sample_out"))
                outputs.append(Port("carry_out"))
                outputs.append(Port("fwd"))
            elif rail == "q":
                outputs.append(Port("yq_handoff"))   # yq -> i0.carry_in
                outputs.append(Port("xi_handoff"))   # yi -> i0.sample
                outputs.append(Port("itrig"))
            else:
                outputs.append(Port("yi"))      # -> port R0
                outputs.append(Port("yq"))      # -> port R1
                outputs.append(Port("trig"))

            lines = []
            lines.append("    MOVE R{state:cs}, R{in:carry_in}")
            if not is_last:
                lines.append("    MOVE R{state:old_save}, R{state:d0}")
            for i in range(n_taps - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{n_taps - 1}}}, R{{in:sample}}")
            if gated:
                lines.append("    ADD R{state:dcnt}, R{data:dg_one}")
                lines.append("    MOVE R{state:dcnt}, R0")
                lines.append("    CMP R{state:dcnt}, R{data:dg_decim}")
                lines.append("    BR.NZ _fx_skip")
                lines.append("    XOR R{state:dcnt}, R{state:dcnt}")
                lines.append("    MOVE R{state:dcnt}, R0")
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, n_taps):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:partial}")
            if is_last and rail == "i":
                lines.append("    {write:yi}")
                lines.append("    MOVE R0, R{state:cs}")
                lines.append("    {write:yq}")
                lines.append("    {jump:trig}")
                if gated:
                    lines.append("    HALT")
                    lines.append("_fx_skip:")
                    lines.append("    HALT")
            elif is_last:  # q rail last: hand yq (R0) + passenger yi (cs) to i0
                lines.append("    {write:yq_handoff}")
                lines.append("    MOVE R0, R{state:cs}")
                lines.append("    {write:xi_handoff}")
                lines.append("    {jump:itrig}")
            else:
                lines.append("    {write:partial}")
                lines.append("    MOVE R0, R{state:old_save}")
                lines.append("    {write:sample_out}")
                lines.append("    MOVE R0, R{state:cs}")
                lines.append("    {write:carry_out}")
                lines.append("    {jump:fwd}")

            template = "start:\n" + "\n".join(lines) + "\n"
            progs[ids[cell_idx]] = CellProgram(
                inputs=inputs, outputs=outputs,
                entries=[EntryPoint("default")],
                data=data, state=state, assembly_template=template,
            )
        return progs

    # ---- connections / jumps / layout --------------------------------------
    def _rail_connections(self, rail: str) -> List[Tuple[str, str, str, str]]:
        ids = self._rail_ids(rail)
        conns = []
        for k in range(len(ids) - 1):
            conns.append((ids[k], "partial", ids[k + 1], "partial"))
            conns.append((ids[k], "sample_out", ids[k + 1], "sample"))
            conns.append((ids[k], "carry_out", ids[k + 1], "carry_in"))
        return conns

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        # The NCO/mixer internal wiring (phase→sin/cos columns→mixer + relay), minus
        # the old mixer→(external) edges; then mixer→fhead→q0 and the FIR rail chains.
        # Keep the parent NCO/mixer wiring, dropping only the mixer's stale DATA edges
        # (its yi/yq now go to fhead). KEEP the mixer→phase config-only unlock edge
        # (src port "unlock") when locked — _apply_internal_feedback traces it.
        base = [c for c in super().internal_connections()
                if not (c[0] == "mixer" and c[1] != "unlock")]
        i_first = self._rail_ids("i")[0]
        q_first = self._rail_ids("q")[0]
        q_last = self._rail_ids("q")[-1]
        fir = [
            ("mixer", "yi", "fhead", "yi"),
            ("mixer", "yq", "fhead", "yq"),
            ("fhead", "xq_out", q_first, "sample"),
            ("fhead", "xi_pass", q_first, "carry_in"),
            (q_last, "yq_handoff", i_first, "carry_in"),
            (q_last, "xi_handoff", i_first, "sample"),
        ] + self._rail_connections("i") + self._rail_connections("q")
        return base + fir

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        # NCO/mixer trigger chain (phase→…→mixer), then mixer→fhead→q0, the rail
        # chains, and q_last→i0. The I-rail last cell triggers the external port.
        chain = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp", "relay",
                 "cos_fold", "cos_even", "cos_odd", "cos_interp", "mixer"]
        jumps = [(chain[i], "trig", chain[i + 1], "default")
                 for i in range(len(chain) - 1)]
        q_ids = self._rail_ids("q")
        i_ids = self._rail_ids("i")
        jumps.append(("mixer", "trig", "fhead", "default"))
        jumps.append(("fhead", "qtrig", q_ids[0], "default"))
        jumps.append((q_ids[-1], "itrig", i_ids[0], "default"))
        for ids in (i_ids, q_ids):
            for k in range(len(ids) - 1):
                jumps.append((ids[k], "fwd", ids[k + 1], "default"))
        return jumps

    def output_cell_ids(self) -> List[Any]:
        return [self._rail_ids("i")[-1]]

    def output_cell_id(self):
        # The block exit is the I-rail last cell (NOT the mixer). Set so the build's
        # exit-default targets it (mirrors ComplexRRC / the mixer under lock).
        return self._rail_ids("i")[-1]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """NCO/mixer front (inherited 2-column layout) + a serialized FIR fold to its
        EAST. The mixer at (1,1) hands EAST into the Q rail; the Q rail flows EAST on
        row 1, corners SOUTH into the I rail flowing WEST on row 2, whose last cell is
        the block output (co-located near the mixer's bus edge)."""
        # NCO/mixer occupies columns 0-1, rows 0-5 (from ComplexMixer's unlocked
        # layout). Place the FIR rails in columns 2.. rows 1-2 so the mixer→q0 handoff
        # is EAST-adjacent (mixer at (1,1)).
        col0 = ["phase", "sin_fold", "sin_even", "sin_odd", "sin_interp", "relay"]
        col1_bottom_up = ["cos_fold", "cos_even", "cos_odd", "cos_interp", "mixer"]
        layout: Dict[Any, Tuple[int, int, str]] = {}
        for j, cid in enumerate(col0):
            face = "east" if cid == "relay" else "south"
            layout[cid] = (0, j, face)
        for k, cid in enumerate(col1_bottom_up):
            face = "east" if cid == "mixer" else "north"
            layout[cid] = (1, 5 - k, face)
        # mixer at (1,1) faces EAST → fhead at (2,1) faces EAST → Q rail from (3,1)
        # flowing EAST on row 1; the Q rail corners SOUTH into the I rail on row 2
        # flowing WEST, whose last cell is the block output.
        n = self.cells_per_rail
        q_ids = self._rail_ids("q")
        i_ids = self._rail_ids("i")
        layout["fhead"] = (2, 1, "east")
        for k, cid in enumerate(q_ids):
            face = "south" if cid == q_ids[-1] else "east"
            layout[cid] = (3 + k, 1, face)
        # I rail flows WEST on row 2: i0 under q_last (col 3+n-1), i_last at col 3.
        for k, cid in enumerate(i_ids):
            layout[cid] = (3 + (n - 1) - k, 2, "west")
        if self._fx_pipeline_lock:
            # Unlock corridor: mixer(1,1) flips NORTH and WRITE.CFGs up to
            # transit_unlock(1,0), which relays WEST into phase(0,0) (entering phase's
            # EAST face = phase.lock_face=EAST=1). A free cell above the mixer.
            layout["transit_unlock"] = (1, 0, "west")
        return layout

    # ---- reference ---------------------------------------------------------
    def process_reference_q15(self, input_iq) -> List[Tuple[int, int]]:
        """Bit-exact predictor: the NCO/mixer down-shift (ComplexMixer Q15 reference,
        with the folded phase offset) → real-tap wrapping-MACQ FIR per rail →
        phase-0 decimation. Matches the on-chip cells exactly."""
        mixed = super().process_reference_q15(input_iq)   # [(yi,yq)] uint16
        xi = [v[0] for v in mixed]
        xq = [v[1] for v in mixed]
        taps = [_s16(t) for t in self._coeff_q15]

        def fir(x):
            L = len(taps)
            out = []
            for n in range(len(x)):
                acc = 0
                for k in range(L):
                    s = x[n - k] if 0 <= n - k < len(x) else 0
                    acc = _s16((acc + ((_s16(s) * taps[k]) >> 15)) & 0xFFFF)
                out.append(acc & 0xFFFF)
            return out

        fi = fir(xi)
        fq = fir(xq)
        out = list(zip(fi, fq))
        if self._decimation > 1:
            out = out[0::self._decimation]
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        ref = self.process_reference_q15(input_samples)
        return np.array([complex(_s16(yi) / 32768.0, _s16(yq) / 32768.0)
                         for yi, yq in ref], dtype=np.complex64)

    def reset(self):
        self._phase = 0
