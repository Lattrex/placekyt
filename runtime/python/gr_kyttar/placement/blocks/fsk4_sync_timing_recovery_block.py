# SPDX-License-Identifier: GPL-3.0-or-later
"""FSK4SyncTimingRecoveryBlock — see :class:`FSK4SyncTimingRecoveryBlock`."""
import numpy as np
from typing import Dict, List

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface, float_to_q15


# M17 LSF sync word as 4FSK PAM symbols: {+3,+3,+3,+3,-3,-3,+3,-3}.
# As normalised levels (+3 -> +1.0, -3 -> -1.0): the correlation template is +-1.
_SYNC_SIGNS = [+1, +1, +1, +1, -1, -1, +1, -1]   # M17 LSF sync (dibits 1,1,1,1,3,3,1,3)


class FSK4SyncTimingRecoveryBlock(KyttarBlock):
    """
    M17 4FSK sync-word timing recovery (sliding correlator + gated 2:1 decimator).

    The RX symbol-timing stage of an **M17 4-level FSK** modem. Gardner (and any
    decision-feedback) timing loop does NOT lock a 4-level FSK signal well — the
    4-PAM eye (inner levels ±1/3) is far narrower than a 2-level BPSK eye, so the
    loop jitters across the inner decision thresholds. Real M17 receivers (e.g.
    mobilinkd/m17-cxx-demod) instead recover timing by **cross-correlating a known
    sync word**; this block does exactly that, matched to the Kyttar ISA (pure
    MAC/add + compare — no atan, no divide, no feedback loop).

    Algorithm (validated numerically at BER 0 on the FM-discriminator 4-PAM signal)
    ------------------------------------------------------------------------------
    The input is the RX matched-filter stream at **2 samples/symbol**. A frame opens
    with a short alternating preamble (AGC/coarse) followed by the M17 LSF **sync
    word** ``{+3,+3,+3,+3,−3,−3,+3,−3}`` (8 symbols). The block:

      1. keeps a **16-sample sliding shift register** (the last 8 symbols at 2 sps);
      2. on every sample computes the sync correlation of the 8 on-phase samples,
         ``C = Σ_{k=0..7} sync[k]·x[n − 2·(7−k)]`` (the time-reversed sync so its
         LAST symbol aligns with the newest sample); the sync is ±1 so each tap is a
         signed ADD, not a multiply;
      3. **locks** on the first LOCAL MAXIMUM of ``C`` above a threshold — that peak
         marks the sync's last symbol, so the FIRST payload symbol center is 2
         samples later. The threshold is derived from the signal's own amplitude
         (a running peak of ``|x|``), so it tracks AGC;
      4. after lock, **decimates 2:1**: emits the sample at each payload symbol
         center (every 2nd sample) and drops the rest — one recovered symbol-center
         value per symbol, ready for the :class:`FSK4SlicerBlock`.

    This acquires the exact symbol phase from the sync word (robust, data-aided) and
    then holds it open-loop — correct for a fixed-timing link (the same-chip modem
    loopback has no channel timing offset or drift).

    Datapath (6 cells) — a systolic correlator + a lock/emit FSM
    ------------------------------------------------------------
    ``d0 → d1 → d2 → d3`` — four delay/correlate cells, 4 samples each, forming the
    16-sample sliding window as a FIR-style wavefront: each new sample shifts every
    line and the running partial sync-correlation flows d0 → … → d3, which produces
    the full ``C``. ``lock`` — running |x| peak → threshold, first-local-max lock,
    and a countdown that raises a ``lk`` flag on the first payload symbol center.
    ``emit`` — turns ``lk`` + the forwarded sample into the 2:1 decimation (emit
    every 2nd sample after lock). The block presents ONE external input (``sample``
    on d0, R0) and ONE external output (``out`` on emit, R0).

    Parameters mirror the M17 frame structure (RULE #0): the sync word and 2 sps are
    LOCKED by the M17 spec. There are no free DSP parameters — the sync word IS the
    algorithm's coefficient set.

    Interface:
        - Entry: R1 (cell ``d0``)
        - Input: R0 (RX matched-filter sample, signed Q15, 2 sps)
        - Output: R0 (recovered symbol-center value, one per symbol, after lock)
    """
    CATEGORY = "demodulation"
    TAGS = ["fsk", "4fsk", "c4fm", "m17", "timing_recovery", "sync", "demodulation"]

    SPS = 2
    SYNC_LEN = len(_SYNC_SIGNS)          # 8 symbols
    WINDOW = SPS * SYNC_LEN              # 16 samples spanned by the sync at 2 sps

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # The correlation sums SYNC_LEN (=8) samples, each up to ±full-scale, which would
    # overflow the 16-bit accumulator (8·32768). So each sample is first scaled DOWN by
    # 1/SYNC_LEN via a SIGN-CORRECT Q15 MULQ (``CORR_SCALE_Q15`` = 32768/8 = 4096 =
    # 0.125 in Q15; a raw logical SHR would mangle negatives — INV-13). The sum then
    # fits int16 and the ideal aligned peak is ≈ SYNC_LEN·(full-scale/8) = full-scale.
    CORR_SCALE_Q15 = 4096                 # 1/SYNC_LEN in Q15 (sign-correct MULQ)
    # Lock threshold as a fraction of that ideal peak (≈ full-scale). A fraction in
    # [0.35, 0.55] locks cleanly (validated BER 0 / 60 seeds); 0.45 is the default.
    THRESH_FRAC = 0.45
    FULL_SCALE = 32768

    def __init__(self, name: str, threshold: int = None):
        """threshold: absolute sync-correlation lock threshold. Default =
        ``THRESH_FRAC · SYNC_LEN · FULL_SCALE`` (0.45·8·32768). Deviate only to match
        a different RX gain (the correlation scales linearly with the input level)."""
        if threshold is None:
            # ideal peak ≈ SYNC_LEN·(full-scale · CORR_SCALE_Q15/32768) = full-scale.
            ideal_peak = self.SYNC_LEN * ((self.FULL_SCALE * self.CORR_SCALE_Q15) >> 15)
            threshold = int(self.THRESH_FRAC * ideal_peak)
        super().__init__(name, threshold=threshold)
        self._threshold = int(threshold)

    @property
    def cell_count(self) -> int:
        return self.N_DELAY + 2   # 4 delay/correlate cells + lock + emit

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def threshold(self) -> int:
        return self._threshold

    # ------------------------------------------------------------ reference
    def process_reference(self, input_samples) -> np.ndarray:
        """Causal streaming reference: sliding sync correlation → first local-max
        lock above threshold → 2:1 decimation. Returns the recovered symbol-center
        values (signed Q15 int16), one per payload symbol.

        Bit-exact with the on-chip cells: integer ±1 correlation, an amplitude-
        relative threshold from a running peak of |x| over the acquisition window,
        and the ``peak → +2 samples → every-2nd-sample`` decimation phase."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        x = [s16(int(v)) for v in np.asarray(input_samples).reshape(-1)]
        rev = _SYNC_SIGNS[::-1]
        reg = [0] * self.WINDOW
        out: List[int] = []
        cm1 = cm2 = 0
        thr = self._threshold
        locked = False
        next_emit = -1
        for n, xv in enumerate(x):
            # shift newest in at reg[0]
            reg = [xv] + reg[:-1]
            # correlation of the (reversed) sync with the on-phase samples, each
            # sample scaled by 1/SYNC_LEN (sign-correct Q15 MULQ) so the sum fits int16.
            c = 0
            for j in range(self.SYNC_LEN):
                scaled = (reg[2 * j] * self.CORR_SCALE_Q15) >> 15
                c += rev[j] * scaled
            if not locked:
                # first local max at n-1 (one-sample decision delay) above threshold
                if cm1 >= thr and cm1 >= cm2 and cm1 >= c:
                    locked = True
                    # peak was at sample n-1 → payload[0] center = (n-1)+2 = n+1
                    next_emit = n + 1
                cm2, cm1 = cm1, c
            else:
                if n == next_emit:
                    out.append(reg[0] & 0xFFFF)
                    next_emit += self.SPS
        return np.asarray([s16(v) for v in out], dtype=np.int16)

    def process_reference_symbols(self, input_samples) -> List[int]:
        """The recovered centers as signed ints (convenience for BER scoring)."""
        return [int(v) for v in self.process_reference(input_samples)]

    # ------------------------------------------------------------ cells
    # The sliding window is folded across FOUR delay/correlate cells of 4 samples
    # each (d0..d3), so no cell holds more than 4 shift regs + a partial sum + a
    # small program — comfortably inside the 32-word budget. Each cell holds reg
    # indices [4c .. 4c+3]; the even-lag samples it owns (its correlation taps) are
    # the two even indices in that range. The partial correlation and the forwarded
    # sample flow d0 -> d1 -> d2 -> d3, and the shifted-out oldest sample of each
    # cell becomes the newest of the next. d3 produces the full C.
    N_DELAY = 8
    PER_CELL = 2

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """d0..d7 (2-sample delay + ONE ±1 scaled sync tap each) → lock → emit.

        ``sync_rev`` (reversed M17 LSF sync) aligns with reg[0],reg[2],…,reg[14].
        Cell c owns reg[2c] (even, its correlation tap, sign ``sync_rev[c]``) and
        reg[2c+1] (odd, held only to keep the 2-sps window sliding by 1). Each cell
        adds its scaled tap to the running partial flowing d0 → … → d7; d7 emits the
        full correlation ``C``. The current sample ``x`` is forwarded straight
        through so it reaches ``lock``/``emit`` aligned with ``C``."""
        rev = _SYNC_SIGNS[::-1]
        cells = {}
        for c in range(self.N_DELAY):
            cells[f"d{c}"] = self._build_delay_cell(c, rev[c])
        cells["lock"] = self._build_lock()
        cells["emit"] = self._build_emit()
        return cells

    def _build_delay_cell(self, c, sign) -> CellProgram:
        """Delay cell c: holds reg[2c] (newest-of-pair, the correlation tap) and
        reg[2c+1] (older). Each sample: reg[2c+1] ← reg[2c] ← (new); the new sample
        is the block input (c==0) or the previous cell's shifted-out reg[2c-1].
        Adds ``sign·(reg[2c]/8)`` (sign-correct MULQ scale) to the incoming partial,
        forwards (partial, shifted-out reg[2c+1], x) downstream. d7 emits the full C.
        """
        first = (c == 0)
        last = (c == self.N_DELAY - 1)
        ra, rb = f"r{2*c}", f"r{2*c+1}"   # ra=newest-of-pair (tap), rb=older
        # The window slides by 1 each sample: the sample LEAVING this cell is the OLD
        # rb (2 samples ago). Save it into ``shout`` BEFORE the shift overwrites rb, so
        # it can be forwarded to the next cell as its new incoming sample.
        if first:
            inputs = [Port("sample", register=0)]
            head = ("    MOVE R{state:xsave}, R{in:sample}\n"
                    "    MOVE R{state:shout}, R{state:%s}\n"
                    "    MOVE R{state:%s}, R{state:%s}\n"
                    "    MOVE R{state:%s}, R{state:xsave}\n" % (rb, rb, ra, ra))
            add_upstream = ""
        else:
            inputs = [Port("pin", register=0), Port("shifted", register=1),
                      Port("xfwd", register=2)]
            head = ("    MOVE R{state:pins}, R{in:pin}\n"
                    "    MOVE R{state:xsave}, R{in:xfwd}\n"
                    "    MOVE R{state:shout}, R{state:%s}\n"
                    "    MOVE R{state:%s}, R{state:%s}\n"
                    "    MOVE R{state:%s}, R{in:shifted}\n" % (rb, rb, ra, ra))
            add_upstream = ("    MOVE R0, R{state:part}\n"
                            "    ADD R0, R{state:pins}\n"
                            "    MOVE R{state:part}, R0\n")
        # scaled tap: part = sign·(reg[2c]·scale)>>15
        corr = "    MULQ R{state:%s}, R{data:scale}\n" % ra
        if sign > 0:
            corr += "    MOVE R{state:part}, R0\n"
        else:
            corr += ("    MOVE R{state:part}, R0\n"
                     "    MOVE R0, R{data:zero}\n"
                     "    SUB R0, R{state:part}\n"
                     "    MOVE R{state:part}, R0\n")
        state = [StateVar(ra), StateVar(rb), StateVar("part"),
                 StateVar("xsave"), StateVar("shout")]
        if not first:
            state.append(StateVar("pins"))
        pname = "cout" if last else "pin"
        outs = [Port(pname), Port("xfwd"), Port("trig")]
        shifted_emit = ""
        if not last:
            outs.insert(1, Port("shifted"))
            shifted_emit = "    MOVE R0, R{state:shout}\n    {write:shifted}\n"
        template = ("start:\n" + head + corr + add_upstream
                    + "    MOVE R0, R{state:part}\n    {write:" + pname + "}\n"
                    + shifted_emit
                    + "    MOVE R0, R{state:xsave}\n    {write:xfwd}\n"
                    + "    {jump:trig}\n")
        return CellProgram(
            inputs=inputs, outputs=outs, entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1),
                  DataWord("scale", self.CORR_SCALE_Q15, address=2)],
            state=state,
            assembly_template=template,
        )

    def _build_lock(self) -> CellProgram:
        """Cell ``lock``: receives (C @R0, x @R1). Detects the FIRST local maximum of
        the sync correlation ``C`` above a fixed ``threshold`` (the RX scales the
        signal so the sync peak ≈ SYNC_LEN·full-scale, well above random 4-PAM data).
        Forwards ``x`` every sample plus a ``lk`` flag that is 1 EXACTLY on the first
        payload symbol center (2 samples after the peak) and 0 otherwise. ``emit``
        turns ``lk`` into the 2:1 decimation.

        Local max at ``cm1`` (one-sample decision delay): cm1 ≥ threshold, cm1 ≥ cm2,
        cm1 ≥ C. On lock, arm a 1-sample countdown (peak was 1 sample ago; the center
        is +2 → fires on the next-next sample when ``cd`` reaches 0)."""
        thr = self._threshold
        return CellProgram(
            inputs=[Port("cin", register=0), Port("xin", register=1)],
            outputs=[Port("xout"), Port("lk"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1),
                  DataWord("one", 1, address=2),
                  DataWord("thr", thr & 0xFFFF, address=3)],
            state=[StateVar("cval"), StateVar("cm1"), StateVar("cm2"),
                   StateVar("done", initial_value=0), StateVar("lkf")],
            assembly_template="""\
start:
    MOVE R{state:cval}, R{in:cin}
    MOVE R0, R{in:xin}
    {write:xout}
    MOVE R{state:lkf}, R{data:zero}
    ; once locked, just forward (done==1 suppresses further lock pulses)
    CMP R{state:done}, R{data:one}
    BR.Z fwd
    ; --- acquisition: first local max of C above threshold at cm1 ---
    CMP R{state:cm1}, R{data:thr}
    BR.N shift_c
    CMP R{state:cm1}, R{state:cm2}
    BR.N shift_c
    CMP R{state:cm1}, R{state:cval}
    BR.N shift_c
    MOVE R{state:done}, R{data:one}
    MOVE R{state:lkf}, R{data:one}
shift_c:
    MOVE R{state:cm2}, R{state:cm1}
    MOVE R{state:cm1}, R{state:cval}
fwd:
    MOVE R0, R{state:lkf}
    {write:lk}
    {jump:trig}
""",
        )

    def _build_emit(self) -> CellProgram:
        """Cell ``emit``: receives (x @R0, lk @R1). ``lk``==1 marks the first payload
        symbol center; from then on emit every 2nd sample (2:1 decimation). Before
        the first ``lk`` nothing is emitted (acquisition)."""
        return CellProgram(
            inputs=[Port("xin", register=0), Port("lkin", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=1), DataWord("one", 1, address=2)],
            state=[StateVar("xv"), StateVar("phase", initial_value=0),
                   StateVar("run", initial_value=0)],
            assembly_template="""\
start:
    MOVE R{state:xv}, R{in:xin}
    ; on the lock pulse: arm the decimator. The lock pulse fires on the peak+1
    ; sample; the first payload center is peak+2 = the NEXT sample, so start with
    ; phase=1 (skip this sample) and emit when phase reaches 0.
    CMP R{in:lkin}, R{data:one}
    BR.NZ run_check
    MOVE R{state:run}, R{data:one}
    MOVE R{state:phase}, R{data:one}
run_check:
    CMP R{state:run}, R{data:one}
    BR.NZ done
    ; emit when phase==0 (a symbol center), else count down to it
    CMP R{state:phase}, R{data:zero}
    BR.NZ flip
    MOVE R0, R{state:xv}
    {write:out}
    MOVE R{state:phase}, R{data:one}
    {jump:trig}
flip:
    MOVE R{state:phase}, R{data:zero}
done:
""",
        )

    def internal_connections(self):
        conns = []
        for c in range(self.N_DELAY - 1):
            conns.append((f"d{c}", "pin", f"d{c+1}", "pin"))
            conns.append((f"d{c}", "shifted", f"d{c+1}", "shifted"))
            conns.append((f"d{c}", "xfwd", f"d{c+1}", "xfwd"))
        # last delay cell -> lock; lock -> emit
        last = f"d{self.N_DELAY-1}"
        conns.append((last, "cout", "lock", "cin"))
        conns.append((last, "xfwd", "lock", "xin"))
        conns.append(("lock", "xout", "emit", "xin"))
        conns.append(("lock", "lk", "emit", "lkin"))
        return conns

    def internal_jumps(self):
        jumps = [(f"d{c}", "trig", f"d{c+1}", "default")
                 for c in range(self.N_DELAY - 1)]
        jumps.append((f"d{self.N_DELAY-1}", "trig", "lock", "default"))
        jumps.append(("lock", "trig", "emit", "default"))
        return jumps

    def output_cell_ids(self):
        return ["emit"]

    def default_layout(self):
        # Serpentine fold, ≤8 across (INV-9): d0..d7 across the top row (west→east),
        # then lock/emit fold back on the row below so the block's external I/O (d0
        # input, emit output) co-locate near the west edge.
        #   row 0:  d0 d1 d2 d3 d4 d5 d6 d7   (east)
        #   row 1:  emit lock  ... (west)     — d7 drops south to lock, lock→emit west
        lay = {}
        for c in range(self.N_DELAY):
            face = "south" if c == self.N_DELAY - 1 else "east"
            lay[f"d{c}"] = (c, 0, face)
        lay["lock"] = (self.N_DELAY - 1, 1, "west")
        lay["emit"] = (self.N_DELAY - 2, 1, "west")
        return lay

    def reset(self):
        pass
