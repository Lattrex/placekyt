# SPDX-License-Identifier: GPL-3.0-or-later
"""GardnerTimingRecovery — see :class:`GardnerTimingRecovery`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class GardnerTimingRecovery(KyttarBlock):
    """
    Gardner symbol-timing recovery — production 4-cell implementation of GNU
    Radio's ``digital.symbol_sync`` control loop (TED_GARDNER).

    A REAL timing-recovery loop that advances/retards WHERE it samples (the older
    fixed-rate-decimator version could not). A 2-samples/symbol input drives a
    Gardner timing-error detector + a 2nd-order PI loop filter + a Q14-NCO
    interpolating resampler. The loop is faithful to GR ``symbol_sync``:

      * PI gains derive from GR's ``loop_bw=0.045`` + ``damping=1.0`` (verified
        against ``digital.symbol_sync_ff(...).alpha()/.beta()`` = 0.08607 /
        0.0019362), applied as power-of-two ``MULQ`` scalings on the error.
      * The interpolator period is CLAMPED to nominal +- ``max_dev`` (GR default
        1.5 full samples => 0.75 half-sample), which is also the integrator
        anti-windup.

    Over a LONG continuous RX stream the loop PLATEAUS at the nominal period and
    recovers BER 0 (fractional offsets 0.3-0.7, >=720-symbol streams, and the full
    coupled MF->Costas->Gardner->Slicer chain). The previous kp/ki PI DID NOT
    CONVERGE — its period collapsed monotonically and the sampler slipped after
    ~180 symbols; see ``process_reference`` for the three fixed-point details that
    make it converge on 16-bit hardware (wide error product to dodge int16
    overflow, a full-width integrator, and matching the chip's TRUNCATING MULQ +
    a half-LSB rounding bias on the integral term to keep the loop DC-neutral).

    Cells (compact 2x2 fold; the period feedback returns via the relay)::

        resampler ──► ted ──► loop_filter
            ▲                      │
            └──── period_relay ◄───┘   (PI filter; writes inst_next back as data)

      * resampler: 2-sample delay line + a Q14 phase accumulator (1.0 = 0x4000);
        each input advances phase and, when phase>=inst_active, emits ONE linearly
        interpolated strobe (value + a parity tag, 0=center / 0x4000=mid). On the
        strobe it ADOPTS ``inst_next`` (the deferred period fed back by the relay
        after the previous strobe). No mid-reset — both strobes use the loop period.
      * ted: on a CENTER, forms the WIDE Gardner error high-word
        ``ewhi = MULHI(mid, (s>>1)-(cprev>>1))`` (the ``>>1`` keeps the BPSK sample
        difference inside int16); passes the center sample through.
      * loop_filter: emits the recovered center forward (to the slicer/bus) AND
        hands ``ewhi`` to the period_relay (dual-face emit).
      * period_relay: the GR PI filter. ``iavg += round(ewhi>>8)`` clamped to
        +-max_dev; ``inst = one_q14 + iavg + (ewhi>>2)``; writes ``inst`` back into
        the resampler's ``inst_next`` as pure data (the Costas-dphase feedback
        model — closes the loop through a data path, not a trigger).

    Interface: a real 2-sps input stream; the output is the recovered
    symbol-rate (center) sample stream (slice its sign for BPSK bits).
    """
    CATEGORY = "recovery"
    TAGS = ["gardner", "timing_recovery", "ted", "symbol_sync", "recovery"]

    # --- GR digital.symbol_sync control-loop constants (loop_bw=0.045, damping=1.0).
    # GR derives alpha/beta from loop_bw+damping; verified against
    # ``digital.symbol_sync_ff(...).alpha()/.beta()`` = 0.08607 / 0.0019362. Rather
    # than carry those Q15 gains through two rounding multiplies (which quantise the
    # per-symbol correction to zero and let a DC bias wind the loop down), we apply
    # the loop filter directly on the WIDE error high-word ``ewhi`` via two power-of-two
    # scalings whose amounts reproduce GR's alpha/beta after the +-max_dev clamp.
    # ``ewhi = MULHI(mid, (s>>1)-(cprev>>1))`` = (mid*(s-cprev)) >> 17.
    #   integral:      iavg += ewhi >> _SB_INTEG    (=> effective integral gain)
    #   proportional:  inst  = avg + ewhi >> _SB_PROP
    # On chip each ``>> n`` is a SINGLE, SIGN-CORRECT ``MULQ(x, 2^(15-n))`` (the ISA
    # SHR is LOGICAL, so a raw shift would corrupt negatives; MULQ rounds and preserves
    # sign in one instruction). The Q15 multipliers are ``_MULQ_INTEG`` / ``_MULQ_PROP``.
    # Values calibrated so avg PLATEAUS at nominal (16384) over >=720-symbol streams
    # with BER 0 across seeds and fractional offsets 0.3-0.7 (see process_reference).
    _SB_INTEG = 8      # integral-term shift on ewhi   (>>8)
    _SB_PROP = 2       # proportional-term shift on ewhi (>>2)
    _MULQ_INTEG = 1 << (15 - 8)   # 128  : MULQ(ewhi,128)  == ewhi >> 8
    _MULQ_PROP = 1 << (15 - 2)    # 8192 : MULQ(ewhi,8192) == ewhi >> 2
    _MULQ_HALF = 1 << 14          # 16384: MULQ(x,16384)   == x >> 1 (sample halving)
    # Half-LSB rounding bias for the integral MULQ (the ISA MULQ floors): adding this
    # before ``MULQ(ewhi,128)`` turns the floor into round-to-nearest, removing the
    # -0.5-LSB DC that would otherwise wind the integrator down. = 2^(8-1) (half of 2^8).
    _INTEG_RBIAS = 1 << (8 - 1)   # 128
    # GR max_dev = 1.5 full samples => 0.75 half-sample => 0.75 * 16384 (Q14).
    _MAXDEV = 12288    # 0.75 sample in Q14; period clamped to 16384 +- _MAXDEV

    # Complex/real 2-sps input lands at R0 of the resampler landing cell; the
    # recovered center sample is the output. (COMPLEX mode overrides this with a
    # two-register xi/xq interface + a two-register yi/yq output — see __init__.)
    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0]
    )

    _CELL_IDS = ["resampler", "ted", "loop_filter", "period_relay"]

    # loop_filter dual-face emit (face codes S=0, E=1, W=2, N=3). `out` egresses
    # SOUTH (outward, toward the bus/downstream slicer); `period_fb` returns WEST
    # to the `period_relay` cell (which forwards it NORTH into the resampler's
    # `period` state — see below). These MUST stay consistent with
    # ``default_layout`` (loop_filter's resting face == FACE_FB so the build's
    # feedback tracer follows it to the relay).
    _FACE_OUT = 0   # south
    _FACE_FB = 2    # west

    def __init__(self, name: str, kp: int = 3, ki: int = 1,
                 pipeline_lock: bool = True, complex: bool = False):
        """
        Args:
            name: Block name.
            complex: When True, 2-rail (I/Q) timing recovery — the SAME I-driven
                Gardner timing loop (NCO, TED on I, PI loop filter, period feedback all
                identical and driven by the I rail only), with a parallel Q rail
                that interpolates the Q sample at each strobe with the SAME ``frac``;
                the block emits the recovered (yi, yq) symbol-center pair, so a
                downstream QPSKSlicer can decode QPSK. The Q15 REFERENCE
                (``process_reference``) is verified: its I channel is BIT-EXACT to the
                real (BPSK) reference and it recovers a QPSK-with-timing-offset stream
                to the +-1/sqrt(2) grid. The ON-CHIP complex cells are a 6-cell
                topology (a ``qdelay`` landing cell that duplicates the Q NCO + owns
                the Q delay line + interpolates Q, and a ``qout`` output cell that
                emits the yi/yq pair) that is BIT-EXACT to the reference — see
                ``_build_complex_cell_programs``. ``complex=False`` (default) is the
                shipped, byte-identical BPSK timing loop.
            kp, ki: DEPRECATED. The loop filter now derives its proportional/integral
                gains from GR's ``loop_bw=0.045`` + ``damping=1.0`` (the
                ``digital.symbol_sync`` control loop) as fixed shift amounts
                (``_SB_PROP`` / ``_SB_INTEG``), NOT raw kp/ki multiplies. These
                arguments are accepted for backward compatibility but IGNORED.
            pipeline_lock: When True (default) the timing loop is SERIALIZED under
                saturated drive (INV-19): on a STROBE the ``resampler`` LOCKs its
                arbiter to the feedback face so the NEXT input sample is HELD until the
                ``period_relay`` closes the loop (writes ``inst_next`` back) and CLEARS
                the lock via a backward ``WRITE.CFG``. Without it, under continuous
                (back-to-back) drive the resampler strobes AGAIN before the PI filter's
                corrected period has fed back, using a STALE ``inst_next`` — the loop
                decouples and the recovered symbols drift from the per-sample reference
                (the Costas-dphase cautionary tale; same idiom as the Costas fix).
                Set False for the legacy inject-and-flush (per-sample quiescence) path.
        """
        super().__init__(name, kp=kp, ki=ki)
        self._kp = int(kp)
        self._ki = int(ki)
        self._pipeline_lock = bool(pipeline_lock)
        self._complex = bool(complex)
        if self._complex:
            # COMPLEX (2-rail) mode: the block lands a complex (xi, xq) sample on
            # the qdelay landing cell (xi@R0, xq@R1 — the ComplexCostasLoop/MF
            # complex-input convention) and emits the recovered (yi, yq) center
            # pair. See build_cell_programs' complex branch for the 6-cell topology.
            self._interface = BlockInterface(
                entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    @property
    def cell_count(self) -> int:
        # REAL: resampler, ted, loop_filter + period_relay (4 cells).
        # COMPLEX: adds a qdelay landing cell (Q delay line + duplicate Q NCO +
        # Q interpolation) and a qout output cell (emits the yi/yq pair) — 6 cells.
        return 6 if self._complex else 4

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The 4 cells implementing the GR symbol_sync control loop (see
        ``process_reference`` for the algorithm this is bit-exact with)."""
        if self._complex:
            return self._build_complex_cell_programs()

        # --- C1 resampler: Q14 NCO + 2-sample delay line + interp + parity. -----
        # Fires ONE strobe per 1.0-sample advance (2 per symbol). The instantaneous
        # HALF-period ``inst_active`` (Q14) is compared against ``phase``; on a strobe
        # it ADOPTS ``inst_next`` (the deferred feedback the relay wrote after the
        # previous strobe — the Costas-dphase feedback model). There is NO
        # mid-reset-to-nominal: both strobes use the loop's period. This is the fix
        # for the long-stream drift/collapse.
        # SERIALIZE-LOCK (INV-19): on a STROBE the resampler LOCKs its arbiter to the
        # feedback face (SOUTH, where period_relay.pout returns) so the NEXT input
        # sample is HELD until the PI filter closes the loop. lock_face is an is_face
        # DataWord (SOUTH=0) so it transforms with orientation; ``one`` engages LOCK.
        # Placed at addresses 3/4 (contiguous after inc@1, one_q14@2). The lock tail
        # runs ONLY on the strobe path (after {jump:val}); the no-strobe ``done`` path
        # never locks, so non-strobe samples keep flowing to advance ``phase``.
        # SERIALIZE-LOCK engage — cost-reduced to fit this budget-tight 7-state landing
        # cell. The arbiter's CONFIG default LOCK_FACE is 00 = SOUTH (verified against
        # the cell CONFIG reset default), which is EXACTLY the feedback face (period_relay sits
        # SOUTH of the resampler and emits NORTH into it). So LOCK_FACE need NOT be
        # written — the reset default already gates all-but-SOUTH. We only set LOCK=1
        # (any nonzero engages it; reuse ``one_q14``=1<<14, no extra data word). This
        # is a 2-instruction tail (was 4). NOTE: this relies on the UN-rotated layout
        # (SOUTH feedback); default_layout places the block accordingly and the block
        # is not auto-oriented in the RX chain. If a future layout rotates it, add back
        # an is_face ``lock_face`` DataWord + the two LOCK_FACE writes (orientation-
        # transformed) — but that needs 2 more register slots freed first.
        rs_lock_data = []
        rs_lock_tail = ("""\
    MOVE R0, R{data:inc}
    MOVE [LOCK], R0
""" if self._pipeline_lock else "")
        resampler = CellProgram(
            inputs=[Port("xi", register=0)],
            outputs=[Port("val"), Port("par"), Port("trig")],
            entries=[EntryPoint("default")],
            # ``inc`` (phase increment) and the old ``one_q14`` (parity toggle) are the
            # IDENTICAL value 1<<14, so they share ONE data word (``inc``) — freeing the
            # register slot the serialize-LOCK's LOCK=1 write needs on this tight cell.
            data=[DataWord("inc", 1 << 14, address=1)] + rs_lock_data,
            # LOOP MEMORY (reset_per_batch): the NCO phase accumulator, the 2-sample
            # delay line (xp/xp2), the instantaneous half-period feedback registers
            # (inst_active/inst_next), and the strobe parity — all carry the previous
            # packet's converged timing lock. A fresh packet MUST start cold (the same
            # cold values as their initial_value: phase warm-0.5, periods nominal, rest
            # 0), else the new packet's first samples arrive into a mis-locked
            # resampler and the recovered symbols slip. ``diff`` is per-sample scratch
            # (written before read each pass), so it need not be flagged.
            state=[StateVar("phase", initial_value=(1 << 14) >> 1,  # warm 0.5
                            reset_per_batch=True),
                   StateVar("xp", reset_per_batch=True),
                   StateVar("xp2", reset_per_batch=True),
                   StateVar("inst_active", initial_value=1 << 14,
                            reset_per_batch=True),
                   StateVar("inst_next", initial_value=1 << 14,
                            reset_per_batch=True),
                   StateVar("parity", reset_per_batch=True),
                   StateVar("diff")],
            assembly_template="""\
start:
    MOVE R{state:xp2}, R{state:xp}
    MOVE R{state:xp}, R{in:xi}
    ADD R{state:phase}, R{data:inc}
    MOVE R{state:phase}, R0
    SUB R{state:phase}, R{state:inst_active}
    BR.N done
    MOVE R{state:phase}, R0
    MOVE R{state:inst_active}, R{state:inst_next}
    SUB R{state:xp}, R{state:xp2}
    MOVE R{state:diff}, R0
    SHL R{state:phase}, #1
    MULQ R0, R{state:diff}
    ADD R0, R{state:xp2}
    {write:val}
    MOVE R0, R{state:parity}
    {write:par}
    XOR R{state:parity}, R{data:inc}
    MOVE R{state:parity}, R0
    {jump:val}
""" + rs_lock_tail + """\
done:
    {jump:trig}
""",
        )

        # --- C2 ted: Gardner error on a CENTER strobe; pass the center through. ---
        # par==0 => CENTER, par==0x4000 => MID (the resampler's parity tag). On a
        # MID it just latches ``half`` (the mid sample) and terminates. On a CENTER
        # it forms the WIDE Gardner error high-word:
        #     dc_half = (s>>1) - cph                    # cph = (prev center)>>1
        #     ewhi    = MULHI(half, dc_half)            # signed high word of product
        # The ``>>1`` keeps the BPSK sample DIFFERENCE inside int16 (a full-scale
        # ``s - cprev`` OVERFLOWS int16 and corrupts the error — the true root cause of
        # the old long-stream drift). It is done as ``MULQ(s, 16384)`` = s/2: ONE
        # instruction, SIGN-CORRECT (the ISA SHR is LOGICAL, so a raw shift would
        # mangle negative samples). We keep the previous center's halved value ``cph``
        # as state so only one halving is done per center. Forwards ``ewhi`` (to the PI
        # relay) and ``s`` (to the loop_filter `out`).
        ted = CellProgram(
            inputs=[Port("val", register=0), Port("par", register=1)],
            outputs=[Port("e_out"), Port("c_out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one_q14", 1 << 14, address=2),
                  DataWord("mulq_half", self._MULQ_HALF, address=3)],
            # LOOP MEMORY: ``cph`` is the PREVIOUS center's halved value and ``half``
            # is the mid sample carried ACROSS strobes into the next center's Gardner
            # error — both bridge packets and must cold-start at 0 for a fresh packet.
            # ``csh``/``cs`` are within-pass scratch (written before read each center).
            state=[StateVar("cph", reset_per_batch=True),
                   StateVar("half", reset_per_batch=True),
                   StateVar("csh"), StateVar("cs")],
            assembly_template="""\
start:
    CMP R{in:par}, R{data:one_q14}
    BR.NZ center
    MOVE R{state:half}, R{in:val}
    {jump:trig}
center:
    MOVE R{state:cs}, R{in:val}
    MULQ R{state:cs}, R{data:mulq_half}
    MOVE R{state:csh}, R0
    SUB R0, R{state:cph}
    MULHI R0, R{state:half}
    MOVE R{state:cph}, R{state:csh}
    {write:e_out}
    MOVE R0, R{state:cs}
    {write:c_out}
    {jump:c_out}
""",
        )

        # --- C3 loop_filter: emits the recovered center `out` (forward, toward the
        # downstream slicer/bus) AND hands the Gardner error `e` to the period_relay
        # (the PI filter, which computes + feeds back the period). The PI math lives
        # in the relay, NOT here, for two reasons:
        #   (1) FEEDBACK ROUTING: a feedback loop must not be closed through a TRIGGER
        #       path — if this last datapath cell triggered the resampler directly, the
        #       loop (resampler -> ted -> loop_filter -> period -> resampler) would have
        #       no slack and stall after one center. Instead a relay cell accepts this
        #       cell's emit (freeing `out` to go forward) and writes `period` into the
        #       resampler as PURE DATA (no trigger — read by the NEXT sample, like the
        #       Costas dphase feedback). The data-only feedback breaks the cyclic
        #       dependency so the loop runs continuously.
        #   (2) BUDGET: the full PI filter (integ accumulate + +/-256 clamp + corr +
        #       period + >=1 floor) does not fit in one cell alongside the dual-face
        #       emit. The relay has ample register space.
        #
        # DUAL-FACE EMIT: this cell rests facing the feedback (WEST) direction so the
        # build's feedback tracer (``_apply_internal_feedback``, which follows the
        # cell's resting fwd_face) finds the relay; it emits `e_fb` + ``fb_trig`` WEST
        # to the relay, then FLIPS to FACE_OUT and emits `out` SOUTH (the highest-address
        # WRITE, so ``_patch_last_write_handoff`` patches only the egress), then a FINAL
        # flip back to WEST. face codes S=0,E=1,W=2,N=3.
        loop_filter = CellProgram(
            inputs=[Port("e_in", register=0), Port("cval", register=1)],
            outputs=[Port("out"), Port("e_fb"), Port("fb_trig"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_out", self._FACE_OUT, address=2, is_face=True),
                  DataWord("face_fb", self._FACE_FB, address=3, is_face=True)],
            state=[StateVar("es"), StateVar("cs")],
            assembly_template="""\
start:
    MOVE R{state:es}, R{in:e_in}
    MOVE R{state:cs}, R{in:cval}
    MOVE [FACE], R{data:face_fb}
    MOVE R0, R{state:es}
    {write:e_fb}
    {jump:fb_trig}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{state:cs}
    {write:out}
    {jump:trig}
    MOVE [FACE], R{data:face_fb}
""",
        )

        # --- C4 period_relay: the GR PI loop filter + the deadlock-breaking relay.
        #
        # Triggered by the loop_filter on each CENTER (``fb_trig``); receives the WIDE
        # Gardner error high-word ``ewhi``. Runs GR's symbol_sync control loop:
        #     iavg += SAR(ewhi, _SB_INTEG)                       # integral (WIDE accum)
        #     iavg  = clamp(iavg, -_MAXDEV, +_MAXDEV)            # +- max_dev on avg
        #     avg   = one_q14 + iavg
        #     inst  = avg + SAR(ewhi, _SB_PROP)                  # + proportional
        # then WRITES ``inst`` (pure data, NO trigger) into the resampler's ``inst_next``
        # state — the Costas-dphase feedback model (read by the NEXT strobe). Keeping
        # the integrator ``iavg`` at FULL width (never requantised) and deriving the
        # period as ``one_q14 + iavg`` is what stops the drift: a per-symbol Q15
        # ``beta*e`` would round to zero and let a DC bias wind the loop down.
        # SAR = arithmetic right shift via the sign-extension-mask idiom (the ISA SHR
        # is logical): SHR then OR the top-n-bit mask when the value is negative.
        # The +- _MAXDEV clamp on iavg == GR's max_dev clamp on the interpolator period
        # (also the anti-windup); the small proportional term needs no separate clamp.
        # Each ``ewhi >> n`` is ONE sign-correct ``MULQ(ewhi, 2^(15-n))`` (see the class
        # constants) — far cheaper and safer than a logical-SHR sign-extension idiom.
        MAXDEV = self._MAXDEV
        ONEQ = 1 << 14
        period_relay = CellProgram(
            inputs=[Port("e_in", register=0)],
            outputs=[Port("pout")],
            entries=[EntryPoint("relay")],
            # LOOP MEMORY: ``iavg`` is the PI integrator — the accumulated timing
            # correction, the heart of the converged lock. It MUST cold-start at 0 for
            # a fresh packet (else the new packet inherits the old period bias and
            # slips). ``es`` is per-trigger scratch (written before read each center).
            state=[StateVar("iavg", reset_per_batch=True), StateVar("es")],
            # SERIALIZE-LOCK release (INV-19): after writing the corrected period back
            # into the resampler's ``inst_next`` (the {write:pout} data feedback), CLEAR
            # the resampler's arbiter LOCK with a backward ``WRITE.CFG @N, 4`` (R0=0 ->
            # resampler CONFIG[4]=LOCK), releasing the input sample HELD since the last
            # strobe. ``pout`` and the WRITE.CFG both travel the SAME period_relay->
            # resampler corridor (NORTH); the @N authored hop is re-patched to the real
            # resolved corridor hop by build's _apply_internal_feedback (it patches the
            # feedback WRITE hop AND this WRITE.CFG hop together, like the Costas pd_pi).
            # ``lzero`` provides the R0=0 for the config clear. Only emitted when locked.
            data=[DataWord("one_q14", ONEQ, address=1),
                  DataWord("mulq_integ", self._MULQ_INTEG, address=2),
                  DataWord("mulq_prop", self._MULQ_PROP, address=3),
                  DataWord("pdev", MAXDEV, address=4),
                  DataWord("ndev", (-MAXDEV) & 0xFFFF, address=5),
                  DataWord("rbias", self._INTEG_RBIAS, address=6)]
                 + ([DataWord("lzero", 0, address=7)] if self._pipeline_lock else []),
            assembly_template="""\
relay:
    MOVE R{state:es}, R{in:e_in}
    MOVE R0, R{state:es}
    ADD R0, R{data:rbias}
    MULQ R0, R{data:mulq_integ}
    ADD R0, R{state:iavg}
    MOVE R{state:iavg}, R0
    CMP R{state:iavg}, R{data:pdev}
    BR.N ihi
    MOVE R{state:iavg}, R{data:pdev}
ihi:
    CMP R{state:iavg}, R{data:ndev}
    BR.NN ilo
    MOVE R{state:iavg}, R{data:ndev}
ilo:
    MOVE R0, R{state:es}
    MULQ R0, R{data:mulq_prop}
    ADD R0, R{state:iavg}
    ADD R0, R{data:one_q14}
    {write:pout}
""" + ("""\
    MOVE R0, R{data:lzero}
    WRITE.CFG @1, 4
""" if self._pipeline_lock else ""),
        )

        return {"resampler": resampler, "ted": ted, "loop_filter": loop_filter,
                "period_relay": period_relay}

    # ================= COMPLEX (2-rail I/Q) on-chip cells =================
    def _build_complex_cell_programs(self) -> Dict[str, CellProgram]:
        """The 6-cell COMPLEX (I/Q) timing loop — BIT-EXACT with
        ``process_reference(complex=True)``.

        The I timing loop is the SAME shipped BPSK loop (``resampler`` -> ``ted``
        -> ``loop_filter`` -> ``period_relay`` PI). Two new cells add the Q rail
        WITHOUT touching the (register-FULL) resampler's I math:

          * ``qdelay`` — the COMPLEX LANDING cell (xi@R0, xq@R1). It runs on EVERY
            input sample (the reference shifts the Q delay line every sample). It:
              (1) forwards its deferred period ``qinst_next`` -> resampler.inst_next
                  and ``xi`` -> resampler.xi, then TRIGGERS the resampler;
              (2) shifts its OWN Q delay line (xpq/xp2q);
              (3) runs a DUPLICATE Q14 phase NCO — bit-identical to the resampler's
                  (same warm-0.5 phase, same nominal period, fed the SAME deferred
                  period ``qinst_next`` that period_relay computes) — so it strobes
                  on EXACTLY the same samples as the resampler. On its strobe it
                  linearly interpolates the Q rail with the SAME ``frac`` the I
                  resampler uses (``sq_i = xp2q + MULQ(frac, xpq-xp2q)``) and WRITES
                  it (pure data) to ``qout``. It writes on every strobe (center AND
                  mid); the CENTER gate is applied downstream (only a center triggers
                  ``qout``). No parity is tracked here — the reference computes sq_i
                  on every strobe and gates only the OUTPUT on center.
          * ``qout`` — the block's single external OUTPUT cell. On a CENTER it is
            triggered by the loop_filter (which hands it the recovered ``yi``); it
            emits ``yi`` then the held ``yq`` (last written by qdelay on this
            sample's strobe) then ONE trigger — the ComplexCostasLoop/MF complex
            output contract (yi -> R0, yq -> R1) a downstream QPSKSlicer consumes.

        The whole thing is a LINEAR ring (qdelay -> resampler -> ted -> loop_filter
        -> qout, plus the loop_filter -> period_relay -> qdelay PI feedback), so
        every rendezvous is ordered by the chain: qdelay & resampler run UPSTREAM of
        loop_filter, so both ``yq`` (from qdelay) and the deferred period are already
        in place before ``qout``/the resampler-strobe consume them. The period
        feedback returns to qdelay (single target); qdelay bridges it to the
        resampler as a per-sample forward, keeping BOTH NCOs on the identical period.
        """
        ONEQ = 1 << 14
        INC = 1 << 14

        # --- qdelay: complex landing + Q delay line + duplicate Q NCO + interp. ---
        qdelay = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("instfwd"), Port("xifwd"), Port("yq_out"), Port("rtrig")],
            entries=[EntryPoint("default")],
            data=[DataWord("inc", INC, address=2)],
            # LOOP MEMORY (cold-start each packet, exactly like the resampler): the Q
            # NCO phase, the deferred/active periods, and the Q delay line all carry
            # the previous packet's converged lock. ``diffq`` is per-strobe scratch.
            state=[StateVar("qphase", initial_value=INC >> 1, reset_per_batch=True),
                   StateVar("xpq", reset_per_batch=True),
                   StateVar("xp2q", reset_per_batch=True),
                   StateVar("qinst_active", initial_value=ONEQ,
                            reset_per_batch=True),
                   StateVar("qinst_next", initial_value=ONEQ,
                            reset_per_batch=True),
                   StateVar("diffq")],
            assembly_template="""\
start:
    MOVE R0, R{in:xi}
    {write:xifwd}
    MOVE R0, R{state:qinst_next}
    {write:instfwd}
    MOVE R{state:xp2q}, R{state:xpq}
    MOVE R{state:xpq}, R{in:xq}
    ADD R{state:qphase}, R{data:inc}
    MOVE R{state:qphase}, R0
    SUB R{state:qphase}, R{state:qinst_active}
    BR.N qdone
    MOVE R{state:qphase}, R0
    MOVE R{state:qinst_active}, R{state:qinst_next}
    SUB R{state:xpq}, R{state:xp2q}
    MOVE R{state:diffq}, R0
    SHL R{state:qphase}, #1
    MULQ R0, R{state:diffq}
    ADD R0, R{state:xp2q}
    {write:yq_out}
    {jump:rtrig}
    HALT
qdone:
    {jump:rtrig}
""",
        )

        # --- resampler: the SHIPPED I timing loop, verbatim (no pipeline lock in
        # the complex path — the qdelay/qout rendezvous is ordered by the chain, and
        # the acceptance harness drives per-sample). Its ``inst_next`` state is fed
        # by qdelay's per-sample forward (kept identical to the qdelay NCO period).
        resampler = CellProgram(
            inputs=[Port("xi", register=0)],
            outputs=[Port("val"), Port("par"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("inc", INC, address=1)],
            state=[StateVar("phase", initial_value=INC >> 1, reset_per_batch=True),
                   StateVar("xp", reset_per_batch=True),
                   StateVar("xp2", reset_per_batch=True),
                   StateVar("inst_active", initial_value=ONEQ, reset_per_batch=True),
                   StateVar("inst_next", initial_value=ONEQ, reset_per_batch=True),
                   StateVar("parity", reset_per_batch=True),
                   StateVar("diff")],
            assembly_template="""\
start:
    MOVE R{state:xp2}, R{state:xp}
    MOVE R{state:xp}, R{in:xi}
    ADD R{state:phase}, R{data:inc}
    MOVE R{state:phase}, R0
    SUB R{state:phase}, R{state:inst_active}
    BR.N done
    MOVE R{state:phase}, R0
    MOVE R{state:inst_active}, R{state:inst_next}
    SUB R{state:xp}, R{state:xp2}
    MOVE R{state:diff}, R0
    SHL R{state:phase}, #1
    MULQ R0, R{state:diff}
    ADD R0, R{state:xp2}
    {write:val}
    MOVE R0, R{state:parity}
    {write:par}
    XOR R{state:parity}, R{data:inc}
    MOVE R{state:parity}, R0
    {jump:val}
done:
    {jump:trig}
""",
        )

        # --- ted: the SHIPPED Gardner TED, verbatim. ---
        ted = CellProgram(
            inputs=[Port("val", register=0), Port("par", register=1)],
            outputs=[Port("e_out"), Port("c_out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one_q14", ONEQ, address=2),
                  DataWord("mulq_half", self._MULQ_HALF, address=3)],
            state=[StateVar("cph", reset_per_batch=True),
                   StateVar("half", reset_per_batch=True),
                   StateVar("csh"), StateVar("cs")],
            assembly_template="""\
start:
    CMP R{in:par}, R{data:one_q14}
    BR.NZ center
    MOVE R{state:half}, R{in:val}
    {jump:trig}
center:
    MOVE R{state:cs}, R{in:val}
    MULQ R{state:cs}, R{data:mulq_half}
    MOVE R{state:csh}, R0
    SUB R0, R{state:cph}
    MULHI R0, R{state:half}
    MOVE R{state:cph}, R{state:csh}
    {write:e_out}
    MOVE R0, R{state:cs}
    {write:c_out}
    {jump:c_out}
""",
        )

        # --- loop_filter (complex): a DUAL-FACE cell exactly like the real block,
        # but ``yi_out`` (the recovered center) goes to the internal ``qout`` cell
        # (SOUTH) instead of an external port, and the Gardner error ``e_fb`` goes to
        # the period_relay (WEST). It flips FACE between the two forward handoffs. Both
        # are FORWARD (qout, period_relay follow loop_filter in the dict), so the
        # router resolves each WRITE's hop; the FACE flips steer them. Its resting face
        # is WEST (the feedback-error direction) — but the real feedback edge the build
        # traces is period_relay -> qdelay, so loop_filter's face here only needs to
        # steer its own two emits.
        # Face codes S=0, E=1, W=2, N=3. In the COMPACT 3x3 fold (see default_layout)
        # loop_filter sits at (2,1): its chain-next ``qout`` is directly SOUTH (2,2) and
        # the ``period_relay`` is directly WEST (1,1). So yi_out egresses SOUTH (@1 ->
        # qout, the chain-forward face == the cell's resting fwd_face) and e_fb egresses
        # WEST (@1 -> period_relay, perpendicular so the two rails never collide).
        _CFACE_OUT = 0   # south (yi_out -> qout, chain-forward)
        _CFACE_FB = 2    # west  (e_fb  -> period_relay, perpendicular)
        loop_filter = CellProgram(
            inputs=[Port("e_in", register=0), Port("cval", register=1)],
            outputs=[Port("yi_out"), Port("e_fb"), Port("fb_trig"), Port("otrig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_out", _CFACE_OUT, address=2, is_face=True),
                  DataWord("face_fb", _CFACE_FB, address=3, is_face=True)],
            state=[StateVar("es"), StateVar("cs")],
            assembly_template="""\
start:
    MOVE R{state:es}, R{in:e_in}
    MOVE R{state:cs}, R{in:cval}
    MOVE [FACE], R{data:face_fb}
    MOVE R0, R{state:es}
    {write:e_fb}
    {jump:fb_trig}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{state:cs}
    {write:yi_out}
    {jump:otrig}
""",
        )

        # --- qout: the single external OUTPUT cell. Triggered by loop_filter on a
        # CENTER, it emits yi (-> R0) then the held yq (-> R1) then ONE trigger — the
        # complex-output contract. ``yq`` is the last value qdelay wrote this sample.
        # yi lands at R0 (loop_filter's yi_out), yq at R1 (qdelay's yq_out). No state
        # regs (they would ALIAS the pinned R0/R1 inputs). WRITE does not clobber R0.
        qout = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("yi_e"), Port("yq_e"), Port("trig")],
            entries=[EntryPoint("default")],
            assembly_template="""\
start:
    MOVE R0, R{in:yi}
    {write:yi_e}
    MOVE R0, R{in:yq}
    {write:yq_e}
    {jump:trig}
""",
        )

        # --- period_relay: the SHIPPED PI filter, verbatim, EXCEPT ``pout`` returns
        # to qdelay's ``qinst_next`` (qdelay then forwards it to the resampler each
        # sample, keeping both NCOs on the identical period). No pipeline-lock config
        # write in the complex path.
        MAXDEV = self._MAXDEV
        period_relay = CellProgram(
            inputs=[Port("e_in", register=0)],
            outputs=[Port("pout")],
            entries=[EntryPoint("relay")],
            state=[StateVar("iavg", reset_per_batch=True), StateVar("es")],
            data=[DataWord("one_q14", ONEQ, address=1),
                  DataWord("mulq_integ", self._MULQ_INTEG, address=2),
                  DataWord("mulq_prop", self._MULQ_PROP, address=3),
                  DataWord("pdev", MAXDEV, address=4),
                  DataWord("ndev", (-MAXDEV) & 0xFFFF, address=5),
                  DataWord("rbias", self._INTEG_RBIAS, address=6)],
            assembly_template="""\
relay:
    MOVE R{state:es}, R{in:e_in}
    MOVE R0, R{state:es}
    ADD R0, R{data:rbias}
    MULQ R0, R{data:mulq_integ}
    ADD R0, R{state:iavg}
    MOVE R{state:iavg}, R0
    CMP R{state:iavg}, R{data:pdev}
    BR.N ihi
    MOVE R{state:iavg}, R{data:pdev}
ihi:
    CMP R{state:iavg}, R{data:ndev}
    BR.NN ilo
    MOVE R{state:iavg}, R{data:ndev}
ilo:
    MOVE R0, R{state:es}
    MULQ R0, R{data:mulq_prop}
    ADD R0, R{state:iavg}
    ADD R0, R{data:one_q14}
    {write:pout}
""",
        )

        # DICT ORDER = positional order. Each cell's default (unlisted) trigger goes
        # to its POSITIONAL-NEXT cell; the ordering below makes loop_filter's local
        # ``otrig`` reach qout (positional-next) and qdelay's ``rtrig`` reach the
        # resampler (positional-next). All other triggers/handoffs are declared
        # explicitly in internal_connections/internal_jumps.
        return {"qdelay": qdelay, "resampler": resampler, "ted": ted,
                "loop_filter": loop_filter, "qout": qout,
                "period_relay": period_relay}

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Forward data handoffs + the period FEEDBACK (relay -> resampler).

        The resampler tags each strobe (val + par); the TED branches center/mid on
        par and forms the wide Gardner error. The relay runs the PI filter and feeds
        the corrected instantaneous period back to the resampler's ``inst_next``
        state (the loop closure).
        """
        if self._complex:
            return [
                # qdelay (landing) bridges to the resampler each sample: the deferred
                # period (kept identical to its own NCO period) + the raw xi.
                ("qdelay", "instfwd", "resampler", "inst_next"),
                ("qdelay", "xifwd", "resampler", "xi"),
                # qdelay hands the interpolated Q sample (every strobe) to qout.
                ("qdelay", "yq_out", "qout", "yq"),
                # I chain (identical to the real block).
                ("resampler", "val", "ted", "val"),
                ("resampler", "par", "ted", "par"),
                ("ted", "e_out", "loop_filter", "e_in"),
                ("ted", "c_out", "loop_filter", "cval"),
                # loop_filter hands the recovered center yi to qout, and the Gardner
                # error to the period_relay; ``fb_trig`` is declared here too so its
                # JUMP resolves to the (non-positional-next) period_relay explicitly.
                ("loop_filter", "yi_out", "qout", "yi"),
                ("loop_filter", "e_fb", "period_relay", "e_in"),
                ("loop_filter", "fb_trig", "period_relay", "e_in"),
                # PI feedback returns to qdelay's deferred period (single target);
                # qdelay forwards it to the resampler (above), keeping both NCOs
                # locked to the identical period.
                ("period_relay", "pout", "qdelay", "qinst_next"),
            ]
        return [
            ("resampler", "val", "ted", "val"),
            ("resampler", "par", "ted", "par"),
            ("ted", "e_out", "loop_filter", "e_in"),
            ("ted", "c_out", "loop_filter", "cval"),
            # FEEDBACK (via the relay PI filter): the loop_filter hands the Gardner
            # error high-word ``ewhi`` to the `period_relay` (forward, WEST); the relay
            # runs the PI controller and writes the corrected instantaneous period as a
            # pure data WRITE into the resampler's ``inst_next`` state (backward,
            # NORTH). Closing the loop through a data WRITE (not a trigger) keeps the
            # feedback independent of the forward path (see its program).
            ("loop_filter", "e_fb", "period_relay", "e_in"),
            ("period_relay", "pout", "resampler", "inst_next"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """JUMP triggers. The resampler triggers the TED via its `val` emit (only
        on a strobe); the TED triggers the loop filter via `c_out` (only on a
        center). The loop_filter triggers the `period_relay` via `fb_trig` (the
        feedback path). The relay does NOT trigger the resampler — the period lands
        as pure data, read by the next external sample (the Costas dphase model).
        No-strobe / mid cases terminate locally (the `trig` outputs)."""
        if self._complex:
            return [
                # qdelay triggers the resampler EVERY sample (both NCO paths). The
                # resampler triggers ted on a strobe (val); ted triggers loop_filter
                # on a center (c_out); loop_filter triggers period_relay (fb_trig) and
                # qout (otrig, its positional-next). qdelay writes yq to qout as pure
                # DATA (no trigger) — qout fires only on the loop_filter center.
                ("qdelay", "rtrig", "resampler", "default"),
                ("resampler", "val", "ted", "default"),
                ("ted", "c_out", "loop_filter", "default"),
                ("loop_filter", "fb_trig", "period_relay", "relay"),
                ("loop_filter", "otrig", "qout", "default"),
                # Dead-end the no-advance triggers so they never fall through to the
                # positional-next cell (one recovered symbol per CENTER only).
                ("resampler", "trig", "__terminate__", "default"),
                ("ted", "trig", "__terminate__", "default"),
            ]
        return [
            ("resampler", "val", "ted", "default"),
            ("ted", "c_out", "loop_filter", "default"),
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            # The resampler's NO-STROBE `trig`, the TED's MID-strobe `trig`, and the
            # loop_filter's local `trig` must JUMP NOWHERE — only a CENTER strobe may
            # advance the chain and emit a recovered symbol. Without these, an
            # unmapped `trig` falls through to the positional-next cell, firing the
            # chain (and a downstream slicer) on every input => ~2x outputs (one per
            # input sample, not one per symbol).
            ("resampler", "trig", "__terminate__", "default"),
            ("ted", "trig", "__terminate__", "default"),
            ("loop_filter", "trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        if self._complex:
            # 6-cell complex layout — a COMPACT 3x3 fold of the old 5-wide
            # longitudinal strip (INV-8/9/14: a multi-cell block must fold; I/O near
            # one bus-facing edge; <=8 across). The forward chain snakes down at
            # ``ted`` (Costas serpentine idiom) so the whole loop lands in a 3-wide
            # footprint with a SHORT feedback return::
            #
            #   col:    0                 1                  2
            #   row 0: qdelay(E)          resampler(E)       ted(S)
            #   row 1: transit_fb_0(N)    period_relay(W)    loop_filter(S)
            #   row 2:                                       qout(S->out)
            #
            # FORWARD chain: qdelay(0,0,E) -> resampler(1,0,E) -> ted(2,0,S) ->
            # loop_filter(2,1,S) -> qout(2,2). Every forward internal handoff is
            # @1-abutted along this connected fwd_face path. qdelay ALSO writes the
            # interpolated ``yq`` — it rides the SAME forward fwd_face path (its
            # in-line resampler/ted/loop_filter forward transit traffic, the universal
            # routing-cell rule) to land in qout. loop_filter is DUAL-FACE (like the
            # Costas qpd): its chain-forward face is SOUTH, so yi_out egresses SOUTH
            # (@1 -> qout at (2,2), its chain-next) and e_fb egresses WEST (@1 ->
            # period_relay at (1,1)) — the two rails are PERPENDICULAR so they never
            # collide (``_CFACE_OUT``=south / ``_CFACE_FB``=west in
            # ``_build_complex_cell_programs``). qout sits on the bottom edge (a
            # bus-facing edge) as the single external OUTPUT. FEEDBACK:
            # period_relay(1,1) --WEST--> transit_fb_0(0,1) --NORTH--> qdelay(0,0),
            # traced backward by ``_apply_internal_feedback`` (@2). DICT ORDER ==
            # build_cell_programs order (positional mapping).
            return {
                "qdelay": (0, 0, "east"),
                "resampler": (1, 0, "east"),
                "ted": (2, 0, "south"),
                "loop_filter": (2, 1, "south"),   # dual-face: yi_out SOUTH, e_fb WEST
                "qout": (2, 2, "south"),          # output cell on the bottom edge
                "period_relay": (1, 1, "west"),   # feedback WEST -> transit -> qdelay
                # FACE-only transit return corridor (period_relay -> qdelay).
                "transit_fb_0": (0, 1, "north"),
            }
        """Compact 2x2 fold (maintainer-approved): resampler(0,0)->ted(1,0) on row 0
        facing east; the loop_filter folds down to (1,1). It is a DUAL-FACE cell
        (see ``build_cell_programs``): it emits `out` SOUTH (outward, the
        recovered center to a downstream slicer/bus) and `period_fb`/`fb_trig` WEST
        (the feedback) via in-program FACE flips. Its LAYOUT face is WEST — the
        feedback direction — so the build's feedback tracer
        (``_apply_internal_feedback``, which follows the cell's fwd_face) finds the
        relay. The `out` egress face is set at runtime by the in-program flip,
        independent of the layout face.

        Feedback return (the deadlock-free ring): loop_filter(1,1) --WEST-->
        period_relay(0,1) --NORTH--> resampler(0,0). The ``period_relay`` is a
        PROGRAMMED consumer (NOT a face-only transit): it acks the loop_filter on
        capture so the ring has slack, then writes `period` NORTH into the resampler
        as pure data (no trigger). It RESTS facing NORTH so the feedback tracer
        follows its fwd_face to the resampler. The loop_filter rests facing WEST
        (the final MOVE [FACE]), so its runtime resting face matches its layout face
        for the tracer."""
        return {
            "resampler": (0, 0, "east"),     # val/par EAST -> ted
            "ted": (1, 0, "south"),          # e_out/c_out SOUTH -> loop_filter(1,1)
            "loop_filter": (1, 1, "west"),   # dual-face: out SOUTH, period_fb WEST
            # Programmed relay: receives period_fb (EAST, from loop_filter) and
            # forwards `period` NORTH into the resampler (the deadlock fix).
            "period_relay": (0, 1, "north"),
        }

    def output_cell_id(self) -> Any:
        if self._complex:
            # COMPLEX: the external output (yi/yq pair) leaves the ``qout`` cell.
            return "qout"
        """The recovered center `out` leaves the loop_filter (the last datapath
        cell), which ALSO carries the `period_fb` feedback WRITE. Declaring it
        explicitly tells the build to patch ONLY the loop_filter's LAST output
        WRITE/JUMP for the brokered output route (``output_at_last_write``),
        leaving the feedback WRITE + the relay's feedback path intact, regardless
        of how many cells the block has or their dict ordering."""
        return "loop_filter"

    def output_face_addr(self) -> Any:
        if self._complex:
            # COMPLEX: qout is a plain single-face output cell (its yi/yq WRITEs all
            # egress on its resting fwd_face, set by the route) — no baked-in face
            # word to rewrite.
            return None
        """The loop_filter is a DUAL-FACE output cell: its ``out`` WRITE fires on the
        in-program ``MOVE [FACE], R{face_out}`` flip (addr 2), independent of the
        cell's resting ``fwd_face`` (which carries the WEST feedback). So the build
        must rewrite THIS face word to the drawn route's first-hop direction — else
        the ``out`` word fires on the baked-in/rotated ``face_out`` and, when the
        route leaves another way (a rotated/relocated block), shoots into empty cells
        and stray-executes (the phantom-route bug). ``face_out`` is DataWord addr 2."""
        return 2

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference Q15/Q14 Gardner loop modelling the on-chip cells EXACTLY
        (bit-exact with the chip; recovers timing BER=0 frac 0.3-0.7 over LONG
        continuous streams — the loop PLATEAUS at the nominal period, it does NOT
        collapse).

        This is the GR ``digital.symbol_sync`` control loop (see the class
        docstring): a 2nd-order PI whose gains ``alpha`` (proportional) and
        ``beta`` (integral) derive from GR's ``loop_bw=0.045`` + ``damping=1.0``,
        with the interpolator period clamped to nominal +/- ``max_dev`` (0.75
        half-sample = GR's 1.5 full-sample). The three fixed-point details that
        make it converge on 16-bit hardware:

          * ``inst`` (instantaneous HALF-period, Q14, 16384 = 1.0 sample) is fed
            back to the resampler and used for BOTH the mid and center strobe.
            There is NO mid-reset-to-nominal (the old design's collapse driver).
          * The Gardner error uses a WIDE (32-bit) product ``ewhi = MULHI(mid,
            dc_half)`` where ``dc_half = (s>>1) - (cprev>>1)`` — the ``>>1``
            keeps the BPSK sample DIFFERENCE inside int16 (a full-scale ``s -
            cprev`` OVERFLOWS int16 and corrupts the error, the true root cause of
            the drift). MULHI gives the signed high word, no decomposition bias.
          * The integrator ``iavg`` is kept at FULL width and the period is
            derived as ``avg = nominal + iavg`` (clamped). Quantising a per-symbol
            ``beta*e`` to Q15 before integrating rounds it to zero and lets a tiny
            DC bias wind the loop down — accumulating in a wide register avoids it.

        ``input_samples`` is a real (or complex; the real part is used) 2-sps
        stream. Returns the recovered symbol-center samples as Q15 int16.
        """
        def s16(v):
            return v - 0x10000 if v & 0x8000 else v

        def u16(v):
            return v & 0xFFFF

        def mqr(a, b):
            # The ISA MULQ TRUNCATES (arithmetic floor >>15), it does NOT round.
            # Matching this exactly is REQUIRED for chip<->reference bit-exactness: a
            # +1/2-LSB rounding bias here is amplified by the timing FEEDBACK loop into
            # a slow integrator drift (the coupled MF->Costas->Gardner chain slips).
            return (s16(a) * s16(b)) >> 15

        def mulhi(a, b):
            return (s16(a) * s16(b)) >> 16     # signed high word (ISA MULHI, floor)

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            sq = [float_to_q15(float(c.real)) for c in arr]
            sqq = [float_to_q15(float(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:  # (N,2) real [xi,xq]
            sq = [int(x) & 0xFFFF for x, _ in arr]
            sqq = [int(y) & 0xFFFF for _, y in arr]
        elif arr.dtype.kind == "f":
            sq = [float_to_q15(float(x)) for x in arr]
            sqq = [0] * len(sq)
        else:
            sq = [int(x) & 0xFFFF for x in arr]
            sqq = [0] * len(sq)

        ONE = 1 << 14           # nominal half-period (1.0 sample) in Q14
        out = []
        outq = []               # complex: the recovered Q center per symbol
        iavg = 0                # WIDE integrator (raw, not requantised)
        avg = ONE
        inst_active = ONE       # period used for the CURRENT strobe
        inst_next = ONE         # deferred period (lands on the NEXT strobe)
        cprev = 0
        midv = 0
        phase = ONE >> 1        # warm start: 0.5 half-period pre-accumulated
        xp = 0
        xp2 = 0
        xpq = 0                 # complex: parallel Q delay line (same shift as I)
        xp2q = 0
        parity = 0
        for idx, v in enumerate(sq):
            xi = s16(v)
            xp2 = xp
            xp = xi
            if self._complex:
                xp2q = xpq
                xpq = s16(sqq[idx])
            phase += ONE
            if phase >= inst_active:
                phase -= inst_active
                inst_active = inst_next        # apply the deferred feedback
                frac = u16(phase << 1)
                s = xp2 + mqr(frac, u16((xp - xp2) & 0xFFFF))
                # complex: interpolate Q with the IDENTICAL frac (same resampler math).
                sq_i = (xp2q + mqr(frac, u16((xpq - xp2q) & 0xFFFF))
                        if self._complex else 0)
                if parity == 0:                # CENTER
                    # dc_half = (s>>1) - (cprev>>1), via sign-correct MULQ halving.
                    dch = u16((mqr(u16(s & 0xFFFF), self._MULQ_HALF)
                               - mqr(u16(cprev & 0xFFFF), self._MULQ_HALF)) & 0xFFFF)
                    ewhi = mulhi(u16(midv & 0xFFFF), dch)
                    cprev = s
                    out.append(s16(u16(s)))
                    if self._complex:
                        outq.append(s16(u16(sq_i)))   # recovered Q center
                    # integral term ewhi>>8 via MULQ; proportional ewhi>>2 via MULQ.
                    # The ISA MULQ TRUNCATES (floor), so the integral term carries a
                    # systematic -0.5-LSB bias that a pure PI integrator accumulates
                    # into a slow drift (fine for a fixed offset, but the coupled
                    # MF->Costas->Gardner chain then slips). Pre-adding the half-LSB
                    # ``_INTEG_RBIAS`` before the MULQ makes it ROUND-to-nearest, which
                    # keeps the integrator DC-neutral (iavg bounded ~|10| over 2000+
                    # symbols). The proportional term is transient (not integrated) so
                    # its truncation is harmless and left as-is.
                    iavg += mqr(u16((ewhi + self._INTEG_RBIAS) & 0xFFFF),
                                self._MULQ_INTEG)
                    iavg = max(-self._MAXDEV, min(self._MAXDEV, iavg))
                    avg = ONE + iavg
                    # instantaneous period = avg + proportional term. The +-max_dev
                    # clamp on ``iavg`` (== GR's clamp on the integrated period) is the
                    # anti-windup; the small proportional term does not need its own
                    # clamp (verified BER 0 without it).
                    inst_next = avg + mqr(u16(ewhi & 0xFFFF), self._MULQ_PROP)
                else:                          # MID
                    midv = s                   # capture mid sample; no feedback
                parity ^= 1
        if self._complex:
            # (N_sym, 2) recovered (yi, yq) center pair per symbol.
            return np.array(list(zip(out, outq)), dtype=np.int16)
        return np.array(out, dtype=np.int16)

    def reset(self):
        """Stateless reference (process_reference is self-contained)."""
        pass
