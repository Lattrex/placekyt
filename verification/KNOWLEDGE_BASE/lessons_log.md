<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block as it is verified: what was tried,
what passed/failed, the derived tolerance, and any block-specific gotcha. Promote
anything that generalizes across block classes into `invariants.md`.

---

## QAM16 DD Costas: an INTERNAL-FEEDBACK block with a RECOVERED-OUTPUT TAP, wired end-to-end 2026-07-22

This is the recipe for making a **feedback (PLL/Costas-class) block emit its recovered
signal to a DOWNSTREAM on-chip block** — the hardest block class (a data feedback loop
+ a mid-block output tap). The QAM16 DD Costas went from "recovers I/Q internally but
never reaches a consumer (0 egress)" to "Costas→slicer routes+builds+recovers symbols"
by copying the order-4 QPSK `ComplexCostasLoop` structure EXACTLY. Three coupled fixes,
all of which have a subtle failure mode:

1. **`output_registers=[0,1]` on the block interface (NOT [0]).** The build's complex-
   egress patch (`engine/build.py` ~L964, ~L1413) gates `src_is_complex_out` on
   `len(spec.output_registers) > 1`. A feedback block that shipped `output_registers=[0]`
   (because its scalar interface entry is one reg) takes the SINGLE-rail
   `_patch_last_write_handoff` path → only the LAST tap WRITE (yq_tap) gets the route
   hop; yi_tap keeps its internal @30 hop and fires back into the loop → **0 egress**.
   Declaring `[0,1]` makes the build steer BOTH recovered rails to the route.
2. **A dedicated dual-face `tap` cell (rotate→tap→islice_pi), modeled on the order-4
   `qpd`.** It forwards yi/yq to the loop on `face_internal` AND taps yi_tap/yq_tap on
   `face_tap` (an `is_face` DataWord the bus router overrides to the route's first-hop
   exit — DISTINCT from the internal face so they never collide). Emit the tap pair as
   the program TAIL (after the internal forwards + their trigger) so the block→block
   coalesced tail-patch (`_patch_complex_packet_last_handoff`, fired when BOTH tap rails
   go to ONE downstream broker) steers both tap rails while the internal forwards keep
   @1. Wire the tap to a BLOCK (the slicer), NOT to a chip port: chip-port egress
   (`_patch_complex_output_port_handoff`) walks ALL WRITEs and over-patches the internal
   forwards too (loop breaks).
3. **A compact SERPENTINE fold (INV-8/9/14), NOT a flat strip.** The 10-cell block as a
   flat 10-wide row filled the top edge → "no free corridor" for the auto-router. A 5×2
   snake (phase..table_cos EAST on row 0, turn SOUTH, rotate/tap/islice_pi/qslice_err/pi
   WEST on row 1) puts the dphase feedback pi→phase @1 DIRECTLY above (no transit cell) —
   the exact order-4 4×2 pattern. `_apply_internal_feedback` traces this @1 corridor
   cleanly; a fold that puts `pi` mid-array (dphase WRITE and re-trigger JUMP resolving to
   DIFFERENT hops) does NOT close the loop.
4. **ANCHOR the block at (0,0) so the landing (`phase`) cell abuts x16_in.** With the
   fold, a non-(0,0) anchor shifted `phase` away from the input port and the injected
   sample never reached the landing cell (phase's accumulator stayed all-zero across
   every sample → nothing fires → 0 output). The fold is anchor-sensitive.

RESULT: Costas→slicer routes+builds and recovers 13/16 constellation symbols exactly on
a random stream (BER ~0.04). REMAINING for BER 0: (a) the slicer fires 2×/symbol — the
tap→slicer delivery isn't atomic (broker delivers yi_tap and yq_tap as 2 triggers; the
2nd/complete fire is correct, so batch_check phase-1 is the real stream). (b) The residual
~4% is DD Q15 precision on 16-QAM's tight decision regions (the recovered outer ±3 points
occasionally cross a threshold; amp-sensitive — 0.85 input scale gives fewer errors). A
CONSTANT-symbol settle test is DEGENERATE for a DD loop (err=Im{y·conj(slice y)}=0 on-grid
→ no phase info → drifts); always characterize with a RANDOM stream.

## QAM16 slicer REBUILT to GR decision_maker + Costas tap/acquisition BLOCKERS 2026-07-22

Rebuilt `QAM16SlicerBlock` as a 1:1 `digital.constellation_decoder_cb(constellation_16qam())`
drop-in. GR's map is non-separable, BUT the nearest-point decision FACTORS exactly into two
per-axis binary tests + a 16-entry permutation LUT (verified == `decision_maker` over the
whole plane, `qam16_sign_outer_lut`): `sign=(v>=0)`, `outer=(|v|>=2/√10)`,
`key=(Is<<3)|(Io<<2)|(Qs<<1)|Qo`, `symbol=LUT[key]`, LUT=`[1,6,10,13,4,3,15,8,0,7,11,12,5,2,14,9]`.
3-cell islice/qslice/lut (2 per-axis tests + |v| don't fit one 32-word cell). `lut`'s key input
is pinned R17 (above the 16-entry table at addr 1..16) — the same aliasing trap the mapper hit.
`test_qam16_slicer.py`: exact points + noisy grid + mapper→slicer loopback identity + INV-4
mutations — all green.

**COSTAS BLOCKERS (modem NOT yet BER-0 end-to-end):**
1. **No recovered-output tap.** The DD `QAM16ComplexCostasLoopBlock` recovers (yi,yq) at its
   `rotate`/`qslice_err` cell but `output_cell_id` returned None → it emits only the pi/dphase
   feedback; the recovered pair never reaches a downstream slicer. It was only ever tested via
   `process_reference`, never wired block→block. Adding a dual-face tap (like the order-4 QPSK
   Costas `qpd` yi_tap/yq_tap) overflows `rotate`/`qslice_err` (both at register ceiling with the
   derotate / PAM slice). A dedicated `tap` relay cell (rotate→tap→islice_pi, cell_count 9→10)
   BUILDS+ROUTES, but its mid-block `tap_trig` is NOT patched to fire the downstream → 0 output.
   This is the SAME multi-day trig-resolution problem this log documents for the order-4 QPSK
   Costas (`_patch_last_jump_handoff` / the tap-trig retarget). Reverted to the clean committed
   block; the tap is unfinished.
   **PRECISE DIAGNOSIS (resume point):** with the tap cell in place it BUILDS+ROUTES; disasm of
   the built tap cell shows the tap emits 4 WRITEs — yi_fwd/yq_fwd (@30, internal to islice_pi)
   then yi_tap/yq_tap. Only the LAST tap write (yq_tap) + tap_trig get patched to the route hop
   (@25); **yi_tap keeps @30** (fires toward islice_pi, never egresses) → 0 output. Root cause is
   in `engine/build.py` ~L989: a source whose output cell "carries handoffs"
   (`_output_cell_carries_handoffs` True — my tap IS a source of an internal_connection) takes the
   `_patch_last_write_handoff` path (patches ONLY the last WRITE) instead of the dual-rail
   `_patch_complex_output_port_handoff`; the latter only fires for a CHIP-PORT egress
   (`not isinstance(tgt, BlockEndpoint)`), not block→block. The order-4 QPSK `qpd` works block→block
   because it emits only ONE internal write (`err`) + the yi_tap/yq_tap pair, so the co-rail
   patch lands right. FIX DIRECTIONS: (a) restructure so the tap cell has ≤1 internal write (make
   `qslice_err` itself tap, like qpd — but it's at the register ceiling; aggressive reclaim
   needed: read sinv/cosv from input regs like the rotate reclaim freed 2 state), OR (b) extend
   the block→block complex-egress patch in build.py to co-rail both tap writes when the source
   declares 2 output rails (broad blast radius — touches every complex block). Also the 10-cell
   Costas is a full-width row-0 strip (INV-9 violation) → the auto-router congests ("no bus path")
   with the 3-cell slicer; the modem will need a hand-placed .kyt (like FSK4) AND the Costas
   refolded ≤8 across.
   **UPDATE 2 (same session — the tap MECHANISM is now solved; the fold + acquisition remain):**
   * **The tap needs `output_registers=[0,1]` on the block interface.** The build's complex-egress
     patch keys `src_is_complex_out` on `len(spec.output_registers) > 1`. The Costas shipped
     `output_registers=[0]` → the tap took the single-rail `_patch_last_write_handoff` (only the
     LAST tap write got the route hop; yi_tap kept its @30 internal hop → 0 egress). Setting
     `[0,1]` makes both rails get patched.
   * **CHIP-PORT egress OVER-patches** a 4-write tap cell: `_patch_complex_output_port_handoff`
     walks ALL WRITEs, so the tap's 2 internal forwards (yi_fwd/yq_fwd) ALSO get the port hop
     (loop breaks). The BLOCK→BLOCK coalesced path (`_patch_complex_packet_last_handoff`, both
     rails to ONE downstream broker) patches only the TAIL writes → correct. So wire the tap to
     the SLICER (one broker), not to x16_out directly.
   * **A dedicated `tap` cell (rotate→tap→islice_pi) + `output_registers=[0,1]` + a SERPENTINE
     fold ≤6 across ROUTED AND BUILT the Costas→slicer chain** (the flat 10-wide strip → "no free
     corridor"). CONFIRMED: route ok + build ok.
   * **REMAINING BREAK: the dphase feedback corridor in the custom fold.** Serpentine (phase(0,1),
     pi(2,2)) → pi's dphase WRITE (@26) and re-trigger JUMP (@28) resolved to DIFFERENT hops →
     loop never closes → 0 recovered symbols. The proven feedback is pi at the row END facing
     SOUTH onto a STRAIGHT west row-1 return to phase; a serpentine with pi mid-array breaks
     `_apply_internal_feedback`'s corridor trace. NEXT: keep the straight-return feedback but
     narrow the forward chain, OR hand-place+hand-route the whole modem in a `.kyt` (bypasses the
     auto-router corridor search AND lets you draw the feedback lane explicitly — FSK4/SSB pattern).
2. **DD acquisition is marginal for 16-QAM.** Standalone at 1 sps (feed constellation points
   directly, no MF) it LOCKS BER0 over foff∈[0.001,0.003] (`alpha_q15=0x0400,beta_q15=0x0020`)
   or [0.002,0.005] (`0x1000/0x0040`); the shipped defaults `0x0800/0x0040` are marginal
   (BER~0.015). But the MF-DECIMATED 2 sps path (the QPSK-modem topology) does NOT acquire — BER
   cliffs from 0 (foff=0) to 0.75 (any offset). The DD algorithm locks PERFECTLY in float (150/150),
   so it's a Q15 + 2-sps-phase-doubling + residual-ISI interaction, unsolved. Path forward for a
   BER-0 modem: (a) finish the tap-trig patch, (b) run at the 1-sps symbol-synchronous operating
   point (the KB's QPSK-modem workaround), (c) ship a hand-placed dense .kyt (auto-route congests
   with the 10-cell Costas + 3-cell slicer). The mapper+slicer are DONE + GR-verified + committed.

## QAM16 mapper REBUILT to GR constellation_16qam() + the table/register aliasing bug 2026-07-22

The legacy `QAM16SymbolMapperBlock` used an INVENTED separable-Gray map
`(I_bits<<2)|Q_bits` (per-axis Gray `{-3:00,-1:01,+1:11,+3:10}`) that matches GR
`digital.constellation_16qam()` on **0 of 16** symbols. GR's map is a {±1,±3}/√10 grid
but its bit→point assignment is an idiosyncratic PERMUTATION (NOT separable). Rebuilt the
mapper to store GR's EXACT `points()` table; `test_qam16_mapper.py` re-derives `points()`
from GNU Radio and pins the baked Q15 table against it (a GR bump can't silently drift).

- **16 pts > the single-cell I+Q LOAD-indirect budget (PSKSymbolMapper's MAX=14):** a
  16-entry I table (16 words) + 16-entry Q table (16 words) can't co-fit one 32-word cell
  with a program. Split into 3 cells: `acc` (4-bit MSB-first accumulator) → `itab` (I
  table, emits `i_fwd` + forwards `idx+1`) → `qtab` (Q table, emits the (I,Q) pair down
  the shared complex-egress corridor). Explicit `internal_connections`/`internal_jumps`/
  `output_cell_id="qtab"` + a 3-cell linear `default_layout` (the default positional
  auto-wire can't resolve a mid-chain cell that emits both a port and an internal handoff).
- **THE BUG (cost the session's biggest chunk): a TABLE entry aliased an INPUT register.**
  Memory IS the register file — `mem[n] == Rn`. The Q table sits at addresses 1..16, so
  it occupies R1..R16. `qtab`'s `addr` input was pinned to **R1**, which is **mem[1] = the
  q[0] table entry**. Delivering the address (1, for symbol 0) OVERWROTE q[0], so symbol 0
  read its Q back as the delivered address (Q=0x0001) instead of −0.316. EVERY other
  symbol was exact — only idx=0 hit the clobbered slot, which is why it looked like a
  "symbol-0 warm-up". FIX: pin `qtab`'s inputs ABOVE the table (R17/R18). LESSON: when a
  cell has a LOAD-indirect table at addr 1..M, its INPUT and STATE registers must live at
  addr 0 or >M — an input/state landing in 1..M silently corrupts the table (and only the
  colliding index shows it). This is the LOAD-table analog of the MF partial→cs aliasing.
  Trace it by reading the built cell's Rn live (`read_cell_memory` per symbol): R18=addr=1
  but R0 stayed 1 after LOAD → LOAD read a clobbered `mem[1]`.
- **`LOAD Rn` is a SINGLE table deref, not double.** The ISA doc writes `R0 = mem[mem[Rn]
  & 0x1F]`, but since R0..R31 ARE mem[0..31], `mem[Rn]` = the register's VALUE, so it is
  one lookup into the table: `LOAD Rn` with Rn holding `1+idx` gives `mem[1+idx]` = the
  table entry. (Confirmed against the working FSK4 mapper's level-table LOAD.)
- **Complex-egress (I,Q) pair:** the mapper emits I then Q down one corridor (out_i/out_q
  from `qtab`), de-interleaved by out_tag — the same contract as the QPSK mapper feeding
  the ComplexUpsampler. The DUT drains all words/trigger (fsk4_dut `_run_single_block_stream`)
  and de-interleaves I0,Q0,I1,Q1,...; verify by symbol-value (a lag search absorbs any
  cold-start), like the QPSK/FSK4 modem BER checks.

## M17 4FSK timing recovery: sync-word CORRELATION, not Gardner (algorithm chosen) 2026-07-21

Follow-up to the Gardner-can't-lock-4PAM finding below. Researched + numerically validated
the RIGHT timing-recovery algorithm for 4-level FSK before building.

- **What real M17 does:** the reference demod (mobilinkd/m17-cxx-demod) recovers timing by
  SYNC-WORD CORRELATION (a sliding cross-correlation against the known ±3 sync symbols;
  `find_peak()` → the within-symbol sample offset IS the sampling instant) + a light
  feedforward Kalman predictor between frames. NOT a Gardner. DMR/P25/C4FM decoders (OP25
  C4FM, DSD, dsdcc) similarly avoid raw Gardner on the 4-level path (custom DPLL / symbol-
  rate-tone PLL / crossing trackers + sync correlation); Gardner only appears on their
  CQPSK/PSK paths.
- **Numerically, on OUR FM-discriminator 4-PAM signal (2 sps):** decision-directed feedback
  trackers (Mueller-Müller, DD/normalized Gardner) are UNSTABLE — worst-case BER 0.2–0.5
  across seeds; decision errors derail the loop (a documented 4-PAM failure mode). Oerder-
  Meyr feedforward wants ≥4 sps + an atan (poor ISA fit). **Data-aided correlation against
  an M17-style ASYMMETRIC sync word wins: BER 0 across 60 seeds**, pure MAC + compare (no
  atan/divide/feedback) — the best Kyttar-ISA fit and what M17 itself uses.
- **The validated algorithm (proto_fsk4_sync_model.py, BER 0 / 60 seeds):** frame = a short
  alternating +3/-3 preamble + the M17 LSF sync word {+3,+3,+3,+3,-3,-3,+3,-3}. The RX slides
  the (time-REVERSED) sync's ±1 template over the on-phase samples; each sample is pre-scaled
  by 1/SYNC_LEN via a SIGN-CORRECT Q15 MULQ (CORR_SCALE_Q15=4096; a raw logical SHR mangles
  negatives, [[invariants]] INV-13) so the 8-tap sum fits int16; lock on the FIRST local max
  of C above a fixed threshold (~14745, a fraction of the ideal ≈full-scale peak); then
  decimate 2:1 from peak+2. The ALTERNATING preamble alone is ambiguous (half-symbol/polarity
  self-similar) → the ASYMMETRIC sync word gives the unique peak. Account for the RRC MF
  GROUP DELAY (search the full early range, not a narrow window). The RX must be scaled so the
  recovered OUTER level ≈ ±1.0 (MF gain ≥1.3) for the fixed 2/3 slicer + fixed correlation
  threshold (validated at gain 1.5).
- **STATUS: FSK4SyncTimingRecoveryBlock authored + BUILDS (10 cells: d0..d7 systolic ±1
  correlator @ 2 samples/cell → lock → emit; fits the 32-word budget after splitting the
  16-sample line 2/cell and the gate into lock+emit). The block's `process_reference` and the
  self-contained model both recover BER 0. All 10 cells EXECUTE on-chip (verified via
  `enable_trace`/`get_trace`: the d0→…→d7→lock→emit wavefront fires every sample). OPEN: the
  on-chip datapath VALUE is wrong (correlation C saturates / recovered symbols garbage) —
  an arithmetic bug in the correlation scaling or the 2-samples-per-cell delay-line indexing
  (cell c must present reg[2c]=x[n-2c] with the leaving sample = old rb saved before the
  shift). NOT yet registered in manifest/GRC/orientation (do that only once chip output ==
  process_reference). NEXT: instrument the per-cell tap registers (the disassembler's linear
  per-cell dump is unreliable — data/code interleave by address; use register reads or a
  reduced 2-cell chain with known impulse) to pin the delay index, then verify C matches the
  reference stream before the lock/emit.**

---

## M17 4FSK: FSK4SymbolMapper + FSK4Slicer blocks; Gardner does NOT lock 4-PAM timing 2026-07-21

Two NEW tier-3 blocks for an **M17 4-level FSK (C4FM)** modem, both DONE (verified,
GRC-bound, orientation-invariant 100%), plus a HARD, honest finding on the RX timing loop.

- **FSK4SymbolMapperBlock (1 cell).** Bit stream (0/1) → one signed PAM deviation LEVEL per
  DIBIT. M17 Gray map PINNED **LSB-first** (RULE #0): dibit `(b0,b1)`, `d = b0 + 2·b1`,
  `(1,0)→+3, (0,0)→+1, (0,1)→−1, (1,1)→−3`; levels normalised so `+3 → +1.0` (Q15 full
  scale) → table by `d` = `[+1/3, +1, −1/3, −1]`. Feed a FrequencyModulator with
  `sensitivity = 2π·2400/fs` (fs = sps·4800; sps=2 → 9600) for the M17 ±2400/±800 Hz
  deviations (a full-scale +1.0 level advances π/2 rad/sample = 2400 Hz). The M17 spec
  tables the map MSB-first — I TRANSPOSED it to the prompt's LSB-first convention (stated
  loudly). Bit-accumulator + 4-entry LOAD-indirect table. Gate: LEVEL TABLE + LSB-first
  order pinned bit-for-bit vs `digital.chunks_to_symbols_bf` over dibit indices; bit-exact
  chip==reference; mutations (sign-flip / inner-outer swap / one-symbol shift) FAIL.
- **FSK4SlicerBlock (1 cell).** Discriminator LEVEL → 2-bit dibit (b0 LSB first, then b1).
  The dibit's two bits ARE the two decision flags — **no lookup table**: `b0 = (|y| ≥ 2/3)`
  (magnitude), `b1 = (y < 0)` (sign). This falls straight out of the inverse Gray map and
  is what let it fit one cell (an early table+LOAD version OVERFLOWED the 32-word cell:
  28 instrs + 8 data words). Gate: the STRONGEST test is the **mapper→slicer LOOPBACK is
  bit-for-bit the identity** on random bits (pins the shared LSB-first convention);
  mutations (flipped sign / dropped magnitude / one-bit shift) FAIL.
- **Both are ORIENTATION-INVARIANT 100%** (added to `test_orientation_invariance.py`,
  0 xfail): single real rail; the mapper emits None-gaps (1 level / 2 bits), the slicer
  drains 1 word/trigger in the harness — both produce the IDENTICAL word list in all 8 D4.
- **Single-block on-chip DUT for a VARIABLE-rate block:** neither `run_block_dut` (1
  word/trigger) fits — the mapper emits 1 level per 2 bits, the slicer 2 bits per level.
  Use a drain-ALL-words-per-trigger runner (`verification/tests/fsk4_dut.py`, modelled on
  `test_psk_symbol_mapper._run_index_dut`).

- **HARD, HONEST FINDING — GardnerTimingRecovery(complex=False) does NOT recover 4-level
  PAM timing to BER 0.** The full RX (QuadratureDemod → RRC matched filter → Gardner →
  FSK4Slicer) was built and driven end-to-end. The DSP is CORRECT: a host reference and
  an ideal FIXED-PHASE decimation of the very same chain recover **BER 0** (the eye is
  open; TX/discriminator/MF are right). But Gardner — in its OWN bit-exact
  `process_reference` AND on-chip — plateaus at **BER ~0.21–0.31**. The Gardner TED
  S-curve for this FM-discriminator 4-PAM signal is actually correct (zero at τ=0, right
  slope), and the loop PERIOD holds at nominal (16384 Q14), yet the recovered symbol
  values are enormously JITTERED (per-symbol std 0.3–0.46 on a ±1 grid) — the loop sits in
  a high-timing-jitter regime it can't leave. Swept exhaustively and it does NOT reach
  BER 0: decision-directed TED (slice the center samples before differencing) → ~0.10–0.21;
  lower loop bandwidth (integral shift 8→14, proportional 2→6) → ~0.21; initial-phase
  seeding → ~0.07. None close. The 4-PAM eye is NARROWER than BPSK's (inner levels ±1/3),
  so the same absolute timing jitter that BPSK tolerates smears 4-PAM across the decision
  thresholds. Gardner's fixed loop_bw=0.045 / damping=1.0 (kp/ki are IGNORED per its
  docstring) is tuned for the 2-level (BPSK/QPSK-per-axis) case that the shipped modems
  use, where it is BER 0. **A genuinely BER-0 4-PAM receiver needs a different timing
  recovery** (feedforward Oerder–Meyr, or full Mueller–Müller with a proper multilevel
  S-curve) — a new block, NOT a retune of Gardner; retuning was proven insufficient.
  CAUTION (my own error, logged so the next agent doesn't repeat it): several intermediate
  "BER 0" readings during this investigation were MEASUREMENT BUGS in ad-hoc slicing/lag
  code — always score against the block's own `process_reference` with a correct
  guard+lag, and treat a recovered-symbol scatter (mean/std per true symbol) as the honest
  lock metric, not a single BER number from a hand-rolled comparator.

---

## FULL-DUPLEX shared-port modem + orientation 100%: QPSK modem works on the hand-built .kyt 2026-07-20

- **Shared input-port fan-out (→ [[invariants]] INV-24).** The full-duplex QPSK modem
  shares ONE `x16_in` between a TX chain (mapper→upsampler→RRC→upconvert) and an RX chain
  (MF→Costas→Gardner→slicer), multiplexed by `stream_id`. On the user's HAND-BUILT
  `examples/qpsk_modem/qpsk_modem.orig.kyt` one stream recovered and the other emitted
  ZERO — because the two input nets diverged AT the port cell (which has one `fwd_face`).
  Fixed with `_apply_port_diverts` (build.py): promote the port cell to a broker that
  lands the diverting stream AT the port (HOP_CNT==31), relays it toward its block through
  the DOWNSTREAM broker (two chained `@1` brokers span a non-adjacent target — the corner
  case the original single-hop broker never handled), and RESTORES the port face for the
  transiting stream. RX recovers **BER 0/132** on the orig file; TX passband correct.
- **PROVE a modem the way it's USED, not with a synthetic proxy.** The recurring failure
  this session was "fixed" claims verified on toy 1-port→2-block projects or auto-routed
  placements — which do NOT reproduce the user's topology. The real oracle: load the
  hand-built `.kyt` → `BuildEngine.build` (NO auto-route; the router can't place this) →
  `stream_targets(...)` → host on `SimServer` → drive both `stream_id`s over a socket
  (`process_batch`). See `verification/kyttar/tests/proto_orig_e2e.py` /
  `proto_orig_rx_ber.py`. The user's `.kyt` is PROTECTED — never modify/delete/auto-route
  it; restore from `qpsk_modem.orig.kyt` if a build leaves the working copy dirty.
- **Verification-harness CARRIER convention (cost hours, twice).** A coherent RX chain
  (MF→Costas→Gardner→slicer) is BASEBAND — no downconverter in front. Drive it with a
  SMALL residual carrier offset the Costas can pull in (`foff=0.008`), NOT the TX
  upconvert frequency (`4000/32000 = 0.125`, ~16× the pull-in range). The 0.125 carrier
  yields BER ~0.68 on ANY correct RX and masqueraded as a delivery bug. Match the
  KNOWN-GOOD test's convention (`test_qpsk_modem_ber.py`: `foff=0.008, toff=0.45`).
- **TX RRC passband at sps=2 LOOKS rough — that's correct, not a filter bug.** Only 2
  samples/symbol + an 8-sample carrier period → the eye can't see the pulse shape. Verify
  numerically: the chip TX output's near-zero-sample fraction matches the RRC reference
  (0.188), NOT the unfiltered zero-stuff (0.5), and correlates 0.82 once the QPSK
  constellation convention (a rotation + conjugation) is aligned.
- **Orientation invariance is now 100% (0 xfailed) — the "flake" was 4 real bugs.** The
  `ComplexMixer cw` "flaky test" was three deterministic anti-orientation handoff bugs
  (named-cell internal-face restore no-op; port complex fan-in double-relay; router
  weaving egress through the block body) + one DUT-harness manhattan-hop bug (the "NCO
  residual"). All catalogued in [[invariants]] INV-23. LESSON: a "flaky" orientation test
  is almost always a REAL deterministic bug — reproduce it IN-PROCESS outside pytest
  (`proto_mixer_orient.py` / `proto_nco_orient.py` build every orientation in one script);
  it will be stable there and the pytest "flakiness" is just which orientations ran
  before it. Do NOT xfail to hide it.

---

## MODEL: internal feedback/"transit" cells are now FIRST-CLASS block cells 2026-07-20

- **Change (the user's requirement):** block-INTERNAL routing/feedback cells
  (`default_layout` entries with a `transit_*` id, e.g. the Costas `transit_fb_0`
  corner) used to be second-class — a separate `Placement.transit_cells` list,
  rendered light-blue (`CellKind.TRANSIT`), and EXCLUDED from the block's
  footprint. They are now first-class `PlacedCell`s carried in `Placement.cells`
  (still tagged by the `transit_*` id): they render with the OWNING block's
  colour/label, COUNT in the block's `bounding_box()` / PortMap footprint
  (`io_colocated` / auto-place area), transform rigidly with the block, and follow
  the same DRC/overlap rules. `Placement.transit_cells` is now a read-only
  FILTERING VIEW of `cells`, so the ~19 router/DRC read-sites kept working unchanged.
- **Key mechanic:** kept the `transit_*` id-PREFIX (every build/router/DRC consumer
  that keyed on it — exit-cell selection, universal routing-program stamping,
  feedback materialisation, the bus-router restore-face map — keeps working) but
  MERGED the representation. `is_transit_cell(cell)` in `model/placement.py` is the
  single source of truth for the tag.
- **Gotchas fixed while merging (all double-count/duplicate bugs from cells that
  now appear in BOTH `cells` and the `transit_cells` view):**
  - `Placement` had to stop being a `@dataclass`: a `transit_cells` InitVar and a
    `transit_cells` @property collide (the dataclass takes the property object as
    the InitVar default → `'property' object is not iterable`). Hand-wrote
    `__init__`/`__eq__`/`__repr__` with `__slots__` so `transit_cells` can be BOTH a
    back-compat constructor kwarg (merged into `cells`) AND the filtering property.
  - `drc.py` counted transit cells twice (once via `cells`, once via the view) →
    a FALSE "overlap" ( `complexcostasloop[transit_fb_0]` vs `[transit]` at the same
    pos). Removed the separate transit pass; fixed the utilization double-count too.
  - `MoveBlockCommand._shift` / `MoveBlockToChipCommand` shifted transit cells a
    second time (they're in `cells` now). Dropped the extra transit handling.
  - `_place_transit` / `_unplace_transit` (project.py) now add/remove a first-class
    `PlacedCell` with a synthesised `transit_N` id instead of appending to the
    (now read-only) list.
- **Persistence:** new `.kyt` files serialise internal cells INLINE under `cells:`
  (they have a `cell_id`); the legacy separate `transit_cells:` block still LOADS
  (each positionless entry → a synthesised `transit_N` `PlacedCell` merged into
  `cells`). Round-trip is stable.
- **Byte-identical proof:** the built bitstream for the identity (un-rotated) case
  is UNCHANGED — ComplexCostasLoop `sha256=38267a9f…` (434 words) and Gardner
  `sha256=59a7a6ba…` (222 words) before AND after. Full regression 100% green
  (1190 passed). Internal cells are the SAME cells, just represented as first-class.
- **Note:** the light-blue `CellKind.TRANSIT` look is RETAINED for INTER-block
  connection route waypoints (the bus spine between blocks) — the user objected only
  to light-blue cells *inside* a block, which no longer happens.

---

## IMPORTER BUG: complex Q-rail split silently dropped for tapped port names 2026-07-19

- **Symptom (found via a hand-routed QPSK modem .kyt):** an imported complex RX chain
  had a NONSENSICAL leftover flyline — the QPSK slicer's `in_i` looked satisfied by a
  routed net, yet the router still demanded another route into it; and the slicer's
  `in_q` was UNCONNECTED. Root: the GRC importer's complex I/Q BLOCK->BLOCK edge split
  (`grc_import.py` `_iq_sibling`) SILENTLY failed to synthesise the Q-half net for the
  PARAM-DEPENDENT tapped output ports — the order-4 Costas `yi_tap`/`yq_tap` and the
  complex Gardner `yi_e`/`yq_e`. So the Costas->Gardner and Gardner->slicer complex
  links imported with the I rail ONLY; the Q rail was never wired -> the derotation /
  slice ran against a stale/zero Q and the design could not work.
- **Root cause:** `_iq_sibling` formed the Q sibling from a TRAILING ``i`` only
  (`port[:-1]+"q"`: `xi`->`xq`, `yi`->`yq`, `in_i`->`in_q`). The tapped forms carry the
  ``i`` marker in the MIDDLE (`y`**`i`**`_tap`, `y`**`i`**`_e`), so the trailing rule
  produced `yi_taq`/`yi_q` (non-existent) and returned None -> no Q net. This surfaced
  only after the QPSK RX blocks (order-4 Costas tap, complex Gardner) started being
  IMPORTED as a duplex .grc; the per-block explicit-anchor demos hand-wire both rails so
  never hit it.
- **Fix:** `_iq_sibling` now tries BOTH conventions and takes whichever names a REAL Q
  port on the same cell: the trailing-``i`` swap (`in_i`->`in_q`, `xi`->`xq`, `yi`->`yq`)
  AND the position-1 marker swap after an ``x``/``y`` prefix (`yi_tap`->`yq_tap`,
  `yi_e`->`yq_e`). Verified: the full-duplex qpsk_modem.grc now imports all 16 nets (was
  14 — the 2 missing were the Costas-tap Q and Gardner-out Q rails); BPSK modem import +
  duplex e2e stay green (17 import tests). A real scalar `out` port correctly stays
  unsplit (returns None).
- **LESSON:** when a complex block exposes a DECORATED I/Q output name (a tap, a
  suffixed rail) rather than the bare `yi`/`yq`, the importer's pair-synthesis must
  match the ``i``/``q`` MARKER wherever it sits, not assume it's the last char. A
  silently-dropped Q rail looks like a routing/DRC mystery (a flyline that "shouldn't
  be there"), not an import bug — check the NET LIST (is the yq_* / *_q sibling present?)
  before blaming the router.

## complex Gardner RE-FOLDED to a compact 3x3 (was a 5-wide strip) — INV-8 DONE 2026-07-19

- **DONE:** the `complex=True` GardnerTimingRecovery `default_layout` is now a compact
  3-wide x 3-tall fold (was a 5-wide x 2-tall longitudinal strip). Routes with
  auto_orient=False, BIT-EXACT to `process_reference(complex=True)` (0/0 I/Q
  mismatches). Gates green: test_gardner_complex_reference (bit-exact + QPSK BER0),
  test_pipeline_saturation, test_qpsk_modem_ber (folded RX chain). The REAL (BPSK)
  Gardner path is byte-identical (only the `if self._complex:` layout branch + the
  complex loop_filter's two face constants changed).
- **The fold + the 4 constraints that make it hard (all had to hold at once):**
  layout `phase/ted turns SOUTH` (Costas-serpentine idiom): qdelay(0,0,E)
  resampler(1,0,E) ted(2,0,S) / period_relay(1,1,W) loop_filter(2,1,S) /
  qout(2,2,S); transit_fb_0(0,1,N).
  1. FORWARD chain qdelay->resampler->ted->loop_filter->qout stays face-abutted @1
     along a single connected fwd_face path (ted turns SOUTH to drop to row 1).
  2. qdelay writes `yq` to qout — it rides that SAME forward fwd_face path (the
     in-line cells forward transit traffic). Break the path and qout gets NO Q rail
     (symptom in the harness: "Q=0" / "too few outputs").
  3. loop_filter is DUAL-face: `_CFACE_OUT` flipped 1->0 (yi_out now SOUTH -> qout at
     (2,2), the chain-next) and `_CFACE_FB` flipped 0->2 (e_fb WEST -> period_relay at
     (1,1)). The two rails are PERPENDICULAR (SOUTH vs WEST) so they never collide —
     the same dual-face discipline the order-4 Costas qpd needs.
  4. period_relay(1,1) --WEST--> transit_fb_0(0,1) --NORTH--> qdelay(0,0) is the
     feedback corridor (@2), traced backward by `_apply_internal_feedback` following
     loop_filter's/period_relay's resting fwd_face.
- **⚠️ A fold can help the block STANDALONE yet HURT the auto-placer for a dense
  design.** The folded Gardner routes fine standalone (auto_orient=False) and in the
  RX chain with EXPLICIT anchors, but it made the FULL-DUPLEX QPSK modem's
  `import->auto_pnr` packing LESS reliable (dropped from ~5/8 to 0/8 fully-routed
  trials) — the fold changed the tap-egress geometry the auto-placer keys on. The
  duplex acceptance path therefore uses EXPLICIT anchors (like the BPSK modem's own
  `bpsk_modem_demo.py`), NOT auto_pnr; auto_pnr/GUI-import is the best-effort GUI
  workflow. LESSON: measure a fold's effect on BOTH the standalone route AND the
  target dense design — "compacter" is not automatically "more packable" for a
  placer whose heuristic depends on the exact fold shape.

## order-4 Costas RE-FOLDED to a compact 4x2 (was a 7-wide strip) — INV-8 DONE 2026-07-19

- **DONE:** the order-4 (QPSK) `ComplexCostasLoopBlock` `default_layout` is now the
  COMPACT 4x2 serpentine (`phase,sin_fold,cos_fold,table_sin` on row 0;
  `pd_pi,qpd,rotate,table_cos` on row 1) — the SAME fold as order-2 with the extra
  `qpd` cell inserted. Was a 7-wide longitudinal STRIP (`phase..qpd` all on row 0,
  pd_pi below qpd) that congested the dense QPSK modem (a 7-wide block leaves too
  little bus channel on the 10-wide array, INV-9). Now x[0..3], EVEN columns
  (INV-14), I/O co-located on the west edge. Routes with auto_orient=False, locks
  QPSK (mean|yi| ~ 23100 = 0.707*32767, identical to the strip), and recovers BER 0
  through the full MF->Costas->Gardner->slicer chain. Gates green:
  test_complex_costas_build.py (9), test_qpsk_modem_ber.py (4), pipeline saturation.
- **THE ONE CHANGE that matters (dual-face collision):** qpd is a DUAL-face cell —
  its err/trig go to pd_pi on `face_internal`, its yi_tap/yq_tap tap leaves on
  `face_tap`; these MUST be DIFFERENT faces or the tap (esp. the yq rail) collides
  with the err path. In the fold pd_pi sits WEST of qpd, so `face_internal`=WEST(2)
  (was SOUTH(0) in the strip). `_apply_rotate_tap_face` sets `face_internal` from the
  cell's default_layout resting face and `face_tap` from the tap ROUTE's first-hop
  exit — in the modem the tap routes SOUTH, distinct from the WEST err. So the fold
  "just works" once the layout face is right; no cell-program rewrite beyond flipping
  the face_internal default.
- **⚠️ THE TRAP THAT COST HOURS — amp=0.9 clips a QPSK burst and mis-locks the
  Costas (looks EXACTLY like a fold/routing bug).** A first fold attempt read as
  "broke yq_tap delivery" (BER ~0.28-0.40, consistent across placements) — it was NOT
  the fold. A QPSK constant-modulus burst peak-driven to amp=0.9 CLIPS in Q15 (both
  axes carry +-0.707 = +-23170, and the RRC pulse-shaping overshoot pushes the peak
  past full-scale), which mis-locks the order-4 Costas -> ~40% symbol errors. The BPSK
  modem tolerates amp=0.9 (one real axis); QPSK needs **amp <= 0.7** (what the proven
  test_qpsk_modem_ber uses). LESSON: when a QPSK/complex chain shows a stubborn
  non-zero BER that's INSENSITIVE to placement, SUSPECT THE STIMULUS AMPLITUDE
  (Q15 clipping) before blaming the layout — drive amp<=0.7 and re-measure. The
  folded Costas RX went from BER 0.40 (amp 0.9) to BER 0 (amp 0.7) with NO block
  change. This also means: a lock-magnitude check (|yi|~23100) is NECESSARY but NOT
  SUFFICIENT — a clipped input can still show a plausible |yi| while the bits are wrong;
  gate on END-TO-END BER, not just the lock magnitude.
- **auto_place does NOT route the folded Costas tap** (the tap egress geometry differs
  from what the auto-placer expects); the RX chain + the GRC-import gates now place
  the RX blocks at EXPLICIT anchors (mf(0,0),cos(0,3),gar(0,6),sli(6,8)) — the Gardner
  DIRECTLY below the Costas so the SOUTH tap has a clean corridor — and route with
  auto_orient=False. This is the modem's floorplan idiom (explicit anchors, like the
  BPSK modem), not auto_place.

## ComplexUpsamplerBlock — new, bit-exact vs interp_fir_filter_ccc; sps<=4 cap 2026-07-19

- **Why:** the QPSK MODEM's TX chain needs COMPLEX pulse-shaping (the QPSK mapper
  emits genuine I/Q symbols, unlike BPSK where Q=0). The shipped `UpsamplerBlock` is
  real single-rail; the TX front half `mapper->upsample->RRC` therefore needed a
  2-rail zero-stuffer. Authored `ComplexUpsamplerBlock` (the 2-rail twin of
  UpsamplerBlock); the complex RRC pulse-shaping reuses the existing
  `ComplexRRCMatchedFilterBlock` (a complex RRC FIR) fed the zero-stuffed symbols.
- **DONE + bit-exact on-chip + committed:** 1:1 vs GNU Radio
  `filter.interp_fir_filter_ccc(sps, [1+0j])` — one complex input -> the sample +
  sps-1 (0,0) pairs, kept sample pass-through on BOTH rails, zeros exact -> bit-exact
  in Q15. Each output is a yi/yq PACKET (`WRITE yi; WRITE yq; JUMP`), the
  complex-packet contract ([[INV-17]]). Gate: `verification/tests/test_complex_upsampler.py`
  (12 tests: sps in {2,3,4}, full-scale edges, bit-exact ref, repeat/swapped-IQ/
  wrong-rate/empty mutations, + the hardware-limit guard).
- **HARDWARE LIMIT (INV-0/INV-7): single-cell sps<=4.** The unrolled emit is a
  3-word packet PER OUTPUT (`WRITE yi; WRITE yq; JUMP`), vs the real Upsampler's
  2-word packet (`WRITE; JUMP`) — so the complex block's single-cell ceiling is
  HALF the real block's (4 vs 8). sps=5 overflows the ~31-word cell (verified: build
  "Not enough register space"). The block RAISES on sps>4 (never silently clamps);
  `test_sps_above_ceiling_raises` is the executable guard. The QPSK modem runs at
  sps=2, comfortably within. A larger complex factor would need a multi-cell burst
  emitter (not built).
- **VERIFYING a rate-EXPANDING COMPLEX block (harness note):** there is a
  `run_block_dut_complex` (per-sample I/Q) and a `run_block_dut_rate` (real,
  rate-expanding) but NO complex-rate runner. `run_block_dut_complex` already drains
  the WHOLE per-trigger burst into `outputs_q15` (a list of per-trigger word bursts);
  for a rate-expander just FLATTEN that and de-interleave `[::2]`/`[1::2]` into the
  expanded I/Q channels, then `compare_complex_against_grc` both rails. No new harness
  needed.
- **GRC binding (INV-22):** shipped `gr-kyttar/grc/kyttar_complex_upsampler.block.yml`
  + a `complex_upsampler` shim (dsp_markers.py, re-exported in `__init__.py`). Kept it
  a SEPARATE grc id (not `kyttar_upsampler` with `io_type=complex`) because the BPSK
  modem's .grc ALREADY sets `io_type=complex` on `kyttar_upsampler` yet wants the REAL
  UpsamplerBlock (BPSK carries real symbols; the complex io_type there is a GRC-wire
  cosmetic). Dispatching the real `kyttar_upsampler` on io_type would have silently
  swapped the BPSK modem's TX block. `kyttar_complex_upsampler` auto-maps to
  ComplexUpsamplerBlock via snake->Pascal (no `_TYPE_OVERRIDES` entry needed) — the
  same convention every other `complex_*` block follows.

## QPSK coherent RECEIVER demo — DSP proven BER 0 on-chip; GRC binding + layout NOT done 2026-07-18

⚠️ **CORRECTED 2026-07-18 (was headed "modem — DONE"; both claims were wrong — CM
review).** It is a *receiver*, not a modem (no TX chain), and it is NOT done: the GRC
binding is incomplete (INV-22) and two blocks lay out as longitudinal strips (INV-8).
Do NOT cite this as a finished demo. What IS real vs what is OWED:

- **REAL (the hard DSP):** `examples/qpsk_modem/` recovers 2-bit symbols at **BER 0**
  on-chip through the real build+simKYT path (MF -> Costas order-4 -> complex Gardner ->
  QPSK slicer; carrier + fractional-timing offset). batch_check.py headless driver;
  README. Gates: `placekyt/tests/test_qpsk_modem_ber.py` (programmatic + import BER-0).
  The BER-0 recovery is the load-bearing result and it holds.
- **OWED — GRC binding (INV-22):** `kyttar_qpsk_slicer` has NO `.block.yml` → renders as
  **Missing Block** in GRC. The Costas `order`, MF `decimation`, and Gardner `complex`
  params are NOT exposed in their existing `.block.yml`s, and the param-dependent output
  ports (Costas order-4 yi/yq pair; Gardner complex yi/yq) are not declared. Until the
  bindings expose every param + resolve with no Missing Block, this is NOT GRC-usable.
  (Task #484.)
- **OWED — layout (INV-8/14):** the order-4 Costas and the complex Gardner place as
  straight east-west STRIPS (I/O on opposite edges), not folded blocks like the RRC.
  They only route today with auto_orient=True (GUI default); with auto_orient=False they
  do not route — the symptom of the un-folded strip. Re-fold both (even column count,
  I/O co-located, ≤8 across) so they route regardless of orient.
- **OWED — naming:** rename to a RECEIVER (or add a real TX chain to make it a modem).
- The engine-bug fixes below (build.py handoff patcher, grc_import.py param threading)
  are real and stand.
- **TWO real placeKYT engine bugs found + fixed while assembling the chain:**
  * `engine/build.py` `_apply_brokers`: the complex-packet handoff patcher
    (`_patch_complex_source_handoff`) patched EVERY WRITE/JUMP on the output cell — correct
    for a PURE output cell (MF i4) but WRONG for the order-4 Costas `qpd`, which is BOTH the
    loop's phase detector (err/trig->pd_pi @1) AND the block output (yi_tap/yq_tap->bus).
    Patching all clobbered the internal err/trig → pd_pi never fired → loop never locked.
    FIX: `_patch_complex_packet_last_handoff` (tail external rails only), gated on
    `_output_cell_carries_handoffs`. The MF pure-output path is unchanged.
  * `engine/grc_import.py` `_resolve_port`/`_iq_sibling`: called `catalog.port_map()`
    WITHOUT the instance params, so PARAM-DEPENDENT port sets (order-4 Costas `yq_tap`,
    complex Gardner `yi_e/yq_e` — absent from the default order-2/real PortMap) collapsed a
    numeric port index onto rail 0 and silently DROPPED the Q rail. FIX: thread the
    instance's coerced params through (`_INSTANCE_PARAMS`) + fall back to type defaults.
    Also mapped `kyttar_qpsk_slicer -> QPSKSlicerBlock` (hidden spec, unreachable by the
    snake->Pascal fallback). This ALSO corrected a latent BPSK-mapper mis-resolution
    (`out_i`->`out`); the BPSK import test assertion was updated to the correct `out`
    wiring (duplex BPSK BER test stays green). 290 import/build/route/demo tests green.

## (superseded) QPSK modem — all 4 RX blocks proven on-chip; chain assembly pending 2026-07-18

- **All four QPSK RX building blocks are DONE + bit-exact on-chip + committed:**
  * `ComplexRRCMatchedFilterBlock(decimation=M)` — MF + mod-M output gate (2 sps out).
  * `ComplexCostasLoopBlock(order=4)` — QPSK carrier recovery; the `qpd` output cell
    emits a COMPLEX pair `yi_tap`+`yq_tap` (dual-face tap) + `tap_trig`, so it feeds a
    2-rail downstream even though `interface.output_registers` reports `[0]` (the router
    wires the actual output PORTS, not the interface count — the complex pair egresses).
  * `GardnerTimingRecovery(complex=True)` — 2-rail I/Q timing recovery (in [0,1], out
    [0,1]); emits the (yi, yq) center pair.
  * `QPSKSlicerBlock` — (I,Q) -> 2 Gray bits, GR constellation_qpsk map.
- **The RX chain:** x16_in(I/Q) -> MF(decim=sps/2) -> Costas(order=4) -> Gardner(complex)
  -> QPSKSlicer -> 2 bits. Every internal handoff is a COMPLEX yi/yq pair (2 WRITEs + 1
  trigger) — the ComplexCostasLoop/MF output contract — until the slicer, which emits the
  2-bit symbol. This mirrors the coherent BPSK RX flagship (test_coherent_rx_grc_autopnr)
  but QPSK: same auto-place + bus/broker/crossover routing, complex taps instead of a
  single yi rail. QPSK has a 90-degree carrier ambiguity → BER acceptance is
  rotation+lag tolerant (try 4 constellation rotations, like the reference tests).
- **PENDING (task #483): assemble the demo** = author `examples/qpsk_modem/` (a .grc with
  the 4 real blocks + source/sink + stimulus + a 2-bit sink; a batch_check.py headless
  BER-0 driver; a programmatic auto-P&R BER-0 acceptance test like test_flagship_ber; a
  README). Model it on `examples/coherent_bpsk_rx/` + `placekyt/tests/test_coherent_rx_grc_autopnr.py`.
  The TX: random 2-bit symbols -> QPSK constellation (1/sqrt2 per axis) -> RRC 2 sps ->
  carrier + fractional-timing offset (the complex Gardner now handles fractional timing).

## GardnerTimingRecovery complex=True (2-rail I/Q) — REFERENCE proven, on-chip WIP 2026-07-18

⚠️ **UPDATE 2026-07-18: the on-chip 6-cell build was SUBSEQUENTLY COMPLETED and is
BIT-EXACT** on both I and Q (`verification/tests/test_gardner_complex_reference.py::
test_complex_on_chip_bit_exact` — qdelay landing + duplicate Q14 NCO + qout output; the
"RAISES on build" note below is obsolete). BUT the block is still NOT "done": (1) its
`complex=True` topology lays out as a longitudinal STRIP (INV-8) like the order-4 Costas
— re-fold it; (2) the `complex` param is NOT exposed in the Gardner `.block.yml`, nor the
complex yi/yq output pair (INV-22). The WIP notes below are kept for the framework-gotcha
catalogue (they're all still true), but read them as SOLVED, not open.

- **Why:** a QPSK modem needs SYMBOL-TIMING recovery on BOTH I and Q. The shipped
  Gardner is single-rail (I only, for BPSK). Added a `complex: bool = False` param.
  `complex=False` is byte-identical to the shipped block (37 regressions green:
  test_gardner_recovers_on_chip + test_pipeline_saturation + test_coherent_rx_grc_autopnr).
- **REFERENCE PROVEN + COMMITTED:** `process_reference(complex=True)` runs the IDENTICAL
  I-driven timing loop (NCO, Gardner TED on I, PI loop filter, period feedback — all
  copied verbatim, driven by I only) and adds a PARALLEL Q delay line interpolated at
  every strobe with the SAME `frac`, returning `(N_sym, 2)` recovered (yi, yq). Verified:
  (a) the complex ref's I channel is BIT-EXACT to the real ref on the same I stimulus
  (0/200), and (b) it recovers a random QPSK stream (RRC 2 sps, fractional timing offset
  0.4) at **BER 0** (best rotation+lag, QPSK 90° ambiguity). This is the hard DSP and it
  is banked.
- **ON-CHIP cells = WIP; `complex=True` RAISES on build** (NotImplementedError) so no
  silently-wrong bitstream ships. A subagent attempt (stashed then discarded) built a
  6-cell topology (resampler→qrail→ted→period_relay→relay6→loop_filter) that BUILT + ran
  but was NOT bit-exact (drained only 1 packet, wrong values) and became register-fragile
  (overflowed on a later edit). The framework interactions that make it hard, all
  confirmed real this session:
  * the Q interpolation does NOT fold into the 32-word resampler (overflows ~2 state regs)
    → Q needs a dedicated cell, so the resampler must FORWARD frac + raw xq to it;
  * the internal-JUMP resolver only reaches a cell's POSITIONAL-NEXT cell (see the order-4
    Costas + MF entries) → a cell can't trigger two different destinations; multi-dest
    handoffs need extra relay cells;
  * the complex-egress build patch only fires for an output cell carrying NO internal
    handoffs → the feedback must leave the output cell;
  * the MF register-aliasing footgun (inputs computed from a bare count aliasing
    auto-packed state) bites every added-state cell — re-derive input regs from the real
    data-top and disassemble the BUILT cell (`engine.disasm.disassemble_word` over
    `read_cell_memory`) the instant a value looks shifted.
- **WORKAROUND / QPSK-modem path today:** run the QPSK RX with SYMBOL-SYNCHRONOUS input
  (integer sps, no fractional timing offset) so no timing loop is needed — the proven
  MF(decim)→Costas(order=4)→QPSKSlicer chain recovers BER 0. Add the complex Gardner
  (fractional timing) as the follow-up.
- **RESUME POINT for the on-chip cells:** build the SIMPLEST split — keep the 4 real cells
  100% UNCHANGED (they lock BER 0), add ONE `qrs` cell that receives (xq every sample,
  frac on strobe, and the strobe/parity tag) from the resampler, owns xpq/xp2q, interpolates
  yq with the same frac, and hands yq forward so the output cell emits the (yi, yq) packet.
  Verify each cell's emitted value against `process_reference` at the register level (dump
  `read_cell_memory` per strobe) BEFORE wiring the next — the value bug is where the split
  diverges from the reference's `sq_i = xp2q + mqr(frac, xpq-xp2q)`.

## ComplexRRCMatchedFilter decimation — RESOLVED, bit-exact on-chip 2026-07-18

- **Status:** DSP DONE (bit-exact); GRC binding OWED. `decimation=M` (GR
  `fir_filter_ccf(M, taps)` `decim`) is BIT-EXACT on-chip vs the block's decimated Q15
  reference for M=1,2,4 (0 mismatches, both I and Q). New gates
  `test_complex_decimation_matches_reference[2,4]` +
  `test_complex_decimation_is_no_op_at_M1` in test_complex_harness.py. decimation=1
  (the un-gated MF) is UNCHANGED. The coherent RX can now run carrier/timing recovery at
  2 sps instead of the sample rate — the QPSK-modem throughput win.
  ⚠️ NOT fully done per INV-22: `decimation` is not yet exposed in
  `gr-kyttar/grc/kyttar_complex_rrc_matched_filter.block.yml` — expose it (default 1) so
  the 2-sps decimation is settable from GRC. (Task #484.)
- **Reference:** `process_reference(decimation=M)` = `full_output[0::M]` (phase 0), matching
  GR `fir_filter_ccf`. The on-chip counter uses `initial_value=decim-1` so it fires on
  samples 0, M, 2M, ... (phase 0) — match that here, NOT `[M-1::M]`.
- **On-chip gate = the PROVEN FIRFilterBlock decimator gate**, ported to the MF's last
  I-rail cell: after the delay-line shift (runs EVERY sample) do
  `ADD dcnt,one; MOVE dcnt,R0; CMP dcnt,decim; BR.NZ _mf_skip; XOR dcnt,dcnt; MOVE dcnt,R0`,
  then the MAC + emit (yi/yq/trig), then `HALT`, then `_mf_skip: HALT`. INV-13: the branch
  target is a REAL HALT (never a {write}/{jump} label); the emit path HALTs so it does not
  fall into the skip block (a remote {jump} does NOT stop local execution). ``dcnt`` uses
  ``initial_value=decim-1`` and is NOT `reset_per_batch` (True would zero it on every
  injected symbol in the per-sample drive → drop-all).
- **THE ROOT-CAUSE BUG (why the earlier attempts drop-all'd / emitted yi=0): a REGISTER
  ALLOCATION COLLISION.** Adding the `dcnt` state + 2 gate DataWords (dg_decim/dg_one)
  grew the last cell's DATA block, but the old input-register formula
  `partial_reg = (n_taps+1) + n_state` did NOT account for the extra data words — so the
  `partial` and `carry_in` inputs aliased onto the auto-packed `cs` (yq) and `dcnt`
  registers (disasm showed `MOVE cs, dcnt` and `ADD R0, cs` where partial should be). FIX:
  derive the input regs from the ACTUAL data-top:
  `partial_reg = max(data addresses) + n_state + 1` (the FIR block's
  `last_data_addr + len(state) + 1` convention). Identical to the old formula for
  non-gated cells (data-top == n_taps there), so the plain MF is byte-for-byte unchanged.
- **LESSON:** when adding state+data to a cell whose INPUT registers are computed from a
  bare `n_taps`/`n_state` formula, re-derive the input regs from the real data-top — an
  input aliasing a state reg silently corrupts the datapath (here: partial→cs, carry→dcnt).
  Disassemble the BUILT cell (`engine.disasm.disassemble_word` over `read_cell_memory`) the
  moment on-chip values look "shifted" — it showed the `MOVE R5,R6`/`ADD R0,R5` aliasing
  instantly, after three theory-driven dead ends.

---

## ComplexCostasLoop order=4 (QPSK) — DSP LOCKS on-chip; layout+binding NOT done 2026-07-18

- **Status:** ⚠️ CORRECTED — DSP proven, block NOT done. order=4 builds + routes + LOCKS
  a QPSK carrier on-chip through the real placeKYT pipeline (late mean|yi| ~ 23083 =
  0.707*32767, the ±45deg grid). order=2 (BPSK) UNCHANGED (37 build+saturation tests
  still green). New gates: `test_costas_order4_in_catalog` / `_builds_and_routes` /
  `_built_bitstream_locks_qpsk` in test_complex_costas_build.py.
  **TWO things still OWED before "done":** (1) the order-4 `default_layout` is a
  longitudinal STRIP (phase..qpd along row 0, pd_pi below) — I/O on opposite edges,
  violates INV-8; it only routes with auto_orient=True. Re-fold it (INV-8/14). (2) the
  `order` param is NOT exposed in `gr-kyttar/grc/kyttar_costas_loop.block.yml`, and the
  order-4 yi/yq output pair is not declared there (INV-22) — no way to pick QPSK from GRC.
- **THE FIX (the qpd trig-JUMP-@0 bug): it was the LAYOUT, resolved by placing pd_pi
  BELOW qpd, not east of it.** The router resolves a cell's `trig` JUMP hop via its
  POSITIONAL-NEXT default (`_find_output_target`, the cell_pos+1 branch) — internal_jumps
  is NOT consumed for hop resolution (only for `__terminate__` + portmap external-port
  exclusion). So qpd's trig gets @1 IFF qpd's fwd_face traces to pd_pi at distance 1. In
  the earlier straight-line layout (phase..qpd..pd_pi ALL on row 0) qpd faced EAST but the
  router's distance trace to pd_pi came back 0 → JUMP @0/local. FIX: order-4 layout = a
  7-wide row-0 forward chain phase..rotate..qpd, with **pd_pi dropped to row 1 BELOW qpd**;
  qpd is dual-face (face_internal=SOUTH → err+trig to pd_pi @1; face_tap=EAST → recovered
  yi_tap/yq_tap out). This ALSO clears the corridor congestion that made the output net
  unroutable (an 8-wide row-0 block filled the top edge x16_in..x16_out).
- **Other order-4 gotchas hit + fixed:** (1) rotate is now a PLAIN internal forward cell
  (yi/yq → qpd, single fwd_face) — NOT the dual-face output_cell rotate; that removes the
  hardcoded `face_internal=WEST` that mis-sent yi/yq. output_cell_id() returns "qpd" for
  order-4 (rotate for order-2). (2) The 2-term PD (err=sign(yi)*yq - sign(yq)*yi) does NOT
  fit pd_pi's cell with the PI+pipeline-lock → split into a ``qpd`` cell (QAM16 pattern).
  (3) NAME COLLISION: pd_pi's order-4 INPUT port must be ``errin`` NOT ``err`` — the router
  `_resolve_named_input` matches a same-named STATE var (pd_pi still has StateVar("err"))
  BEFORE the input, so an input named "err" misrouted qpd's err WRITE to the state register
  (dest=6 not R0) and the lock came in low (20668 vs 23059). Renaming the port → err WRITE
  dest=0, lock 23059.
- **LESSON (supersedes the earlier multi-layer WIP notes below):** the trig-hop resolution
  is 100% the router's positional-next distance trace — get the layout so the output cell's
  forward face ABUTS its trig target and drop everything else off that face. A mid-chain
  output cell (qpd) works cleanly when its two consumers go in DIFFERENT directions
  (SOUTH=loop, EAST=out) via the is_face dual-face idiom. Verify with read_cell_memory:
  the trig JUMP word should be `73dX` (@1) not `73ff` (@0/local).

### (superseded) earlier WIP notes
- **Status:** REFERENCE + ALGORITHM PROVEN; on-chip NOT YET LOCKING. The Costas file
  was REVERTED to the proven order-2 (BPSK) state to protect the shipped BPSK modem +
  coherent RX; order-4 is a focused follow-up. (Do NOT ship order-4 until it locks
  on-chip vs the reference.)
- **GR order-4 PD (confirmed by running `digital.costas_loop_cc(0.05, 4)` — locks QPSK,
  corr 1.0):** `err = sign(yi)·yq − sign(yq)·yi`. The order-2 BPSK PD is `sign(yi)·yq`.
  A `process_reference` with this 2-term PD LOCKS QPSK (late mean|yi| ≈ 23170 =
  0.707·32767, the ±45° grid) — the math + Q15 reference are correct.
- **KEY CONSTRAINT: the 2-term PD does NOT fit in the single pd_pi cell** alongside the
  pipeline-lock machinery (~37 words vs the 32-word cell ceiling — program + data +
  state share one cell's 32-word unified memory). Adding even ONE scratch reg overflows
  ("No register space for state 'err'"). This is the SAME limit the 16-QAM DD Costas hit
  and solved with an incremental-error 3-cell pipeline (islice_pi|qslice_err|pi).
- **CHOSEN FIX (CM-approved): split the order-4 PD into an extra ``qpd`` cell** between
  rotate and pd_pi (order-2 stays 7 cells unchanged; order-4 = 8 cells). qpd computes
  the 2-term err and forwards ONE finished ``err`` to a PI-only pd_pi. Per-order
  `_CELL_IDS` / `cell_count` / `internal_connections` / `internal_jumps` /
  `default_layout`. It BUILDS + ROUTES and ALL cells fire — verified by exec-tick trace.
- **REMAINING BUG (where the follow-up picks up): qpd→pd_pi TRIGGER defaults to LOCAL.**
  Per-cell exec trace: phase/sin_fold/cos_fold/table_sin/table_cos/rotate/qpd all fire;
  **pd_pi shows 0 exec ticks**. qpd emits `err` (external_write, lands at pd_pi's east
  face) but qpd's `{jump:trig}` assembles to `0x73ff` = hop 31 = LOCAL goto (self-
  terminate) instead of @1-abutment to pd_pi — so pd_pi is never triggered and no dphase
  feeds back (only 1 output ever emerges). The build's positional @1-abutment defaulting
  covers the KNOWN chain (rotate→pd_pi in BPSK) but not a NEW mid-chain cell's trig. The
  fix is on the BUILD side: qpd's declared `internal_jump` (qpd.trig→pd_pi) must patch
  the trig JUMP hop to @1 like rotate→pd_pi gets (see `_HOP1_CNT`/`_set_cell_hop1` +
  the rotate/pd_pi handoff patching in engine/build.py). Also verify the LAYOUT drop
  is geometrically CONTINUOUS: table_cos(4,0,S) must drop to the cell at (4,1) — the
  first order-4 layout put rotate at (3,1) and table_cos→rotate silently broke (rotate
  never fired). Continuous snake row0 `phase..table_cos(4,0,S)` → row1 (4,1)rotate →
  (3,1)qpd → (2,1)pd_pi → transits (1,1)(0,1) → phase.
- **UPDATE (2nd attempt, straight-line layout): the trig-JUMP-@0 bug is NOT layout.**
  Retried with the PROVEN QAM16-style STRAIGHT-LINE layout (row-0 forward chain phase..
  pd_pi, pd_pi faces south onto a row-1 west return) — the QAM16 DD Costas locks with
  exactly this topology and its `qslice_err→pi` handoff is the analog of `qpd→pd_pi`.
  Result: SAME break. All cells fire EXCEPT pd_pi. Dumped qpd's cell memory
  (`read_cell_memory`): `err` WRITE = `0x67c4` = **@1, dest 4 (CORRECT** — reaches pd_pi
  east), but the trig `JUMP = 0x73ff` = **@0 (LOCAL)**, and a STRAY `WRITE 0x6322 = @6,
  dest 2` (router sink-default leak). So ONLY the trig JUMP is wrong; the data WRITE is
  right. The block-level qpd is STRUCTURALLY IDENTICAL to QAM16's qslice_err (both
  `{write:err}` then `{jump:trig}`, outputs err+trig) and my pd_pi ≡ QAM16 pi
  (input err@R0) — so the block defs are not the problem.
- **DEEPER ROOT (next resume point): the entanglement is with `rotate`'s
  `output_cell_id()="rotate"` + dual-face/tap_trig machinery.** In order-4 rotate is now
  MID-chain (position 5, with qpd+pd_pi after it). The build's mid-block-output patching
  (`_output_cell_carries_handoffs` → `_patch_last_write_handoff`/`_patch_last_jump_handoff`,
  and the `_default_unrouted_exit_hops` @1 defaulting which touches ONLY the exit cell)
  resolves the yi_tap route on rotate but leaves qpd's trig JUMP at the router's local
  default. QAM16's rotate has NO yi_tap/tap_trig/output_cell_id, so its qslice_err→pi trig
  gets @1 cleanly. FIX DIRECTION: either (a) make qpd's declared internal_jump
  (qpd.trig→pd_pi) get @1-patched by the build like the BPSK rotate→pd_pi does — find WHO
  sets rotate.trig→pd_pi @1 in order-2 and extend it to any internal_jumps chain member;
  or (b) drop rotate's yi_tap for order-4 and expose the output from pd_pi's own yi (needs
  a yi tap on pd_pi) so rotate stops being the output_cell. Reverted AGAIN to protect BPSK.
- **LESSON: inserting a cell into a proven feedback loop is multi-layer.** Each fix
  surfaced the next: register ceiling → cell split → layout discontinuity (a cell stops
  firing) → mid-chain trig JUMP not @1-patched (build-side, entangled with the
  output_cell_id/tap machinery, NOT the layout). Trace exec-ticks per cell FIRST (which
  cell stops firing pinpoints the break) THEN dump the last-firing cell's WRITE/JUMP hops
  with read_cell_memory (distinguishes "data reaches / trigger doesn't"). The reference
  proving the algorithm kept every iteration focused on WIRING, not math.

## QPSKSlicerBlock — hard decoder vs GR constellation_decoder_cb(qpsk) 2026-07-18

- **Status:** PASS / DONE. 1 cell. On-chip BIT-EXACT to `process_reference` and to
  GR `digital.constellation_qpsk().decision_maker` over the 4 quadrants + 40 random
  samples (0 mismatches, direct drive).
- **MATCH THE GR MAP, VERIFIED AGAINST GR ITSELF.** GR `constellation_qpsk()` index
  map is **MSB = imag-sign (Q≥0→1), LSB = real-sign (I≥0→1)**: `sym = (Q≥0?2:0) |
  (I≥0?1:0)`. Confirmed by calling `constellation_qpsk().decision_maker(z)` per
  quadrant: I+Q+→3, I−Q+→2, I+Q−→1, I−Q−→0. Do NOT assume the axis/bit order — read
  it off GR. This is the same map `SoftDemodulatorBlock('qpsk')` emits (its 2 LLR
  signs equal these 2 symbol bits), so a soft-demod chain and a hard-slicer chain
  agree bit-for-bit on a clean channel.
- **QPSK slice = 2 pure sign tests → 1 cell** (vs the 16-QAM slicer's 2 cells): QPSK
  is constant-modulus, so there is NO PAM magnitude threshold — each axis is just
  `CMP axis,0; BR.N skip; OR R0,#1`. Reused the proven 16-QAM slicer idiom (`SHL
  sym,#1` writes R0; `OR R0,#1`; `MOVE sym,R0`), saving both I and Q to state FIRST
  (like QAM16) so no live-register hazard.
- **HARNESS GOTCHA (cost ~20 min, not a block bug): `run_block_dut_complex` defaults
  `in_ports=("xi","xq")`.** If a complex block names its input ports differently
  (this block uses `in_i`/`in_q`), you MUST pass `in_ports=("in_i","in_q")` or the
  harness wires `x16_in` to non-existent ports → the landing cell reads stale
  (non-negative) inputs → every symbol comes out `0b11` (3). SYMPTOM = "all outputs
  are the max symbol". Direct-drive (place block, `inject_data_physical` xi/xq +
  `inject_jump`, read port) is the ground-truth cross-check — it exonerated the block
  instantly (correct `[2]` for I−Q+). LESSON: when a complex-block harness run gives a
  constant/degenerate output, verify the input ports actually landed (read the landing
  cell's input regs with `read_cell_memory`) BEFORE suspecting the block.

## QuadratureDemodBlock — FM demod vs GR quadrature_demod_cf 2026-07-05

- **Status:** PASS / DONE vs GNU Radio `analog.quadrature_demod_cf`. 2 cells. DUT
  bit-exact to `process_reference_q15` (0 mismatch); metric vs GR is a CORRELATION
  gate (≥0.999), not bit-exact — see below.
- **MATCH THE FUNCTION, NOT GR'S LITERAL OP.** GR computes `gain·atan2(Im d, Re d)`
  where `d = x[n]·conj(x[n-1])`. Implementing `atan2` on-chip (CORDIC) needs ~47
  cells on this accumulator ISA — a third of the array — because the MOVE-through-R0
  tax means CORDIC's tightly-coupled X/Y/a can't fit one iteration per cell. THAT WAS
  THE WRONG ALGORITHM. FM demod does not need absolute phase; it needs *rate of change*
  of phase, which has a direct MAC form: the **standard FM discriminator**
  `out = gain·(I·dQ − Q·dI) = gain·di`, and `di = Im(x·conj(x[n-1]))` is ALREADY the
  imaginary part the `conjmult` cell computes. → 2 cells, all MAC/mul/sub (the fabric's
  strengths). Before grinding a multi-cell transcendental, ASK: "does the GR block's
  MATH need this, or just its OUTPUT?" `atan2`-then-difference **is** a MAC-only
  discriminator (`d/dt·atan2(Q,I) = (I·Q'−Q·I')/(I²+Q²)`; numerator = `di`).
- **CORRELATION-GATE CONTRACT (RULE #0 deviation, CM-approved).** GR's block literally
  calls `atan2`; the discriminator is its first-order-equivalent derivative form. They
  AGREE for the constant-|x| (limited / AGC'd) signal a real FM RX operates on
  (`di = sin(Δphase) ≈ Δphase`). Verified corr vs GR: **0.99999** at low deviation
  (Δphase ≤ ~0.33 rad) → **0.997** at high deviation (~1.3 rad/sample), degrading
  gracefully as `sin()` compresses vs the linear angle. The block documents the
  algorithm deviation loudly (docstring) and the metric is `correlation`. Any FM RX
  hard-limits/AGCs ahead of this block, which is exactly the regime it matches GR in.
- **`di` reuses conjmult.** `conjmult` already emits `di = cur_q·pv_i − cur_i·pv_q`
  (two Q15 MULQ truncations then SUB) and holds the previous sample. The `gain` cell is
  the same `2^p·Kp` saturating-scale pattern as the NCO/VCO output stage. `x[-1]=0` →
  `di[0]=0`, matching GR's `out[0]=gain·arg(0)=0`.
- **Cautionary note:** the CORDIC atan2 (proven bit-exact to true `atan2` at 5.5 LSB,
  neg-flag 2-cell fold) is real and works, but is 45+ cells — kept only as a reference
  for a block that GENUINELY needs `atan2` and can afford the area, or after an ISA
  revision (a direct ALU destination would collapse it to ~18 cells). For FM demod it
  was massive overkill.

## FrequencyModulatorBlock — FM modulator / VCO vs GR frequency_modulator_fc 2026-07-04

- **Status:** PASS / DONE vs GNU Radio `analog.frequency_modulator_fc`, 19 tests
  (bit-exact substrate + GR correlation + 5 mutation gates + 4-point sensitivity
  sweep + accum-first ordering). Real-in / complex-out, 10 cells.
- **The VCO is the NCO with ONE changed cell.** `FrequencyModulatorBlock` SUBCLASSES
  `NCOBlock` and overrides ONLY the phase cell (the cos/sin quarter-wave table
  pipeline — fold/even/odd/interp ×2 + emit — is inherited verbatim). The NCO phase
  cell adds a CONSTANT `freq_word`; the VCO phase cell adds the RUNTIME INPUT scaled
  by `kscale = sensitivity/pi` (Q15 MULQ). Cleanest way to add a new block: reuse a
  proven multi-cell datapath, change the single differing cell. GOTCHA: the NCO's
  cell builders are NESTED functions inside `build_cell_programs`, so you can't
  override one method — call `super().build_cell_programs()` then REPLACE
  `cells["phase"]`.
- **kscale derivation (Q15 input → 16-bit phase-word).** GR advances `dphi =
  sensitivity·x` radians; on-chip `2π ≡ 65536`, and the input arrives as Q15
  (`x_q15 = x·32768`), so `dphi_word = x_q15·sensitivity/π`. The multiplier is
  `kscale = sensitivity/π` via MULQ. For `kscale ≤ 1.0` (Q15) the block requires
  `|sensitivity| ≤ π` — a HARDWARE DEVIATION documented loudly (comment + docstring
  + manifest `HW-DEVIATION:`) and RAISED on out-of-range (INV-0). GR takes any
  sensitivity; real modems use `sensitivity = 2π·f_dev/fs ≪ π`.
- **GR ACCUMULATES FIRST, then emits.** `out[0] = exp(j·sensitivity·x[0])`, NOT phase
  0. My first reference lagged one sample (emitted at phase 0, then incremented) →
  corr still 1.0 but a per-sample offset. Fix: scale+add the input BEFORE the two
  fold WRITEs. A dedicated `test_fm_phase_accumulates_first` asserts Q[0]≠0 for a
  nonzero drive (a lag bug leaves Q[0]==0). Phase-cell WRITE order: emit ph_sin at
  the NEW phase, ADD quarter → ph_cos, then SUB quarter to restore state (WRITE
  always sends R0, so R0 must hold the value at each WRITE).
- **Metric = CORRELATION vs GR (not per-sample amplitude).** The DUT is BIT-EXACT to
  `process_reference_q15`, but vs GR the per-sample max error grows to ~100 LSB over
  a run — this is the 16-bit phase-word DRIFT (each `sensitivity·x` advance is
  quantised to 2π/65536 rad; GR uses a float64 accumulator). That drift is a
  documented substrate limit, so DSP-equivalence is measured by correlation (≥0.999,
  ~1.0), which isolates that the FM tone has the right SHAPE. The table-interp floor
  (~11 LSB) is inherited unchanged from the NCO.
- **New harness: `run_block_dut_real_to_complex`.** The existing complex driver
  hardwires a TWO-operand xi/xq sample; an FM modulator ingests ONE real word per
  trigger (`WRITE x → R0` + `JUMP`) and emits complex `yi,yq`. Added a real-in /
  complex-out driver (reuses `ComplexDUTResult`) — the correct fit for VCO-class
  blocks (real control → complex out).

---

## SoftDemodulatorBlock — BPSK soft demapper vs GR soft decoder 2026-06-25

- **Status:** PASS / DONE vs GNU Radio `digital.constellation_soft_decoder_cf`
  (BPSK), 12 tests; full verification suite 257; placekyt 937 / 16 skipped.
- **A single MULQ.** On chip the soft demapper is `LLR = coeff·I`, one `MULQ`,
  where `coeff = min(0.5, 2/σ²·llr_scale)`. The block was already proven on the
  LLR harness (test_complex_harness.py); this gives it a dedicated suite + makes
  it a manifest-`done` block.
- **noise_variance is a REAL knob.** `coeff` tracks `2/σ²` and SATURATES at the
  production scale 0.5 for any realistic `σ² ≤ 4`, then scales down for very high
  noise (`σ²=10 → coeff=0.2`). GR's BPSK soft decoder emits `4·I`, so the LLR
  comparator aligns the two scales with `llr_scale = coeff/4` (0.125 at the
  production scale). Both regimes match GR on sign + (rescaled) magnitude.
- **Metric = LLR (sign exact + magnitude floor).** The SIGN is the hard bit the
  FEC decoder acts on → must agree exactly outside the near-zero dead zone; the
  soft magnitude is held to a derived Q15 floor. Mutations (flipped sign, halved
  magnitude, +1 delay, empty) all fail.
- **Fixed a latent reference bug.** `process_reference` referenced a nonexistent
  `self._inv_variance_q15` and would `AttributeError` if ever called. Rewrote it
  to model the on-chip `LLR = (coeff·I)>>15` exactly and added `llr_coeff_q15` +
  `process_reference_q15` (the bit-exact predictor the EXACT gate uses).

---

## BandRejectFilter — firdes.band_reject (notch, S=2) 2026-06-25

- **Status:** PASS / DONE vs GNU Radio `firdes.band_reject` + `fir_filter_fff`, 30
  tests; full verification suite 245; placekyt 937 / 16 skipped. COMPLETES the
  four firdes convenience filters (Decision B).
- Shares `_firdes.py` + the FIRFilterBlock subclass pattern. Band-stop / notch;
  normalized to unity gain at DC (`fmax` over `taps[n]`, like low_pass). The notch
  has a LARGE centre tap ⇒ `Σ|h| > 2` ⇒ COEFFICIENT HEADROOM **S=2** (the highest
  of the four — exercises the FIR's S≥2 last-cell budget path end-to-end on the
  real route+sim). Q15 taps bit-exact firdes for all six windows (INV-16). Default
  39-tap = 9 cells. Mutations (inverted, wrong-band, +1 delay, empty) all fail.
  Label "Band Reject Filter".

---

## BandPassFilter — firdes.band_pass (two cutoffs) 2026-06-25

- **Status:** PASS / DONE vs GNU Radio `firdes.band_pass` + `fir_filter_fff`, 30
  tests; full verification suite 215; placekyt 937 / 16 skipped.
- Shares `_firdes.py` + the FIRFilterBlock subclass pattern. Takes TWO cutoffs
  (`low_cutoff_freq`, `high_cutoff_freq`); normalized to unity gain at the band
  CENTRE (`fmax` over `taps[n]*cos(n*freq)`, `freq=pi*(lo+hi)/fs`). Q15 taps
  bit-exact firdes for all six windows (INV-16). Default 39-tap = 9 cells, S=1.
  Mutations (inverted, wrong-band, +1 delay, empty) all fail. Label "Band Pass
  Filter".

---

## HighPassFilter — firdes.high_pass (same pattern as LowPassFilter) 2026-06-25

- **Status:** PASS / DONE vs GNU Radio `firdes.high_pass` + `fir_filter_fff`, 30
  tests; full verification suite 185; placekyt 937 / 16 skipped.
- Reuses the shared `_firdes.py` designer (built for LowPassFilter) and the same
  FIRFilterBlock subclass pattern. The only design difference is the
  normalization: a high-pass is unity-gain at NYQUIST, so `fmax` accumulates
  `taps[n]*cos(n*pi)` (the `(-1)^n` alternation), exactly as `firdes.cc`. Q15
  taps bit-exact firdes for all six windows (INV-16); float ~1 ULP. Default
  39-tap (fs32k/co4k/tw2k) = 9 cells, S=1. Mutations (inverted, wrong-cutoff, +1
  delay, empty) all fail. GRC label "High Pass Filter".

---

## LowPassFilter — firdes reimplemented in pure Python (GR absent at runtime) 2026-06-25

- **Status:** PASS / DONE vs GNU Radio `firdes.low_pass` + `fir_filter_fff`, 31
  tests; full verification suite 155; placekyt 937 passed / 16 skipped.
- **A convenience FIR IS a FIRFilterBlock + a tap designer.** Like DCBlocker,
  `LowPassFilter` SUBCLASSES the verified FIRFilterBlock and just supplies
  firdes-designed taps — zero new datapath, all headroom/saturation/fold
  machinery inherited. Params mirror GRC's Low Pass Filter verbatim (gain,
  samp_rate, cutoff_freq Hz, transition_width Hz, window, beta).
- **THE constraint that shaped the build — GR is NOT in the runtime `.venv`.**
  GNU Radio is importable only on the verification host (`/usr/bin/python3`), not
  in the customer-modem `.venv` the blocks run in. So the block CANNOT
  `import gnuradio.filter.firdes` (Decision B's literal wording). Instead
  `blocks/_firdes.py` REIMPLEMENTS firdes op-for-op in pure Python: `compute_ntaps`
  (`int(atten*fs/(22*tw))` → next odd), the `gr::fft::window` builders (Hamming/
  Hann/Blackman/Rectangular/Blackman-Harris cos-windows + Kaiser via the `Izero`
  Bessel series), the windowed-sinc, and the unity-gain normalization — each cast
  point matched (double product → float32 per tap, double `fmax`, `float *= double`
  restore).
- **"Bit-exact firdes taps" is NOT achievable across the run boundary — and that's
  fine.** Two last-bit sources, both sub-ULP: (a) GR's C++ `coswindow` is compiled
  with FMA (Blackman/Blackman-Harris differ by 1 ULP even on the GR host); (b) the
  `.venv` links a DIFFERENT libm than the GR host, so `sin`/`cos` differ in the
  last bit and ANY window's float tap can move ~1 ULP. The honest, hardware-
  meaningful gate is the **Q15-quantized** tap: `float_to_q15(mine) ==
  float_to_q15(firdes)` is BIT-EXACT for EVERY window (the sub-ULP float diff never
  crosses a Q15 boundary), so the on-chip filter IS provably the firdes filter.
  The float-tap test asserts a derived floor (< 1e-6, far below ½ Q15 LSB), not bit
  equality — promoted to INV-16.
- **Tolerance inherited, not tuned.** A normalized firdes low-pass has Σ|h|
  slightly >1 (sidelobes) → COEFFICIENT HEADROOM S=1 (default 39-tap = 9 cells).
  DUT-vs-GR uses the headroom-aware `q15_quant_floor(N, head_shift=S)`; DUT-vs-
  `process_reference_q15` is EXACT. Taps symmetric (linear phase) ⇒ delay=0,
  reversed-tap convention moot.
- **GRC + import.** `kyttar_low_pass_filter.block.yml` (label "Low Pass Filter") +
  the `kyttar.low_pass_filter` marker wrapper; `grc_import` maps
  `kyttar_low_pass_filter` → `LowPassFilter` through the existing snake→Pascal
  fallback (`(pascal+"Block", pascal)`) — no `_TYPE_OVERRIDES` entry needed.
- **Shared designer.** `_firdes.py` exposes `low_pass`/`high_pass`/`band_pass`/
  `band_reject`; the High/Band-pass + Band-reject convenience blocks reuse it.

---

## HARNESS — complex (I/Q) + LLR (soft-decision) support 2026-06-24

- **Additive, real path untouched.** New `run_block_dut_complex` /
  `run_gnuradio_ref_complex` / `compare_complex_against_grc` /
  `compare_llr_against_grc` sit alongside the real ones; all 109 prior tests stay
  green (125 total with the 16 new).
- **Complex input = two-operand transaction.** A complex block lands its sample as
  `WRITE xi -> in_regs[0]`, `WRITE xq -> in_regs[1]`, then ONE `JUMP entry` — the
  exact representation `sim_bridge.process_batch(complex=True)` and the on-chip
  Costas/MF lock tests use (xi@R0, xq@R1). The driver reuses INV-1 (placement hop)
  and INV-6 (resolve entry+regs WITH params) unchanged.
- **Complex output egress — wire ONE net, not two (the real gotcha).** A complex
  output cell (the MF's `i4`) emits BOTH `yi` and `yq` as two WRITEs from one cell.
  Wiring ONLY `yi -> x16_out` makes both ride the same bus corridor out, arriving
  INTERLEAVED `[yi, yq, yi, yq, ...]` — BIT-EXACT vs the reference. Wiring a SECOND
  net `yq -> x16_out` creates a dual-route-to-one-port conflict that
  `_patch_last_write_handoff` (patches only the highest-addr WRITE) cannot resolve →
  the build succeeds but egress is SILENTLY ZERO. So the driver wires the primary
  output port only and de-interleaves. (Verified: yi-only -> 2 words/sample,
  maxerr 0; yi+yq -> 0 output.)
- **Complex comparator gates BOTH channels.** I and Q each pass the per-channel
  amplitude/exact metric + derived floor; a swapped I/Q, negated Q, or Q-only
  latency all FAIL (an I-only check would miss them — mandatory mutations cover
  each).
- **LLR metric = SIGN agreement + magnitude.** An LLR's sign is the hard bit the
  FEC decoder acts on, so sign agreement must be perfect (outside a near-zero
  dead zone where a flip is quantization-benign); the soft magnitude is held to a
  derived Q15 floor after the block's LLR scale is applied to the GR reference.
  GR BPSK `constellation_soft_decoder_cf` emits `4*I`; the Kyttar SoftDemod emits
  `0.5*I` -> `llr_scale = 0.5/4 = 0.125` aligns them (signs identical). Dead-zone
  threshold is a FLOAT on the scaled ref ([-1,1) units), NOT *32768 (a units bug
  that made the sign gate never fire — caught by the flipped-sign mutation).
- **Proven on:** ComplexRRCMatchedFilterBlock (complex, vs `fir_filter_ccf`: I 11 /
  Q 12 LSB within an 18-LSB floor; bit-exact 0 LSB) and SoftDemodulatorBlock (LLR,
  vs the GR soft decoder: 0 sign mismatch, 1 LSB magnitude). Mutations
  (swap I/Q, negate Q, +1 delay, wrong taps, empty, flip LLR sign, LLR +1 delay)
  all FAIL the gate as required.

---

## IIRBiquadBlock — Q15 biquad via half-and-double-MSUQ (the keeper) 2026-06-24

- **The "impossible" claim was half-right.** An earlier pass marked IIR BLOCKED:
  "a Direct-Form feedback term `a1*y` reaches ~2.0, overflows the 16-bit Q15
  accumulator, needs ISA guard bits." The OVERFLOW is real (a1 = -2cos(omega), so
  |a1| up to ~2 > Q15 full scale), but the conclusion was wrong — it's the classic
  fixed-point-DSP problem with a classic fix, no ISA change.
- **The real bug was a silent CLAMP.** The old block did
  `a_q15 = float_to_q15(min(1, max(-1, a)))` — clamping every |a|>1 feedback coeff
  to ±1.0, i.e. building a COMPLETELY DIFFERENT (wrong) filter for any sharp pole,
  with no error. That clamp, not the architecture, was the defect.
- **The keeper — half-and-double MSUQ.** Store each feedback coeff HALVED (`a/2`,
  always representable since |a|<2 ⇒ |a/2|<1) and apply its `MSUQ Ra,Rb`
  (`R0 -= (Ra*Rb)>>15`, arch_spec v0.11 §4.12, MAC opcode MODE=11) TWICE.
  Subtracting `a/2 * y` twice == subtracting `a*y`, and EACH product is in range,
  so no intermediate overflow. A stable biquad's output is itself bounded, so the
  whole Direct-Form-I accumulator stays in range — NO saturating shift needed
  (unlike the FIR gain restore), single cell, bit-exact with GR's accumulation
  order. (Verified MSUQ executes correctly on simKYT first: a gentle |a|<1 biquad
  matched the float ref to 1e-4 before relying on the double-MSUQ.)
- **Precision is the real (documented) limit, not overflow.** GR `iir_filter_ffd`
  uses DOUBLE-precision feedback taps; Q15's 15 fractional bits are coarser and the
  recursive-loop quantization error GROWS as poles approach |z|=1. Measured vs GR
  (butterworth-2): cutoff 0.10-0.40 = 3-16 LSB (production-accurate); 0.05 ~53 LSB
  (marginal); 0.02 ~160 LSB. So: ship the proven range, GUARD the sharp-pole edge
  with a known-limit test (INV-7 style) that flips if precision is ever improved.
- **Gate (16 tests, all green):** DUT == `process_reference_q15` EXACT at EVERY
  cutoff (the datapath IS the predictor); DUT ≈ GR `iir_filter_ffd` in the
  production range; a sharp-pole known-limit guard (16 < err_LSB < 2000); and
  MANDATORY mutations — inverted, the clamped-a1 REGRESSION (the original bug must
  fail the gate), +1 delay — all FAIL (INV-4).
- **Disassembler gap found + fixed.** `bitstream.py` decoded only the top-level
  MAC (0xD) / MUL (0xC) opcodes, mislabeling MACQ/MSU/MSUQ/MULQ/MULHI all as
  "MAC"/"MUL". Decoded the 2-bit MODE field [11:10] per the spec so sub-modes show
  their real mnemonic. The disassembler — not the ISA — was incomplete; MSUQ is a
  real, simKYT-correct instruction.
- **Generalizes:** see invariants.md INV-15 (any Q15 block needing a coefficient
  with |.|>1 uses store-halved + apply-twice; cascade the split for |.|>2).

## ComplexMixerBlock — DONE: multiply_cc via NCO + a signal-RELAY cell 2026-06-25

The complex mixer (= multiply_cc(signal, sig_source_c) = in·exp(jθ_n)) is COMPLETE
and verified vs GNU Radio (19 tests; full verification suite 297; placekyt 937).
It REUSES the verified NCO interpolated cos/sin pipeline verbatim (with a sign-
applying interp so cos/sin come out signed, no amplitude) + a mixer cell doing the
full complex product yi=xi·cos−xq·sin, yq=xi·sin+xq·cos (4 MULQ).

- **THE fix — a mid-pipeline RELAY cell for the signal.** The signal (xi,xq) must
  travel phase→mixer (the pipeline ends), but a value forwarded across ~8 skipped
  cells arrives 0 (the substrate forward-distance limit: IQUpconvert's skip-4 works,
  the NCO's phase→emit skip-8 failed). The budget-tight pipeline cells can't
  passthrough 2 extra values either. The clean fix: insert a CHEAP relay cell
  (2 state, ~6 instr, no table) mid-chain (after sin_interp, before cos_fold) so
  xi,xq hop phase→relay (skip-4) then relay→mixer (skip-4) — both within the proven
  distance. 11 cells, column-major fold, mixer faces east to the bus.
- **Overflow note:** yi=xi·cos−xq·sin can exceed Q15 for a full-scale signal; the
  DUT wraps and the bit-exact reference models the wrap, but the GR-amplitude test
  drives signal amplitude ≤ 0.5 so the product stays in range (DUT wrap == GR float).
- **Generalised** to [[kyttar-cell-asm-conventions]]: to carry a value across a long
  datapath, hop it through a cheap relay cell every ≤4 cells, not a single far
  forward. This + the NCO completes the tier-1 GRC-parity queue.

---

## ComplexMixerBlock — cos/sin done (reuses NCO); blocked on signal routing 2026-06-25

The complex mixer = multiply_cc(signal, sig_source_c) = in*exp(j theta_n). It
REUSES the verified NCO interpolated cos/sin pipeline verbatim (phase | sin{fold
even odd interp} | cos{...} | mixer), with a sign-applying interp (the mixer wants
signed cos/sin, no amplitude) and a mixer cell doing the full complex product
yi=xi*cos-xq*sin, yq=xi*sin+xq*cos (4 MULQ). The 10-cell block BUILDS, ROUTES,
EGRESSES, and the bit-exact reference is written.

- **THE blocker — the SIGNAL doesn't reach the mixer.** The phase cell forwards the
  input (xi,xq) to the mixer cell (the last of 10), and it arrives 0 (output all
  zero; echoing confirms xi=0 at the mixer). IQUpconvertBlock forwards phase->upmix
  over 6 cells (skip-4) and works; this is skip-8 and fails -- a forward over too
  many intermediate cells doesn't deliver, even though the column-major layout
  places phase and mixer physically adjacent (so it's a CHAIN-distance limit, not a
  physical-routing one). The NCO hit the same wall (phase->emit neg forward arrived
  0) and dodged it by computing neg LOCALLY in the fold -- but the signal is an
  external input, it can't be recomputed downstream.
- **Why passthrough doesn't fit:** routing xi,xq THROUGH the pipeline needs each hop
  cell to forward 2 extra values, but every pipeline cell is budget-tight (fold ~23
  instr + 4 data + 3 state; even/odd carry 18-word tables; interp already has 5
  inputs). Adding a 2-value passthrough overflows the 32-reg/cell budget in all of
  them.
- **The fix (not yet built):** a dedicated signal-RELAY path -- a couple of cheap
  cells (no table, few instr) interleaved so xi,xq hop <=4 cells at a time from
  phase to the mixer; OR a shorter cos/sin pipeline (a single-cell 17-entry table
  gives 37 LSB but halves the cell count, putting the mixer within skip-4 of phase);
  OR pin down the exact forward-distance limit and route within it. The cos/sin half
  is proven, so the mixer is finished modulo this signal route. nco-style WIP in
  complex_mixer_block.py was reverted to the old real-mixer so the suites stay green.

---

## NCOBlock — DONE: complex interpolated NCO bit-exact vs GR sig_source_c 2026-06-25 (iter 5)

The 10-cell interpolated complex NCO is COMPLETE and verified vs GNU Radio
``analog.sig_source_c`` (21 tests; full verification suite 278; placekyt 938).

- **The off-grid bug (iter-4) was an output FAN-OUT failure.** `fold.idx` was fanned
  to even+odd+interp; only the FIRST destination (even) received it — odd and interp
  got 0, so the odd cell looked up garbage and the interp never swapped P/Q. The fix:
  emit idx as **two separate writes** `idx_e`→even, `idx_o`→odd (one output port per
  destination, like the phase cell's ph_sin/ph_cos), and forward the parity
  `par=idx&1` from the even cell to the interp. A single output port driving multiple
  cells is the trap — `{write:idx_e}{write:idx_o}` is reliable, fan-out is not.
- **Budget reclaim:** the 2nd write put the fold 1 over; computing `frac=(w&0x1FF)<<6`
  as `SHL #7; SHR #1` (instead of `AND mask1ff; SHL #6`) drops the `mask1ff` data
  word — same instruction count, gap +1.
- **The complete keeper design** (angle-fold + parity-split + amp-then-sign +
  face-east folded egress) is in the iter-4 entry below; iter-5 only fixed the
  fan-out + budget. Result: BIT-EXACT vs ``process_reference_q15`` on both channels
  at grid AND off-grid frequencies; ~1 LSB vs GR grid-aligned; ~10 LSB off-grid vs
  GR at the DUT's actual (freq_word) frequency = the derived 33-entry-table
  interpolation floor. Off-grid vs GR's EXACT frequency shows the separate, expected
  freq_word-quantization drift (fs/65536 Hz resolution), corr=1.0.
- **Generalised** to [[kyttar-cell-asm-conventions]]: never drive multiple cells from
  one output port (emit one write per destination); folded-egress needs the output
  cell's FACE = its bus direction; explicit input regs don't reserve from the state
  gap (place data past the highest input reg); amplitude-then-sign in emit.

---

## NCOBlock — iter 4: full datapath + egress working; grid-aligned bit-exact; off-grid interp bug 2026-06-25

The 10-cell interpolated complex NCO is ~90% done. It BUILDS, ROUTES, EGRESSES two
words/trigger, and is BIT-EXACT vs the reference AND matches GR ``sig_source_c`` to
**1 LSB** on grid-aligned frequencies (freq_word a multiple of 512). Reverted to the
working original (suite green); best WIP saved at
`verification/KNOWLEDGE_BASE/drafts/nco_block_WORKING.py`.

- **FOLDED-EGRESS SOLVED (the iter-3 blocker).** A 2-row fold egresses only when the
  output cell's FACE = its egress direction toward the bus (NOT via io_colocated,
  which can be False — the RRC egresses with it False). The winning layout is a
  COLUMN-MAJOR serpentine: col 0 flows SOUTH (phase→sin_interp, faces "south"), the
  corner cell faces "east", col 1 flows NORTH (cos_fold→emit, faces "north") and
  **emit faces "east"** so its two writes egress east, off-block, to the bus. With
  the wrong face the bus taps an internal cell (it read cos_fold's idx) or nothing.
- **Two-write complex egress** needs `emit` to compute both yi,yq then `{write:yi}`
  `{write:yq}` — both ride the bus interleaved (harness de-interleaves).
- **MORE substrate gotchas found (add to [[kyttar-cell-asm-conventions]]):**
  * **A fan-out of one output to 3 cells silently drops the 3rd.** `fold.idx →
    even, odd, interp` delivered idx to even+odd but left interp's idx = 0. Fix:
    don't fan a value to 3 — derive it once and forward from a 2nd hop (the even
    cell computes `par = idx&1` and forwards it to interp).
  * **A long forward (first cell → last cell across the whole chain) fails.** The
    phase cell's `neg_sin/neg_cos → emit` arrived as 0; a mid-chain forward
    (interp → emit) works. So compute `neg` in the fold, carry it fold→interp, and
    apply the sign there/at emit (a short forward).
  * **Explicit input registers do NOT reserve themselves from the gap.** The
    resolver allocates state from `gap = range(next_data_addr, base)` BEFORE inputs;
    a cell with 5 inputs at R0..R4 and data at addr 1..2 puts state on R3/R4 →
    collides with the frac/neg inputs (the value read back is the state, not the
    input). Fix: place the cell's DATA past the highest input register (e.g. addr 5)
    so the gap starts above the inputs.
  * **Amplitude-then-sign**: emit applies amp (MULQ) THEN negates, so the bit-exact
    reference must do `neg ? -((mag·amp)>>15) : ((mag·amp)>>15)` (negate-after-amp),
    not negate the table value first — a 1-LSB-on-negatives difference otherwise.
- **REMAINING BUG — off-grid interpolation.** All grid-aligned tests use frac=0 and
  EVEN idx, so interpolation + the odd path were under-tested. Off-grid: `idx=8`
  (frac≠0) is bit-exact, but `idx=16` produces a magnitude (~25749) LARGER than both
  table endpoints (table[16]=23170, table[17]=24279) — impossible for linear interp,
  so the interp used a wrong P/Q or frac for that idx. The even/odd tables +
  addressing + frac are all PROVEN correct in isolation (`even[8]@addr9=table[16]`,
  `odd[8]@addr9=table[17]`, `frac=13056`), so the fault is in an on-chip forward or
  the interp's MULQ/SUB for larger idx — needs cell-echo instrumentation to localize
  (echo eval/oval/frac/delta from the interp at idx=16). Once fixed, the
  grid-aligned-proven pipeline makes the full block bit-exact; then GR-amplitude
  verify (~11 LSB off-grid floor), mutations, GRC yml, ComplexMixer.

---

## NCOBlock — iter 3: DSP pipeline works BIT-EXACT on chip; blocker = folded egress 2026-06-25

Big progress. The interpolated complex NCO was REDESIGNED to fit the substrate and
the sin/cos datapath now computes BIT-EXACT on simKYT. The block is still not done:
the 10-cell folded layout doesn't egress correctly. WIP at
`verification/KNOWLEDGE_BASE/drafts/nco_block_parity_split.py.draft`; nco_block.py
reverted to the working original so the suite stays green.

- **The keeper design — parity-split table + angle-fold.** Two changes made it fit
  the 32-reg/cell budget AND avoid cross-cell straddle:
  1. **Angle-fold:** fold the quadrant mirror INTO the angle (`q = mir ? 16384-within
     : within`) so interpolation is always FORWARD `table[idx]→table[idx+1]` — no
     per-cell mirror/step logic in the lookup. idx_bits=7 → 10-11 LSB (validated).
  2. **Parity-split table:** the 33-entry table is split EVEN (`table[0,2,…,32]`,
     17 entries) / ODD (`table[1,3,…,31]`). Since `idx` and `idx+1` always have
     OPPOSITE parity, each table cell does ONE unconditional LOAD (no range test,
     no straddle, no cross-cell addressing). The interp cell re-pairs by parity.
  10 cells: `phase | (fold even odd interp)_sin | (…)_cos | emit`.
- **THE substrate calling conventions (cost the most time — promote/remember):**
  * **ALU first operand must be a NAMED register, never R0.** `AND R0, x` /
    `ADD R0, x` / `SHR R0, n` (R0 as the *source* `Ra`) are MISCOMPILED (silently
    wrong). The SECOND operand MAY be R0 (`SUB zero, R0`, `ADD p, R0` are fine).
  * **An input port at R0 must be MOVEd out before R0 is clobbered**, and a value
    read from `R{in:x}` (which aliases R0 for the landing reg) can be read ONCE —
    after the first ALU op R0 changes. Save it to a state reg immediately
    (IQUpconvert does exactly this: `MOVE state, R{in:phase}` first).
  * **`AND` does NOT set the branch flag** — a `BR.Z` must be preceded by an
    explicit `CMP R0, R{data:zero}` (CMP may take R0 as `Ra`).
  * **Per-cell budget:** usable gap = `(31 - instr_count) - data_top - 1` ≥
    state + (inputs not pinned to R0). The fold only fit after moving `neg` out to
    the phase cell (phase computes `neg_sin = phase>>15`, `neg_cos =
    (phase+16384)>>15` and forwards them straight to emit).
  * **Multi-write handoff + DANGLING outputs:** a cell that `{write:}`s several
    output ports works ONLY if every port has a real internal destination. A
    DANGLING output (e.g. `ph_cos` with no consumer in a bisect) MISROUTES the
    other writes (it showed as a clean 90°-shifted sine — the fold received
    `ph_cos`=phase+16384 instead of `ph_sin`). Fan-out (one output → 3 cells, e.g.
    `idx`→even/odd/interp) DOES work.
- **VALIDATED:** the 6-cell sin pipeline (phase→fold→even→odd→interp→emit, 1-row
  layout) is BIT-EXACT vs the reference for all 16 test phases (full quarter-wave
  incl. the mirror). Reference `_sine_q15` mirrors the datapath op-for-op; n=0 =
  (amp, 0) (GR phase-0 start).
- **THE remaining blocker — folded 10-cell egress (P&R geometry).** A 1-row chain
  egresses; the 10-cell needs a 2-row/2-col FOLD (≤8 across, INV-9) and there the
  output cell's egress is geometry-sensitive: `port_map.io_colocated` must be True
  (input + emit on the SAME bus-facing edge). Observed: column-major+`face=east`
  put emit on the EAST edge opposite the WEST input → bus tapped `cos_fold`'s `idx`
  (1 wrong word); 2-row+`face=west` (phase 0,0 / emit 0,1) → empty. The fix is the
  right fold + face so `io_colocated=True` with emit on the bus edge (study the
  FIR `_fold_geometry` and the Costas/RRC `default_layout`, which solve exactly
  this for folded/feedback blocks). Once egress lands, the 6-cell-proven pipeline
  makes the full block bit-exact — then GR-amplitude verify (~11 LSB derived floor,
  grid-aligned freq_word), mutations, GR-native params, GRC yml, ComplexMixer.

---

## NCOBlock — build attempt: validated, blocked on per-cell register budget 2026-06-25 (iter 2)

A FULL build attempt was made (the WIP block is saved at
`verification/KNOWLEDGE_BASE/drafts/nco_block_interpolated.py.draft`). The
algorithm + reference are VALIDATED; the block is NOT done because the
interpolated complex NCO exceeds the substrate's per-cell register budget and
needs a ~10-cell split. nco_block.py was reverted to the working original so the
suite stays green (test_data_words builds NCOBlock).

- **Reference VALIDATED vs GR.** The complex reference (interp quarter-wave,
  phase-0 start, amplitude MULQ) matches exact float: 1.4 LSB on grid-aligned
  freq_word (e.g. 2000/32000 → freq_word=4096), 37 LSB worst-case off-grid; n=0 =
  (amp, 0). The on-chip `_sine_q15` mirrors the fold+table datapath op-for-op.
- **Architecture builds — modelled on IQUpconvertBlock** (the proven 6-cell NCO:
  phase | sin_fold | cos_fold | table_sin | table_cos | combine, with
  `internal_connections`/`internal_jumps`/`default_layout`). Complex egress copies
  the matched-filter pattern (`{write:yi}{write:yq}`, wire ONE net, harness
  de-interleaves). The complex harness needs the NCO to declare TWO trigger inputs
  (R0,R1, ignored) so `run_block_dut_complex` drives it.
- **THE blocker — per-cell budget (the number).** The resolver packs data low and
  instructions high; usable gap registers for state+preserved-inputs is
  `gap = (31 − instr_count) − data_top − 1`. Interpolation breaks two cells:
    * FOLD (decomp → idx, idxB, frac, neg): ~24 instr + 5 data + 3 state + 1
      preserved input → gap = (31−24)−4−1 = 2 < 4 needed. **"No register space for
      state 'fidx'."**
    * TABLE+interp (17 entries = 18 data words): gap = (31−12)−19−1 = −1. The
      17-entry table alone leaves no room for the interp arithmetic + 4 state.
- **THE fix — split into a 10-cell datapath (fully worked out, fits each cell):**
  `phase | sinA | sinB | sinTab | sinInt | cosA | cosB | cosTab | cosInt | emit`
    * fold_a (per ch): phase → frac (=(phase&0x3FF)<<5), neg (=phase>>15), fidx
      (=phase>>10). ~7 instr, fits trivially.
    * fold_b: fidx → idx, idxB. loc=fidx&15; mir=(fidx>>4)&1; if mir loc=16−loc;
      idx=loc; idxB=loc+1−2·mir. ~18 instr + 4 data + 2 state → gap 8 ≥ 3. Fits.
    * tab: LOAD table[idx]→write valA; LOAD table[idxB]→write valB (write each
      straight from R0, NO state). 7 instr + 18 data + 2 input → gap 4 ≥ 2. Fits.
    * interp: mag = valA + (valB−valA)·frac (SUB, MULQ frac, ADD). ~6 instr, 1
      state, 3 input. Fits easily.
    * emit: apply neg sign + amplitude MULQ to cos_mag & sin_mag; `{write:yi}`
      (cos) `{write:yq}` (sin). frac/neg PASSTHROUGH-plumbed fold_a→…→interp/emit.
  10 cells folds ≤8 across (e.g. 5×2, INV-9). This is the largest tier-1 block by
  far; the remaining work is mechanical (write the 10 cells + the frac/neg
  passthrough ports + iterate build→route→sim) but substantial.
- **OPEN design decision (worth review):** 37 LSB is the 17-entry (idx_bits=6)
  linear-interp floor — defensible as a documented table-NCO limit (cf. the IIR
  3–160 LSB), but coarse for a SOURCE. A 33-entry table (idx_bits=7, ~10 LSB) or
  65-entry (~4 LSB) needs an even bigger cross-cell table. Pick the precision/cell
  tradeoff before finishing the build.

---

## NCO / ComplexMixer — de-risked build design (still planned, NOT blocked) 2026-06-25

SoftDemod (the third block of the older note below) is now DONE. The remaining two
tier-1 complex blocks are FEASIBLE (no ISA wall) but are each a full block-build —
larger than the firdes/SoftDemod steps. This note records the CONCRETE, measured
design so the next iteration builds without re-deriving.

- **The golden is EXACT FLOAT.** Measured: GNU Radio `analog.sig_source_c(fs,
  GR_COS_WAVE, f, amp)` matches `amp·exp(jθ_n)` to **0.002 LSB** (it uses a
  high-precision NCO, not a coarse table). So the Kyttar NCO's table+interp error
  is the WHOLE error vs GR — the tolerance is the table-approximation bound
  (derived, documented like the IIR pole-precision limit), not a quantization
  excuse. (Use a `blocks.head(sizeof_gr_complex, N)` to bound the free-running
  source or `tb.run()` never returns — cost real time.)
- **Phase starts at 0.** GR's first output (n=0) is `(amp, 0)` = `amp·(cos0, sin0)`
  — phase 0, THEN increment. The CURRENT NCOBlock increments phase BEFORE the
  first output (`phase = phase + freq_word` then look up), so its n=0 is at
  phase=freq_word — a one-sample PHASE OFFSET vs GR. Fix: emit at the current
  phase, increment after (init phase=0).
- **Interpolation is mandatory and PROVEN.** Linear interpolation on the phase
  fraction, quarter-wave table with symmetry. Measured max error vs exact (amp
  0.9), `idx_bits` = phase MSBs used for the table index:
    * idx_bits=6 (17 quarter entries — the CURRENT table size): **37 LSB** (vs
      ~1600 with no interp — interpolation alone is a 40x win on the same table).
    * idx_bits=7 (33 quarter entries): **10 LSB**.
    * idx_bits=8 (65 quarter entries): **4 LSB**.
  33 entries just exceeds a 32-word cell, so idx_bits≥7 puts the table across ≥2
  cells (cross-cell interp, intricate). idx_bits=6 fits one cell but 37 LSB is
  coarse for a SOURCE (0.1% amplitude). Pick the table size for the target derived
  tolerance and document it as the table-NCO floor.
- **Output is COMPLEX (I=cos, Q=sin).** Emit BOTH from the output cell as two
  WRITEs but wire only ONE net to x16_out (the harness de-interleaves
  `[yi,yq,yi,yq]`); wiring a second net silently zeros egress (HARNESS note below).
  cos = sin(phase + 90°) = sin(phase + 16384), so the datapath does TWO
  symmetric+interpolated lookups per sample.
- **Harness: NCO is a complex SOURCE.** Input is just a trigger (value ignored).
  `run_block_dut_complex` drives two input regs; an NCO needs a single trigger in +
  two output words. Either extend the complex driver for a 1-in/2-out source, or
  drive via `run_block_dut` (single trigger) and read 2 words/sample, de-interleave.
- **Params (Decision A):** `sample_rate`, `frequency` (Hz), `waveform`, `amplitude`;
  derive `freq_word = round(frequency/sample_rate·65536)` internally; label "Signal
  Source". Verify on GRID-ALIGNED frequencies (integer freq_word) to isolate the
  table floor from the freq_word-vs-exact-f drift (fs/65536 Hz resolution, drift
  grows with n — document separately).
- **Blast radius is SMALL (checked).** `IQUpconvertBlock`, `ComplexMixerBlock`,
  `CostasLoopBlock`, `ComplexCostasLoopBlock` carry their OWN embedded `freq_word`
  NCO — they do NOT construct `NCOBlock`, so refactoring NCOBlock's signature does
  not touch them. The one geometry test that names NCOBlock
  (`test_data_words::test_abutting_handoff_resolves_entry_and_dest`) uses
  `catalog.resolved_io(...)` for the EXPECTED entry/in_reg, so it is robust to
  NCO internals as long as NCO keeps a single trigger INPUT register.
- **ComplexMixer = multiply_cc(signal, sig_source_c)** — a frequency shift
  `in·exp(jθ_n)`, reusing the NCO's complex exponential (4 MULQ for the complex
  product). BUILD THE NCO FIRST.

---

## NCO / ComplexMixer / SoftDemod — analysis + harness gap (not yet built) 2026-06-24

These three remaining tier-1 blocks are FEASIBLE but each needs infrastructure
the current harness lacks; analysis is captured here + in the manifest so the next
run resumes without re-deriving. They are NOT blocked (no ISA wall like the IIR) —
they are larger than one autonomous step at the production-quality bar.

- **Shared gap — a COMPLEX / multi-channel verification harness.** `run_block_dut`
  is real-only (one i16 in, `read_port_i16` out). Complex blocks carry I/Q on two
  registers/channels (input_registers=[0,1] = xi/xq; output written `write yi`
  (ch0) + `write yq` (ch1) + one `jump`, see `complex_rrc_matched_filter_block`).
  A complex DUT path = inject `[I,Q]` (or a trigger for a source), read via
  `read_port_with_channels` → split channel 0=I / 1=Q, compare each. Build this
  ONCE; NCO, ComplexMixer, and SoftDemod all need it (SoftDemod needs complex IN,
  float-LLR out).
- **NCO (analog.sig_source_c).** Measured: `sig_source_c(fs, GR_COS_WAVE, f, amp)`
  = `amp·(cos θ_n + j·sin θ_n)`, `θ_n = 2π f/fs·n` (n=0 → I=amp, Q=0). Must output
  COMPLEX. Param refactor (decision A): sample_rate / frequency(Hz) / waveform /
  amplitude, derive `freq_word = round(f/fs·65536)` (16-bit). **The real work is
  PRECISION:** the existing 64-entry quarter-wave table (no interpolation) is
  ~1600 LSB off GR's exact float sin/cos — not a match. Linear interpolation is
  REQUIRED (64-entry+interp ≈ 40 LSB; 256-entry+interp ≈ 3 LSB — prototype
  confirmed). A 256-entry table (65 quarter words) spans cells (LOAD is per-cell)
  → cross-cell interpolation, intricate. Also the 16-bit freq_word DRIFTS vs GR's
  exact frequency and the drift GROWS with sample index — verify on grid-aligned
  frequencies (integer freq_word) to isolate the table floor; document the off-grid
  freq resolution (fs/65536 Hz) separately.
- **ComplexMixer (multiply_cc + sig_source).** The existing block is a REAL mixer
  (`in·cos`), NOT a complex multiply → does not match GR. `multiply_cc` is the full
  complex product `(ac−bd)+j(ad+bc)` (4 MULQ); the fused convenience block =
  `multiply_cc(signal, sig_source_c)` = a frequency shift `in·exp(jθ_n)`, so it
  reuses the NCO's complex exponential. Build the NCO first.
- **SoftDemod (constellation_soft_decoder_cf).** Emits approximate LLRs (soft bits)
  from complex symbols; the metric is on the soft values, and the GR soft decision
  depends on the constellation object — characterize it empirically before building.
  Build after NCO/ComplexMixer.

---

## DecimatorBlock — GR fir_filter_fff(M,taps) = FIR + emit-every-M 2026-06-24

- **Status:** PASS / DONE vs GNU Radio `filter.fir_filter_fff(M, taps)`, 25 tests;
  full verification suite 93/93; placekyt suite 937 passed / 16 skipped.
- **A decimator IS an FIR + a mod-M emit gate.** GR's `fir_filter_fff(M, taps)`
  emits the full FIR sampled at PHASE 0 — `y_full[0::M]` (confirmed: it equals
  `fir_filter_fff(1,taps)[0::M]`). So DecimatorBlock SUBCLASSES the verified
  FIRFilterBlock: every wavefront cell runs each input sample (delay line /
  partial forwarding / headroom saturation all inherited), and ONLY the last
  cell's OUTPUT is gated by a counter (start M-1, emit when it hits M, reset). The
  block emits on input samples 0, M, 2M, … → aligns with GR at delay 0.
- **Reuse, don't reimplement.** Non-last cells come VERBATIM from
  `super().build_cell_programs()`; only the last cell is rebuilt to splice in the
  counter (so its register allocation accounts for the extra data/state). The
  bit-exact reference is the inherited `process_reference_q15` decimated `[::M]`.
- **The counter + the headroom restore must SHARE the last cell.** The FIR's
  bias-and-shift restore (~9 instrs + 2 data) does NOT fit beside the counter
  (~8 instrs + 2 data + state) — a 13-tap S=1 decimator failed to build. Fix: the
  decimator restores the gain with the CHEAPER DOUBLING-saturate (`ADD R0,R0` +
  `BR.NV +2; SHR R0,#15; SUB satneg,R0`, S times) — the FIR docstring's
  alternative, bit-identical to `clamp(acc·2^S)` so the inherited reference STILL
  predicts the DUT exactly, but cheap in fixed overhead. With it the restore +
  counter coexist for the small S a decimation filter needs.
- **S=1 is the COMMON case, not an edge.** A normalized anti-alias low-pass has
  Σ taps = 1 but Σ|taps| slightly >1 (sidelobes) → `S=ceil(log2 Σ|h|)=1`. So the
  decimator MUST support S>0 (an S=0-only block would reject most real filters).
- **Harness: decimated output via the per-sample None pattern.** `run_block_dut`
  records `None` for the silent (non-emit) inputs, so the emitted stream is
  `dut.outputs_q15[::M]` and a dead block still fails (a real test asserts
  `emitted iff index%M==0`). Aligns with GR at delay 0.
- **Budget caps (re-derived against the allocator).** Counter+restore shrink the
  last cell's tap room with S: single-cell ceiling 4 (S=0) / 2 (S=1); multi-cell
  last cell 3 (S=0) / 2 (S=1) / 1 (S=2). `_segment_offsets` is overridden to
  ALWAYS cap the last cell (it always has the counter).
- **KNOWN LIMIT (guarded).** Σ|h| > 4 (head_shift > 2) raises a clear ValueError
  — the doubling restore (4 instrs × S) no longer fits beside the counter. Every
  realistic anti-alias decimator (normalized, or up to ~4× gain) is covered; a
  bigger-gain filter scales the taps down or uses FIR+gain ahead of
  decimate-by-[1.0]. (`test_decimator_excess_headroom_raises`.)
- **Param rename:** `decimation_factor` → `decimation` (matches the GRC yaml and
  GR's `decim`; the old yaml `make` passed `decimation=` to a `decimation_factor`
  constructor — a latent import mismatch, now fixed). Updated callers:
  `modem_110b_demo.py`, the `.kyt` demo, the `gr-kyttar` `decimator.py` wrapper.

---

## IIRBiquadBlock — BLOCKED: recursive Q15 needs accumulator guard bits 2026-06-24

- **Status:** BLOCKED (ISA/datapath limitation → out of autonomous scope per the
  guardrail). No block source changed; only the manifest + this note.
- **Manifest factory was wrong:** GR has NO `filter.iir_filter_fff`. The real
  factory is `filter.iir_filter_ffd(fftaps, fbtaps, oldstyle)` (Direct Form I).
  `oldstyle=False` (scipy/Matlab) is `y[n]=Σff·x[n-i] − Σ_{j≥1} fb·y[n-j]` with
  `fb[0]=a0` — exactly the block's `b/a` convention. Corrected the grc_block.
- **Root cause — no accumulator guard bits.** A Direct-Form biquad's feedback
  term `a1·y` has `|a1|` up to ~2 (`a1 = −2cos(ω)/a0`; `<2` but routinely `>1`
  even for gentle low-pass) and `|y|` up to ~1, so `−a1·y` reaches **~2.0** — not
  representable as a Q15 partial, and it overflows the 16-bit accumulator
  mid-chain. The 16-bit cell ALU has no guard bits.
- **Why the FIR fix doesn't transfer.** COEFFICIENT HEADROOM (INV-13) pre-scales
  the accumulator and restores at the end — but a recursive filter must store the
  fed-back `y` at FULL Q15 scale to recurse correctly, so the feedback path can't
  be pre-scaled. And no accumulation ORDER fixes it in general: splitting `a1`
  into two halves and interleaving the `a2` subtraction keeps partials in range
  for some low-fc/low-Q filters but OVERFLOWS for fc≥0.25 / Q≥2 / etc. (measured).
  The V flag is not sticky (INV-13) so a per-term saturate can't catch the
  mid-chain wrap either.
- **Secondary limits.** A resonant filter's output `|y|` itself exceeds 1.0 and
  saturates where GR float doesn't (fc=0.15, Q=5 → |y|=2.3). And the EXISTING
  block is independently broken: it clamps a-coeffs to [−1,1] (`min(1,max(−1,a))`),
  destroying any real biquad with `|a1|>1`.
- **What it needs / when to revisit.** Accumulator guard bits (a wider recursive
  accumulator, e.g. Q15 + 2–3 integer guard bits) in the cell ALU — a simKYT/.so
  (Rust) ISA change, out of scope for an autonomous run. NOTE the recursive Q15
  PRECISION itself is fine in-range (prototype max err ~1.6–9 LSB for pole radius
  up to ~0.92), so once guard bits exist this is a normal empirical/pole-tolerance
  + zero-input-limit-cycle verification, not a redesign.

---

## DCBlockerBlock — GR dc_blocker_ff is an FIR (reuse the datapath) 2026-06-24

- **Status:** PASS / DONE vs GNU Radio `filter.dc_blocker_ff`, 28 tests; full
  verification suite 68/68; placekyt GUI/engine suite 937 passed / 16 skipped.
- **The key insight — dc_blocker is LTI, i.e. a SYMMETRIC FIR.** Reverse-
  engineered from GR's impulse/step response (no source needed): SHORT form
  (`long_form=False`) = `x[n-(D-1)] - MA_D²(x)` (TWO cascaded length-D moving
  averagers → a triangular kernel, `2D-1` taps, group delay `D-1`); LONG form
  (`long_form=True`, GR default) = `x[n-(2D-2)] - MA_D⁴(x)` (FOUR cascaded,
  `4D-3` taps, group delay `2D-2`). The subtracted MA cascade has unit DC gain
  and the delayed-impulse minus it gives `Σtaps = 0` (a true DC notch). Confirmed
  bit-for-bit (float, <1e-4) against GR for D∈{2,4,8,16,32}, both forms. So
  DCBlockerBlock just **SUBCLASSES FIRFilterBlock** with these taps — zero new
  datapath, all the headroom/saturation/fold machinery inherited. (This is the
  "reuse existing datapaths" mandate paying off — like the queued firdes filters.)
- **Params mirror GR's GRC `dc_blocker_xx` VERBATIM:** `length` (GR's `D`,
  default 32) and `long_form` (default True) — NOT the old POC's `alpha` (a
  totally different one-pole IIR; the prior block did not match GR at all).
- **Taps are SYMMETRIC** ⇒ the FIR's reversed-tap convention is moot; pass them
  straight through. And both DUT and GR carry the same group delay ⇒ compare at
  `delay=0` (as for fir_filter).
- **Tolerance — headroom-aware, DERIVED not loosened.** dc-blocker taps have
  `Σ|h| ≈ 1.5..2` ⇒ COEFFICIENT HEADROOM (INV-13) always engages with **S=1**
  (coeffs scaled by ½, saturating-shift restore ⇒ the block SATURATES on
  overload, no rollover). S=1 costs ~1 bit of coefficient precision, so the plain
  `N+1` floor is too tight (it false-failed by ~N/8). Added a headroom term to
  `q15_quant_floor(op_count, head_shift=S)` = `N·(2^(S-1)+1)+1` (=`2N+1` at S=1):
  each tap can carry up to `2^(S-1)` LSB of coeff-quantization error from the ½
  scaling ON TOP of its ~1 LSB MAC truncation. A real fixed-point worst case
  (empirically bounds the error with ~18% margin), not a tuned number. Verified
  two-tier exactly like the FIR: DUT vs GR float (amplitude, headroom floor) AND
  DUT vs `process_reference_q15` (EXACT, models the saturating datapath).
- **Latent FIR `_fold_geometry` bug found & fixed (n=26).** The GR default
  (length=32, long_form=True) is 125 taps = **26 cells**, a count the FIR's own
  tests never hit. The even-column-preference fold scanned `H=FOLD_HEIGHT..1` and
  took the first H dividing n with an even quotient — for n=26 the ONLY such H is
  **1**, giving a **26×1 line** that runs off the 10-wide array (`unplaced_cell
  outside fabric`). Fix: cap the accepted even-column fold to `≤ MAX_CELLS_ACROSS
  = 8` (INV-9); when none qualifies, fall through to the compact fold (n=26 →
  7×4). Changed NO FIR-tested geometry (their even folds are all at tall H, ≤8
  wide). Refined INV-14.
- **INV-11 extended to the GUI port-stub/flyline renderer.** `chip_canvas`
  resolved port geometry via `port_cell_provider(type, library)` WITHOUT params,
  so a params-scaled block (FIR/DC blocker, output on the LAST cell) collapsed to
  its 1-tap default (output on cell 0) → for a placed multi-cell instance the
  output stub landed on a non-existent cell and silently vanished (and an
  out↔out wiring test couldn't find the stub). Threaded `blk.params` through a new
  arity-tolerant `_port_cells_for` helper (3-arg provider, 2-arg fallback) at all
  three call sites. Same root cause as INV-11, new surface (the GUI, not the
  router).
- **Blast radius / callers (the guardrail).** Making DCBlocker a GR-faithful
  FIR changed its default footprint 1→26 cells, which ~12 placekyt test files use
  as a SMALL fixture (geometry-sensitive corridor/abutment assertions). Fixed
  every caller to a 1-cell instance (`length=2, long_form=False`) so those
  fixtures are byte-for-byte unchanged in geometry; updated the two param-aware
  tests (editable_params now `{length, long_form}` both topology-changing;
  resolved_io/footprint closures made params-aware) and the `kyttar_dc_blocker`
  GRC import fixture (alpha → length/long_form). No tolerance or test weakened.

---

## FIRFilterBlock — COEFFICIENT HEADROOM saturation (the keeper) 2026-06-24

- **Why the prior fixes were wrong — the V flag is NOT sticky.** End-only clamping
  (entry below) and per-cell clamping BOTH ROLL OVER on a high-gain filter: a sum can
  overflow a mid-chain `MACQ` and WRAP BACK into range by the final op, so the final
  op's V flag reflects nothing and the clamp misses the overflow. Proven on the real
  build → auto-route → simKYT path: a 40-tap all-0.5 FIR (gain 20) on a steady 0.9
  input emitted `[…0.9, −0.875…]` — a sign-flipping wrap mess — instead of pinning at
  +1.0. Per-TAP clamping fixes it but collapses TAPS_PER_CELL to 1 (40-tap → 40 cells):
  rejected.
- **The keeper — COEFFICIENT HEADROOM (accumulator scaling), user-mandated.**
  `S = max(0, ceil(log2 Σ|coeff|))`. Scale every coeff by `2^-S` before Q15 (store the
  SCALED coeffs as `_coeff_q15`; keep originals for the float ref). Now `Σ|scaled| ≤ 1`
  ⇒ the accumulator is in range at EVERY tap and EVERY cell — intermediate wrap is
  IMPOSSIBLE. Restore the gain at the very END with ONE SATURATING left shift by S
  (single cell: after the last MACQ; multi-cell: on the LAST cell after its final ADD).
  Normalized filter (Σ ≤ 1) → S=0, a NO-OP: identical to a plain Q15 FIR, bit-exact GR.
  Promoted to (rewritten) **INV-13**.
- **SHL doesn't set V → the restore can't use a V-flag clamp.** Detect shift overflow
  in O(1) instr with a bias-and-shift test: `acc<<S` overflows iff
  `(acc + 2^(15-S)) >> (16-S) != 0` (logical), then pin to the rail of the ORIGINAL
  sign via `0x7FFF + signbit` (one `0x7FFF` word gives both +0x7FFF and −0x8000).
  Exhaustively verified == `clamp(acc·2^S)` for all acc, S∈0..15. A doubling-loop
  (`ADD R0,R0` ×S + `BR.V`) also works but its 2·S instructions overflow the last
  cell's budget at large S — the bias-and-shift is constant-cost.
- **Build-engine GOTO gotcha (cost real time).** A `GOTO`/branch whose target LABELS a
  `{write}`/`{jump}` placeholder is miscompiled — the engine rewrites it with the
  placeholder's OUTPUT routing (it becomes a stray output JUMP), corrupting control
  flow. (Confirmed latent in SquelchBlock's `GOTO update` too — its tests just never
  exercise that arm.) FIX: branch to a label on a REAL instruction and use a two-path /
  duplicated-`{write}` + terminal `HALT` structure (the in-range path's HALT is
  REQUIRED — a remote JUMP does NOT stop local execution, else it falls into the sat
  block and double-emits). This was THE reason the first headroom build pinned at
  startup (the GOTO had turned the in-range path into a premature output emit).
- **Budget / fold.** S=0 is UNCHANGED (TAPS_PER_CELL=5, MAX_SINGLE_CELL_TAPS=6; 20-tap
  =4 cells, 40-tap=8, 64-tap=13). For S>0 the last multi-cell cell caps its segment at
  3 taps (budget `4L+18≤32`) and the single-cell ceiling drops to 4 (`4N+16≤32`), so a
  high-gain FIR may use one extra cell: a 40-tap gain-20 (S=5) FIR is **9 cells**.
  `_segment_offsets()` is the single source of the fold (caps + rebalances the tail to
  [1,3] when S>0); `cell_count`, layout, build and the reference all derive from it.
- **Reference.** `process_reference_q15` accumulates the SCALED coeffs (wrapping, never
  leaves range) then applies `_sat_shl` — bit-exact with the datapath (DUT==ref EXACT,
  single + multi-cell, including the gain-20 overdrive pinning at +0x7FFF with no sign
  flip). In-range GR-match asserts on NORMALIZED taps (Σ≈0.95 < 1 ⇒ S=0 deterministic,
  no headroom precision loss; a near-unity Σ that rounds to S=1 loses ~1 bit and would
  exceed the per-tap LSB tol).
- **Result:** 27/27 FIR tests pass; full verification suite 40/40; placekyt GUI/engine
  suite 930 passed / 13 skipped (baseline); `test_data_words::test_multicell_fir_flows
  _correctly` green.

---

## FIRFilterBlock — END-ONLY saturation correction + budget restored 2026-06-24

- **What was wrong:** the first saturation cut (entry below) clamped R0 after
  EVERY MACQ tap (a 3-instruction clamp per tap). That (1) exploded the cell count
  — TAPS_PER_CELL collapsed 5→2 and the single-cell ceiling 7→3, so a 20-tap FIR
  went from ~4 cells to ~10 — and (2) altered the math: clamping intermediate
  partial sums re-normalises legitimate mid-sum excursions and MASKS real overload
  (an overdriven filter produced a clean rescaled sinusoid, not flat-topped rails).
- **The correction (user-confirmed):** clamp the accumulator ONCE, on the FINAL
  accumulation, just before the output WRITE — the last MACQ in a single cell, or
  the cross-cell ADD on the LAST multi-cell cell. Every intermediate tap and every
  cross-cell partial is left WRAPPED; the whole chain is one logical accumulator
  and only its final value is saturated. The `_clamp_lines` helper (BR.NV +2 /
  SHR R0,#15 / SUB satneg,R0) and the priming-MULQ-not-clamped rule are unchanged;
  only the PLACEMENT moved (per-tap → once at the end). Promoted to **INV-13**.
- **Budget RESTORED (re-derived against the resolver's own allocator, not guessed):**
  probed real builds across tap counts — `MAX_SINGLE_CELL_TAPS 3→6` (N=6 fits the
  32-word cell, N=7's 7th delay reg has no free gap register; one below the old
  wrapping FIR's 7 because the single end-only clamp costs one tap) and
  `TAPS_PER_CELL 2→5` (a MID cell — the densest role, with old_save — fits at L=5,
  overflows at L=6; the LAST cell carries the clamp but has NO old_save reg, so a
  FULL L=5 last segment + clamp still fits). 20-tap FIR is **4 cells** again; 64
  taps = 13 cells (same footprint as the original wrapping FIR).
- **Q15 reference fixed to END-ONLY:** `process_reference_q15` now WRAPS every
  intermediate (`_wrap_acc`) and applies the single saturating clamp
  (`_clamp_final`) only to the final op — bit-exact with the datapath (DUT==ref
  EXACT, 0 LSB, single-cell + multi-cell, 2..64 taps). The old `_sat_acc`
  per-step clamp was removed.
- **Latent single-cell delay-orientation bug found & fixed:** the single-cell
  builder shifts so `d0`=OLDEST (`MOVE d{i},d{i+1}` then `MOVE d{N-1},sample`) and
  multiplies `d{i}*c{i}`. The old reference shifted newest-first (`[s]+delay[:-1]`)
  with `c0` on the newest — REVERSED. It was never caught because single-cell was
  capped at 3 symmetric taps AND the single-cell path was only ever gated DUT-vs-GR
  (free Q15 rounding tolerance), never DUT-vs-reference EXACT. With the ceiling now
  6, an asymmetric 4/5/6-tap single-cell EXACT compare exposed it; fixed to shift
  `delay = delay[1:] + [newest]` (delay[i]==d{i}). (INV-12 sharpened: a wider
  single-cell range exercised a path the narrow one never did.)
- **Overload test now genuinely shows rails (the bug was it DIDN'T):** because of
  the END-only corner case (intermediate wrap can bring the final op back in
  range), the old transient/alternating overload stimulus did NOT pin at the rails
  — the saturating reference matched a plain wrapping output and the mutation was
  vacuous. New stimulus drives the FINAL op into overflow (2-tap [0.9,0.9] steady
  0x7FFF/0x8001 → single MACQ is the clamped op; 7-tap / 13-tap steady large
  input → last cell's ADD overflows): DUT pins ≥half its outputs at ±FS and
  matches the reference EXACTLY. The wrap-mutation uses the same 2-tap overload so
  wrap (no final clamp) ≠ end-only-clamp, and asserts the gate REJECTS it (with a
  vacuity guard that the reference actually saturates). Deep-cell mutation now
  perturbs a tap owned by the LAST cell (segments are assigned from the END of the
  tap array → last cell owns the FIRST indices).
- **Routing wall moved (restored footprint):** with K=5 the wall is back near the
  original ~200 taps / 40 cells (placement-noisy in the 41..63-cell band); 64 cells
  (320 taps) fails reliably with "no free corridor". `ROUTING_WALL_TAPS 96→320`.
- **Result:** 26/26 FIR tests pass; full verification suite 39/39.

---

## FIRFilterBlock — SATURATION fix (Q15 overload) 2026-06-24

- **The bug:** the multi-cell FIR let the Q15 accumulator WRAP on signed overflow
  (modulo 2^16) — which flips sign on overload and produces garbage. GNU Radio's
  `fir_filter_fff` is FLOAT and never overflows, so the only correct fixed-point
  equivalent is a SATURATING accumulator (clamp to ±full-scale), as every
  production fixed-point FIR does (TI C5x/C6x). Under full-scale random input the
  chained partial sums overflow and the old block returned corr ~0.5–0.8 vs a
  correct saturating reference.
- **The fix — per-step software clamp:** the ALU has no auto-saturating mode;
  MACQ/ADD WRITE the wrapped value but set the V (signed-overflow) flag. On
  overflow the wrapped result's sign (N) is INVERTED vs the true sum, so the
  3-instruction clamp **`BR.NV +2 ; SHR R0,#15 ; SUB satneg,R0`** computes
  `0x8000 − (R0>>15)` = `N? 0x7FFF : 0x8000` — exactly the right rail. One branch
  on the hot path, two instructions on the (rare) overflow path, ONE shared
  `satneg=0x8000` data word per cell. Verified bit-exact vs a true clamping
  accumulator over millions of random cases AND against the live simulator.
- **DO NOT clamp the priming MULQ.** A single Q15 product `(a·b)>>15` is always
  representable, but **MULQ sets V from the RAW 32-bit product** (which almost
  always exceeds i16). Clamping on it saturates spuriously — the first cut did,
  pinning every output at the rails even in-range. Clamp only the running MACQ
  taps and the cross-cell partial ADD (whose V truly signals acc overflow).
- **Budget/fold impact (INV-7/9):** the clamp costs ~3 extra instrs/tap, so the
  per-cell register budget fills far sooner. Re-derived with the resolver's own
  allocator: single-cell ceiling **7 → 3 taps**, **TAPS_PER_CELL 5 → 2** (a mid
  cell at L=3 overflows the 32-word cell; L=2 fits first/mid/last). `satneg` must
  get an EXPLICIT address (after the coeffs) — an auto address packs at 0 = R0
  and corrupts the accumulator; `partial_reg` shifted +1 to account for it.
- **The verified range moved (more cells/tap):** 64 taps is now 32 cells (was 13)
  but still routes (FOLD_HEIGHT=4 serpentine = 8 wide). The routing wall dropped
  from ~400 taps to **96 taps / 48 cells** ("no free corridor"); 80 taps / 40
  cells still routes. Guard test updated to 96 (the `corridor` reason string is
  unchanged so the check still matches).
- **Reference = bit-exact predictor, not the float ideal (INV-3 sharpened):**
  `compare_against_grc`'s `_saturate_ref_q15` only clips the FINAL value, not
  each step, so it cannot predict a per-step-saturating DUT once an INTERMEDIATE
  sum overflows. Added `process_reference_q15` which models (a) the per-step
  clamp and (b) the CELL-ACCURATE wavefront: each cell holds its own segment
  delay line, ingests the PREVIOUS cell's shifted-out oldest sample (the inter-
  cell forwarding IS the delay — a naive global-delay-line index is WRONG, it
  failed at corr 0.86 on asymmetric taps while the DUT held corr 1.0 vs GR). The
  scaling/overload/deep-cell gates compare the DUT against this reference EXACTLY
  (Metric.EXACT, 0 LSB). A separate test proves the saturating reference equals
  GR-float-clipped where no overflow occurs — so it is real DSP, not circular.
- **Mandatory mutation (INV-4):** `test_fir_overload_wrap_mutation_fails`
  synthesises the OLD wrapping output for an overload case and asserts the gate
  REJECTS it — a gate that can't tell saturate from wrap certifies the bug.
  `test_fir_overload_saturates` additionally asserts the DUT outputs are pinned
  at the rails (proof it clamped, not coincidentally landed in range).

---

## FIRFilterBlock — verified (2..64 taps) 2026-06-24

- **Status:** PASS / DONE. Verified vs GNU Radio `filter.fir_filter_fff` from 2 to
  64 taps (the headline target) within the derived per-tap tolerance (op_count =
  tap count → tolerance = taps+1 LSB). 1-7 taps single-cell; 8+ a multi-cell
  chained partial-sum (systolic) wavefront. Coverage: edge + 3 random seeds +
  single-cell sweep 2..7 + multi-cell sweep {8,9,13,16,32,64} + 4 mutations
  (inverted, wrong-taps, +1 delay, **deep-cell tap**). Result: corr 1.0000,
  error well inside tolerance (e.g. 64-tap: 40 LSB of 65). Probing shows the same
  design stays correct to ~360 taps (72 cells).
- **GR convention (unchanged):** `fir_filter_fff` convolves latest-sample-first —
  pass `reversed(coefficients)` to the reference. The single-cell datapath and
  the multi-cell datapath BOTH match this; keep them on one convention.
- **Two substrate bugs fixed (promoted to invariants):**
  - **Multi-cell egress (INV-11):** the auto-router/placer resolved the block's
    PortMap from the bare type (default = single-cell), so a 13-tap FIR routed
    its output from cell 0, not the real last cell → no egress. Fix: thread
    `block.params` into PortMap resolution across autoroute/bus_router/controller
    AND the autoplacer footprint/port-map providers (an arity adapter keeps old
    2-arg providers working).
  - **Single-cell budget (INV-7):** the old `<=12 taps => 1 cell` threshold
    overflowed the ~31-register cell at 8 taps. Real ceiling is 7; 8+ now fold to
    multi-cell.
- **The bug the OLD 'green' suite hid (INV-12):** the borrowed RRC multi-cell code
  reversed each coefficient SEGMENT — correct only for SYMMETRIC taps. The prior
  suite used EDGE (10 samples) + uniform positive taps, so the deep cells never
  saw data and the mis-ordering cancelled. Under >2*ntaps random input with
  asymmetric taps even an 8-tap (2-cell) FIR failed (corr ~0). Fix: FIR now has
  its OWN multi-cell builder; each cell takes `coeff[N-offset_{m+1} : N-offset_m]`
  in FORWARD order (derived from the cascaded-delay structure, validated against
  the single-cell datapath in float before touching the chip).
- **Layout FOLD (INV-8/9/10) — the GUI revealed it; the harness hid it:** the
  base-class auto-snake laid 8 cells as a 1x8 LINE, so input and output sat on
  OPPOSITE edges → the single bus can't tap both → in GUI place+route the block
  built but the gain→FIR net would not route (a flyline), even though the headless
  verification harness "passed" (it injects/drains directly, not via the bus).
  Fix: FIR now authors an explicit `default_layout` — a column-major serpentine
  fold (down a column of FOLD_HEIGHT=4, over one, up the next). 40 taps (8 cells)
  → the canonical **2x4** with input @(0,0) and output @(1,0) SIDE BY SIDE on one
  edge → `portmap.io_colocated=True`, and the bus taps both. Consecutive cells
  stay adjacent so the wavefront forwarding is unchanged (verification still 21/21).
  LESSON: a headless DUT-vs-GR pass does NOT prove a block places+routes in the
  real GUI/bus flow — verify both. (Now in layout_rules.md + INV-8/9/10.)
- **Known limit (guarded, genuine substrate wall):** ~400 taps (80 cells) exceeds
  the 10x12 array's routing capacity (≤8 cells across per INV-9). The folded
  footprint can't leave a bus channel. `test_fir_routing_capacity_limit` asserts
  it fails to route; flips if the array grows. NOT a tap cap faked to pass.
- **Method note:** model the datapath in plain float FIRST (single-cell vs
  multi-cell) to localise a structural index bug in seconds, before paying for
  build+sim+GNU-Radio round trips.

---

## GainBlock — verified 2026-06-23

- **Status:** PASS. Edge + 3 random seeds + gain sweep {0.25, 0.5, 0.75, 0.9}.
- **Metric:** amplitude, delay=0, op_count=1 → derived tolerance 2 LSB.
- **Result:** max_abs_err 1 LSB, NMSE ~-90 dB, corr 1.0000. The 1-LSB error is
  correct Q15 rounding of a single MULQ (e.g. 0x7FFF*0.5 = 0x3FFF).
- **Mutation tests:** inverted output, wrong gain, +1 sample offset, empty output
  all correctly FAIL the gate.
- **Gotcha:** hit the placement-dependent hop-count trap (zero output) before the
  fix — see invariants.md INV-1. GainBlock is the template for feed-forward,
  single-cell, single-MULQ blocks.

---

## AGCBlock — verified 2026-06-26 (params reworked to GRC-verbatim)

- **Status:** PASS vs `analog.agc_ff`. rate sweep {0.01,0.02,0.05}, reference
  sweep {0.2,0.3,0.5}, mutation (inverted, wrong-reference, empty).
- **Metric:** amplitude, recursive-loop tolerance 80 LSB (observed ~39),
  head_shift=40 to trim the loop start-up transient.
- **GRC-PARITY REWRITE (the headline):** the old AGC had a non-GR model
  (target/attack_rate/decay_rate). GNU Radio `agc_ff` is single-rate proportional:
  `out=in*gain; gain += rate*(reference-|out|); clamp to (0,max_gain]`. Rewrote the
  block + reference + cell program to mirror that VERBATIM (params rate, reference,
  gain, max_gain). A GRC agc_ff design now ports with zero friction. ~9 placeKYT
  tests referenced the old params (test_build/cli/model/catalog/project_io) — all
  updated; the param set is part of the block's contract, so renaming it ripples.
- **Q15 LIMIT (documented, not a bug):** the gain register is Q15 [-1,1), so the
  block is faithful only in the ATTENUATING regime (gain<=1 — strong signal driven
  down to reference). True amplification (gain>1, weak signal pulled UP) overflows
  int16 and wraps. Needs a gain register with integer headroom (e.g. Q8.7) — out of
  scope for the single-cell Q15 block. Tests bound max_gain<=1 and drive a strong
  signal. Same class of constraint as the IIR sharp-pole limit.
- **CELL GOTCHA:** computed |out| into R0 then immediately overwrote R0 with
  `reference` before subtracting — discarding the abs. A dual-face/multi-step cell
  must stash an intermediate (added `abs_save` state) before reusing R0. Always
  trace the actual register at each step, not the intent.

---

## SquelchBlock — verified 2026-06-26 (params reworked to GRC-verbatim)

- **Status:** PASS vs `analog.pwr_squelch_ff`. threshold sweep {-20,-15,-12 dB},
  alpha sweep {0.05,0.1,0.2}, mutation (inverted, no-gating, empty, unsupported-raise).
- **GRC-PARITY REWRITE:** old squelch had a non-GR model (threshold/hysteresis/
  attack_alpha/release_alpha). GNU Radio pwr_squelch is POWER-based: pwr=(1-alpha)*
  pwr+alpha*|x|^2; gate at 10^(db/10). Reworked to mirror pwr_squelch_ff verbatim
  (db, alpha, ramp, gate). db is a dB threshold; derive the linear power threshold.
- **GATED-BLOCK VERIFICATION (the lesson):** a squelch is a GATED-amplitude block,
  not a bit decision. Raw AMPLITUDE comparison FAILS on a single gate OPEN/CLOSE
  transition sample (one side emits the sample, the other emits 0 → ~full-scale diff)
  even though every other sample matches within 1 LSB. So verify TWO ways: (a) the
  open/closed pattern matches GR except a BOUNDED count of edge-transition samples
  (<=3), and (b) on agreeing samples the amplitude matches the floor. Don't pick a
  threshold INSIDE a section's power (genuinely ambiguous gate → many Q15 flaps);
  choose thresholds that cleanly separate the regimes.
- **UNSUPPORTED params raise (sound failure):** ramp!=0 (sinusoidal envelope) and
  gate=True (drop samples — a chip block emits one out per in) are not implemented
  and raise ValueError rather than silently mis-behave.
- **Report artifact gotcha:** write_report must reflect a PASSING comparison or the
  dashboard shows "fail" for a verified block. Emit the report on the always-open
  (no-transition) case where AMPLITUDE genuinely holds; gate behaviour is gated by
  the separate pattern tests.
## MultiplyBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.multiply_ff` (the generic two-stream real product
  `out=a*b`). 19 tests: edge + 3 random + amplitude sweep {0.25,0.5,0.75,0.9} +
  3-seed bit-exact + overflow-corner + 5 mutations. Single cell, single MULQ.
- **Metric:** amplitude, delay=0, op_count=1 → tolerance 2 LSB; measured 1 LSB,
  NMSE ~-92 dB. Bit-exact vs `process_reference_q15` (the wrapping Q15 MULQ).
- **Two-stream fan-in (reused, not reinvented):** the proven complex-burst broker
  delivers the two streams as one transaction — `WRITE a->R0`, `WRITE b->R1`,
  `JUMP`. Drive it from the verify side with `run_block_dut_complex(in_ports=
  ('a','b'), words_per_sample=1)`, carrying the streams as one complex array
  (real=a, imag=b); the single real product lands in the I channel. No new harness.
- **Q15 overflow is a WRAP, not a saturate:** the only product that overflows is
  the exact `(-1.0)*(-1.0)=+1.0` corner — `(0x8000*0x8000)>>15` = 0x8000 = -1.0
  (the MULQ datapath wraps; its V flag is not sticky and nothing clamps a lone
  MULQ). The bit-exact reference models the wrap; a dedicated test pins the corner
  and asserts DUT==wrap. Keep the GR-equivalence stimulus off the simultaneous
  full-scale-negative corner so the product tracks GR float within the floor.
- **Commutativity:** `a*b == b*a`, so a swapped-stream mutation is NOT a corruption
  — don't test it (documented). The teeth come from a WRONG-second-stream mutation
  (reference built with a different b) + inverted/halved/+1-delay/empty.
- **Gotcha (cost me a build):** a `{write:NAME}`/`{jump:NAME}` placeholder must be
  ALONE on its line — the resolver matches `^\s*\{write:(\w+)\}\s*$` (MULTILINE).
  A trailing inline comment leaves the placeholder unsubstituted; the assembler
  then sees the literal and errors `Unknown opcode: {WRITE:OUT}`. Comments are
  fine on real-instruction lines (the MULQ line), never on a placeholder line.
- **Registration:** a built-in block must be added to `placement/blocks/_modmap.py`
  (`ClassName -> module`) or discovery never finds it (`KeyError: unknown block
  type`). The catalog palette/hidden state then comes from the manifest (a block
  absent from `manifest.json` is resolvable but hidden).

---

## AddBlock / SubtractBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.add_ff` / `blocks.sub_ff` (two-stream real
  combiners). 39 shared tests: edge + 3 random + amplitude sweep + 3-seed
  bit-exact (incl. saturation) + 4 saturation-corner + mutations. Single cell.
- **Metric:** amplitude, delay=0, op_count=1 → tol 2 LSB; measured 1 LSB. Bit-exact
  vs the SATURATING `process_reference_q15`.
- **Saturate, don't wrap (the design call):** the Q15 ALU ADD/SUB WRAPS on overflow
  (0.6+0.6 → -0.8, a sign flip) — unacceptable for a production combiner. ADD/SUB
  set the **V** (signed-overflow) flag, so saturate with a `BR.V` to a clamp path.
  KEY INSIGHT: on overflow the true result's sign is `sign(a)` for BOTH add
  (same-sign operands) AND subtract (opposite-sign: a>0,b<0→+; a<0,b>0→−), so ONE
  `SHR a,#15; ADD R0,satpos` (the shared `0x7FFF+signbit` rail) serves both ops —
  the only difference between the two blocks is the ADD vs SUB mnemonic.
- **Reused the FIR's two-path emit shape** (duplicated `{write}`/`{jump}` + a
  terminal `HALT`, `BR.V` target on a REAL instruction `MOVE R0,Rasav`): a branch
  whose target LABELS a `{write}`/`{jump}` placeholder is miscompiled into a stray
  output JUMP. Save `a` BEFORE the ADD — `ADD R0,R1` overwrites R0 (=input a), so
  the sign test needs a presaved copy.
- **In-range only vs GR:** GR float add has no saturation and unbounded range; once
  |a±b| ≥ 1 NEITHER wrap nor saturate can match a float > 1.0. So the GR-equivalence
  stimulus stays in range (|a±b|<1, where saturate ≡ true sum ≡ GR); saturation is
  proven against the saturating reference + direct corner tests, not against GR.
- **Commutativity asymmetry in the mutation set:** add is commutative (no
  swapped-stream test); subtract is NOT (a−b≠b−a) so swapped-streams IS a tested
  corruption. Both share a WRONG-second-stream mutation for teeth.
- **One module, two GRC blocks:** `add_block.py` defines `_TwoStreamAddSub` +
  `AddBlock`/`SubtractBlock`; both map to the same module in `_modmap.py`. Distinct
  classes keep GRC parity (add_ff and sub_ff are distinct GR blocks).

---

## ComplexToFloatBlock / FloatToComplexBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.complex_to_float` / `blocks.float_to_complex`. 20
  shared tests, EXACT gate, err 0 LSB. Single cell each, shared `_IQPassthrough`.
- **Both are the SAME identity datapath:** on the Kyttar substrate a complex value
  is already a two-operand (re@R0, im@R1) pair, so a complex<->float conversion is
  pure relabeling — read the pair, emit it as two words. No arithmetic → EXACT
  (zero Q15 error). The two GR blocks differ only in GRC port typing, so one
  `_IQPassthrough` base + two thin subclasses keeps GRC parity with no dup.
- **Two-word egress, single cell:** mirror the NCO/mixer emit — declare two output
  ports (`out_re`, `out_im`) + a `trig`, `{write:out_re}` then `{write:out_im}`
  then `{jump:trig}`; the harness wires only the primary (out_re) to x16_out and
  both words ride the one corridor, de-interleaved with `words_per_sample=2`.
  `output_cell_ids()=[0]` for the single cell.
- **Driving it:** `run_block_dut_complex(in_ports=('re','im'), words_per_sample=2)`;
  for complex_to_float the GR side reconstructs `output_complex=[complex(re,im)]`
  from its two float sinks so the comparator checks both channels uniformly.
- **Identity makes EXACT trivially correct:** the harness `_to_q15` and the
  comparator `_saturate_ref_q15` are the same round-and-clamp on the same float, so
  DUT == ref bit-for-bit; EXACT (not AMPLITUDE-with-tol) gives the most teeth.

---

## ComplexToMagSquaredBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.complex_to_mag_squared` (|z|²=re²+im²). 21 tests,
  err 2 LSB / tol 3 (op_count=2). Single cell: `MULQ re,re` + `MACQ im,im`.
- **One-sided saturation is cheaper:** power is ALWAYS ≥ 0, so an overflow (|z|≥1,
  range [0,2) vs Q15 [0,1)) can only push the 16-bit accumulator into the
  negative-looking half (bit15 set). Detect with a single `BR.N _sat` → `MOVE R0,
  0x7FFF`. No sign-rail / `0x7FFF+signbit` math (that's only needed when overflow
  can go either way, as in add/sub). Max sum 32767+32767=65534 < 65536 so it can't
  double-wrap back into the positive half — `BR.N` is exact.
- **Symmetry trims the mutation set:** re²+im² is symmetric in re/im, so a swapped
  channel is NOT a corruption (don't test it). Teeth from inverted (power is ≥0),
  halved, wrong-second-stream, +1-delay, empty.
- **In-range vs GR, full-range vs the reference:** GR float power is unbounded;
  keep the GR-equivalence stimulus inside the unit circle (|z|<1, amp≤0.65) where
  the result is representable, and exercise saturation against the saturating
  reference + direct corner tests. The ~2 LSB vs GR is MULQ/MACQ truncation (floor)
  vs GR's rounded float square.
- **complex_to_mag (sqrt) + complex_to_arg (atan2) DEFERRED:** no sqrt/atan/CORDIC
  exists in the tree; single-cell magnitude estimators are approximations that fail
  a sqrt-exact gate, and atan needs a divide (no DIV) or a multi-cell CORDIC. Both
  are new algorithms → Tier-2 (build the CORDIC once, shared with QuadratureDemod).

---

## ConjugateBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.conjugate_cc` (re − j·im). 11 tests, EXACT, 0 LSB.
  Single cell: re passthrough + `SUB 0,im` negate, two-word egress.
- **Negate-wrap corner:** im = −1.0 (0x8000) is the only value whose negate
  overflows (−(−1.0)=+1.0 unrepresentable) → SUB wraps to 0x8000. Model it in the
  bit-exact reference, keep GR-equivalence stimulus off it (same single-corner
  pattern as MultiplyBlock's (−1,−1)).
- **The mutation with teeth is "not conjugated":** for an identity-ish I/Q block,
  the dangerous failure is the block ECHOING its input (no-op) and reading green.
  So the key negative test passes im through UN-negated and asserts the gate FAILS
  — it proves the negate actually happened. (Swapped-channels / +1-delay / empty
  round out the set.)

---

## AbsBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.abs_ff` (|in|). 9 tests, 0 LSB vs GR. Single cell,
  single real input (`run_block_dut`, not the complex driver).
- **Reused the AGC/QAM16 abs idiom:** `CMP xs,0; BR.NN _emit; SUB 0,xs; MOVE xs,R0;
  _emit: MOVE R0,xs`. Branch target `_emit` on a REAL instruction (not the `{write}`
  placeholder). −1.0 (0x8000) is the one abs-wrap corner (|−1.0|→−1.0), modeled in
  the reference, kept out of the GR stimulus.
- **#7 housekeeping:** the backlog "negate" is just `GainBlock(gain=-1)` — no new
  block. `analog.rms_cf` needs the deferred sqrt + a stateful averager → Tier-2.
- **#6 float_to_short/short_to_float resolved as NOT a chip block:** the bus is
  uniformly 16-bit, so a Q15 "float" and an int16 "short" are the same bits; the
  only on-chip op is the constant scale = GainBlock. Recorded in the backlog
  deferred section rather than building a redundant block.

---

## KeepOneInNBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.keep_one_in_n`. 26 tests (n∈{1..5}, 3 seeds), EXACT.
  Single cell: modulo-n emit gate over a pass-through (the decimator's gate, no FIR).
- **Phase matters — measure it, don't assume:** GR keep_one_in_n keeps the LAST of
  each group of n (`keep_one_in_n(3)` of 0..11 → 2,5,8,11 = phase n−1), NOT phase 0.
  An up-counter that emits when it reaches n (then XOR-resets) lands exactly there.
  The kept stream is `outputs[n-1::n]` and the emit-phase contract (emit iff
  i%n==n−1) is asserted directly — the strongest test for a rate adapter.
- **The harness already does decimation:** `run_block_dut` records None on triggers
  that produce no egress, so a drop-decimator needs no harness change (same path the
  DecimatorBlock verifies on). The UPSAMPLING twin `repeat` does NOT fit — it keeps
  only `got[-1]` per trigger, so multiple copies can't be counted → deferred.
- **interleave/deinterleave deferred:** multi-rate + N-stream (topology-varying) +
  pure reorder — needs a multi-stream driver, not the single-rate harness.

---

## MovingAverageBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.moving_average_ff`. 18 tests. SUBCLASSES
  FIRFilterBlock with constant box taps `[scale]*length` (the LowPassFilter
  pattern) — zero new datapath code, all Q15/fold/headroom machinery inherited.
- **A moving average IS a constant-tap FIR:** `scale·Σx[n-k] = Σ(scale)·x[n-k]`.
  Constant taps are symmetric → delay 0, aligned with GR's causal running sum, so
  the comparison is delay=0 like the other symmetric-tap filters.
- **Param mapping:** mirror GRC length + scale; GR's `max_iter` (output-buffer
  bound) and `vlen` don't affect the sample math → not Kyttar params. `scale=1/length`
  is the true average (Σ|tap|=1, S=0); larger scale engages the inherited saturating
  headroom restore (S>0), checked against the bit-exact reference.
- **Inherited single-cell budget edge:** a 4-tap box at scale 0.5 (4 taps + S=1
  restore on ONE cell) exceeds the cell register budget and raises at build — a
  FIRFilterBlock per-cell limit, not moving-average-specific. Pick scale≤1/length
  (S=0) or a length that folds multi-cell. Documented in the test + manifest.

---

## ComplexToRealBlock / ComplexToImagBlock — verified 2026-06-26

- **Status:** PASS. GR `blocks.complex_to_real` / `blocks.complex_to_imag`. 18
  shared tests, EXACT, 0 LSB. Single cell each, shared `_ComplexSelect`.
- **Channel selectors = forward one operand:** a complex sample is the (re@R0,
  im@R1) pair, so selecting a rail is one MOVE of the chosen operand to R0 then
  emit (words_per_sample=1). Two thin subclasses differ only by `_SEL` ('re'/'im').
- **The mutation with teeth is wrong-channel:** compare the real-selector DUT to
  the GR IMAG reference — must FAIL. It proves the block forwards the correct rail
  (the dangerous bug is selecting/echoing the other one). +1-delay / empty round it.
- Completes the Tier-1 GRC-parity backlog buildable set (#1–#11); the sqrt/atan/
  multi-rate/4-operand items are recorded in the backlog's deferred (Tier-2) section.

---

## UpsamplerBlock + the BURST-EMIT primitive — 2026-06-26

- **CRITICAL ISA FACT (was wrong, now corrected):** a remote JUMP does NOT halt the
  issuing cell. The issuer keeps executing the next instruction; only HALT (or R31/R32)
  releases a cell. So ONE cell CAN emit a burst of N outputs in one entry —
  `WRITE,JUMP ×N, HALT` — all N fire. No self-loop or 2-cell pacing is needed.
  (See memory project_jump_does_not_halt_issuer.)
- **UpsamplerBlock** (rate-expand 1->sps): emits the sample then sps-1 zeros, unrolled
  WRITE/JUMP per output. Proven on-chip: a downstream cell consumer receives the full
  burst `[0x4000, 0, 0, 0]` for sps=4 (verified via the consumer's data_arrival trace).
  It is the front half of GR `filter.interp_fir_filter_fff(sps, rrc_taps)`; feed it to
  the RRC pulse shaper (which expects pre-upsampled input, SAMPLES_PER_SYMBOL=4).
- **Two test-harness traps that masqueraded as block bugs (don't repeat):**
  1. A truncated trace read (only first 40 events) hid that the cell ran the WHOLE
     burst — always print the full exec-pc sequence for the cell.
  2. The host OUTPUT PORT has NO FIFO (single-outstanding): a burst emitted faster than
     the host drains COLLAPSES to one word at the port (you read only the last). This is
     a host-drain artifact, NOT a chip bug — verify burst blocks by the DOWNSTREAM
     CELL's data_arrival, or drain per-emit. `run_block_dut` reads one-per-trigger, so
     it is NOT valid for rate-EXPANDING blocks (same class as the deferred 4-operand
     INPUT-burst harness gap). A rate-expand verify harness is owed.

## TX-chain block 1:1 verification (2026-06-26)

- **Rate-expand harness owed -> DELIVERED.** Added `run_block_dut_rate` /
  `RateDUTResult` to `kyttar_verify/dut_runner.py`: drives one input per trigger and
  DRAINS THE WHOLE per-trigger burst (read+ack+run loop until the port is empty),
  returning the flat output stream + per-trigger word lists. This is the rate-aware
  twin of `run_block_dut` (which keeps only `got[-1]`, wrong for rate-EXPANDING blocks).
  Smoke-proven on UpsamplerBlock, then used to verify it 1:1.
- **PSKSymbolMapperBlock (BPSK) == `digital.chunks_to_symbols_bf([1.0,-1.0],1)`** — bit
  0->+1 (0x7FFF), 1->-1 (0x8000), full-scale +/-1 EXACT in Q15 (Metric.EXACT, tol 1).
  The manifest's old `_bc` (complex) grc_block was wrong for BPSK; BPSK is I-only.
- **UpsamplerBlock == `filter.interp_fir_filter_fff(sps,[1.0])`** — a unit-tap interp
  filter IS the zero-stuffer (sample then sps-1 exact zeros). Verified with the new rate
  driver, Metric.EXACT, sps in {2,4,8}. The exact GR zero-stuff primitive is the
  unit-tap interp_fir, NOT `blocks.repeat` (repeat DUPLICATES, doesn't zero-stuff).
- **RRCPulseShaperBlock taps ARE `firdes.root_raised_cosine(1.0,sps,1.0,alpha,ntaps)`**
  bit-for-bit (<1e-5) — the block's closed-form RRC + sum-to-1 normalization equals GR's
  gain=1 firdes taps. So GR equiv = `fir_filter_fff(1, those taps)`. The on-chip
  multi-cell FIR is CAUSAL (out[n]=sum h[k]x[n-k]) like fir_filter_fff -> aligns at
  delay 0, 3 LSB error = 33-tap MAC floor (use `op_count=ntaps` for the derived tol).
  Always check tap-equivalence FIRST (a `test_taps_match_firdes`) before the output
  test — it isolates "same filter" from "same alignment".
- **IQUpconvertBlock == `multiply_cc(bb, sig_source_c) -> complex_to_real`** with
  ph0 = 2pi*freq_word/65536 (the NCO increments BEFORE the first emit, so phase[0]=
  freq_word). The quantized 17-entry quarter-wave LUT matches GR's CONTINUOUS oscillator
  to 1 LSB (corr 1.0000) — table is fine enough that quantization is sub-LSB at normal
  amplitudes.
- **Q15 OVERFLOW CORNER is real datapath behaviour, not a bug (recurring pattern).**
  IQUpconvert at |I|+|Q|>1: `I*cos - Q*sin` can exceed +/-1.0; the Q15 SUB WRAPS while
  GR's float clamps -> a huge max-abs-err if you compare to GR there. SAME as
  MultiplyBlock's documented (-1)*(-1) wrap. The right pattern (per CM: understand root
  cause, don't hide): keep the GR-equivalence stimulus OFF the overflow corner
  (`|I|+|Q|<=1` envelope), and add a DEDICATED test asserting the DUT WRAPS bit-exact
  vs the block's OWN process_reference at the corner. Pass that own-reference as floats
  (Q15/32768) so the compare engine re-quantizes to the same word (in-range -> exact
  round-trip).
- **All four TX-chain blocks (PSKMapper-BPSK, Upsampler, RRC, IQUpconvert) are now
  status=done in the manifest, each with mandatory mutation tests.** TX baseband+passband
  is fully GR-verified; ready to assemble the full-duplex modem.

## FIR decim+interp merge + a MASKED tap-reversal bug (2026-06-27)

- **GR's decim/interp are FIR PARAMETERS, not separate blocks.** `fir_filter_fff(decim,
  taps)` and `interp_fir_filter_fff(interp, taps)` are ONE block each; the GRC
  Low/High/Band filter blocks expose BOTH `decim` and `interp`. So FIRFilterBlock now
  takes `decimation` + `interpolation`; the 4 convenience filters inherit them via
  super().__init__. The standalone DecimatorBlock was an INVENTED block (decimation is
  a parameter) — DELETED, all call-sites migrated to FIRFilterBlock(decimation=M).
- **decim** = mod-M output gate (proven DecimatorBlock logic folded in). HW-DEVIATION
  (decim>1 only, documented loudly + raises): the counter shares the output cell with
  the saturating restore -> Σ|h|≤4. **interp** = zero-stuff burst (L FIR passes/input,
  rate-EXPANDING, verified with run_block_dut_rate). Single-cell interp only for now
  (measured unrolled-burst tap caps: L=2→4, L=3,4→2); larger RAISES "compose
  Upsampler->FIR" (honest limit). Both verified 1:1 vs real GR.
- **MASKED BUG FOUND (the big one): the FIR convolved with taps REVERSED vs real GR for
  ASYMMETRIC filters.** Doubly hidden: (1) the block computed Σ h[reversed]·x, and (2)
  the FIR test's `_gr_fir` golden DELIBERATELY reversed the taps before feeding GR (with
  a false comment "GR convolves latest-sample-first"). Every FIR test used SYMMETRIC
  taps, so reversed==forward and nothing caught it. Surfaced the instant asymmetric taps
  were tried (which RULE #0's full-coverage bar demands). FIX: single-cell + multi-cell
  build paths AND the Q15 reference now reverse each cell's segment to match real
  `fir_filter_fff` (y[n]=Σ_k h[k]x[n-k]); the test golden feeds taps AS-IS;
  asymmetric/ramp tap sets added permanently. RRC/Costas/modem unaffected (symmetric).
- **LESSON: ALWAYS verify a convolution/FIR/correlator with ASYMMETRIC stimulus AND an
  UNDOCTORED GR golden.** A golden that "adjusts" the input to match the DUT is not a
  golden — it's a second copy of the bug. If a test helper transforms taps/inputs before
  GR, that's a red flag: GR must be called exactly as a user/GRC would.

## Block orientation-invariance — the datapath IS invariant; the breaks are I/O-boundary (2026-07-20)

Investigated why the multi-cell complex blocks (ComplexRRC MF, Costas 2/4, complex
Gardner, IQUpconvert, ComplexMixer, NCO) FAIL the D4 orientation harness
(`verification/kyttar_verify/orientation.py`) on `cw`, `cw+cw`, `mirror_v+cw+cw+cw`.

- **KEY FINDING: the block DATAPATH is fully D4-invariant.** Dumping every cell's
  program (head→q0..q4→i0..i4 for the MF) across all 8 orientations, the internal
  cells are **byte-identical** in every orientation; the ONLY cell whose program
  differs is the OUTPUT cell (`i4`), and only in its egress hop (the output→port
  distance legitimately changes with placement). The face/feedback transforms
  (`_apply_orientation_face_words`, `_apply_internal_feedback`, `Placement.transform`)
  are correct. So the "orientation-invariance" failures are **NOT** in the block or its
  internal handoffs — they are at the block↔chip-port I/O boundary (input corridor +
  landing, output egress).

- **BUG 1 (fixed, real, helps the live bridge): `_resolve_input_landings` face-checked
  the PORT cell.** The divert scan (build.py ~1506) walked the corridor from index 0 =
  the chip input PORT cell and compared its fwd_face to the drawn route's first step.
  But the host INJECTS at the port (sets the hop directly) — the port cell does not
  FORWARD on a fwd_face; the first real transit is index 1. For a complex fan-in
  (xi + xq both on `head`, reached from different neighbours) ONE net's drawn first step
  disagrees with the port's own I/O face (NORTH), so the scan falsely reported a divert
  AT the port and produced a bogus BROKER landing (wrong entry/reg) even though the word
  rides straight into the block — so the live bridge injecting from that landing misses
  the block (0 output). This even mis-resolved the IDENTITY MF at (1,1) (broker
  entry 23 / reg 1, vs the correct straight entry 25 / reg 0). FIX: start the divert
  scan at index 1 (skip the port cell). Committed; 116/116 identity green.

- **BUG 2 (partial fix): the input corridor is routed THROUGH the chip OUTPUT-port
  cell.** For orientations where a block's input cell lands far from the input port, the
  CP-SAT joint router let the input net thread straight through the output-port cell
  (9,0) — because the node-disjoint constraint exempts any net's endpoint, and (9,0) is
  blk_out's sink. The landing then resolves to a broker AT (9,0), which can't deliver
  into the block (a word riding a corridor cannot transit a foreign PORT cell — it
  ejects). FIX: forbid every CP-SAT net from occupying a chip-port cell that is not its
  own source/sink. Sound + regression-green. (Does NOT fix the harness's single-block
  "block"-topology route, which comes from the heuristic/maze/bus fallback, not CP-SAT —
  the residual harness failures are that same wrap-through-(9,0) pathology in those
  routers, left for a follow-up.)

- **The harness DUT runner now drives from `input_landings` (the bridge's source of
  truth), not a naive Manhattan hop.** `run_block_dut_complex` used
  `31 - manhattan(port, landing)`, which is correct ONLY for a STRAIGHT corridor;
  under rotation the corridor snakes and the manhattan hop lands the WRITE/JUMP on the
  wrong cell → false "NO OUTPUT". Now it prefers the build's corridor-accurate landing
  (hop/entry/data_addrs), the same the live bridge uses — a faithful oracle.

- **The residual harness failures (`cw`, `mirror_v+cw+cw+cw`, and mixer/NCO `cw+cw`) are
  routing-quality, NOT block invariance.** They are exactly the orientations where the
  input cell lands opposite the input port and the router wraps the input corridor to
  the output-port corner. The real auto-placer never produces these (it flyline-orients
  each block so its I/O faces the ports); the production auto-P&R modem recovers BER 0
  (test_qpsk_modem_ber green). A rotated block placed by hand INTO its own input port
  (e.g. MF cw+cw anchored at (0,0), which covers x16_in) is an inherently unroutable
  placement, not an invariance bug.

- **LESSON: prove datapath invariance by DIFFING the per-cell programs across all 8
  orientations BEFORE chasing the router.** Byte-identical internal cells (only the
  I/O-boundary cells differing) tells you instantly the block is invariant and the fault
  is in the corridor/landing/egress — saving a long hunt in the wrong layer. And INV-1's
  `31 - manhattan` hop is a STRAIGHT-corridor approximation; the general truth is the
  build's corridor-walked `input_landings` (what the bridge uses).

## Port-input complex fan-in under rotation (Part A residual) — `cw` FIXED

- **The `cw` collapse was the input fan-in CORRIDOR snaking THROUGH the chip OUTPUT-port
  cell.** For a `cw`-rotated ComplexRRC/Costas/Gardner/IQUpconvert the two input rails
  (xi/xq) from x16_in routed a bizarre snake out to x16_out's cell (9,0) and back. The
  egress net (blk_out) faces (9,0) toward the port EXIT, and `_apply_routes` overwrites
  the cell's fwd_face — so `_resolve_input_landings`' divert scan sees (9,0)'s built face
  disagree with the drawn route and DIVERTS both rails into dead space (block never
  fires → empty output). FIX (`bus_router._route_chip_bus` / `_bus_bfs`): penalise
  (soft, +1000) any FOREIGN chip-port cell as a transit cell in the Dijkstra. A net with
  a detour avoids the port terminus; a net with no other path (a column-9 egress that
  must pass x16_out to reach x1_out — the `test_different_sink_share` case) still routes.
  A HARD wall broke that test; the soft penalty is the right knob. This took ComplexRRC/
  Costas(2,4)/Gardner/IQUpconvert from FAIL to PASS on `cw`; Gardner is now 8/8.

- **A block OUTPUT cell's emit neighbour must be kept OFF the input-broker candidate
  set.** When a complex block is packed so its output cell emits into the SAME free cell
  the head's input fan-in wants as its broker, the egress faces that cell toward its exit
  and clobbers the broker delivery. FIX: both routers now de-prioritise (bus
  `_free_neighbor`) / exclude (maze `_broker_cells`) any `output_emit_cells` (source cell
  + emit-face neighbour) when seating an input broker — soft, so a walled target still
  takes it. This took ComplexRRC to full 8/8.

- **RESIDUAL (`mirror_v+cw+cw+cw` for RRC/Costas/IQUpconvert; `cw+cw` for Mixer/NCO) is
  ONE class: a corner-packed placement where the output cell and the head input are
  ADJACENT and the egress corridor unavoidably boxes the head.** Traced exhaustively: the
  output cell (e.g. i4/mixer) emits into the head's only escape cell, and every free
  neighbour of the head is consumed by the egress corridor, so the input fan-in broker
  and the block-output emit cell genuinely contend for the same one cell. Routing the
  fan-in FIRST (a new "fanin" order) reserves the broker but then the egress cannot leave
  its own output cell (that emit neighbour is on the input corridor). This is a
  PLACEMENT-congestion / build-side dual-role-broker limitation, NOT a per-net routing
  bug — the fan-in broker cell would have to BOTH relay a multi-operand complex sample
  (N WRITEs + trigger + face-restore) AND forward a transiting egress word. Fixing it
  needs either Part B (place the output cell facing open space) or build-side support for
  a fan-in broker that is also transited. Left for that follow-up; the `cw` primary bug
  is fixed and committed, and `test_qpsk_modem_ber` (the production auto-P&R path) stays
  green.

## 2026-07-21 — Saturation is a REQUIRED per-block gate; NCO/FrequencyModulator INV-20 fix

- **Saturation-safety is now a first-class acceptance gate (AGENTS.md checklist +
  INV-19/INV-20), on par with orientation-invariance.** A block must produce the correct
  output COUNT (its N:M rate) AND correct VALUES under SATURATED drive (whole burst
  back-to-back via `queue_words_physical`, no inter-sample quiescence). The per-sample
  harness HIDES fan-in / handshake / feedback hazards. `test_pipeline_saturation.py` +
  its `test_bespoke_coverage_is_documented` enforce that EVERY "done" block is either
  gated or in `NEEDS_BESPOKE` with a reason — no silent gap. This gap is what let the
  4FSK modem's FrequencyModulator ship dropping HALF its samples under load (352→176).

- **NCO + FrequencyModulator were the last two OPEN INV-20 fan-in cases; now fixed
  (opt-in `pipeline_lock=True`), bit-exact in isolation.** Full mechanism in INV-20. The
  three non-obvious NCO-specific moves: (1) collapse `emit` to 2 operands by signing
  INLINE in the interp cells (a 4-operand emit STARVES under the lock; also update
  `process_reference` to sign-before-amp to stay bit-exact, ≤1 LSB); (2) the `relay` must
  FORWARD DATA (ph_cos through it) — a trigger-only relay doesn't re-fire on the substrate
  so the cos arm never fires; (3) `relay` goes in DICT ORDER between the arms and `emit`
  stays the LAST cell, else the build WIPES emit's program to a bare JUMP (relay becomes
  the exit cell). All found with `chip.get_trace()` per-cell fire counts + bounded `run()`,
  NOT blind guesses.

- **The 3 M17 4FSK blocks are now in the saturation gate:** FSK4SymbolMapper (2:1) +
  FSK4Slicer (1:2) in RATE_1IN (pass); FSK4SyncTimingRecovery in NEEDS_BESPOKE (sync-gated
  decimator — needs a framed burst; covered by the RX BER0-pipelined gate).

- **KNOWN-OPEN:** the NCO/FM unlock corridor is not yet placement-invariant in an
  auto-routed CHAIN (`transit_unlock` doesn't fire when auto-placed) — blocks the fsk4
  modem TX end-to-end. The block itself is proven correct hand-placed. Next task.

- **TEST-HARNESS TRAP (cost ~an hour):** `_STIM_Q15` holds UNSIGNED Q15 words; feeding a
  reference `w/32768` reconstructs negatives as large positives → a phantom "rotation
  drift" that looks like a block bug but is a stimulus-encoding bug. Drive the chip with
  the Q15 words AND the reference with the ORIGINAL signed floats (or the same words the
  reference re-quantises internally). ALWAYS reconcile a "divergence" against a
  hand-computed sample before blaming the datapath.

## 2026-07-21 — Placement legality must survive USER MOVEMENT, not just clean transforms (INV-25)

- **A block's footprint can self-overlap through USER MOVEMENT, and neither the layout
  transform nor the orientation test caught it.** The FrequencyModulator serialize-LOCK
  grew the block to 12 cells (added `relay` + `transit_unlock`). A user Alt-dragged one
  cell (the single-cell "breakout" move) onto another of the block's own cells → emit and
  transit_unlock stacked at one square. The DRC caught it (overlap + an un-routable yq/net),
  but only AFTER the fact — the placement itself was accepted.

- **TWO holes, both fixed:**
  1. `ui/controller._placement_legality` skipped a collision when both cells were the SAME
     block (`prev != b.name`) — so a self-overlap passed. Now flags intra-block overlap too.
  2. `ui/controller.move_cell` (the Alt-drag single-cell move) did NO overlap/off-grid
     validation — it just placed the cell. Now REJECTS a move onto any occupied cell (self
     or cross-block) or off-grid; the GUI shows "Cell move failed: …" and the placement is
     untouched.

- **The orientation test was NOT thorough enough** (user's words). It checked compute-
  invariance via clean `OrientBlockCommand` transforms only — never movement. NEW GATE
  `verification/tests/test_placement_legality.py` (INV-25) proves, per multi-cell block:
  (a) no self-overlap in any D4 orientation, (b) `move_cell` rejects a colliding move,
  (c) move-then-rotate / rotate-then-move never yields an overlap. Added to the AGENTS.md
  acceptance checklist. LESSON: a "rotation test" that only rotates a pristine block misses
  the failure mode that actually bites users — moving cells around AFTER placing/rotating.

- **CAVEAT (still open):** a .kyt SAVED with a pre-fix overlap stays overlapping on load
  (the fix prevents CREATING overlaps, not repairing saved ones). Recovery: drag the
  orphaned cell to a free square (now allowed) or delete + re-place the block.

## 2026-07-21 (cont.) — THE actual FM self-overlap cause: a SET-based collision check dedups self-overlap

- **The real root cause of the FrequencyModulator emit/transit_unlock (6,2) overlap was
  NOT user movement — it was the AUTO-P&R re-fold.** `auto_pnr`'s per-block re-fold tries
  each D4 orientation and keeps one that routes better + doesn't collide, gated by
  `_collides`. `_collides` tested `placement.occupied_positions()`, which returns a SET —
  so two of a block's OWN cells on one square DEDUP to one entry and the self-overlap is
  invisible. The re-fold happily committed an orientation that folded transit_unlock onto
  emit. Fix: `_collides` compares cell COUNT vs unique-position count → True on self-overlap.
- **LESSON: any "does this block collide?" check that builds a SET of positions silently
  swallows self-overlap.** Three places had to learn this the same day: `_placement_legality`
  (iterated cells, but the same-block branch was skipped — fixed), `move_cell` (no check at
  all — fixed), and `_collides` (SET dedup — fixed, and it was THE one the auto-placer hit).
  When validating a footprint, compare the CELL LIST to its unique positions; never trust a
  set's membership alone to prove non-overlap.
- Gate: `test_placement_legality.test_collides_detects_self_overlap` runs the full
  place<->route loop on the fsk4 modem and asserts no committed self-overlap — it FAILS on
  the pre-fix `_collides` and passes after. The per-block orientation/move tests (INV-25)
  cover the other two paths.

## 2026-07-22 — Complex-egress yq rail must CO-ROUTE with yi (shared corridor), not route separately

- **The fsk4 modem's yq→x16_out net (net11) was unrouted → build failed + orphan fly-line.**
  A complex-output block feeding the chip output port emits BOTH rails from its ONE emit
  cell down ONE corridor: yi on out_tag T, yq on out_tag T+1; the port de-interleaves by
  tag (engine.sim_bridge complex-egress). The router routes yi but CANNOT draw a second
  distinct path from the same source cell to the same port, so it leaves yq unrouted.
- **Fix: co-rail resolution.** `controller._resolve_complex_egress_corails` (run right after
  `_run_router` in auto_route_all): an unrouted yq egress net whose yi sibling (same source
  block, both → x16_out, yq.out_tag == yi.out_tag + 1) DID route gets the SAME waypoints —
  both rails ride the shared corridor. Idempotent; no-op for single-rail egress.
- **PROVEN:** the user's hand-placed fsk4_modem.kyt now routes 0 DRC errors + builds, and
  the RX recovers BER 0.0000 end-to-end through SimServer (TX complex_out tag10/11, RX
  stream). The fsk4 modem is the FIRST design to egress TWO complex-baseband rails from one
  emit cell to one output port (QPSK/Weaver upconvert to a single real rail first), so this
  pattern had no prior coverage.
