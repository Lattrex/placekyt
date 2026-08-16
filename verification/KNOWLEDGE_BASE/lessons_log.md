<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block (or per system-level campaign) as it
is verified: what was tried, what passed/failed, the derived tolerance, and any
block-specific gotcha. Promote anything that generalizes across block classes into
`invariants.md`. Entries up to 2026-08-13 were editorially consolidated (duplicate
and superseded material merged into the surviving entries; no durable lesson was
dropped) — append new entries above the oldest ones as before.

---

## CONVERTER-FLAVORS DEADLOCK CLOSED — mixed fan-out keeps the routed path; per-port JUMP entries 2026-08-16

The strict xfail on `test_converter_flavors_grc.py::test_runs_live_recovers_input`
(live run deadlocks, 0 egress) is FIXED and the xfail removed. The recorded
diagnosis ("the `_apply_brokers` mixed branch does not fire") was STALE — trace
forensics (`enable_trace`/`get_trace` per-cell events on the built chip) showed
the mixed branch DOES fire; the deadlock had TWO independent root causes, and
every auto-P&R layout hit at least one (which is why the failure looked
deterministic despite layout randomness):

1. **A mixed fan-out (one rail ABUTTED + one BROKERED) is unbuildable by
   construction — prevent it at ROUTING, don't re-sequence it.** The exit cell
   has ONE output face; every fan-out patcher (INV-17) steers arms by HOP down
   that single face. When the mixer's yq abutted EAST while yi's corridor left
   NORTH, the yi rail's @hop WRITE and its trigger JUMP sailed EAST into the
   abutted consumer (trace: the yi data landed in the abutted gain's registers
   and the trigger halted there), the brokered rail never arrived, and the
   downstream starved. The BUS router already kept mixed fan-outs fully routed
   (the round-4 fast-path rule); the MAZE router's `is_abutment` did NOT — it
   abutted any adjacent block→block net regardless of siblings. FIX
   (`maze_router._route_chip_maze`): a source exit cell whose fan-out group
   mixes plain abutments with routed/port arms keeps EVERY arm fully routed
   (all-abutted groups unchanged). The all-routed fan-out form (arms share one
   corridor, each peels off at its own broker) is the proven one.
2. **A multi-entry rendezvous target needs PER-PORT JUMP entries.** The
   DualFloatToComplex runs DIFFERENT code per input (got_i: latch + relock;
   got_q: latch + emit), but every entry-resolution site (`resolved_io` →
   portmap / `bus_router.target_io` / build's abutment + broker patches) gave
   producers the block's single default entry — so the q arm's delivery JUMPed
   got_i, got_q never ran, and the rendezvous never emitted (trace: the dual
   executed pc=got_i for BOTH faces, halted each time). FIX: `Port` gained a
   declarative `entry` (entry-point NAME); the dual declares
   `i→got_i, q→got_q`; the PortMap resolves it per port and
   `target_io`/`_target_port_entry` steer every delivering net's JUMP at the
   right entry. Ordinary blocks (no declaration) are byte-identical.
3. **Blast-radius find — `_perturb_boxed_outputs` could return a report the
   project no longer matched.** Its tail ALWAYS clears + re-routes, but on a
   worse re-route (the router stack is not perfectly deterministic — CP-SAT is
   time-bounded) it returned the EARLIER, better report while the project held
   the worse routes: auto_pnr then accepted a layout claiming N routed nets and
   the build failed "unrouted connection". It now always returns the report
   matching the live project; an honestly-worse report just makes the sweep try
   the next seed. (Surfaced by the cfir Weaver single-chip gate once the maze
   stopped abutting its mixed TX fan-out.)

Regression pins: `test_mixed_fanout_rails.py` (per-port entries distinct; a
brokered `dual.q` delivery JUMPs got_q in the built fabric; the maze keeps a
mixed fan-out fully routed) — all three fail on the pre-fix engine. The live
converter-flavors run now recovers the input at corr 1.0 on every sampled
layout (8/8).

Meta-lesson: **a recorded diagnosis is a hypothesis, not evidence — re-derive
it from the trace before coding.** The xfail's reason text pointed at the wrong
branch; the per-cell event trace located both true causes in one session.

---

## AddCCBlock + SubCCBlock — the 4-operand wall falls: two-complex-stream combiners + the reusable complex2 driver 2026-08-16

GR `blocks.add_cc` / `blocks.sub_cc` (2 complex streams, elementwise; semantics
pinned LIVE first: memoryless, strict pairing, delay 0, N-input sub = a0−a1−…).
Both green in ONE loop (shared module `add_sub_cc_block.py`, the
AddBlock/SubtractBlock pairing); 67 tests, first-try bit-exact on chip.

- **THE ARCHITECTURE IS FORCED BY THE MACHINERY, not just the math.** Three
  engine contracts pick the topology for any future multi-stream block:
  (1) `build_port_map`/`resolved_io` expose external INPUTS only from THE ONE
  landing cell (first cell with inputs) — a per-rail landing split (ai on one
  cell, aq on another) is UNWIRABLE from GRC; (2) `_iq_sibling` synthesises a
  stream's Q net only for a SAME-CELL/SAME-ENTRY pair; (3) `_elect_join_triggers`
  resolves ONE join address from the landing cell for ALL arms. ⇒ ONE landing
  cell with all 4 operand regs (ai@R0,aq@R1,bi@R2,bq@R3), entries[0]=join.
  The manifest's per-rail decomposition then lives DOWNSTREAM of the join:
  rail_i computes yi=sat(ai±bi) and forwards (yi,aq,bq)+one trig; rail_q
  computes yq and emits the (yi,yq) INV-17 packet (12 words free for fan-out).
- **The 2-arm toggle COUNTING JOIN paces two whole PACKETS, not operands:**
  each source's complex pair is multi-WRITE + ONE JUMP, so two jumps/sample in
  ANY order single-fire the compute — the AddBlock tail verbatim (jsav
  save/restore protects R0=ai across both tail runs). A single-cell 4-input
  form (join 9 + two 6-instr saturating rails + emit) measures ~39 words —
  over budget; that IS the old 4-operand wall, quantified. num_inputs pinned
  to 2 (HW-DEVIATION, raises).
- **Budget tricks that made rail_i fit at 30/32:** `OP R0, R{in:bi}` reads the
  input latch ONCE and the accumulator-ISA result lands in R0 regardless of
  operand order (`SUB R{state:asav}, R{in:bq}` in rail_q computes aq−bq into
  R0 with NO pre-MOVE — dest-register-free subtraction, minuend from state);
  each input register is read exactly once (the ComplexMixer stale-latch trap).
  Sub's overflow restore uses the MINUEND's sign (a−b overflow ⇒
  sign(a)=−sign(b) ⇒ result sign = sign(a)) — same rail serves both ops.
- **IMPORTER FIX (pinned by test): GRC numeric port index counts COMPLEX
  ports.** `_resolve_port`'s index branch mapped `[mixb,'0',comb,'1']` to
  ports[1]=aq — stream b's yi landed on stream a's Q rail and b's imag rail
  silently vanished. Fix: for blocks with ≥2 complete on-cell input I/Q pairs,
  numeric indices select I-halves only (aq/bq come from the I/Q split). Gated
  on ≥2 pairs so every existing single-pair block (xi/xq, in_i/in_q, the
  dual's i/q, out_re/out_im) keeps raw positional mapping — no regressions
  (dual/converter/example gates re-run green).
- **THE DRIVER (the dispatched deliverable): `kyttar_verify.run_block_dut_complex2`
  (+ `_pipelined`)** — 4 operands as two (re,im) packets, two JUMPs/sample, hop
  and join entry from the build's corridor-accurate `input_landings`; the
  saturated twin queue_words the whole two-packet stream and bounds the run
  (INV-19 harness rule). MultiplyCCBlock (later wave) drives through it
  unchanged. Saturation coverage is NEEDS_BESPOKE (the shared harnesses emit
  ONE jump/sample — would leave the join half-fired and the run would FALSELY
  fail); the bespoke gate asserts saturated == per-sample BIT-EXACT plus a
  drive-non-vacuity probe (swapped pipelined sub streams must change output).
- Tolerances: AMPLITUDE vs GR per rail via `compare_complex_against_grc`
  (op_count=1, delay=0, in-range stimulus |a±b|<1 per rail); EXACT vs
  `process_reference_q15` including per-rail saturation corners (pins at
  ±full, never wraps — verified mixed-rail: I pins + while Q pins −).
  Mutations: inverted / wrong-second-stream / per-rail (aq-only fails Q while
  I stays clean; bi-only fails I) / wrong-op (Add vs sub_cc golden) /
  +1-delay / empty all FAIL; sub's swapped-streams FAILS (required), add's
  swap asserted commutative-equal (documented). All 8 D4 orientations equal
  identity through the new driver.
## RMSBlock + RMSCFBlock — rms_ff/rms_cf pair, error-feedback IIR + quartic sqrt, 2x2 fold 2026-08-16

= GR `blocks.rms_ff` / `blocks.rms_cf` (param `alpha` verbatim, default 1e-4).
ONE shared module (`rms_block.py`): a `_RMSCoreBlock` base holds the IIR tail +
the 3-cell sqrt pipeline; the twins differ ONLY in the power front (x² vs
re²+im²). Verified: bit-exact vs the q15 reference on every stream tried
(edge/random/alpha sweep incl. default), LIVE-GR settled tail max err 4–8 LSB
(derived TOL 16), 36 tests green + saturation/orientation/legality gates.

- **PIN GR FIRST paid off in one line:** `rms_ff` computes
  `avg=(1-alpha)*avg+alpha*x²` THEN `out=sqrt(avg)` — first output
  `sqrt(alpha)*|x0|`, avg starts 0. The manifest's formula matched, but the
  sqrt-after-update order and the first-output value are only pinnable live.
- **THE IIR TRAP — bare `MULQ(alpha_q, d)` STALLS at small alpha.** Truncation
  zeroes every increment with `|d| < 2^15/alpha_q` LSB; at GR's DEFAULT
  alpha=1e-4 (alpha_q=3) the averager stalls up to 10923 LSB (1/3 full scale)
  short. FIX = full-precision ERROR FEEDBACK: keep S = y*2^15 + acc_lo as two
  16-bit words; `alpha_q*d = (MULQ<<15) + (MUL&0x7FFF)` EXACTLY (floor-division
  identity, MULQ truncates toward -inf = arithmetic >>15), so
  `t=acc_lo+lo15; y+=MULQ+(t>>15); acc_lo=t&0x7FFF` loses nothing and y
  converges within ±1 LSB at ANY representable alpha. Costs 8 instrs + 2 state
  regs over the naive form — fits one cell WITH the x² front (19 words).
  (AGCBlock's `rate` MULQ has the same stall latent at its 1e-4 default.)
- **THE SQRT (no-sqrt ISA):** normalize y by counting `SHL #1` to [0.5,1)
  (Nlog10's loop), quartic LSQ of `sqrt(0.5+f/2)` (all coeffs sub-unity, fit
  0.53 LSB — pick the representation FIRST, the Nlog10 lesson), then denorm:
  ×1/√2 when s odd + `SHR #1` under a counter for the s/2 shifts (INV-34, no
  variable shift). EXHAUSTIVE bound over all 32768 power words: err in
  [-4.5, +0.6] LSB, pinned by a guard test. A quartic beats unrolled
  Newton-Raphson here: same accuracy class, no >1 constants, 15 instrs.
- **NEW GENERAL TRAP — a GOTO in the block's EXIT cell is DESTROYED by the
  build's output-handoff pass.** `GOTO` assembles to a local hop-31 JUMP; in
  the exit cell the handoff pass rewrote it into the EXTERNAL output JUMP
  (memory dump: the loop tail became a second port-trigger, hop 22), so the
  denorm shift loop ran ONCE — every s>=4 output exactly 2x. The SAME GOTO
  loop in a mid-chain cell (norm) is untouched. Rule: EXIT cells use
  CONDITIONAL branches only (do-while on SUB's Z flag; SHR sets Z for the
  k==0 pre-test). Extends the INV-13/INV-19 exit-cell-structural-role family.
- **CF WRAP CORNER:** re=im=-1.0 → 0x8000+0x8000 wraps to ZERO with N clear —
  ComplexToMagSquared's single `BR.N` end-check form would emit 0. Guard N
  after EACH step (`MULQ re,re; BR.N sat; MACQ im,im; BR.NN ok`); the corner
  is pinned bit-exact. (ComplexToMagSquaredBlock itself has this latent corner
  — its stimulus stays inside the unit circle.)
- **VERIFICATION SHAPE for an averager:** the settled tail is
  alpha-INDEPENDENT (it's the mean power) — so (1) the settled-tail gate is
  robust to alpha quantization, but (2) a wrong-alpha mutation needs a
  TRANSIENT window to have teeth: use an amplitude-STEP stimulus and compare
  the full post-warm-up trajectory (also gives the +1-delay mutation its
  teeth; measured teeth 5000+ LSB vs TOL 16). Warm-up is DERIVED:
  n = ceil(10/alpha_eff) (e^-10 residual ≤ 1.5 LSB power). The default-alpha
  HW-DEVIATION (1e-4 → 3/32768, 8% slower) is pinned on a 113k-sample
  constant-amplitude run: tail matches GR, mid-transient FAILS (the warm-up
  guard is load-bearing). Build the GR golden's long constant vector INSIDE
  the GR script — 113k words inline overflows the subprocess argv limit.
- **Tolerance derivation (16 LSB):** sqrt path ≤4.5 + settled power err ≤2.5
  amplified by d(sqrt)/dY = 90.5/sqrt(Y) (≤2.78 at stimulus RMS ≥0.18 → ≤7)
  + warm-up residual ≤4. Near-zero amplitude the amplification explodes
  (90 LSB/LSB at Y=1) — intrinsic to sqrt, not a bug; GR-gate stimuli keep
  RMS ≥ 0.18, everything below is covered by the bit-exact gate.
## HammingDecoderBlock — Hamming(7,4) syndrome decoder, BIT-EXACT; the FUSED word+syndrome accumulator 2026-08-16

Systematic Hamming(7,4) hard-decision FEC decoder (7:4 rate-reducing, raw 0/1
words, tier 1, NO GR counterpart — golden = the standard syndrome decoder,
Hamming 1950 / Lin & Costello §3.3). CONVENTION (pinned, shared verbatim with
HammingEncoderBlock): wire = `d3 d2 d1 d0 p2 p1 p0` MSB-first, even parity
`p2=d3^d2^d1, p1=d3^d2^d0, p0=d3^d1^d0` ⇒ H columns (d3..p0) `[7,6,5,3,4,2,1]`,
syndrome→flip LUT `[0,1,2,8,4,16,32,64]`. 21 tests; 112/112 single-bit errors
corrected ON-CHIP; round-trip golden-encoder→DUT identity at 0/1 errors; all
16×21 double-bit errors GATED as deterministically uncorrectable (distance 3).
Durable lessons:

- **The naive shape does NOT fit — count words BEFORE authoring.** The obvious
  single-cell design (pack-7 loop + per-bit column LUT + flip LUT + 4-bit emit
  loop) needs ~37 instructions + 16 table words + 5 state ≈ 58 of the 30 usable
  words; even 2-cell splits with a syndrome LOOP over a packed word ran ~33-45.
  The real budget arithmetic (resolver): instructions sit at `31-N .. 30`, R31
  is a reserved HALT, data from addr 1, state only in the gap between — so
  **data + state + instructions ≤ 30** (R0 + R31 are never allocatable).
- **THE FUSED ACCUMULATOR (the trick that made it fit): one 16-bit register
  carries the packing word AND the running syndrome.** Store pre-shifted column
  constants `T[j] = (col[j] << (2+j)) | 1` and update `reg' = (reg<<1) ^
  bit*T[j]`. The `|1` is the packing bit (bits 6..0); each column contribution
  enters at bits [2+j,4+j] and the remaining `6-j` shifts align every one at
  bits [10..8] where the XORs accumulate the syndrome. In-flight, a
  contribution at step k occupies [2+k,4+k] while the word occupies [0,k-1] —
  provably disjoint (XOR==OR for the packing bit). After 7 bits: `reg>>8` = the
  syndrome, `reg&0x7F` = the word — ONE internal operand instead of two, and
  the whole front cell is 17 instr + 9 data + 3 state. Verified exhaustively
  (all 128 words fused-model == standard decode) BEFORE building on-chip.
- **The down-counter IS the LOAD address.** `count` runs 7..1 and directly
  addresses the T table at 1..7 (`LOAD count`), and `SUB count, one` sets the
  Z flag for the group boundary — `MOVE` preserves flags, so `SUB; MOVE; BR.NZ`
  needs NO separate CMP and no `zero`/`addr` words. (Mind: SHL/SHR are ALU ops
  and DO update flags — no shift may sit between the SUB and the BR.)
- **`{write:name}` / `{jump:name}` placeholders must be ALONE on their line.**
  The resolver's regex is `^\s*\{write:(\w+)\}\s*$` — a trailing `;` comment
  silently un-matches it, the placeholder survives to assembly and the build
  dies with `Unknown opcode: {WRITE:COMB}`. Comment the line ABOVE, never
  inline. (First real build failure of this block; everything else ran first
  try.)
- **Dual-use DataWords bought the fix cell its budget:** the flip LUT at addr
  1..8 already contains 1 (`flip[1]`, doubles as `one` AND the table-base
  offset since flip[s] sits at 1+s) and 4 (`flip[4]`, the emit-counter seed).
  The emit loop slides a `&0x78` window (bits 6..3) and peels with `SHR #6` —
  no nibble extraction, no separate window mask beyond 0x78. Input pinned at
  R0, OUTSIDE the 1..8 table range (the QAM16 table-aliasing trap).
- **Deliberate no-reset with masked reads:** front does NOT clear the packed
  word bits between groups (no budget for it) — stale bits climb into reg[7+],
  but the syndrome window is XOR-cleared each group and every downstream read
  is masked (`>>8` sees only cleared syndrome bits; the data window is
  `&0x78`). Document such invariants at the read site, and cover them with a
  MULTI-GROUP stream test (a single-group test can never see staleness).
- Feed-forward 2-cell chain: saturation-safe with NO lock (RATE_1IN gate,
  saturated flat stream == per-sample), D4-invariant 8/8, placement-legal.
  Metric: raw-word BIT-exact, delay 0, tol 0 (byte blocks are never Q15 — the
  XorBlock lesson).

---

## HammingEncoderBlock — systematic Hamming(7,4) FEC encoder, bit-exact 2026-08-16

**THE CONVENTION PIN (verbatim — HammingDecoderBlock MUST derive from this exact
statement):** systematic codeword layout MSB-first on the wire =
`d3 d2 d1 d0 p2 p1 p0`, where the data nibble arrives MSB-first (d3 first), and
parity bits are `p2 = d3^d2^d1`, `p1 = d3^d2^d0`, `p0 = d3^d1^d0` (even parity).
Golden = the standard systematic G = [I4 | P] (Hamming 1950; Lin & Costello);
executable pin: `HammingEncoderBlock.encode_nibble()`. The test's INDEPENDENT
G-matrix golden + golden syndrome decoder both live in
`test_hamming_encoder.py` (min-distance-3 self-check; 112-case
decoder-inverts-encoder check; DUT round-trip clean AND under a rotating
single-bit error).

- **Shape = PackKBits(k=4) fused with UnpackKBits(k=7), split 2 cells.** A
  4:7 rate expander needs BOTH a cross-trigger accumulator and a counted-loop
  burst emit; the whole thing (accumulate 10 + parity 14 + emit loop 11 + resets)
  is ~39 instructions — nowhere near one cell's 32-word budget (INV-7 checked
  BEFORE authoring, per the dispatch). The fit that works: cell `pack` =
  accumulate + attach p2 (20 instr, 28/32 words), cell `expand` = attach p1+p0 +
  burst emit (20 instr, 28/32 words). Straight 2×1 fold (nlog10's proven
  `default_layout` shape) — even column count, I/O co-located (INV-14).
- **The P (parity) flag IS the parity encoder.** `AND w, mask` sets P = XOR of
  all result bits, so each parity bit costs 4 instructions (`AND; BR.NP skip;
  OR bit; MOVE`) with NO per-bit extraction. Masks address the SHIFTED data-bit
  positions (nibble pre-shifted `<<3` into the codeword frame: m_p2=0x70,
  m_p1=0x68, m_p0=0x58, all within bits 6..3) so already-attached parity bits
  (bits 2..0) can never contaminate a later parity. Split the three paritys
  across the cells by BUDGET, not by concept — p2 rides with the packer, p1/p0
  with the emitter; the wire format between them is just "codeword with only p2
  attached".
- **Register-budget tricks that made it fit:** (a) countdown counter
  (`StateVar(initial_value=4)` + `SUB;MOVE;BR.NZ`) beats count-up+CMP by 2
  instructions — MOVE preserves flags, so SUB's Z survives to the branch (no
  CMP); same trick ends the emit loop. (b) One DataWord `four` = both the p2 OR
  bit (1<<2) and the counter reload (the INV-19 merge-identical-DataWords
  trick). (c) NO per-iteration window mask in the emit loop: `SHR #6` then
  `AND one` isolates bit 6 regardless of garbage above it, so UnpackKBits'
  `kmask` AND is unnecessary — −1 instr, −1 word.
- **INV-33 respected by construction:** every StateVar pinned explicitly
  (data @1..N, state above); first instruction of each cell consumes/copies the
  R0-landing input before any ALU op clobbers R0.
- **Saturation (INV-20 checked, as the dispatch demanded):** straight 2-cell
  feed-forward chain — no feedback corridor, no reconvergent fan-in → no
  serialize-LOCK. Gated in `test_pipeline_saturation.py` RATE_1IN (saturated
  flat stream == per-sample flat stream). Orientation: all 8 D4 green;
  placement legality green.
- Bit-exact on the FIRST on-chip run (0 errors, all gates): reading the KB
  first (UnpackKBits counted-loop, PackKBits ALU-lands-in-R0, INV-33/34) is
  what made that happen. Metric DECISION, tolerance 0, delay 0 (+1-shift
  mutation fails as required). Raw 0/1 words, NOT Q15; input LSB-masked
  (`& 1`) with a dedicated stray-high-bits edge test.

---


## Crc16Block — frame CRC-16 via the SHL carry flag; the golden-with-no-GR recipe 2026-08-16

Single-cell, rate-reducing (frame_len bytes → ONE 16-bit CRC word), chip
BIT-EXACT (EXACT, tol 0) on the FIRST build+run attempt — the LFSRScrambler +
PackKBits shape models plus the accumulated invariants made this a pure
assembly job. Durable notes:

- **The SHL CARRY flag is the cheap MSB-first CRC select.** `SHL Rcrc, #1`
  sets `C` = the shifted-out bit 15 (guide §4.3) and `MOVE` preserves flags,
  so `SHL; MOVE crc,R0; BR.NC skip; XOR crc,poly; MOVE crc,R0` does one
  polynomial step in 5 words — no 0x8000 mask word, no fb StateVar, no GOTO
  merge (the LFSRScrambler GOTO-in-tail trap avoided by construction). The
  branchful AND-mask form costs 11 loop words and overflows the cell (33/32);
  the carry form fits at **29/32**. When a bit-serial datapath needs the
  pre-shift MSB/LSB, reach for the shift's C flag before an AND mask.
- **Decrement in 2 instructions, not 3:** ALU ops are two-source →
  `SUB Rn, Rone; MOVE Rn, R0` (the shipped LFSR/PackKBits 3-instr
  `MOVE R0,x; op; MOVE x,R0` form spends a word for nothing). Frame counter
  and bit counter each saved a word this way.
- **`crc ^= byte << 8` self-masks the input:** `SHL R{in:sample}, #8` drops
  bits 8–15 of the input word, so a stray-high-bits guard costs zero
  instructions (tested: dirty vs clean inputs identical).
- **The golden-with-no-GR recipe (this block is the template):** (1) cite the
  exact catalogue model (CRC RevEng: CRC-16/CCITT-FALSE, poly 0x1021 init
  0xFFFF refin/refout=false xorout=0, check 0x29B1); (2) pin the pure-python
  golden against an INDEPENDENT stdlib implementation BEFORE any DUT compare
  (`binascii.crc_hqx(data, init)` IS this engine for poly 0x1021 — crcmod is
  not installed, crc_hqx is); (3) anchor MULTIPLE catalogue check values
  on-chip (XMODEM 0x31C3, AUG-CCITT 0xE5CC, UMTS 0xFEE8, CMS 0xAEE7) so the
  param space is pinned by published vectors, not self-consistency; (4) run
  the strongest INV-4 mutations as REAL on-chip mutants (wrong-poly DUT,
  wrong-init DUT), model-level only where a real mutant is impossible
  (reflected feed, +1 shift step).
- Raw-word streams throughout (the XorBlock lesson): raw byte injection,
  EXACT integer equality; the output word IS the CRC (not Q15). Reflected
  CRC models (ARC/MODBUS/KERMIT) are NOT this engine — documented loudly.
- Gates: 43-test suite green; saturation REAL_1IN (frame_len=4 → 4 live CRC
  words on the 16-word stimulus); orientation-invariant 8/8; placement-legal;
  binding complete (yml + shim + `_TYPE_OVERRIDES` pin of `kyttar_crc16`).
  `install.sh` (sudo) still needed on the host for the GRC palette refresh.

---

## QPSK modem: Gardner → MMTimingRecovery swap (certified timing in the flagship) 2026-08-16

The quarantined complex Gardner was replaced by the certified
MMTimingRecoveryBlock in the QPSK modem — chain order UNCHANGED
(MF → Costas(order=4) → timing → slicer; carrier-first, so the DD timing loop
sees a derotated constellation and the example keeps its foff=0.008 showcase).

- **Drop-in at EXACT parity, no gain stage:** the Costas order-4 output sits at
  ±0.707 per axis, which the M&M 4-PAM decision device slices consistently to
  the outer level — decisions are a constant-scaled version of the true
  symbols, so the TED zero is unmoved. Verified BER 0.0000 (160/160) at
  seeds 5/6/7 × toff 0.45/0.7 on the programmatic chain, the imported .grc
  chain, AND the shipped duplex .kyt through the stream-routed SimServer path
  (a shipped-artifact gate the qpsk example previously lacked — now
  `test_shipped_kyt_recovers_ber_zero`).
- **Adding a "nominal-scale" gain stage HURT:** ComplexGain 1.34 between
  Costas and MM caused double-strobing (~316 outputs for 160 symbols) and BER
  ~0.65. For a constant-modulus constellation already sliced consistently,
  do NOT gain-stage toward the 0.949 outer level — that rule is for
  multilevel (16-QAM) inputs whose 4 levels must each slice correctly.
- **CHAIN-LEVEL OPERATING ENVELOPE (not a timing-block property):** toff=0.3
  fails for BOTH Gardner and MMTiming with near-identical BERs
  (0.43/0.36/0.61 vs 0.43/0.37/0.60 over seeds 5/6/7) while 0.45/0.7 are
  BER 0 for both — the failure is UPSTREAM of the timing block (the
  MF/Costas front end at that sampling-phase/foff combination). When a swap
  candidate matches the incumbent's pass/fail map exactly, the shared
  failures are the chain's, not the block's. The shipped operating point
  (toff=0.45) is what the gates pin.
- **14-cell MMTiming fits the duplex floorplan:** import + auto_pnr placed
  the full 8-block modem cleanly; route-quality ratchet pinned at +4 (two
  placement-forced wall detours around the bigger footprint). RX-only
  explicit anchors: mf(0,0), costas(0,3), mm(2,6), slicer(8,9).
- **bpsk_modem + coherent_bpsk_rx KEEP Gardner (documented decision):** their
  chains carry the Costas order-2 SINGLE REAL rail into the timing block;
  MMTiming is complex-in, so a swap needs a null-Q splice into a mid-chain
  complex block plus a dangling-yq egress answer — plumbing risk with no
  behavioral gain (Gardner is BER-0-verified in those demos; the README
  honesty note stays). Revisit if a real-rail M&M variant ever ships.

## ISA CONFORMANCE — shift counts are immediate fields; sim + docs aligned to the design (INV-34) 2026-08-13

A design-review pass confirmed the shifter's contract from the silicon up: the
barrel shifter takes its count from the immediate `CNT[9:6]` field and bit[10]
is reserved (exactly what the instruction FIELD TABLE says — PROGRAMMING_GUIDE
§4.3). Prose elsewhere and the simulator had drifted from the field table and
described a register-count variant — both are now aligned to the design at the
root: the assembler rejects `[Rm]` count syntax outright, the decoder treats
bit[10] as reserved, and the mode is unrepresentable in the instruction type.
INV-34 records the rule + a source-scan gate
(`verification/tests/test_silicon_isa_subset.py`).

Two blocks that had leaned on the drifted simulator behavior were restructured
to immediate-count constructions — both SMALLER than before:

- **VaricodeEncoderBlock**: the packed SRAM word now stores the code
  LEFT-ALIGNED at bit 15 with the length in bits[3:0] (alignment done in
  Python at table-build time, free), so the emit loop is a fixed-position
  walk — `SHR #15` extracts the current bit, `SHL #1` advances. Net −1
  instruction; the `SUB len,one` doubles as the loop test since MOVE
  preserves flags.
- **VaricodeDecoderBlock**: `cur << pend0` for pend0 ∈ {0,1} is the
  arithmetic identity `cur + cur*pend0` — a branchless MUL/ADD pair, −2
  instructions vs a CMP-guarded branch. The shipped psk31_transceiver.kyt
  was regenerated (its baked panel image carries the packed format).

Meta-lesson: **the field tables are the canonical ISA reference; prose and the
simulator are kept conformant to the design.** When a "discovered" feature only
appears in prose or in observed simulator behavior, check the field tables
before using it. (GOTO, for contrast, is confirmed real: assembler sugar for
`JUMP hop_cnt=31, <label addr>` — a local jump the hardware implements.)

## ComplexToMagBlock + ComplexToArgBlock — CORDIC vectoring, UNROLLED pipeline, chip BIT-EXACT incl. saturated 2026-08-13

The CORDIC engine (vectoring mode: magnitude + atan2). ONE debug cycle each to
bit-exact — the LMS forensics workflow + INV-33 paid off directly.

**ISA notes (verified per INV-34's authority order):**
- **Shift counts are immediate instruction fields** (`CNT[9:6]`, bit[10]
  reserved — PROGRAMMING_GUIDE §4.3). Data-dependent shift amounts use the
  immediate-count constructions in INV-34.
- **`GOTO label` is the unconditional local branch** (a local JUMP, 1 word).
  `BR.A` is NOT "always" — flag A = "result was all-ones". There is NO
  unconditional BR flag; multi-path cells end each path in GOTO (or its own
  `{jump:port}` duplicate).
- **WRITE and MOVE preserve the FLAGS** (only ALU/logic/shift/CMP set them), so
  a sign-test SHR can drive a branch ACROSS an interleaved `{write:...}`.

**Architecture decision — UNROLL, don't loop:** a looped XY cell needs ~25
instructions + 8 reg/data words > the 32-word cell. Loop overhead (counter,
bound, two indexed shifts, temporaries) doesn't shrink with fewer iterations
per cell. One-cell-per-iteration (immediate `#i` shifts) deletes ALL loop
state: 21-instr mag cells / 23-instr arg cells, and the chain pipelines.
17 cells (mag, 9x2 serpentine) / 30 cells (arg, 8x4 serpentine of interleaved
XY_i/Z_i pairs; XY_i streams its PRE-update y to Z_i, which owns ATAN[i] as a
data word — no indexed table, no LOAD needed).

**Numerics (spike-derived, cell-exact reference BEFORE silicon):** prescale 1/4
(K*|v| hits 2.33 and wraps); ones-complement asr in PRE (`((v^msk)>>n)^msk` —
no +sgn); masked identities in the loop (sigma*asr(y,i) = ((y^msk)>>i)+sgn);
HALF-TURN Q15 angle — 16-bit wrap IS mod 2pi, the +-pi seam is free. Mag: MULQ
1/K + saturating <<2 restore (INV-13). Measured vs GR: mag max 19.7 LSB, arg
max 0.0026 rad (|v|>=0.1; input-quantization-limited below — 1 input LSB
subtends ~1/(|v|*pi) half-turns). Gates locked at ~2x.

**The one debug cycle — INV-33's no-data-words corollary:** cells with NO data
words auto-allocated state at R0/R1/R2, ON TOP of the inputs (the gap-scan
starts at max_data_address+1 = 0). Symptom: build clean, triggers propagate,
every register value garbage. Fix: pin EVERY StateVar register explicitly.
Promoted into INV-33.

**Saturation-SAFE (a first for a multi-cell DSP chain this size):** fully
feed-forward + stateless → `run_block_dut_pipelined` is BIT-EXACT to
per-sample; both blocks joined COMPLEX_2IN2OUT as positive saturation gates.
**But anchor matters:** at the harness default (1,1) the 9-wide mag chain's
egress corridor routed through the col-0 input-delivery cells → ingress/egress
CONTEND under saturated duplex load → EventLimit livelock (the single-chip
cousin of INV-32's broker rule). At (0,1) the corridors are disjoint and it
passes. The COMPLEX_2IN2OUT tuple grew an optional 4th element (anchor).
Follow-up candidate: teach the single-chip router the INV-32 corridor-disjoint
preference so anchors stop mattering.

**Layout trick:** the arg serpentine ends at column 2 of the last row so the
output cell's westward egress is free; the earlier 10-wide version ended on the
east edge with NO free corridor ("no free corridor between the ports" — a
LAYOUT problem, not a router bug; reshape the serpentine, don't fight routing).

Unlocks: envelope/AM detection, AGC magnitude, RMS, FM/PM demod exactness,
phase-difference/frequency estimation, resolver-angle motor control.

## LMSEqualizerBlock — DD complex LMS equalizer, chip BIT-EXACT + GR scale-covariant 2026-08-13

- **THE GO/NO-GO METHOD (worth reusing):** (1) float model proven EXACT vs the
  GR golden (5e-7 — this validated the update convention AND caught that GR's
  ``constellation_qpsk()`` points are **±1.414±1.414j**, components OUTSIDE
  Q15!); (2) Q15-ize with exact chip semantics (MULQ truncation `(a*b)>>15`,
  explicit saturating adds); (3) measure, don't guess — the "stall at 56%" was
  three experiments of wrong metric: the model had converged PERFECTLY to the
  α-scaled solution (LMS is scale-covariant; α = ½ unit-circle constellation).
  Chip contract: DD-only SPIKE cold start at tap 0 (delay-0 for causal
  channels — a CENTER spike converges to the delay-m solution and wastes
  anticausal taps) reaches GR-with-training's steady state exactly.
- **INV-13 for ADAPTIVE taps:** headroom can't be derived from static
  coefficients — it is a DESIGN BUDGET (taps halved, envelope Σ|w_eff| ≤ 2)
  + saturating tap adds as the safety net. At α = ½ the converged Σ|w_half|
  < 1, so the MAC chain never clips (measured: 0 sat events post-transient).
- **Architecture (14 cells + 1 transit, 8×2):** per-tap F (filter+mirrors) /
  U (master taps+update) rows; straight-line multi-hop backward broadcast of
  the gradient (BCAST one WEST face); FARTHEST-FIRST triggers so no jump ever
  transits a mid-flip cell (the router's farthest-sibling rule, in-block);
  flip-and-restore face discipline on the h↓/w↑ mirror writes; w-mirror
  cold-start values emitted as SAME-ADDRESS DataWords over the mirror ports
  (else sample 1 filters with w=0). Ground truth: a forwarded packet follows
  each transited cell's CURRENT fwd_face — corridors are resting faces, and
  per-sample choreography must never flip a face while a word can transit.
- **Traps → INV-33** (register contract, R0-as-accumulator + the acc-in-R0
  delivery idiom, positional program↔layout pairing, feedback-pass dest-reg
  ambiguity → order broadcast cells early). Plus: `output_cell_id()` REQUIRED
  when the output leaves a non-last cell (the router taps the last non-transit
  cell otherwise — routes started at the transit); generic transit
  materialization added to `_apply_block_cell_faces` (before, only the
  feedback tracer created them — a forward-corridor transit stayed unfaced
  and silently deflected everything).
- **INV-19 KNOWN LIMIT (guarded):** saturated drive does not quiesce (the
  gradient broadcast races the next forward pass). PER-SAMPLE contract; the
  serialize-LOCK choreography (IN locks until BCAST unlocks) is the follow-up.
- **Debug workflow that worked:** trace events carry `data_raw`/`word` (NOT
  `value`); per-cell memory via `read_cell_memory(cid, addr)`; config FACE at
  bits [9:8] of `read_config`; watch-the-memory-across-build-passes bisection
  found both the face reset (`_apply_routes`) and the feedback-pass clobber in
  minutes each.

---

## Multi-chip GUI: live view, repeat-burst loop, stale-server triage, landing render 2026-08-13

Four user-reported gaps in the 2P2S multi-chip demo path, all fixed + gated:

- **Live view plumbed for MultiChipSimServer** (waveform panel + cell
  animation showed nothing). KEY DISCOVERY: re-calling `enable_trace` on a
  `MultiChipSimulation` starts a FRESH buffer (there is no `clear_trace`) —
  that gives the drain+clear cycle the live view needs. `drain_trace` drains
  EVERY chip (events tagged `_chip`, time-sorted) and resets buffers so
  nothing grows unbounded under the repeat-burst loop. The TraceModel was
  already chip-aware (`append_live(chip, …)`); the refresh groups drained
  events by chip. Animation runs per-chip; breakpoint mode stays single-chip.
  Gate: `test_multichip_live_view.py`.
- **Repeat-burst loop (what a slider means for batch sim):**
  `kyttar.source(repeat=True)` re-arms after the sink drains each generation
  (BatchSession.result_consumed gate — never overruns a slow sink), so the
  flowgraph is a CONTINUOUS burst loop and a set_gain mid-run lands ONE BURST
  LATER within the SAME Run. Burst boundaries DRIFT across a repeating
  stimulus — amplitude is the claim, not phase (the gate compares sorted
  multisets). Gate: test_repeat_bursts_apply_slider_within_one_run (real
  gr.top_block, set_gain mid-run, first burst 0.5x / last 0.25x).
- **TRAP (order): stop the old server BEFORE clearing traces** —
  stop_gnuradio_server does a final trace drain, so a rehost added after
  clear_traces() repopulated the panel with the PREVIOUS project's residual.
  Order gate added.
- **Stale server across project switches (the "everything is broken" triage):**
  three seemingly separate breakages (gain_2p2s "busted", "unknown op
  'process_batch'", BPSK "0 recovered") were ONE root cause —
  `start_gnuradio_server` early-returned whenever ANY server was running, so
  after a multi-chip host every later project ran against the STALE
  MultiChipSimServer on 58950. Fixes: start is idempotent only for the SAME
  project, otherwise restarts on the same port; `_after_project_loaded`
  re-hosts a running server on File>Open. TRIAGE ORDER LESSON: when
  "everything broke at once", suspect ONE shared component (the port-58950
  server) before per-example causes. Gate:
  test_server_rehost_on_project_switch.py (mutation-proven).
- **gain_2p2s `server_port=0` (the trap's second occurrence):** gen_grc.py
  still wrote a port that connects nowhere; must be 58950 (placeKYT's default
  host bind). A `.grc` shipping `server_port=0` = "GRC does nothing".
- **Inter-chip landing cells now RENDER** as a TRANSIT marker faced toward the
  adjacent occupied bus cell (the destination port's landing cell is
  build-programmed but covered by no design-level route). Verified GRAPHICAL
  ONLY: words flow end-to-end (real-client repro, all four streams 256/256).
  The continuous cross-chip route HIGHLIGHT remains open (task).

---

## LIVE coefficient writes end-to-end + multi-chip/multi-cell tuning + re-P&R canonicalization 2026-08-13

Live tuning: a GRC slider retunes the running fabric (sim + hw) with no
rebuild. The server half existed with ZERO callers; plumbed end-to-end.

- **DESIGN:** `engine.port_config.live_coeff_writes` resolves every SINGLE-CELL
  block whose cell program stores a param as a SAME-NAMED DataWord (the
  GainBlock pattern) to `{block: {param, hop, dest, to_word}}`; `to_word`
  re-instantiates the block with the new value and reads back the data word —
  the exact fixed-point conversion, no per-block table to drift. The server
  applies WRITEs on BOTH param paths (burst `grc_params` header + the
  standalone `set_grc_params` push) and BOTH backends (sim injects the
  IDENTICAL WRITE word hw sends over USB). The client marker's `set_gain`
  updates the advertised params AND fires a fire-and-forget push.
- **TRAP (would have shipped a wrong-cell demo): GR codegen CONSTRUCTION order
  is NOT the .grc walk order** — order-based name reconstruction keys
  same-type blocks SWAPPED (slider A retunes cell B). Fix: explicit
  `block_name` param on the marker + yml (the placeKYT block name, verbatim);
  REQUIRED for multi-instance designs.
- **TRAP (full regression caught it): a GRATUITOUS coefficient WRITE is not
  free.** Writing whenever the advertised value wasn't in an empty dedup cache
  fired WRITEs on every Run, and on BROKER-ROUTED layouts a manhattan-hop
  WRITE misdelivers into the wrong cell. Fix: the dedup cache is SEEDED with
  each block's DESIGN value — a design-matching advert never writes;
  `set_chip` resets the cache to the seed. CAVEAT: a REAL slider change on a
  broker-routed design still uses the manhattan hop — verified sound for
  port-fed heads and straight abutted chains; corridor-accurate hops for
  arbitrary cells are follow-on.
- **TRAP: `from gnuradio import kyttar` resolves to the INSTALLED
  dist-packages OOT even with the repo path first on sys.path** (namespace
  package). Fix: grc_instantiate_check.py aliases the repo module into
  ``sys.modules['gnuradio.kyttar']`` — the gate is repo-coherent; the
  user-facing staleness signal stays with the grcc-smoke skip.
- **MULTI-CHIP LIVE TUNING (2P2S):** `multi_chip_live_coeff_writes` re-bases
  each tunable block's WRITE to its CHAIN HEAD with the SAME composite hop
  arithmetic the streams ride (far-die hop = local − Σ transit-chip bus
  crossings). gain_2p2s ships FOUR live sliders, each pinned by block_name —
  gate proves far-die + head retune with zero crosstalk
  (test_multichip_live_writes_retune_each_die).
- **MULTI-CELL / MULTI-PARAM tunables (shape-invariant only):** the resolver
  map is `{block: {params, hops, to_writes}}` — every same-named-DataWord
  param of a block (AGC's reference/rate/max_gain), any cell (CoherentRX's
  kp/ki in its string-keyed cell — placement cells carry `cell_id`, the
  orientation-independent key). `to_writes(values)` re-instantiates and DIFFS
  the compiled data words and raises **ShapeChange** when anything but
  non-face data-word VALUES differs — a shape-changing value is REFUSED
  atomically, never half-applied.
- **RE-P&R ON A PLACED .kyt — the double-rotation root cause:** planners model
  CANONICAL shapes and apply orientation as a RELATIVE transform; a re-opened
  .kyt's blocks still carry the previous P&R's rotation, so every feasible
  plan applied double-rotated → overlap/off-grid on EVERY attempt. Fix:
  auto_pnr canonicalizes block orientations pre-sweep (inverse D4 op list).
  MUTATION-PROVEN via the shipped-qpsk re-P&R gate.
- **FAILURE MUST RESTORE:** the sweep clears routes per attempt; a total
  placement failure used to raise with the design at ZERO routes. auto_pnr now
  snapshots the full pre-sweep state (placements + route/out_tag per net) and
  restores it verbatim before raising
  (test_total_pnr_failure_restores_placements_and_routes).
- **BOUNDARY:** grcc reads the INSTALLED ymls — a new marker param needs
  `gr-kyttar/install.sh` before GRC GUI regeneration picks it up (the
  grcc-smoke gate self-skips with a named stale-install reason).

---

## Example-audit round 6/6b — QPSK import fixed, Route All quality, GUI import pre-place, metrics gaps closed 2026-08-12

- **QPSK IMPORT (two compounding apply-path defects):** (1)
  `_abut_single_cell_terminals` — a serpentine-era re-seat pass — MOVED
  single-cell blocks into the already optimally-tight CP-SAT pack (overlap →
  legality throw); CP-SAT already enforces the single-cell in≠out split
  in-model, so the pass is SKIPPED for CP-SAT plans. (2) The flat 10 s solve
  limit starved HEAVY designs (QPSK ≈ 50 block cells needed 25 s); the limit
  is now ADAPTIVE (≤24 cells → 10/6 s, heavier → 25/15 s).
- **GUI import pre-place destroyed auto_pnr's virgin geometry:**
  `MainWindow._import_grc` ran a free-standing `auto_place()` BEFORE
  `auto_pnr`; auto_pnr snapshots CURRENT placements as the virgin geometry
  every sweep attempt re-plans from, so the pre-place replaced the import-time
  geometry with an already-packed layout → the position-dependent serpentine
  planner derived overlapping plans on EVERY attempt ("overlap at (6,1)").
  WHY VERIFICATION MISSED IT: the debug harness mirrored the INTENDED sequence
  instead of executing the GUI handler — a hand-rolled mirror of a GUI flow
  verifies nothing about the GUI flow. GATE:
  `placekyt/tests/test_gui_import_userpath.py` drives the REAL
  `MainWindow._import_grc` offscreen (only modal inputs stubbed),
  mutation-proven.
- **COHERENT RX "snake":** the MENU "Route All" (`use_bus="auto"`) ran the
  greedy per-net BFS first (no ordering retries / broker-quality selection).
  Fix: the menu handler passes `use_bus="always"`, same as import. **TRAP
  (cost a full regression): never change `"auto"`'s greedy-first semantics
  inside `_run_router`** — the per-block DUT harness rides
  `auto_route_all()`'s default `"auto"` and 451 verification tests failed at
  once under bus routes. Route-quality policy belongs at the CALLER, never in
  the shared mode the harness depends on.
- **INV-22 infrastructure exemption:** CrossoverBlock is a done block with NO
  GR counterpart (routing infrastructure).
  `test_grc_binding_complete._INFRASTRUCTURE_BLOCKS` exempts it and asserts
  the manifest AGREES via `grc_block: "(none …)"` so a real DSP block can
  never sneak into the exemption.
- **Manifest rows UN-HIDE blocks and wake every manifest/catalog-driven gate:**
  adding QPSKSlicer/Crossover made them palette-visible, so the
  saturation-coverage roll-up immediately demanded entries — as designed.
  TRAP: QPSKSlicer first landed in RATE_1IN, which drives ONE port — a
  paired-input block passes VACUOUSLY there. Match the saturation list to the
  block's port arity, then PROBE the drive really produces the reference
  stream (it moved to REAL_2IN with a genuine paired drive).
- **METRICS-TABLE GAPS:** every block in every shipped example is now a real
  dashboard row — QPSKSlicerBlock (load-bearing in the BER-0 modem with ZERO
  per-block coverage — the INV-25 trap in the flesh) got the full
  GR-equivalence gate (`constellation_qpsk().decision_maker` golden,
  verification/tests/test_qpsk_slicer.py); QAM16ComplexCostasLoopBlock's
  proven whole-chain BER-0 drive now emits its report. NOTE:
  a local scratch probe (proto_qam16_rx_ber.py, untracked) was a
  FAILED-topology dead end at BER 0.86 — never cite it.

---

## Example-audit round 5 — QT time sinks STRAND finite-stream tails (the real blank-scope mechanism); compact duplex packs; port fly-line proven+drawn 2026-08-12

- **THE TRUE BLANK-SCOPE MECHANISM (an earlier stale-tab diagnosis was wrong;
  this one is pixel-proven):** the GR scheduler STRANDS the tail of a FINITE
  stream — measured in PURE GR with a held python source: a 200-sample burst
  delivers only 192 items to a QT time sink (8 stranded), an 8-item burst
  delivers NOTHING, and WORK_DONE does NOT flush the tail. So a scope sized ==
  its burst NEVER fills; an un-plotted time sink renders a DEFAULT-axis frame
  (1024/srate) — that axis arithmetic is the diagnostic fingerprint of a
  starved scope. **FIX:** the display sinks loop the genuine one-batch result
  (`server_repeat=True` — display-only, no chip re-run) on echo, data_link,
  cw-RX, psk31-RX. PIXEL-PROVEN via offscreen qwidget renders + a
  `nitems_read(0)` probe. The cw/psk31 RX chains WERE DECODING ALL ALONG —
  every "RX broken" report was display stranding.
- **PORT FLY LINES (user's graphical-artifact hunch verified with a sim trace,
  then fixed):** a port→ABUTTING-landing net had `route: None` yet the
  injection trace showed the word arriving, hopping one cell, and executing —
  physically sound, only un-drawn. The bus router's direct-injection branch
  now also covers the adjacent case and emits the real 2-point route.
- **COMPACT DUPLEX PACKS:** `_pnr_abutment_first` now applies to BOTH
  topologies (audio_meter import: 81→37 cells). NEW SAFETY NET: the sweep runs
  compact attempts first, then the same reserve sweep with the pack OFF —
  ~1-in-5 imports had every compact pack wall one egress net, and the sweep
  must degrade to the always-routing family instead of failing the import.
- **THE SWEEP CONTAMINATION TRAP (cost half a day):** interleaving
  compact/serpentine attempts broke the bpsk duplex import even though BOTH
  families work alone — the SERPENTINE placer's flow order reads CURRENT block
  positions (documented non-idempotent), and the loop's "revert" was a silent
  no-op, so every serpentine attempt after a compact one re-planned from the
  compact pack's scattered blocks. FIX: capture the VIRGIN import placements
  once and restore before EVERY attempt.
- **PANEL TRANSCEIVERS MUST STAY PER-SAMPLE — fresh evidence:** the cw .grc
  forced pipelined produced a RUNAWAY keyer (250,095 TX words for a
  1,224-sample message): slammed chars overwrite the fetch cell's ROM region
  mid-play. Real panel saturation needs HOST-side completion back-pressure —
  future work, documented; the server refusal exists for this.
- **UI:** the project-open fit rect unites SRAM/peripheral PanelItems with the
  cell grid (`test_fit_includes_panels.py`); `_after_project_loaded` calls
  `WaveformPanel.clear_traces()` so switching projects never leaves stale
  traces (gated in test_waveform.py, from round 4).

---

## Example-audit round 4 — true abutment placement, fan-out-port keep-off, broker machinery flushes 2026-08-11

- **Single-cell blocks sat one cell apart instead of abutting** because the
  CP-SAT abutment-first placer only ran as the rescue path. FIX:
  abutment-first designs (block topology) take the CP-SAT pack as PRIMARY
  (serpentine stays the fallback); safety is the unchanged auto-P&R acceptance
  loop. data_link = one ABUTTED 11-block column, 45→16 cells, 2 routed nets.
- **Two real bugs the abutted packs exposed:** (1) a fan-out port abutting a
  fed block is UNROUTABLE-SOUND (INV-24 geometry): the port's single fwd_face
  cannot serve the sibling arm, and the MAZE escalation then shipped a
  silently-wrong two-direction port (tremolo: 200 outputs, all zeros). FIX:
  hard CP-SAT keep-off (fan-out port ⇒ fed INPUT cells at manhattan ≥2) + an
  auto_pnr acceptance gate. (2) the bus router had NO abutment fast-path — an
  ABUTTED pair got a corridor wrapped around the whole source block. FIX: an
  abutment pre-pass in `_route_chip_bus` (every net from that source cell must
  abut the SAME target; a mixed abutted+routed fan-out keeps the fully-routed
  path).
- **Three more flushed out by full regression:** (a) `_apply_brokers`
  EARLY-RETURNED on a design with NO brokers at all — fully-abutted packs are
  the first such designs, so the abutted fan-out / replicated-WRITE machinery
  never ran (fanin2: one last-wins WRITE). The early return is gone. (b) the
  CP-SAT single-cell in≠out rule exempted PORT consumers — packs cornered a
  join against x16_out with input==output face; the rule now treats a
  chip-output egress as the consumer. (c) KNOWN LIMIT: designs carrying a
  StreamSplitterBlock keep the serpentine layout — the abutted pack
  intermittently breaks the splitter's replicated exit tail; abutment-first is
  auto-disabled for them until proven.
- **ROUTE-QUALITY SELECTION IN THE SWEEP:** clean layouts are scored by TOTAL
  route excess; near-optimal (≤4) accepts immediately, else the sweep
  continues (budget-bounded) and the lowest-excess clean layout wins.
- **GRC only re-reads a flowgraph from disk on explicit open/reload** — after
  pulling regenerated examples the user must reload the .grc (or restart GRC);
  screenshots showing OLD scope axes are the tell.

---

## Example-audit round 3 — strict shortest-path router, the deadlock-cycle guard, the blank-scope display contract 2026-08-11

- **THE ROUTER (bus_router) had FOUR compounding defects, all fixed:**
  1. *Discounted corridor sharing*: `_bus_bfs` priced a fresh cell 5 and a bus
     cell 1, so a net would detour up to ~5× its manhattan length to ride
     someone's corridor (audited: 21 cells for manhattan 5; 25-for-3 weaving
     beside its own path). Now STRICT shortest-path (`_HOP_COST`/cell);
     sharing and straight-runs are sub-hop TIE-BREAKS only (direction-aware
     Dijkstra with a turn penalty).
  2. *Own-emit-cell broker exclusion*: `output_emit_cells` de-prioritised the
     source's emit neighbour for ALL nets including the net whose own source
     emits there — every A→B across one free cell staircased to a far-side
     broker. A net may now broker ON ITS OWN emit cell (foreign emit cells
     stay last-resort).
  3. *Distance-blind broker choice*: the broker was picked by bus/spine
     membership BEFORE routing. Now ALL legal candidates are routed and the
     shortest (then fewest-turns) wins.
  4. *v2 backbone always won + portfork walls*: v2 is kept only if the legacy
     loop can't route everything strictly SHORTER; only the FARTHEST port-fork
     sibling rides straight (near ones broker at the fork); orderings gained
     `portfork_far` and the best ordering is picked by (routed, total length).
  Also: a terminal broker's onward `bus_dir` is no longer pre-committed to its
  arrival direction — the first transiting net claims it; pre-committing
  walled 3-arm splitter fan-outs.
- **THE DEADLOCK-CYCLE GUARD (promoted to INV-32):** a block's OUTPUT corridor
  must NEVER transit a broker that DELIVERS INTO that same block. The
  strictly-shortest router routed data_link's f2c→pack net back through f2c's
  own input-delivery broker: per-sample everything passed; under saturated
  drive the chip HARD-DEADLOCKED (sim stop_reason='Deadlock', zero output).
  The router hard-forbids it in BOTH routing orders;
  `test_shipped_kyt_saturated_matches_per_sample` (data_link) is the pin.
- **ROUTE-QUALITY RATCHET:** `verification/tests/test_route_quality.py` audits
  every shipped example .kyt — per-net excess ≤ 8 over manhattan, no cell
  revisits, per-file total excess pinned. Remaining nonzero excess is
  placement-forced.
- **THE BLANK-SCOPE DISPLAY CONTRACT:** (1) a QT `time_sink` draws NOTHING
  until it receives a FULL `size` buffer — size every scope to the burst its
  chain actually delivers. (2) `kyttar.sink` emits the chip stream as
  q15/32768 FLOATS — byte/ASCII values plot at ~0.002 on a 0..250 axis;
  rescale ×32768 in front of any word-value scope. Promoted to an AGENTS §5b
  bullet: a shipped demo's scopes must be sized and scaled so the verified
  output is actually VISIBLE.
- **SATURATION TRUTH TABLE for the examples (evidence, not lore):** data_link
  + audio_meter — pipelined and PROVEN saturated bit-exact (no panel, no
  join). audio_effects — must stay per-sample: the fork→join arms are the
  CROSS-BLOCK INV-20 reconvergent fan-in; saturated echo returns the right
  COUNT with every VALUE wrong (join sample-skew);
  `test_saturated_join_skew_KNOWN_LIMIT` flips when a cross-block
  serialize-lock ships. channel_selector — per-sample (FreqXlatingFIR is
  saturation-bespoke). cw/psk31 — per-sample (panel contract; the server
  REFUSES pipelined for panel designs). bpsk_modem — verified saturated.
- **ADVERSARIAL REVIEW of the router fix (4 verified findings, all acted on):**
  the DRC wait graph was blind to cycles through blocks (fixed as `check_bus`
  check (c), scope deliberately narrowed to the deadlock-CERTAIN own-block
  shape — a general block-supernode graph FALSE-POSITIVED the proven-saturated
  coherent RX; static cycle tests over-approximate, the saturated example
  gates are the empirical authority); a share-test rewrite had dropped the
  only coverage of the hazard-disabled fallback (restored); `_HOP_COST`
  dominance is now asserted per chip against real W×H.
- **OPEN DEFECT (pre-existing, marked strict xfail with full evidence):**
  `test_converter_flavors_grc.py::test_runs_live_recovers_input` deadlocks —
  the ComplexMixer's MIXED 2-rail fan-out (yq ABUTTED, yi BROKERED) is not
  re-sequenced: the built mixer output cell holds ONE Write/Jump pair instead
  of two steered rail pairs; DualFloatToComplex starves. Reproduced
  bit-identically at pristine HEAD in a clean worktree — NOT the router
  change. The `_apply_brokers` mixed branch exists for exactly this shape but
  does not fire for this exit cell. Shipped examples unaffected (ssb_weaver
  fan-out gates pass).

---

## Example-audit round 2 — GRC contracts are THREE-layered; brokers replace the duplex weave; demo stimuli must be real 2026-08-10

- **THE THREE-LAYER GRC CONTRACT (root of every dtype bug):** (1) the yml
  declares dtypes (drives GRC's red arrows), (2) the MARKER's `io_signature`
  enforces itemsizes at `connect()` (runtime truth — a byte-out slicer vs a
  float-out yml means the flowgraph can NEVER actually Run in GRC, and this
  sat latent for a long time), (3) the INSTALLED tree is what the user's GRC
  actually reads (repo edits are invisible — and look BROKEN — until
  `install.sh`). The static lint checks (1);
  `test_examples_grc_instantiate.py` drives GRC's own Platform +
  generated-top-block construction with repo ymls+markers, closing (2). A
  schema-invalid yml (missing `file_format`) silently loads as "Missing
  Block" and its connections DROP: never edit ymls by regex without
  re-validating the GRC schema.
- **DEMO STIMULI MUST BE REAL:** both transceivers shipped `rx_sig`
  PLACEHOLDERS (silence / a constant) — every headless gate passed while the
  user saw NO decoded output. The .grcs now embed genuine stimuli (a rect
  keyed envelope + EOT blip; diff-encoded Varicode ±0.9 symbols) and
  `test_examples_grc_userpath.py` runs the SHIPPED .grc (GRC-generated)
  against the SHIPPED .kyt asserting TX bit-exactness AND the decoded RX
  text. A gate that substitutes its own client script does not verify the
  artifact the user opens.
- **BROKERS REPLACE THE DUPLEX WEAVE:** the RX tap/tailxo Crossover relays and
  the on-corridor RX emit are gone. Standard build BROKERS (routes ending one
  cell short of the target — `broker_plan` handles PORT-source conns too) tap
  the corridors; the RX emit sits OFF the return corridor behind a fork broker
  whose deliver ENTRY the template precomputes by assembling the build's own
  `_broker_program` (entry addresses depend only on program structure, so
  template and build agree by construction). Remaining corridor-shared cells
  are ONLY CrossoverBlocks at genuine crossings. Gate:
  `test_kyt_route_transits.py`.
- TX-only psk31_tx/cw_tx examples removed (transceivers supersede;
  `psk31_tx_golden.py` lives in psk31_transceiver/ now).

---

## Fan-out lifted (splitter + counting join), Conjugate chain fixed, duplex port cell freed 2026-08-10

- **CONJUGATE (root cause found by inserting it into channel_selector):** the
  `_apply_brokers` mixed-fan-out merge (a) MISCLASSIFIED a plain complex
  abutment pair (out_re+out_im → ONE consumer) as a fan-out — the condition
  never required distinct targets — and (b) passed the pre-encoded `_HOP1_CNT`
  (30) where a RAW hop belongs, authoring words that sail 30 cells past the
  consumer and leak out the port. The auto-placer's non-determinism was why
  some chains dodged it. Fixed both; test_conjugate_chain.py pins BOTH
  abutment and routed topologies.
- **FAN-OUT (four mechanisms, each proven end-to-end in
  test_fanout_chains.py):** (1) `_patch_single_rail_multi_handoff` — a
  single-WRITE source cell's exit tail is REWRITTEN into N replicated WRITEs
  (+ per-arm JUMPs desc-by-hop); the old path silently delivered ONE arm
  ("last wins"). Over-budget cells BuildAbort NAMING kyttar_splitter
  (`raise BuildError` was unreachable — DRCError is a dataclass, not an
  exception; new BuildAbort). (2) StreamSplitterBlock: 1-cell relay with a
  RESERVED 14-HALT exit tail (≤8 arms), GRC-placeable, manifest done (exact vs
  blocks.copy). (3) the importer AUTO-SPLICES a splitter for single-rail
  fan-outs to ≥2 different blocks / ≥3 inputs, and behind a port fanning ONE
  stream to ≥3 blocks (the port fork is only proven to 2 arms). (4) bus
  router: same-source nets FORCED out the committed source direction.
- **COUNTING JOIN (the race the splitter exposed):** deepest-arm election
  cannot order EQUAL-DEPTH sibling arms — the combiner fired with a stale
  operand (placement-lucky passes!). Add/Multiply/Subtract carry a `join`
  entry: toggle counter, fires on the LAST arrival in ANY order. THREE traps:
  (a) an entry with no matching LABEL resolves to the program's FIRST
  instruction — label the compute body `default:` when anything precedes it;
  (b) GOTO assembles to an opcode-0x7 word and the handoff patcher REWRITES
  EVERY 0x7 word — use fall-through/BR.cond only in patchable cells; (c) R0
  is both the ALU result register and input a0 — the counter must
  save/restore it.
- **DUPLEX PORT CELL:** the TX ctl moved off the panel-port cell; a plain
  routing cell legally merges two inbound faces onto one exit. The
  face-setting net must source from a RAW_OUTPUT_HOPS block: sourcing it at a
  non-RAW block lets the build patch its emit cell and kills the TX outright.
  GUI note: "the output path goes through the SRAM panel and shows up on the
  other side" is the panel ROUND TRIP (ctl → x1_out → SRAM device → x1_in →
  consumer) — correct by design.

---

## PSK31 FULL TRANSCEIVER — the shared-panel duplex architecture 2026-08-09

TX (SRAM Varicode encoder chain) + RX (SRAM Varicode DECODER chain) duplex on
ONE chip with ONE shared panel — TX sample-exact vs the psk31 golden WHILE RX
decodes text exactly, headless and through the genuine GR client.

- **RX-tail template first** (engine/panel_pnr._apply_rx_template): the
  decoder's reads route through its embedded SramController's `lookup` entry,
  so EVERY read writes its own R3/R4 push-read descriptors — that
  per-transaction protocol is what makes a SHARED panel safe (no global
  descriptor state to clobber).
- **Shared images by addr_base:** SramControllerBlock (+ the Varicode encoder)
  gained `addr_base` — the lookup adds it before the read; panel_requirements
  ships the table at the offset; synthesize_panel merges two clients' images
  and REFUSES overlaps. TRAPS: (1) `ADD Rx, Ry` leaves its result in R0
  (accumulator ISA) — a missing store-back read the UNOFFSET address, hit
  sparse 0, and unpack(len=0) underflowed the emit loop into an INFINITE
  zero-bit stream (the KeepOneInN template is the canonical
  ADD-then-MOVE-back exemplar). (2) the base variant overflowed the 32-word
  cell — conditional slimming keeps base==0 byte-identical.
- **Duplex corridor geometry:** RX input rides THROUGH the TX crossover
  (transit); RX ctl reaches the panel THROUGH the TX ctl cell; RX egress
  exits via the TX crossover's track_c — now a DATA track with the RX
  stream's own wire tag. CrossoverBlock gained `restore_face` (the broker
  self-restore) because relays that OTHERS transit must not leave the face
  flipped.
- **Engine fixes the transceiver forced (all regression-gated):**
  RELAY_LANDING — nets into a CrossoverBlock land ON the cell (entry runs
  there), never stripped to an abutting broker; route[0] face pinning skipped
  only when it IS a RAW source's own cell; refresh_panel_params must not
  re-derive hop_c for a DATA track_c; params that ADD instructions shift
  entry addresses — ALWAYS re-resolve descriptors after the LAST param
  mutation; stream_targets' chain-walk honors an EDGE-level out_tag (two
  streams sharing one exit relay).
- **Debugging method that worked:** the sim's enable_trace/get_trace per-cell
  event stream — every root cause was READ off the trace (who executed what
  pc, where a word landed, which face it left on), not guessed.

---

## CW FULL TRANSCEIVER — the streaming fixed-unit decoder + the kicker-form duplex 2026-08-09

TX (SRAM Morse keyer) bit-exact vs the ITU-R golden WHILE the same chip decodes
incoming keyed audio — shared panel (keyer ROM + reverse LUT at addr_base
16384), headless AND through the genuine GR client.

**The architecture decision:** the ADAPTIVE two-pass CW decoder (global-min
unit, panel scratch + replay) is verified as CELLS + PROTOCOL through a
HOST-ORCHESTRATED harness — it is NOT a self-contained live chain. Rather than
fake it, the block gained a SECOND, honest mode: `unit_samples > 0` = a 4-cell
STREAMING skimmer locked to the configured unit (exactly how real CW skimmers
run, and the keyer's samples_per_dot IS that configuration). The adaptive mode
+ all its tests are byte-identical untouched.

Traps (each cost a debug cycle):
- **ALU results land in R0 — ALWAYS.** `SHL R{state},#1` does NOT shift in
  place. The KeepOneInN ADD-then-MOVE-back idiom is THE pattern.
- **RAW_OUTPUT_HOPS on every self-hop-authoring block** — without it the build
  re-patched authored `@N` hops into a livelock. A block that writes literal
  hops in its templates MUST declare RAW.
- **Never let an auto-allocated input register land on R0**: the emit cell's
  push-read landing register auto-allocated to 0 and the dest-0 delivery
  wedged the panel pump. Pin push-read landing registers explicitly.
- **End-of-stream is not a thing on an async fabric**: a trailing OFF run
  never flushes (no level change). The honest streaming convention: an EOT
  BLIP (>=2u silence then >=1 ON sample) flushes the final char and is itself
  never decoded; the reference mirrors the chip (NO synthetic flush).
- **Exact-address overlap checks pass sparse interleavings that are still
  wrong**: the LUT at 12288 sat INSIDE the keyer's char*128 ROM span with no
  exact collision. Place shared-panel regions above the co-tenant's full
  RANGE, not just off its populated addresses.
- Word gaps: spaces are dropped (documented v1 limit); LUT[1]=' ' handles the
  leading-gap seed.

Duplex geometry: the keyer's completion owns the TX crossover's track_c, so
the kicker-form RX egress crosses on its own relay pair — the second
duplex-template branch.

---

## channel_selector + audio_effects — complex-rail synthesis, single-fire JOINS, three engine limits 2026-08-09

- **FIXED — `re`/`im` I/Q-rail synthesis (`_iq_sibling`).** The importer's
  complex edge split only knew the `i`/`q` naming; converter-class blocks name
  their rails `re`/`im` (`out_re`/`out_im`). Their Q rails were silently never
  wired — the all-zeros channel_selector. Diagnosis path worth repeating:
  print the imported net list and LOOK for the missing sibling nets — five
  complex edges must yield ten rails.
- **NEW — single-fire dataflow JOINS.** A join used to fire the combiner once
  PER ARM. Now: join blocks declare a data-only `sink` entry;
  `grc_import._elect_join_triggers` elects the DEEPEST arm as THE trigger via
  `Connection.entry_override`; landings/stream_targets/sim_bridge carry EVERY
  landing of a fan-out stream per sample, data-only arms first, trigger last.
  CONTRACT: joins are per-sample-paced; a slammed burst can race operands.
  (Superseded for equal-depth arms by the counting join, 2026-08-10 above.)
- **LIMIT — single-cell complex-in→complex-out blocks (Conjugate) mis-deliver
  under the auto abutment handoff** (fixed 2026-08-10, see the fan-out entry).
- **LIMIT — port fan-out caps at ~2 arms** (a 3rd corridor reliably fails
  placement) — hence racks of 2-arm effects, and the importer's ≥3-arm
  splitter splice.
- **LIMIT — a block's output cell cannot fan out** without the splitter
  machinery (fixed 2026-08-10).
- **auto_pnr placements are NONDETERMINISTIC across runs** (wall-clock
  `time_budget_s` stops the attempt sweep at different points) — ship + gate
  the `.kyt`; "works on my import" is no evidence.
- **Real-GR-client failures the gates caught:** (1) the `keep_one_in_n` marker
  faked its rate change client-side (`set_relative_rate` + partial-return
  `work` on a `gr.sync_block`, whose return is BOTH produce and consume) — the
  input tail never drained and `tb.run()` hung forever. Marker convention:
  plain 1:1 pass-through — markers must never fake rate changes. (2) a
  REQUIRED `float_to_complex` `im` input left unconnected — splice
  `blocks_null_source → f2c.im`; the importer drops the null source.

---

## audio_meter (two-stream analog duplex) — regime-mirroring golden lessons 2026-08-09

Reached its DERIVED bounds (audio 148/222 LSB, meter 0.0044/0.066 dB) only
after three root-cause fixes — none was "widen the tolerance":

- **The GR golden must drive feedback blocks in the CHIP'S verified regime.**
  The chip AGC's gain register is Q15 (attenuating); the golden ran `agc_ff`
  UNCAPPED. Near zero-crossings uncapped GR gain exceeds 1.0, the chip clamps,
  and the trajectories split ~15% for hundreds of samples. Symptom signature:
  a large error that DECAYS at the loop rate is a REGIME mismatch, not
  accumulation. (First theory — upstream Q15 warm-up error integrating in the
  loop — failed a back-of-envelope by 300×. Do the arithmetic before
  believing a mechanism.)
- **Squelch closing time is loop arithmetic:** the power IIR (alpha 0.01)
  decays ~0.044 dB/sample; from −9.6 dB the −25 dB threshold needs ~450
  silence samples. A 96-sample tail demoed a squelch that never closed. GR
  emits EXACT 0.0 when gated; tiny nonzero tails mean the gate is still open.
- **Derived out_tags must fit the 5-bit DEST field:** hashed stream tags of
  36/47 wrap silently — zero egress. Confined to 2..31 with collision probing.
- **Gate hole closed (INV-22): yml `make:` kwargs vs shim `__init__`.** The
  installed marker took a long-dead arg while its yml passed the new ones —
  real GRC Run would TypeError, and the binding gate never checked constructor
  acceptance. New static (ast) case
  `test_done_block_yaml_make_kwargs_accepted_by_shim` covers every done block
  (mutation-verified).
- The real-GR-client gate class grew its first TWO-STREAM duplex case
  (audio+meter source/sink pairs through the DuplexRendezvous on one hosted
  chip).

---

## Panel corridors: GUI-visible routes, anchored ports, route-derived params 2026-08-09

- **Don't invent a second route convention:** panel corridors were authored as
  routes stopping one cell short with delivery by hop count, while the rest of
  placeKYT uses routes that START/END ON the endpoint cells (the GUI renders
  that). Template corridor routes now include their endpoints; `_phys_pts`
  strips the trailing on-target waypoint so realized faces/hops are identical.
- **"Less surface" on the PortMap was false economy:** the panel-facing return
  input was left OUT of the PortMap, so the GUI had no cell to anchor the net
  to and interactive routing had no target. `build_port_map` now exposes the
  panel return port from `panel_requirements`. A port the model can't resolve
  is a port the USER can't route.
- **`refresh_panel_params`** (engine/panel_pnr.py, called by build): every
  placement-derived panel parameter is RE-DERIVED FROM THE CURRENT ROUTES at
  build, with named warnings when a value changes — 'the routes are the truth'
  holds for panel params exactly as for faces. Gates: stale-corrupted params
  rebuild to EXACT output; refresh is a NO-OP on a fresh auto-P&R. KNOWN GAP
  (unchanged): the refresh re-derives only the FIRST panel-backed block's
  params — a duplex design's second (RX-half) panel block stays template-only.

---

## The REAL GR client loop gate — a passing socket test is NOT GUI verification 2026-08-09

- The CW GUI run garbled while a "server path verified" socket test passed —
  the test was UNFAITHFUL: it sent a plain `process_batch` RPC, but the REAL
  `kyttar.source` with a `stream_id` dispatches `process_batch_duplex`, a code
  path the test never touched.
- **ROOT CAUSE:** `_process_batch_duplex` honored `pipelined: true`
  UNCONDITIONALLY → the whole char burst queued at fabric speed → the CW
  char-slam (302/1530 samples). FIX: `SimServer.force_per_sample` — the HOST
  sets it when the hosted project has SRAM panels; both pipelined branches
  REFUSE the header and run per-sample. The safety precondition is enforced
  server-side, not trusted from the flowgraph. Result: the genuine client loop
  is bit-exact for BOTH transceivers.
- **THE GATE CLASS (the actual lesson):**
  `placekyt/tests/test_gr_client_loop_examples.py` runs the REAL client stack
  — genuine kyttar.source/sink + marker chain in a real gr.top_block under the
  GNU Radio interpreter in a SUBPROCESS, against the real hosted server. That
  is what pressing Run in GRC executes, minus the literal Qt window. **Rule:
  no 'works over the server / in GRC' claim unless THIS class of gate ran. A
  hand-rolled RPC is not the client; the client is the client.**

---

## CWKeyerBlock v2 — STANDALONE ASCII-in transmitter; record flow control kills the startup race 2026-08-09

- v1 lost the first "-." of the first 'C' ("CQ…" keyed as "NQ…"): at startup
  the pipeline is empty, triggers arrive with minimal separation, and the
  later records' push-reads OVERWRITE the player's live step/count registers
  mid-play. Pacing was timing-luck, not a guarantee.
- **v2 = a genuine standalone transmitter.** Input = ASCII bytes at runtime.
  The panel holds a MESSAGE-INDEPENDENT Morse ROM (one run-record region per
  code point at `char * 128`; the sparse panel's unwritten words read 0 = an
  implicit END record, so unmapped chars key silence for free). The fetch
  cell's `char` entry computes the region base with ONE `SHL #7`; the PLAYER,
  after each record, sends a COMPLETION KICK through the crossover's control
  track to the fetch's `next` entry — record sequencing is FLOW-CONTROLLED by
  handshake: the next fetch physically cannot start mid-play. Panel READ
  AUTO-INCREMENT is the enabler (per-word address rewrites need 37 words —
  over the cell).
- **Template bug found by the panel log:** the crossover's `dest_a` resolved
  the controller's input register BY NAME with a silent R0 fallback — every
  key vanished into the accumulator. Fixed generically via
  `catalog.resolved_io`. **Lesson: never resolve a register by port NAME with
  a silent default — resolve structurally, and make missing lookups raise.**
- **grcc toolchain gotcha:** GRC loads the SYSTEM-installed block ymls FIRST —
  they shadow `~/.local/share/gnuradio/grc/blocks` AND `GRC_BLOCKS_PATH` (and
  there's a `~/.cache/gnuradio/grc` cache on top). Until install.sh re-runs,
  grcc emits calls against the stale installed yml.
- **KNOWN LIMIT (documented, honest):** byte-level saturation — a NEW
  character arriving mid-character truncates the keying. Character pacing is
  the contract (physically ~100+ ms/char of air time vs µs of fabric; the GRC
  server's per-sample path provides it naturally).

---

## GUI "Run as GNURadio Server" of panel designs: register panels on EVERY hosting path 2026-08-09

- `_setup_panels` (register the SramPanelDevice + preload the ROM + held-ack)
  ran ONLY on the local-Sim path; all three SERVER hosting paths (server
  start, reset-RPC rehost, per-batch dirty rebuild) loaded the bitstream and
  never registered panels — no output, empty panel Inspector. Fix: run it
  after every server-side `engine.load` (Qt-free, safe on the server thread).
  Gate: `test_server_panel_examples_e2e.py` drives the EXACT GUI server path
  on the SHIPPED .kyt over a real socket.
- **LESSON:** a subsystem hook added to one run path must be audited across
  EVERY chip-hosting path (local run, server start, rehost, dirty-rebuild,
  hardware). "The headless pipeline is the same code path as the GUI" is only
  true for the parts you proved.

---

## SRAM-panel chains END-TO-END: GRC import → template auto-P&R → build → run 2026-08-09

Both ham TX chains ran genuinely end-to-end through the REAL user pipeline
(`import_grc` → `auto_pnr` → build → sim) with AUTO-GENERATED .kyts. The
previous "proofs" were COMPOSED per-block, and the chains could not run —
FIVE whole-chain-only defects, every one found by RUNNING the placed chain and
reading the sim trace:
1. **No streamed key→address path**: the controller's `read` auto-increments
   and `set_addr` is a separate burst — arbitrary char lookup was impossible.
   Fix: the `lookup` entry (rd_addr := incoming word, FALL THROUGH into read).
2. **Push-read descriptors defaulted to 0** — reads delivered NOWHERE. Fix:
   placement-derived descriptors computed by the panel template from the
   routed return corridor.
3. **WRITE-only bit emission**: the emit cell wrote bits with no per-bit JUMP
   — a downstream BLOCK runs only when jump-triggered, so the consumer fired
   once per char. The block's own SRAM gate was BLIND to this: a PORT captures
   every passing WRITE. **Generalizes: verify a block's emission the way a
   downstream BLOCK consumes it, not only at a port.**
4. **Default-entry collisions at multi-entry relays**: the egress net entered
   the crossover on its DEFAULT entry, turning every envelope sample into a
   panel lookup — a runaway read loop. Fix: `Connection.entry_override`
   (model+IO+build+broker) so each net picks its track.
5. **GRC variables silently dropped**: `interp: sps` kept the block DEFAULTS
   (an all-zero TX that looked like a routing bug). Fix: the importer resolves
   variable-name params from the flowgraph's `variable` blocks.
- New machinery (engine/panel_pnr.py): `synthesize_panel` (importer half) and
  `apply_panel_template` (controller pinned at the panel port, corridors,
  crossover where input and egress corridors cross, descriptors derived
  post-route). auto_pnr fails with NAMED PlacementErrors.
- Build fix that generalizes: the final waypoint of a port→block net faces
  toward the net's TARGET-PORT cell (via `panel_requirements` return_cell),
  not the block's entry cell.
- KNOWN HAZARD → later hardened: back-to-back bursts at fabric speed can swap
  tracks at the crossover (single relay register serves both tracks). Both
  demos PACE injection; the server's per-sample enforcement (see the GR client
  loop gate entry) is the systemic guard.

---

## Ham TX examples (superseded by the transceivers) — durable floorplan lessons 2026-08-08

The TX-only psk31_tx / cw_tx examples were later replaced by the full
transceivers; what survives from building them:
- **PSK31's shaper is the raised-cosine ENVELOPE, not RRC**: its input is the
  symbol stream HELD N samples/symbol; PSK31 = amplitude envelope on
  reversals. The generic BPSK/QPSK modem = zero-stuff upsampler + RRC. The
  envelope depends ONLY on the reversal pattern, so the `bpsk_bit0_positive`
  convention is documentation, not correctness, for a PSK31 TX.
- **Panel-ring floorplan gotcha:** a cell placed INSIDE the panel corridor
  ring is trapped from the outside chain; and `auto_route_all` re-orients
  hand-placed blocks even when every net is drawn — hand-draw ALL routes and
  skip auto-route when nothing is unrouted.
- **Composed-proof topology (when a one-pass sim doesn't exist yet):** verify
  each block on real hardware-sim independently, compose, and SEPARATELY prove
  the whole chain builds+routes on one array — but never conflate "builds on
  one array" with "one-pass end-to-end sim"; say which was proven. (The
  transceivers later achieved the true one-pass proof.)
- **Panel push descriptor HOP_CNT is the port-hop convention** (consumed at
  31, so a SHORT corridor uses a LARGE field); determine it empirically
  against the BUILT bitstream — it is NOT `route_len`.
- **Single-16-bit-port packing:** LOAD, EMIT, and LOOKUP corridors must be
  disjoint; a colliding transit cell BUILDS fine but corrupts at RUNTIME —
  verify by running, never by build-ok.

---

## The SRAM-backed block wave — recipes + walls (5 blocks un-quarantined) 2026-08-07

Five table-heavy ham blocks hit the same measured substrate boundary (INV-29:
~21 LOAD entries / 32-word cell) and were rebuilt SRAM-backed (INV-31) or via
on-the-fly generation. The reusable recipes:

**VaricodeEncoderBlock — the FIRST SRAM-backed DSP block (the recipe).**
- **The pack trick kills BOTH walls:** each Varicode entry packs into ONE
  16-bit SRAM word — code LEFT-ALIGNED at bit 15, length in bits[3:0]
  (INV-34 format). (1) TABLE SIZE → the 128 words live in the panel (address
  == ASCII code point). (2) VARIABLE-LENGTH EMIT → the panel push-read
  returns a FIXED word per symbol; the emit cell walks the aligned code with
  `SHR #15` / `SHL #1` and emits exactly `length` bits + `00`. The variable
  length becomes a small in-cell counter, NOT a variable-length burst across
  the panel port.
- **LOAD phase (once):** a persistent placed SramControllerBlock streams
  `set_addr(0)` then one `write` per word; the controller AUTO-INCREMENTS
  wraddr. GOTCHA: the address counter is CELL STATE — load in ONE persistent
  chip run; re-instantiating the chip per word resets wraddr and nothing
  commits.
- **LOOKUP phase (per byte):** a tiny `emit` cell is the push-read CONSUMER;
  the panel ORIGINATES WRITE(word)+JUMP into the chip input port, landing
  `sram[byte]` in the emit cell AND kicking its entry. Bit-exact over FULL
  ASCII 0..127 through real routing.
- **REAL bug (ISA flag semantics):** `MOVE` does NOT set Z, so a `BR.NZ` after
  a MOVE branches on the LAST flag-setting op. The "test register" idiom
  (`OR R0, R{len}` — a self-op that sets Z without changing the value) fixes
  it; later superseded by folding the loop test into `SUB len,one` (flags
  survive the following MOVE/WRITE).
- **Table transcription trap:** an HTML-scrape of the Varicode table smoothed
  rows into DUPLICATES (impossible in a unique code). Take tables from source
  (fldigi) and self-check the structural invariants (128 unique, 1-bounded,
  no internal '00', ≤10 bits).

**VaricodeDecoderBlock — the reverse of the recipe.**
- **ADDRESS SCHEME:** the codeword INTEGER value directly indexes the panel
  (sparse, 128 words populated, max codeword 955). Works because every code
  starts with '1' (equal value ⟹ equal code). Unpopulated reads default 0 and
  are dropped like an unknown pattern.
- **CHAR_OFFSET (+1) — the load-bearing subtlety:** ASCII NUL is a real code
  point, but storing `char==0` is indistinguishable from an unpopulated read.
  Store `char + 1`; the emit cell subtracts 1. WATCH FOR THIS in any
  reverse-map block whose stored value can legitimately be 0.
- **Sparse addresses can't use auto-increment:** each pair does
  `set_addr(codeword_int)` THEN `write(char+1)` (the encoder's 0..127
  contiguous load could rely on auto-increment).
- **Decode state machine:** a single '0' is AMBIGUOUS (intra-code vs first bit
  of the '00' delimiter) — hold a `pend0` flag; commit it on a following '1'
  (the branchless `cur + cur*pend0` identity), treat '00' as the delimiter.
  Leading idle '0's are skipped.
- **CELL BUDGET was the real fight:** the accumulate state machine went
  41 → 24 instructions via: `CMP Rx, R{zero}` for zero tests (AND/CMP set Z;
  MOVE does not); most-common arm LAST so it falls through into the shared
  HALT; and **split the EMIT into its own cell** (the natural mirror of the
  encoder's push-read consumer). 3 cells, each fits.
- `GOTO` is the unconditional branch (bare `BR` errors); `TST`/`JMP` don't
  exist.
- **PROOF:** BIT-EXACT vs the golden over FULL ASCII + message + random,
  AND round-trip vs the golden encoder, through the real `SramPanelDevice`.

**CWKeyerBlock — move the TIMING FSM off-cell as run records.**
- The single-cell keyer needs ~50 instructions + a ~48-entry Morse table + an
  edge LUT — no partition fits. **The fix:** *run the timing FSM ONCE at
  BUILD time*; the keying schedule becomes RUN RECORDS `(base, step, count)`
  in the panel; the on-chip cell is a ~15-instruction UNIFIED run player
  (`cur=base; loop: emit LUT[cur]; cur+=step; count--`). ONE loop serves all
  four run kinds by choice of (base, step): OFF=(0,0), FLAT=(1,0), RISE=(2,+1),
  FALL=(2+e-1,−1) — `step` is a signed register.
- **Edge choice:** keep the raised-cosine (Hann) edge as a SMALL in-cell LUT
  (`2 + edge_samples` words), NOT a cosine recurrence — the recurrence drifts
  12.5 LSB at edge=32 because `2·cos(w)` exceeds Q15 and the error compounds;
  the LUT is exact. FALL = the rise walked in reverse. HW-DEVIATION: on-chip
  `edge_samples ≤ MAX_ONCHIP_EDGE = 4` (RAISES above; the v2 player's END-test
  + done-kick cost 4 instructions, dropping the original cap of 8 — edge=5
  overflows the register allocator).
- Golden = International Morse + CW timing, ITU-R M.1677-1 (dash=3, intra=1,
  inter-char=3, word=7; PARIS=50 units; dot_ms=1200/wpm) — transcribed from
  the source PDF, spot-checked. BIT-EXACT (0 LSB) through the real panel.
- Harness gotcha: a long word-space run needs a drain-until-quiescent pump
  loop, not a fixed budget.
- (This generalizes the Varicode recipe to blocks whose PER-SAMPLE state
  machine — not just a table — overflows the cell: precompute the schedule
  off-cell, stream records, play against a small in-cell shape LUT.)

**CWDecoderBlock — panel SCRATCH + two passes (unbounded working state).**
- The lazy read of its quarantine ("move the table off-cell and the state
  fits") was TRUE but INCOMPLETE: the golden takes the GLOBAL MINIMUM run
  length to lock the dot unit *before* classifying, and a causal running-min
  single-pass decoder MIS-DECODES (proven: `CQ`→`FQ`, `Z`→`L`) — any char
  STARTING with dashes classifies its leading element before the unit locks.
  The unbounded run buffer is load-bearing.
- **The fix:** the run buffer lives in panel SCRATCH; two-pass decode. Pass 1
  (streaming, bounded state): threshold → runs, WRITE each packed
  `(level<<15)|length` run to scratch, fold the running-min unit. Pass 2:
  READ the runs back with the FINAL unit, classify, LUT-push-read chars.
  Bit-exact to the golden incl. the ambiguity-limit cases. Round-trip latency
  is fine: CW decode is a BATCH decode, not a sample-rate feedback loop.
- **Adaptive-timing estimator (how the golden does it):** the dot unit is the
  running minimum of the ON-runs AND the short OFF-gaps (both exactly 1 unit).
  A "first element = dot" seed misreads C/T; a two-centroid EMA drifts.
  INHERENT LIMIT: a message of ONLY single-dash chars (`TT`) carries no 1-unit
  reference — blindly unresolvable, gated as a known-limit test.
- The assembler REJECTS `BR.POS`/`BR.NEG` — use `BR.N` on a reformulated
  `a-b`. (Blocks containing POS/NEG never reach assembly in the harness —
  their use is UNPROVEN.)
- **REUSABLE:** panel SCRATCH as an unbounded working buffer + a two-pass
  streaming algorithm is the template for ANY block whose blocker is
  *unbounded accumulated state*, not just a static LUT.

**RaisedCosineEnvelopeBlock — on-the-fly NCO cosine beats a table (PATH B).**
- Two walls removed WITHOUT a table and WITHOUT a deep buffer:
  1. sps-entry envelope table (129 folded @ sps=256) → the PROVEN NCO
     33-entry quarter-wave + linear interp reconstructs `sin((n+0.5)π/N)` for
     ANY N — table size independent of sps. Reused NCOBlock's
     fold/even/odd/interp cells VERBATIM. **SRAM is the WRONG tool for a
     computable smooth periodic function** — a cosine is cheaper generated
     than stored.
  2. 1-symbol reversal LOOKAHEAD (looked like an sps-deep FIFO) → a 1-symbol
     PIPELINE LATENCY with sign-only state (3 sign registers). Documented
     group delay = sps samples.
- Derived tolerance: the NCO's analytic ~11-LSB interp floor + 1 LSB MULQ =
  ENV_TOL_LSB=12 (measured peak 11 over sps 2..256). On-chip == the op-for-op
  `process_reference_q15` is 0 LSB.
- **BUG (only visible on real sim):** a 2-stage pipeline made `rev_end`
  ALWAYS 0 (2nd halves never tapered) — the Python golden that skipped the
  pipeline hid it. Fix: 3-stage `s_pp | s_prev(emitted) | s_held`. A matching
  REFERENCE isn't enough; the on-chip build must be run.
- ISA gotchas: `MULU` is not an opcode; intra-cell control is `BR.<cond>` +
  fall-through (JUMP is inter-cell); hardware `MULQ = (A·B)>>15` FLOORS
  (toward −∞, no rounding) — the reference must floor too.
- **REUSABLE:** any COMPUTABLE periodic shaper (raised-cosine, Hann/Hamming,
  chirp) → feed a within-symbol phase counter into the NCO quarter-wave sine
  column; needs-neighbour decisions → a short sign pipeline with documented
  latency, not a deep buffer.

---

## GRC-binding backfill + reconciliation campaign (INV-22 enforced) 2026-08-07/08

~36 done blocks had missing or drifted GRC bindings; all brought
param-complete, and `test_grc_binding_complete.py` (now 211 cases) HARD-FAILS
any regression. The durable rules:

- **A gate-reported missing param is one of three things:** (a) a REAL honored
  param the yml forgot → ADD it (GR name/dtype/default) + wire `make:`; (b) a
  DIFFERENTLY-NAMED yml param for the same class param (drift) → RENAME
  verbatim + fix make/callbacks/shim; (c) a param the class accepts but
  RAISES on / intentionally unsupported → `GRC_UNSUPPORTED_PARAMS` on the
  class (the ONLY legitimate omission). Read the CLASS `__init__` and its GR
  counterpart to decide — never guess. Keep the `kyttar/<shim>` marker
  signature + `_advertise_grc_params` in lockstep with the class names
  (drift-detection keys off them).
- **`spec.params` comes from `inspect.signature`**, so a kwarg with a None
  default (a back-compat alias) STILL counts — expose it or whitelist it;
  there is no "optional doesn't count" escape.
- **MANIFEST short name ≠ class name (the load-bearing find):** some blocks
  carry a legacy short manifest name (`AddConst`, `FreqXlatingFIR`,
  `QuadratureDemod`) while `catalog.type_name` is the class name with a
  `Block` suffix. The manifest name is ALSO the report filename and dashboard
  key, and DSP tests key on the class name — renaming either side desyncs a
  consumer. Fix: catalog ALIASES (`_MANIFEST_ALIASES` + a get-side
  `<name>Block` fallback), never a second `_specs` key (double-lists the
  palette), and `_TYPE_OVERRIDES` pins the grc id → MANIFEST name.
- **`_TYPE_OVERRIDES` pinning:** snake→Pascal misses acronym case
  (`kyttar_cw_keyer`→`CwKeyerBlock` wants `CWKeyerBlock`;
  `…FirFilter…` wants `…FIRFilter…`); and the case-insensitive fallback only
  sees VISIBLE specs — pin overrides for reliability.
- **Notable per-block decisions:** RRCPulseShaper's yml was STALE (exposed
  old `span`/`sps` the constructor no longer accepts — a phantom param;
  rewrote to GR-verbatim firdes names, kept span/sps as SHIM aliases so old
  .grcs load); ComplexRRC's `beta/sps/span/headroom_shift` →
  GRC_UNSUPPORTED aliases; `pipeline_lock` (INV-20 build hint, no GR
  counterpart) → GRC_UNSUPPORTED on Costas/Mixer/NCO; NCO `offset`+`phase`
  are REAL sig_source_c params → added; LFSRScrambler `reset_tag_key` raises
  → GRC_UNSUPPORTED; DualFloatToComplex's 6 placement-internal ctor params →
  GRC_UNSUPPORTED (the binding stays a true float_to_complex drop-in;
  distinct grc id from FloatToComplex, same GR label); the four firdes
  filters + FIRFilterBlock gained `decimation`/`interpolation` (REAL params
  that change the build); IIRBiquad gained GR-native `fftaps/fbtaps/oldstyle`
  verbatim.
- Six SRAM/ham blocks with NO GR counterpart got placeKYT-native bindings
  (`[Kyttar]/Ham`, `[Kyttar]/Memory` palettes) — INV-22 applies to every done
  block, GR counterpart or not.

---

## Nlog10Block — Q15 log10 via mantissa/exponent split, 2-cell, scaled-dB HW-DEVIATION 2026-08-07

= GR `blocks.nlog10_ff` (`out = n*log10(in)+k`, params VERBATIM). Verified vs
LIVE GR: max_abs_err 4 LSB (tol 10), corr 0.99999999.

- **THE ALGORITHM:** `X = 2^e·m`, `m∈[1,2)`; `log2(1+f)` for `f=m−1` is a
  through-origin cubic (LSQ fit, peak ~1.3e-3). Peak end-to-end error
  ~0.008 dB across the whole positive Q15 domain.
- **THE KEY TRICK — fold the output scale into the coefficients:** the cubic's
  leading coeff (1.42) is >1, but the block emits a SCALED dB, so multiply the
  whole log2 term by `A = n·log10(2)/db_scale` — every folded coeff is
  sub-unity ⇒ plain MULQ Horner, no INV-15, no LUT. Pick the output
  representation FIRST, then every constant is representable.
- **No CLZ on this ISA** — normalize with a shift-count loop (`SHL #1` under a
  counter); the unconditional loop-back MUST be `GOTO`. Valid signed branches
  are `BR.GE`/`BR.LT` — there is NO `BR.GT`/`BR.LE`.
- **THE BUG THAT COST TIME: `WRITE` ALWAYS SENDS R0**, not the named output
  register. A multi-value forward needs R0 loaded before each WRITE
  (`<frac in R0>; {write:frac}; MOVE R0, em15; {write:em15}`). The
  WRITE→dest-register mapping is by output-port ORDER — keep output ports and
  WRITEs in the same order as the target's input ports. Diagnosed with
  `chip.read_cell_memory` after driving one sample.
- **Derived tolerance 10 LSB:** `A_q15` rounding enters the exponent term
  ×|e−15|≤15 ⇒ ~7.5 LSB dominant. Mutation subtlety: the wrong-`n` mutation
  must use n=12 (shares db_scale with n=10) — n=20 also scales db_scale and
  leaves the scaled word ~unchanged (a scale-invariance blind spot).
- **HW-DEVIATION:** the chip emits `(n·log10(in)+k)/db_scale` with db_scale an
  auto-derived power of two; in≤0 floors at −db_scale dB (0x8000) vs GR's
  ~FLT_MIN clamp.

---

## Pre-existing test failures cleaned up; converter_flavors live-recovery documented 2026-08-08

- Catalog-enumeration tests built every block at its GR-verbatim default;
  char_to_float's default scale=1 is unrepresentable and correctly RAISES —
  build it at scale=128 via the tests' per-block override map.
- ssb_weaver_cfir: stale hardcoded expectations vs a correct build
  (IQUpconvert is 8 cells with its INV-20 lock, not 6).
- BPSK loopback fixtures asserted a bits-in==bits-out identity that was true
  only by ACCIDENT (two inversions cancelling). Fixed the RIGHT way:
  PSKSymbolMapper gained `bpsk_bit0_positive` (default True; False = GR
  constellation_bpsk) so the fixtures use a TRUE identity. The real bpsk
  example was never affected (BER is inversion-immune).
- **KNOWN-FRAGILE, documented:** `test_converter_flavors_grc::
  test_runs_live_recovers_input` builds+routes fine but the live round-trip
  returns 0 egress — a fragile LIVE-recovery infra test, NOT a
  block-correctness issue (every converter block is individually GR-verified).
  See the round-3 audit entry for the precise mixed-fan-out defect.

---

## The factory is turnkey: `factory_dispatch.py` is the single source of the build prompt 2026-08-06

- `verification/tools/factory_dispatch.py` prints the exact builder prompt for
  any block, filled from `manifest.json` (`<block>` / `--next` / `--next
  --claim`). It is the ONE source of the methodology — FACTORY.md's prose is a
  mirror of its `TEMPLATE`; to change how blocks get built, edit that
  TEMPLATE, not scattered copies.
- The prompt encodes INV-25 per-block: a `poc: true` entry renders a clause
  telling the builder the code EXISTS but was NEVER verified — finalize +
  verify across the full parameter range and EXPECT real bugs.
- Turnkey path: add a `planned` manifest entry → `factory_dispatch.py <Block>`
  → hand the prompt to an agent → it follows AGENTS.md to verify+commit or
  quarantine → record cost with `factory_metrics.record`.

---

## 2026-08-06 factory batch — per-block lessons (tiers 1–3)

**PackKBitsBlock — bit-exact vs blocks.pack_k_bits_bb (k=2..8).**
GR packs the LOW bit of k input bytes MSB-first, drops a trailing partial
group, masks each input `& 1` (all probed against LIVE GR first). THE BUG THE
GATE CAUGHT: `AND Ra, Rb` computes into R0 and leaves Ra UNCHANGED — `bit`
still held the raw input and the OR leaked its high bits. Invisible on clean
0/1 stimulus; the dedicated input-LSB-mask edge test (stray high bits: 3/5/2)
exposed it. **Every ALU op lands in R0; MOVE it back before the next op — the
#1 single-cell assembly gotcha, silent under gentle stimulus.** Rate-reducing
harness: compare the non-None words per k-bit group.

**NotBlock — bit-exact vs blocks.not_bb (full 8-bit width).**
`not_bb` complements the FULL byte (`0x00→0xFF`); the on-chip NOT complements
16 bits, so mask back: `NOT R{in}; AND R0, 0x00FF`. Mutations target exactly
the width (low-bit-only invert and wrong-mask XOR must FAIL). Exhaustive
0..255 sweep.

**ComplexGainBlock (poc → done) — the PoC WRAPPED instead of saturating.**
The old datapath accumulated with plain ADDs (wraps); its OWN reference
modelled saturation — it disagreed with itself AND GR on every overloading
sample, hidden because the one modem using it stays in range at gain 2.4
(textbook INV-25). FIX = INV-13 doubling variant: store `gain/4`, MULQ, then
restore ×4 with two saturating `ADD R0,R0` doublings, pinning to x's sign via
`0x7FFF + signbit` (sign captured BEFORE the doublings). TWO TRAPS (diagnosed
via `chip.get_trace()`): (1) `GOTO` over a sat block compiles to an EXTERNAL
output JUMP AND falls through — each rail written TWICE; use CONDITIONAL
branches only and converge paths at a REAL-instruction anchor (`MOVE R0,R0`),
never a placeholder label. (2) **Hardware MULQ TRUNCATES toward −∞** (no
rounding bias) — model it as arithmetic `>>15` in the reference or it
disagrees ±4 LSB after the `<<2`. Derived tolerance 7 LSB
(= 2^S·(coeff 0.5 + trunc 1.0) + 1); measured 6.

**ComplexRRCMatchedFilterBlock (poc → done).**
Three PoC bugs the gate (not the modems) found: (1) INVENTED unit-energy taps
— matched GR's SHAPE but not amplitude (old == GR at gain 0.7105 exactly);
now firdes-exact taps. (2) wrong param names → GR-verbatim
`gain/samp_rate/sym_rate/alpha/ntaps` (+decimation), old names kept as
aliases so shipped .kyts load. (3) no overflow protection → the INV-13
headroom restore per rail. GR-equivalence gated at S=0 (gain 0.6, the
bit-clean drop-in regime — peak 11 LSB / derived 18); the S=1 shipped default
(gain 0.7105) is pinned to its exact Q15 reference + the modem BER-0 gates
(the ~20 LSB there is expected headroom rounding, not a bug; gain=1.0
saturates the full-scale QPSK drive and regressed BER — keep 0.7105).
decimation>1 now RAISES (the old block "accepted" it but silently never
decimated); ntaps ≤ 32 (INV-9 fold cap). KEPT the serialized-rail
datapath/cell-ids VERBATIM — shipped .kyts reference those exact cell ids.

**BPSKSlicerBlock (poc → done) — the PoC was INVERTED with the wrong tie.**
GR `binary_slicer_fb`: `<0 → 0`, `>=0 → 1` (tie → 1). The PoC computed the
EXACT INVERSE in assembly AND reference — hidden because every chain BER
metric is 180°-inversion-tolerant. "Used in a BER-0 modem" is NOT
verification (INV-25). Fixed to GR's decision, verified bit-exact incl. the
0x0000 tie; the inverted + wrong-tie mutations FAIL. `out_mode` byte/word
packing is a documented HW-DEVIATION (Kyttar-only port-pressure
optimization); input port is `llr`, not `sample`.

**ComplexCostasLoopBlock (poc → done) — order-4 complex PORT egress was broken.**
Feedback-loop verification shape (the MMTiming pattern): gate is DECISION —
a Q15 loop and GR's float loop converge to the SAME symbol decisions along
DIFFERENT soft trajectories; amplitude spread is diagnostic only. THE POC BUG
(order 4 only): the port-egress complex-out discriminator resolved the block
spec PARAM-BLIND (an order-DEPENDENT interface read as order-2 single-rail),
and for a FUSED output+handoff cell the single-rail patch stranded yi_tap on
its internal hop, COLLIDING with the internal err handoff. FIX in
engine/build.py: resolve the complex-out flag WITH the placed instance's
params, and `_patch_last_n_write_handoff` — patch the last N (=output
register count) TAIL WRITEs with distinct tags, leaving earlier internal
WRITEs untouched. **The editable-install-shadows-worktree trap:** the venv's
editable finder hard-maps `gr_kyttar` to the main checkout; check
`import gr_kyttar.<mod>; mod.__file__` FIRST.

**AddConstBlock — saturating single-cell ADD of an immediate.**
A bare Q15 ADD WRAPS (0.9+0.5 → −0.6 sign-flip); reuse the AddBlock/FIR
restore verbatim (`BR.NV` forward skip; `0x7FFF + signbit`). Copying an
existing saturating block rather than inventing the idiom was the whole job.

**LFSRScramblerBlock — bit-exact vs digital.additive_scrambler_bb; GOTO-in-tail trap.**
GR uses a RIGHT-shifting **Fibonacci** LFSR (`out = sr & 1`;
`newbit = parity(sr & mask)`; `sr = (sr>>1) | (newbit<<len)`), confirmed by
reading `next_bit()` out of live GR — NOT a left-shifting Galois. THE BUG: a
`GOTO` just before the shared `{write}/{jump}` tail assembled to a local JUMP
that did NOT stop fall-through — the newbit=1 path double-shifted the
register exactly when parity was odd. FIX = a branchless merge (`MOVE` does
not touch flags, so P survives for the branch; one shared
`SHR sr,#1; OR R0,fb; MOVE sr,R0` tail). `AND sr,mask` sets P =
parity(sr & mask) = the Fibonacci feedback in ONE op. Register reclaims:
count DOWN (reuse `count` as compare AND reload); input lands in R0.
HW-DEVIATIONS raise: bits_per_byte==1, len≤15, reset_tag_key unsupported.

**MultiplyConstComplex — TRUE complex-constant multiply (scales AND rotates).**
Distinct from ComplexGain (same real gain both rails, no rotation). HEADROOM:
a complex multiply SUMS two products per rail — restrict `|re|,|im| < 2` so
each `/4` product is < 1/2 and the sum can never wrap; the ONLY overflow
point is the final saturating `<<2`. Derived tolerance 13 LSB
(2^S·(2·0.5 + 2·1.0) + 1); measured 9. TWO CELLS (mul → sat): the full
product needs ~28 instrs > one cell. Clean feed-forward — no INV-19/20 lock
needed. Re-confirmed: a branch target must be a REAL instruction, never the
`{write}` placeholder (removing the anchor built but computed wrong).
Mutations include dropped-cross-term (vs a non-rotating golden) and
sign-swapped term (vs the conjugate golden).

**FreqXlatingFIR — fused mixer+FIR channelizer; saturation BESPOKE.**
GR-equivalence decomposition (derived empirically): GR's output rotator
`exp(-j·fwT0·(L-1)/2)` FOLDS INTO the NCO as an initial phase offset, so the
block is a plain down-mixer (init phase θ0) → real complex FIR → decimate;
no complex taps, no output rotator (max|Δ| < 2e-6 vs GR for L=1..17,
decim 1/2/4). **The fan-in-vs-fan-out bug:** a mixer cell cannot be a
reconvergent fan-in AND a serialized fan-out source — rails came back
SCRAMBLED; a DEDICATED 1-pair HEAD cell (verbatim the ComplexRRC head) fixed
it instantly. **The decimation wall:** the gate's dcnt state + 2 data words
break a last cell carrying 2-4 taps — `_segment_sizes()` caps the gated last
cell to EXACTLY 1 tap when decimating. HW-DEVIATIONS (raise): Σ|taps| ≤ 1;
≤ 24 taps (INV-9). **The saturation wall (named, BESPOKE):** the mixer is
MID-chain, and the INV-20 unlock assumes the unlock cell IS
`output_cell_id()` — a mid-chain config-unlock is a BUILD-ENGINE change;
`pipeline_lock=True` RAISES NotImplementedError (never a silently-empty
variant); fully verified PER-SAMPLE, drive it un-saturated.

**GardnerTimingRecovery — QUARANTINE: not a symbol_sync_cc(Gardner) drop-in on a Nyquist channel.**
The block had green tests that prove build + self-consistency on its OWN
synthetic stimulus — the INV-26 trap: **GR's own symbol_sync_cc(Gardner)
FAILS that stimulus (BER ~0.45)**, so the block was tuned to a signal GR
cannot lock. On the industry-standard matched-filter Nyquist channel GR locks
BER 0 across the offset sweep; the DUT recovers at BER ~0.04–0.12 (reference
AND on-chip). ROOT CAUSE: the Q15 TED halves both samples (`>>1`) before the
product; that truncation + coarse power-of-two loop gains make the timing
estimate jittery — variance, not drift. A fix is a DATAPATH REDESIGN (the
M&M cubic-Farrow datapath MMTimingRecoveryBlock already ships), not a tune.
Outcome `needs_human`; a strict-xfail flips green the moment a redesign makes
the DUT BER 0. **GENERALIZES: always confirm the GR golden LOCKS on the
verification channel before trusting DUT-vs-GR — an inverted result (DUT
passes, GR fails) means the stimulus is wrong, not GR.**

**UnpackKBitsBlock — counted loop beats unrolling at k=8.**
GR reads the LOW k bits, emits MSB-first (probed, not assumed). An unrolled
4-instr/bit emit hits the 31-instruction ceiling at k=8; a COUNTED LOOP
(`SHR w,#(k-1)` peel, `SHL w,#1` advance, `BR.NZ loop`) is constant-cost for
all k. Shifts and AND write R0, not the source reg — store back explicitly.
Keep the backward branch separated from `{jump:out}`.

**DiffEncoderBlock — bit-exact vs digital.diff_encoder_bb (M2+M4).**
LIVE-GR truths: the param is `coding` (default DIFF_DIFFERENTIAL); NRZI is
`(x+prev+1) mod M` (a +1 bias only); **GR restricts NRZI to modulus 2**
(raises otherwise — found only by exercising the combination against live
GR). Modulo with no %-op: sum < 2M ⇒ ONE conditional subtract (`CMP R0,M;
BR.LT store; SUB R0,M`) — general for ANY modulus. Single-cell inline state ⇒
saturation-safe with NO lock (the INV-19 hazard is specifically a CROSS-CELL
feedback edge; 1-cell inline state settles before the next trigger).

**DiffDecoderBlock — bit-exact; state is the previous INPUT.**
`y = (x − x_prev) mod M`, cold-start x[-1]=0 (the ENCODER's state is the
previous output — don't mix them up). `(x − prev) & (M−1)` is the correct
non-negative modulo for power-of-two M. HW-DEVIATION: modulus must be a power
of two (bitmask modulo); RAISES otherwise. Straight-line datapath, no branch
near the tail — the GOTO hazard avoided by construction. Round-trip on-chip
(GR encodes, the block decodes) proves it IS the inverse; two mutations also
proven on the REAL on-chip DUT.

**MapBBBlock — per-symbol LUT remap (digital.map_bb), bit-exact.**
GR seeds a 256-entry identity table then overwrites `d_map[i] = map[i]&0xFF`
for i < len(map) — so out-of-range inputs PASS THROUGH and values are
byte-masked (all three probed). HW-DEVIATION: `LOAD [Rn]` masks the address
to 5 bits; the largest single-cell table is **21 entries** (the build names
the exact ceiling — sweep N downward, don't estimate). METRIC = EXACT byte
equality (DECISION only diffs the low bit — wrong for a byte remap).

**XorBlock / AndConstBlock — byte streams are RAW words, not Q15.**
The Q15 verification path (`_to_q15`, `_saturate_ref_q15`,
AMPLITUDE/DECISION) silently saturates byte values on BOTH sides — byte/int
blocks need raw-word injection + a direct integer-equality gate. XorBlock's
INV-4 was proven at the SUBSTRATE level: build an `AND R0,R1` mutant block,
run it on-chip, confirm the gate rejects it. AndConst: Metric.DECISION
compares only the LSB — proven to MISS a bit-7 error that EXACT catches; and
this GR build rejects constants outside 0..255 (unsigned byte, no `-1`
aliasing — attempt-1's test premise was wrong, verify the actual contract).

**CharToFloatBlock / FloatToCharBlock — the int8↔Q15 boundary.**
A Kyttar "float" IS a Q15 word, so GR's `char_to_float` default scale=1 asks
for outputs ~127× outside Q15 — the faithful domain is `scale ≥ 128`
(int8→Q15 ADC conversion); RAISES below (never silently clamps semantics).
Datapath: `(c<<8) * B >> 15` with `B = round(128·32768/scale)`; `c<<8` fits
int16 for every int8. FloatToChar mirrors it (`scale ≤ 128` in reverse) and
must round HALF-TO-EVEN (`lrintf`/np.rint semantics — a half-up DUT fails on
every exact tie; implemented exactly with MULQ floor + MUL low-15 remainder +
a bump iff `r>2^14` or (`r==2^14` and q odd)). GR's `vector_sink_b` yields
UNSIGNED bytes — reinterpret ≥128 as v−256 on BOTH sides. THE REAL BUG: the
input lands at R0 and this block reads it TWICE — the first op clobbers it;
`MOVE k, R{in:sample}` as instruction #1. **Any block that consumes its input
register more than once MUST save it first.**

**DelayBlock — integer-sample delay line, EXACT, delay 1..12.**
THE BUG: with NO data words the auto-allocated `d0` landed ON input register
R0 → the block ECHOED its input (a "block echoes input" symptom = suspect a
state↔input register collision BEFORE the datapath). Fix: pin the delay-line
registers explicitly (the INV-33 no-data-words corollary, first sighting).
Alignment done right (INV-2): a delay is a KNOWN shift — assert the impulse
lands at index `delay` exactly; don't pass `delay=D` to the comparator (that
models a DUT DROPPING samples — the opposite). HW LIMIT: delay 13 BUILDS but
silently emits nothing (state collides with instructions) — the naive
word-count estimate is 1 too high; MAX_DELAY=12, and the claimed max depth
must be SIMULATED, not counted.

---

## Saturated drive breaks RATE-EXPANDING TX chains; the duplex schedule switch 2026-07-27

- **Rate-EXPANDING chains deadlock saturated (input-side, fully isolated):**
  the saturated `queue_words_physical` drive collapses a rate-expanding TX
  (bit → 4 passband words) to ~1 output word — an INPUT-side deadlock when
  the next input reaches the input cell before the current input's multi-word
  expansion finishes propagating. Depth-sweep proof: ≤2 inputs in flight OK,
  3+ deadlocks; continuous output draining does NOT help. RX demods and the
  16-QAM TX are rate-REDUCING → safe (why only 16-QAM shipped saturated).
  The fix is the INV-20 serialize-LOCK on the expanding block (Upsampler);
  until then those examples stay per-sample — never flip `pipelined: yes`
  blind.
- **Full-duplex has NO throughput penalty; duplex ≈ simplex** (~146 kSa/s RX
  on the 16-QAM modem). A shared input PORT serializes the input CORRIDOR,
  not the array's COMPUTE. (An earlier "chains throttle each other" claim was
  a MEASUREMENT ARTIFACT: an arbitrary bounded `run()` between interleaved
  packets added dead time. Let the input port SELF-PACE; report chip-time;
  never insert a fixed inter-packet run to "let it settle".)
- **SATURATED is the real drive; per-sample-to-quiescence is a verification
  view.** The per-sample drive never puts two samples in flight, so no block
  ever feels back-to-back pressure and "rate" is latency mislabeled. Only the
  saturated drive reveals the real serial barriers.
- **The interleaved saturated FRAMING INVARIANT (submit-order INDEPENDENT):**
  within each sample emit all streams' DATA, then all streams' JUMPs, with
  the COMPLEX stream WRAPPING the real one (`RX_data, TX_data, TX_jump,
  RX_jump`). Derive the order from complex-ness, NOT submit order — the
  rendezvous races two threads, and a submit-order-relative rule silently
  drops one direction. See `sim_bridge._process_batch_duplex`.
- **A GRC-first design carries per-run options IN THE RPC HEADER, sourced
  from a block param — never process env.** An env var set in the client
  shell is invisible to the long-lived server process. The `schedule`
  dropdown (interleaved vs sequential) rides
  source → rendezvous → `process_batch_duplex` header; non-default wins so
  setting it on either duplex source works.
- **The INSTALL BOUNDARY (the real time-sink):** GRC imports the INSTALLED
  OOT (dist-packages), a SEPARATE copy from the repo. Repo edits "pass"
  in-process while the GUI runs stale code. Check the installed copy before
  believing an OOT edit is live; headless tests can bypass with
  `PYTHONPATH=gr-kyttar/python`, the GUI cannot.
- Gotcha: a complex-stream `kyttar.sink` must be `in_type=True` or
  `top_block.connect` raises an itemsize mismatch.

---

## Full-duplex 16-QAM modem — assembling the biggest example (process lessons) 2026-07-24

The full-duplex TX + coherent RX 16-QAM modem on one 10×12, BER 0 on the
hosted .kyt. RX = `MF → ComplexGain(2.4) → MMTimingRecovery → QAM16Costas →
QAM16Slicer`; TX = `QAM16Mapper → ComplexUpsampler(sps2) → ComplexRRC →
IQUpconvert` (single REAL passband out).

- **Gain-staging between the MF and the decision-directed loops is
  load-bearing:** the MF's ÷2 tap pre-scale compresses the constellation
  ~2.8×; the M&M TED and DD Costas slice to FIXED 4-PAM thresholds, so a
  compressed input makes every decision wrong. A ComplexGain ≈2.4 (robust
  window [2.3,2.45]) restores nominal scale. RECURRING: any decision-directed
  stage needs its input at nominal scale — RMS/outer-level match, not
  peak-scaling.
- **Timing BEFORE carrier for a same-chip modem** (foff≈0, so M&M can precede
  the DD Costas). Over a real channel a coarse-FLL stage would come first —
  don't add carrier-recovery complexity the channel doesn't require.
- **Drive a full-duplex .kyt through the STREAM-ROUTED batch path, not raw
  port injection:** `x16_in` fans to BOTH chains, distinguished by
  `stream_id` + `out_tag`; a raw `inject_data_physical` fires both and
  corrupts the RX. Resolve `stream_targets(...)` from a CONTROLLER-loaded
  project and drive `_process_batch_duplex` — the path
  `test_shipped_kyt_recovers_ber_zero` uses (it caught a shipped .kyt that
  differed from the auto-P&R build).
- **Verify the CASCADE on-chip, not just each block:** gain-staging, complex
  packet handoffs, and port-name wiring are chain-level. You cannot pipe one
  block's `process_reference` into the next (different shapes; DD loops need
  the correct input scale) — the composition proof is the ON-CHIP cascade.
  Prove BER 0 on-chip EARLY, before authoring the .kyt.
- **Measurement discipline:** SER-by-symbol-LABEL is broken for QAM (90°
  ambiguity + GR's idiosyncratic bit→point permutation make a perfect lock
  read ~93% "errors"). Use grid-distance of the RMS-normalized recovered
  constellation as the label-invariant lock metric during development; score
  true BER only through full rotation+lag+permutation alignment at the end.
  Keep ONE trusted harness — contradictory ad-hoc protos cost hours.
- **Full-duplex on one array is a placement problem, not capacity** (60/120
  cells): ship a HAND-PLACED .kyt (open it, don't import) + a replay script
  so it's reproducible; the .grc is the reference flowgraph.
- **Workflow:** settle the ARCHITECTURE first (research passes), then bounded
  author→adversarial-verify phases with a human between them — and RE-RUN the
  acceptance test yourself; a workflow once reported the shipped .kyt passing
  when it actually recovered BER 0.90.

---

## MMTimingRecoveryBlock — M&M decision-directed timing recovery, on-chip bit-exact 2026-07-24

The 16-QAM timing wall (Gardner leaves ~3% jitter on 4-level axes) SOLVED with
GR `symbol_sync_cc(TED_MUELLER_AND_MULLER)`'s architecture: a **modulo-1
interpolator-control counter** (strobe on underflow, mu = cnt/W) + **cubic
Farrow interpolator** + **decision-directed M&M TED** + **2nd-order PI**.
On-chip BIT-EXACT to `process_reference` (offsets 0.0–0.7); worst per-axis
error 0.277 < 0.316 = BER-0-safe. 14 cells.

- The failing 2sps-Gardner→Costas pipeline was the WRONG TOPOLOGY, not
  tuning: Gardner is a BPSK/QPSK TED (shallow S-curve on multilevel), a plain
  Costas is PSK-only, and a DD carrier loop must not precede coarse-freq
  under a large offset.
- The modulo-1 counter fixes the conflated symbol-clock/interpolator-phase
  failure (self-noise, can't stay locked). Per input sample: W=1/L+v;
  strobe = cnt<W; cnt=mod(cnt−W,1); at strobe mu=cnt/W; the PI runs EVERY
  sample (e=0 off-strobe); esign=−1.
- **ISA-friendly reformulation:** mu = cnt<<1 (single SHL; W≈0.5 makes it
  bit-identical to the divide — the ISA has no divide); Q15 MULQ loop filter.
  Verified identical to the wide-scale model AND to GR.
- **Cubic Farrow coeffs overflow Q15** (|c| to 2.5) → TRUE Farrow structure:
  4 sub-filter MACs with coeffs stored Q13 (÷4), Horner in mu, result <<2.
- **default_layout dict ORDER must match build_cell_programs() key order**
  (positional pairing — a physically-ordered fold silently mis-resolves every
  internal handoff). → INV-33.
- **Generic router bug fixed** (runtime router): `_find_output_target`
  IGNORED `internal_jumps`, so a JUMP to a non-positional-next cell or a
  NAMED entry fell through to the positional default. Any multi-cell block
  with named-entry triggers needs the explicit resolution loop.
- **Two-rail reconvergence:** a cell must NOT write DATA to one neighbor AND
  trigger a DIFFERENT neighbor (router mis-bundles) — strictly-linear trigger
  thread; the parallel rail delivers as a PURE DATA 1-hop write.
- **Feedback closure needs a declared `transit_fb_*` cell** so
  `_apply_internal_feedback` traces the corridor through stable faces; keep
  the feedback SHORT.
- **Serialize-LOCK on EVERY sample, not just strobes** (differs from
  Gardner): strobe-only locking left no-strobe samples un-serialized and they
  corrupted the TED's decision state. `MOVE [LOCK],Rn` engages on BIT0.

---

## QAM16 mapper + slicer + DD Costas — the RX back-end recipe 2026-07-22

All three GR-vetted (the legacy blocks used an INVENTED constellation matching
GR on 0/16 symbols — purged). The durable recipes:

- **GR's constellation_16qam() bit→point map is a fixed non-separable
  PERMUTATION**, but the nearest-point decision FACTORS into two per-axis
  binary tests + a 16-entry LUT (`sign=(v>=0)`, `outer=(|v|>=2/√10)`,
  `key=(Is<<3)|(Io<<2)|(Qs<<1)|Qo`, `symbol=LUT[key]`) — verified equal to
  `decision_maker` over the whole plane. The mapper stores GR's EXACT
  `points()` (re-derived from GR in-test so a GR bump can't drift).
- **Table/register aliasing (cost the most time):** memory IS the register
  file — a LOAD-indirect table at addr 1..M occupies R1..RM, so an INPUT or
  STATE register pinned inside that range silently corrupts exactly the
  colliding index (symbol 0's Q read back as the delivered address). Pin
  inputs/state at 0 or >M. Trace it by reading the built cell's registers
  live per symbol.
- **`LOAD Rn` is a SINGLE table deref** (`R0 = mem[value-of-Rn & 0x1F]`).
- **The feedback-block-with-recovered-output-tap recipe (DD Costas →
  slicer):** (1) `output_registers=[0,1]` on the interface — the build's
  complex-egress patch keys on it; `[0]` takes the single-rail patch and
  strands one rail. (2) a dedicated dual-face `tap` cell (internal forwards
  on `face_internal`, tap pair on `face_tap`, tap emitted as the program
  TAIL); wire the tap to a BLOCK (one broker — the coalesced tail-patch
  steers both rails), NOT to a chip port (the port path walks ALL WRITEs and
  over-patches the internal forwards, breaking the loop). (3) a compact
  serpentine fold that puts the pi→phase feedback @1 directly adjacent
  (a fold with `pi` mid-array resolves the dphase WRITE and re-trigger JUMP
  to DIFFERENT hops and the loop never closes). (4) anchor so the landing
  cell abuts the input port.
- **THE BER-0 FIX — the last datapath cell's trig MUST SELF-TERMINATE
  (`__terminate__`).** Without it the router defaulted pi's trig JUMP to a
  positional-next cell and LOOPED BACK THROUGH the live datapath — 2 outputs
  per input + a corrupted DD lock. DEBUG NOTE: "2 outputs per input" on a
  feedback block ⇒ suspect the last cell's trig, not the tap/broker. And a
  CONSTANT-symbol settle test is DEGENERATE for a DD loop (on-grid err=0, no
  phase info) — characterize with a RANDOM stream.
- DD acquisition: locks BER0 standalone at 1 sps over a modest foff window;
  the shipped modem operates at foff≈0 with MM timing in front.

---

## M17 4FSK — mapper/slicer blocks; Gardner cannot lock 4-PAM; sync-correlation timing 2026-07-21

- **FSK4SymbolMapperBlock (1 cell):** bit stream → one signed PAM level per
  DIBIT; M17 Gray map pinned LSB-first (`d = b0 + 2·b1` →
  [+1/3, +1, −1/3, −1], +3 → full scale). Feed a FrequencyModulator with
  `sensitivity = 2π·2400/fs`. (The M17 spec tables the map MSB-first — the
  transposition is stated loudly.)
- **FSK4SlicerBlock (1 cell):** the dibit's two bits ARE the two decision
  flags — `b0 = (|y| ≥ 2/3)`, `b1 = (y < 0)` — no lookup table (a table+LOAD
  version overflowed the cell). Strongest gate: mapper→slicer LOOPBACK is
  bit-for-bit the identity (pins the shared bit convention).
- **HARD FINDING — Gardner does NOT recover 4-level PAM timing to BER 0**
  (plateaus at ~0.21–0.31 in its own reference AND on-chip; the DSP up to the
  slicer is proven right by a fixed-phase decimation recovering BER 0). The
  4-PAM eye is narrower than BPSK's, so the same timing jitter smears across
  the thresholds; retuning proven insufficient — a different algorithm is
  required. CAUTION (own error, logged): several intermediate "BER 0"
  readings were MEASUREMENT BUGS in ad-hoc slicing/lag code — score against
  the block's own `process_reference` with a correct guard+lag, and treat a
  per-true-symbol recovered scatter (mean/std) as the honest lock metric.
- **The chosen algorithm (validated BER 0 / 60 seeds): sync-word
  CORRELATION** — what real M17/DMR/P25 decoders do (sliding correlation
  against the known ±3 sync symbols; the peak offset IS the sampling
  instant). Decision-directed feedback trackers (M&M, DD-Gardner) are
  UNSTABLE on the FM-discriminator 4-PAM signal at 2 sps; Oerder-Meyr wants
  ≥4 sps + atan. Data-aided correlation is pure MAC + compare — the best
  ISA fit. Details: alternating preamble alone is AMBIGUOUS (half-symbol
  self-similar) — the ASYMMETRIC sync word gives the unique peak; pre-scale
  each sample by 1/SYNC_LEN via a SIGN-CORRECT MULQ (a raw logical SHR
  mangles negatives); account for the RRC group delay in the search range;
  scale the RX so the outer level ≈ ±1.0 for the fixed slicer + threshold.
- **FSK4SyncTimingRecoveryBlock:** 10 cells (d0..d7 systolic ±1 correlator @
  2 samples/cell → lock → emit); recovers BER 0 in reference + model; later
  completed on-chip (the fsk4 modem ships BER 0 end-to-end).

---

## Placement legality must survive USER MOVEMENT + the SET-dedup collision trap 2026-07-21

- A block's footprint can self-overlap through user movement (Alt-drag one
  cell onto the block's own cell) — and through the AUTO-P&R re-fold. THREE
  holes, one lesson: `_placement_legality` skipped same-block collisions;
  `move_cell` did no validation; and `auto_pnr._collides` tested
  `occupied_positions()` — a SET, which DEDUPS two own-cells on one square so
  self-overlap is invisible (this was THE one the auto-placer hit). **Any
  "does this block collide?" check that builds a SET of positions silently
  swallows self-overlap — compare the CELL LIST to its unique positions.**
- NEW GATE `verification/tests/test_placement_legality.py` (INV-25 movement
  clause): per multi-cell block — no self-overlap in any D4 orientation,
  `move_cell` rejects colliding moves, move-then-rotate never overlaps. A
  "rotation test" that only rotates a pristine block misses the failure mode
  that actually bites users.
- CAVEAT: a .kyt SAVED with a pre-fix overlap stays overlapping on load;
  recovery = drag the orphaned cell to a free square or re-place the block.

---

## Saturation is a REQUIRED per-block gate; NCO/FM INV-20 fixes; stimulus-encoding trap 2026-07-21

- **Saturation-safety is a first-class acceptance gate** (AGENTS checklist +
  INV-19/20): correct output COUNT and VALUES under saturated drive, or a
  NEEDS_BESPOKE entry with a reason — no silent gap
  (`test_pipeline_saturation.py` + its coverage-is-documented meta-test).
  This gap is what let a modem ship dropping HALF its samples under load.
- **NCO + FrequencyModulator serialize-locks landed** (opt-in
  `pipeline_lock=True`) — the three non-obvious moves are recorded in INV-20
  (2-operand emit, data-forwarding relay, dict-order/exit-cell rules). All
  found with `chip.get_trace()` per-cell fire counts + bounded `run()`.
- **TEST-HARNESS TRAP (cost ~an hour):** a stimulus table held UNSIGNED Q15
  words; feeding a reference `w/32768` reconstructs negatives as large
  positives → a phantom "rotation drift" that looks like a block bug. Drive
  the chip with the Q15 words AND the reference with the ORIGINAL signed
  floats. ALWAYS reconcile a "divergence" against a hand-computed sample
  before blaming the datapath.

---

## Complex-egress yq rail must CO-ROUTE with yi (shared corridor) 2026-07-22

A complex-output block feeding the chip output port emits BOTH rails from ONE
emit cell down ONE corridor (yi on out_tag T, yq on T+1); the router cannot
draw a second distinct path from the same source to the same port, so yq was
left unrouted (build failed + orphan fly-line). Fix:
`controller._resolve_complex_egress_corails` — an unrouted yq egress net
whose yi sibling routed gets the SAME waypoints. Idempotent; no-op for
single-rail egress. Proven: the hand-placed fsk4 modem routes 0 DRC errors
and the RX recovers BER 0 — the first design to egress two complex-baseband
rails from one emit cell to one output port.

---

## Orientation campaign: datapath IS invariant; every break is I/O-boundary 2026-07-20

- **Prove datapath invariance by DIFFING per-cell programs across all 8
  orientations BEFORE chasing the router:** internal cells are byte-identical
  in every orientation; only the OUTPUT cell's egress hop differs. So
  "orientation failures" are at the block↔chip-port I/O boundary (corridor,
  landing, egress) — this diff saves a long hunt in the wrong layer.
- **Fixed:** `_resolve_input_landings` face-checked the PORT cell (the host
  injects at the port; the first real transit is index 1) — a false divert
  produced a bogus broker landing even for the identity placement. The
  CP-SAT router let an input net thread through the chip OUTPUT-port cell
  (endpoint exemption) — forbidden now for foreign port cells. The bus
  router penalizes (soft +1000) any FOREIGN chip-port cell as transit — a
  hard wall broke the legitimate column-9 shared-sink case; the soft penalty
  is the right knob. Block output emit-neighbours are kept off the
  input-broker candidate set (soft).
- **Residual class (documented):** a corner-packed placement where the output
  cell and the head input are ADJACENT and the egress corridor unavoidably
  boxes the head — a placement-congestion limitation, not a per-net routing
  bug. The real auto-placer never produces these; a rotated block hand-placed
  INTO its own input port is inherently unroutable.
- **The "flaky" orientation test was 4 real deterministic bugs** (named-cell
  internal-face restore no-op; port complex fan-in double-relay; router
  weaving egress through the block body; harness manhattan hop on a snaked
  corridor) — all catalogued in INV-23. A "flaky" orientation test is almost
  always a REAL bug: reproduce it in-process outside pytest and it is stable.
- **Full-duplex shared-port fan-out** (INV-24): `_apply_port_diverts` promotes
  the port cell to a broker for a diverting stream (land at the port, relay
  through the downstream broker, restore the face). Prove a modem the way
  it's USED — load the hand-built .kyt → build → stream_targets → SimServer →
  drive both stream_ids; a toy 1-port→2-block project does not reproduce the
  user's topology.
- **Harness carrier convention (cost hours, twice):** a coherent baseband RX
  is driven with a SMALL residual offset the Costas can pull in (foff=0.008),
  NOT the TX upconvert frequency (0.125 ≈ 16× the pull-in range — BER ~0.68
  on ANY correct RX, masquerading as a delivery bug).
- **TX RRC passband at sps=2 LOOKS rough — that's correct:** verify
  numerically (near-zero-sample fraction matches the RRC reference, not the
  zero-stuff), not by eyeballing the eye.

---

## MODEL: internal feedback/"transit" cells are FIRST-CLASS block cells 2026-07-20

Block-internal `transit_*` cells moved from a separate light-blue list into
`Placement.cells` as first-class `PlacedCell`s (block colour, footprint,
rigid transform, same DRC); `transit_cells` is a read-only filtering VIEW;
`is_transit_cell()` is the single tag check. Migration gotchas (all
double-count bugs): a dataclass InitVar and a same-named @property collide
(hand-write `__init__`); the DRC counted transits twice → a false
self-overlap; move commands shifted them twice. Legacy `.kyt`s with a
separate `transit_cells:` block still load. Byte-identical built bitstreams
before/after prove the representation change touched nothing functional.

---

## Importer complex Q-rail sibling; Gardner/Costas refolds; ComplexUpsampler 2026-07-19

- **`_iq_sibling` silently dropped the Q rail for DECORATED port names**
  (`yi_tap`, `yi_e` — the ``i`` marker mid-name; the trailing-``i`` rule
  produced non-existent names and returned None → no Q net). Fix: try both
  the trailing swap AND the position-1 marker swap, taking whichever names a
  REAL port. A silently-dropped Q rail looks like a routing/DRC mystery —
  check the NET LIST for the missing sibling before blaming the router.
- **Complex Gardner re-folded 3×3** (was a 5-wide strip): the forward chain
  stays face-abutted @1; the qdelay→qout Q rail rides the SAME forward
  fwd_face path (in-line cells forward transit traffic — break the path and
  qout gets no Q); the dual-face loop_filter's two rails are PERPENDICULAR so
  they never collide; the feedback corridor traces through the declared
  transit cell. ⚠️ A fold can help the block STANDALONE yet HURT the
  auto-placer for a dense design (duplex import reliability dropped) —
  measure a fold's effect on BOTH; the duplex acceptance path uses EXPLICIT
  anchors.
- **Order-4 Costas re-folded 4×2** (was a 7-wide strip): same fold as order-2
  with `qpd` inserted; qpd is DUAL-face (err/trig on `face_internal`, tap on
  `face_tap` — MUST be different faces). ⚠️ THE TRAP THAT COST HOURS:
  **amp=0.9 clips a QPSK burst and mis-locks the Costas — looks EXACTLY like
  a fold/routing bug** (both axes carry ±0.707 and the RRC overshoot passes
  full scale). QPSK needs amp ≤ 0.7. When a complex chain shows stubborn BER
  INSENSITIVE to placement, suspect stimulus amplitude (Q15 clipping) first.
  A lock-magnitude check is NECESSARY but NOT SUFFICIENT — gate on
  end-to-end BER.
- **ComplexUpsamplerBlock** (2-rail zero-stuffer, bit-exact vs
  `interp_fir_filter_ccc(sps,[1])`): each output is a 3-word packet
  (`WRITE yi; WRITE yq; JUMP`) so the single-cell ceiling is HALF the real
  Upsampler's (sps ≤ 4, RAISES above). Kept a SEPARATE grc id from
  `kyttar_upsampler` — dispatching on io_type would have silently swapped
  the BPSK modem's real TX block. Rate-expanding complex harness: flatten
  `run_block_dut_complex`'s per-trigger bursts and de-interleave.

---

## QPSK receiver era — engine fixes + durable gotchas 2026-07-18

(The receiver-and-blocks WIP entries from this period are superseded by the
shipped QPSK modem; what survives:)
- **`_patch_complex_source_handoff` patched EVERY WRITE/JUMP on the output
  cell** — correct for a PURE output cell, WRONG for a cell that is BOTH a
  loop's phase detector AND the block output. Fix:
  `_patch_complex_packet_last_handoff` (tail external rails only), gated on
  `_output_cell_carries_handoffs`.
- **`_resolve_port`/`_iq_sibling` called `catalog.port_map()` WITHOUT the
  instance params** — param-DEPENDENT port sets collapsed onto rail 0 and
  silently dropped the Q rail. Thread the coerced params through
  (`_INSTANCE_PARAMS`).
- **Trig-hop resolution is the router's positional-next distance trace:** get
  the layout so the output cell's forward face ABUTS its trig target
  (verified in the built cell: the trig JUMP word is @1, not @0/local). A
  mid-chain output cell works when its two consumers go in DIFFERENT
  directions via the dual-face idiom. Inserting a cell into a proven feedback
  loop is multi-layer: register ceiling → cell split → layout continuity →
  trig patching; trace exec-ticks per cell FIRST (which cell stops firing),
  THEN dump the last-firing cell's WRITE/JUMP hops.
- **Input-port NAME collision:** `_resolve_named_input` matches a same-named
  STATE var before the input — an input named like a state var misroutes the
  WRITE to the state register. Rename the port.
- **MF decimation register-aliasing:** adding state+data to a cell whose
  INPUT registers are computed from a bare count formula aliases inputs onto
  auto-packed state — re-derive input regs from the REAL data-top, and
  disassemble the BUILT cell the moment on-chip values look "shifted".
  The decimation counter uses `initial_value=decim-1` and is NOT
  `reset_per_batch` (True would zero it per injected symbol → drop-all).
- **QPSKSlicerBlock:** GR `constellation_qpsk()` map is MSB = imag-sign,
  LSB = real-sign (read it off GR, don't assume). HARNESS GOTCHA:
  `run_block_dut_complex` defaults `in_ports=("xi","xq")` — a block with
  different port names silently reads stale inputs and every symbol comes
  out constant/max. When a complex-block run gives a degenerate output,
  verify the inputs actually landed (read the landing cell's registers)
  BEFORE suspecting the block.

---

## QuadratureDemodBlock — FM demod vs GR quadrature_demod_cf 2026-07-05

- **MATCH THE FUNCTION, NOT GR'S LITERAL OP.** GR computes
  `gain·atan2(Im d, Re d)`, `d = x·conj(x[n-1])`. A CORDIC atan2 needs ~45+
  cells here — the wrong algorithm for FM demod, which needs the *rate of
  change* of phase: the standard discriminator
  `out = gain·(I·dQ − Q·dI) = gain·Im(x·conj(x[n-1]))` — 2 cells, all MAC.
  Before grinding a multi-cell transcendental, ask: "does the GR block's MATH
  need this, or just its OUTPUT?"
- **CORRELATION-GATE CONTRACT (a maintainer-approved RULE-#0 deviation):** the
  discriminator is the atan2's first-order derivative form; they agree for the
  constant-|x| (limited/AGC'd) signal a real FM RX operates on. Verified corr
  vs GR: 0.99999 at low deviation → 0.997 at ~1.3 rad/sample, degrading
  gracefully. The deviation is documented loudly and the metric is
  correlation.
- `x[-1]=0` → `di[0]=0`, matching GR's first output. (The full CORDIC atan2 —
  proven to 5.5 LSB — was later shipped as ComplexToArgBlock where atan2 is
  genuinely the function.)

---

## FrequencyModulatorBlock — VCO vs GR frequency_modulator_fc 2026-07-04

- **The VCO is the NCO with ONE changed cell:** subclasses NCOBlock and
  replaces only the phase cell (constant freq_word → runtime input scaled by
  `kscale = sensitivity/π` via MULQ). Cleanest way to add a block: reuse a
  proven multi-cell datapath, change the single differing cell. GOTCHA: the
  NCO's cell builders are NESTED functions — call
  `super().build_cell_programs()` then REPLACE `cells["phase"]`.
- **kscale derivation:** on-chip `2π ≡ 65536` and the input is Q15, so
  `dphi_word = x_q15·sensitivity/π`; requires `|sensitivity| ≤ π`
  (HW-DEVIATION, raises) — real modems use `2π·f_dev/fs ≪ π`.
- **GR ACCUMULATES FIRST, then emits** (`out[0] = exp(j·sens·x[0])`, not
  phase 0) — a lag bug still shows corr 1.0; a dedicated
  accumulates-first test asserts Q[0]≠0 for a nonzero drive.
- **Metric = CORRELATION vs GR:** bit-exact to its own reference, but the
  16-bit phase word DRIFTS vs GR's float64 accumulator (~100 LSB over a run)
  — a documented substrate limit, so DSP-equivalence is correlation (≥0.999).
- New harness: `run_block_dut_real_to_complex` (one real word per trigger,
  complex out) — the fit for VCO-class blocks.

---

## NCOBlock — interpolated complex NCO, bit-exact vs sig_source_c (the saga, consolidated) 2026-06-25

Final design (10 cells): `phase | (fold even odd interp)_sin | (…)_cos |
emit`, column-major serpentine, emit faces the bus. Bit-exact vs
`process_reference_q15` on both channels at grid AND off-grid frequencies;
~1 LSB vs GR grid-aligned; ~10 LSB off-grid (the 33-entry-table interp
floor); freq_word quantization (fs/65536 Hz) is a separate documented drift,
corr 1.0.

- **Design keepers:** ANGLE-FOLD the quadrant mirror into the angle so
  interpolation is always forward `table[idx]→table[idx+1]`; PARITY-SPLIT the
  33-entry quarter-wave table into EVEN/ODD 17-entry cells (idx and idx+1
  always have opposite parity → one unconditional LOAD each, no straddle);
  linear interp on the phase fraction. Table-size tradeoff (measured):
  17 entries ≈ 37 LSB, 33 ≈ 10, 65 ≈ 4; without interpolation ~1600.
  GR's sig_source_c is a high-precision NCO (matches exact float to
  0.002 LSB) so the table+interp error is the WHOLE error budget. Phase
  starts at 0 (n=0 = (amp, 0)); increment AFTER emit.
- **Substrate conventions this block established (most promoted to
  invariants/guides):** never drive multiple cells from ONE output port —
  emit one write per destination (a fan-out of one output to 3 cells silently
  drops the 3rd); a long forward across ~8 skipped cells arrives 0 — hop
  values through a cheap relay every ≤4 cells, or recompute locally; folded
  egress needs the output cell's FACE = its bus direction; explicit input
  registers do NOT reserve themselves from the state gap — place data past
  the highest input reg; amplitude-then-sign order must match the reference
  exactly (later changed to sign-before-amp for the INV-20 lock).
- **Budget reclaim trick:** compute `frac=(w&0x1FF)<<6` as `SHL #7; SHR #1`
  instead of AND+SHL — drops a mask data word at the same instruction count.
- A dangling declared output (no consumer) MISROUTES the cell's other writes
  — never leave one in a bisect probe.

---

## ComplexMixerBlock — multiply_cc via NCO + a signal-RELAY cell 2026-06-25

The complex mixer (= `in·exp(jθ)`) reuses the verified NCO cos/sin pipeline
verbatim (sign-applying interp) + a mixer cell doing the full complex product
(4 MULQ). **THE fix — a mid-pipeline RELAY cell for the signal:** the signal
must travel phase→mixer, but a forward across ~8 skipped cells arrives 0, and
budget-tight pipeline cells can't passthrough 2 extra values. A CHEAP relay
cell (2 state, ~6 instr) mid-chain makes both hops ≤4 — the general "hop
long-haul values through relays" rule. Overflow note: `|I·cos − Q·sin|` can
exceed Q15 at full scale; the reference models the wrap and the GR-amplitude
stimulus stays ≤ 0.5 amplitude.

---

## HARNESS — complex (I/Q) + LLR support 2026-06-24

- **Complex input = two-operand transaction:** `WRITE xi→R0`, `WRITE xq→R1`,
  ONE `JUMP entry` — the same representation the live bridge uses.
- **Complex output egress — wire ONE net, not two:** both rails ride the same
  corridor interleaved `[yi,yq,…]` (de-interleave in the harness); wiring a
  second net creates a dual-route-to-one-port conflict and egress is SILENTLY
  ZERO.
- **The complex comparator gates BOTH channels** (swapped I/Q, negated Q, and
  Q-only latency mutations each FAIL — an I-only check misses them).
- **LLR metric = SIGN agreement (exact, outside a near-zero dead zone) +
  magnitude floor** after aligning the block's LLR scale to GR's. The
  dead-zone threshold is a FLOAT on the scaled reference, NOT ×32768 (a units
  bug that made the sign gate never fire — caught by the flipped-sign
  mutation).

---

## IIRBiquadBlock — Q15 biquad via half-and-double-MSUQ 2026-06-24

- An earlier pass marked IIR "BLOCKED: needs accumulator guard bits". The
  overflow is real (`a1 = −2cos(ω)`, |a1| up to ~2) but the conclusion was
  wrong — it's the classic fixed-point problem with the classic fix: store
  each feedback coeff HALVED and apply its MSUQ TWICE (INV-15). The old
  block's real defect was a silent CLAMP of |a|>1 coeffs — building a
  completely different filter with no error.
- **Precision is the documented limit, not overflow:** Q15 recursive-loop
  quantization grows as poles approach |z|=1 (cutoff 0.10–0.40 = 3–16 LSB
  production-accurate; 0.02 ~160 LSB). Ship the proven range; guard the
  sharp-pole edge with a known-limit test that flips if improved.
- Gate: DUT == `process_reference_q15` EXACT at every cutoff; the clamped-a1
  REGRESSION mutation must fail. Also fixed: the disassembler decoded only
  top-level MAC/MUL opcodes — decode the 2-bit MODE field [11:10] so
  MACQ/MSU/MSUQ/MULQ/MULHI show their real mnemonic.
- GR's real factory is `filter.iir_filter_ffd(fftaps, fbtaps, oldstyle)`
  (there is NO iir_filter_fff); oldstyle=False is the scipy `b/a` convention
  with `fb[0]=a0`.

---

## The firdes convenience filters (Low/High/Band-pass/Band-reject) 2026-06-25

All four subclass the verified FIRFilterBlock and differ only in the tap
designer + normalization (low: unity at DC; high: unity at Nyquist via the
`(-1)^n` alternation; band-pass: unity at band centre; band-reject: unity at
DC, large centre tap ⇒ S=2 — exercises the deepest headroom path).

- **GR is NOT importable in the runtime venv**, so `blocks/_firdes.py`
  REIMPLEMENTS firdes op-for-op in pure Python (compute_ntaps, the six
  window builders incl. Kaiser's Izero series, windowed-sinc, normalization —
  each cast point matched).
- **"Bit-exact float taps" is NOT achievable across the interpreter boundary
  and doesn't matter** (INV-16): FMA compilation + a different libm move the
  last float bit; the honest hardware-determining gate is the Q15-QUANTIZED
  tap, bit-exact for every window, plus a derived float floor (<1e-6).
- Tolerances inherited from the FIR (`q15_quant_floor(N, head_shift=S)`), not
  tuned; taps symmetric ⇒ delay 0.

---

## SoftDemodulatorBlock — BPSK soft demapper 2026-06-25

A single MULQ: `LLR = coeff·I`, `coeff = min(0.5, 2/σ²·llr_scale)` —
noise_variance is a REAL knob (saturates at 0.5 for realistic σ², scales
down for very high noise). GR's BPSK soft decoder emits `4·I`; align scales
with `llr_scale = coeff/4`. Metric = LLR (sign exact + magnitude floor).
Also fixed a latent reference bug (an attribute that would AttributeError if
called) — rewrote it to model the on-chip op exactly.

---

## FIRFilterBlock — the foundational saga (consolidated) 2026-06-24/27

The multi-cell wavefront FIR, verified 2..64 taps vs `filter.fir_filter_fff`
(corr 1.0, derived per-tap tolerance). The distilled history:

- **Substrate bugs promoted to invariants:** PortMap must resolve WITH params
  (INV-11 — a 13-tap FIR routed its output from cell 0 and emitted nothing);
  single-cell budget ceiling (INV-7); serpentine fold with same-edge I/O
  (INV-8/9/14 — the harness hid the un-routable 1×8 line; the GUI revealed
  it: a headless DUT-vs-GR pass does NOT prove a block places+routes in the
  real bus flow).
- **The hidden coefficient-ordering bug (INV-12):** the borrowed multi-cell
  code reversed each coefficient SEGMENT — correct only for SYMMETRIC taps,
  and the old suite used only symmetric taps and short stimulus, so the deep
  cells never saw data. Under random asymmetric drive even an 8-tap FIR
  failed (corr ~0). Model the datapath in plain float FIRST to localize a
  structural index bug in seconds.
- **Saturation evolution (all wrong turns recorded in INV-13):** per-tap
  clamping alters the math and explodes the cell count; end-only clamping
  misses mid-chain wraps (the V flag is not sticky); the keeper is
  COEFFICIENT HEADROOM — pre-scale by 2^-S so intermediate wrap is
  impossible, restore with ONE saturating shift (bias-and-shift test, since
  SHL doesn't set V). MULQ sets V from the RAW 32-bit product — never clamp a
  lone MULQ.
- **The doctored-golden trap (2026-06-27):** the FIR convolved with taps
  REVERSED vs real GR for ASYMMETRIC filters — doubly hidden because the test
  golden DELIBERATELY reversed taps before feeding GR (with a false comment).
  **A golden that "adjusts" the input to match the DUT is a second copy of
  the bug. GR must be called exactly as a user would; always verify a
  convolution with ASYMMETRIC stimulus and an UNDOCTORED golden.**
- **decim/interp are FIR PARAMETERS, not separate blocks** (GR's
  `fir_filter_fff(decim, taps)` / `interp_fir_filter_fff`): the standalone
  DecimatorBlock was an INVENTED block — deleted, folded in as
  `decimation=`/`interpolation=`. decim = a mod-M output gate on the last
  cell (which must also fit the headroom restore — the cheaper
  DOUBLING-saturate restore made them coexist; Σ|h| ≤ 4 with decim);
  interp = an unrolled zero-stuff burst (small L single-cell only; larger
  RAISES "compose Upsampler→FIR"). GR's decimator emits phase 0
  (`y_full[0::M]`).
- Latent single-cell delay-orientation bug: only exposed when the single-cell
  ceiling rose and an EXACT compare ran on asymmetric taps — a wider
  parameter range exercises paths the narrow one never did (INV-12).
- Routing wall: ~320 taps / 64 cells reliably fails ("no free corridor") —
  a genuine array-capacity limit, guarded by a test that flips if the array
  grows.

---

## DCBlockerBlock — GR dc_blocker_ff is a symmetric FIR 2026-06-24

Reverse-engineered from GR's impulse response: SHORT form =
`x[n-(D-1)] − MA_D²(x)` (triangular kernel, 2D-1 taps); LONG form (default) =
`x[n-(2D-2)] − MA_D⁴(x)` (4D-3 taps); Σtaps = 0 (a true DC notch). So it just
SUBCLASSES FIRFilterBlock — zero new datapath. Params mirror GR verbatim
(`length`, `long_form` — NOT the old PoC's one-pole `alpha`, which didn't
match GR at all). Σ|h| ≈ 1.5..2 ⇒ S=1 always engages, which is what motivated
the headroom-aware tolerance floor `N·(2^(S-1)+1)+1`. The GR default is 125
taps = 26 cells — a count that exposed the even-column fold bug (INV-14 width
cap) AND the GUI port-stub params gap (a params-scaled block's output stub
resolved param-blind and vanished — INV-11's GUI surface). Blast radius: ~12
placekyt tests used DCBlocker as a small fixture — pinned them to a 1-cell
variant so geometry assertions stayed byte-identical.

---

## 2026-06-26 — the single-cell / converter batch (durable notes)

**GainBlock** — the template feed-forward single-MULQ block; 1 LSB = correct
Q15 rounding. First sighting of the placement-dependent hop trap (INV-1).

**AGCBlock** — rewritten GR-verbatim (`agc_ff` single-rate proportional:
`out=in*gain; gain += rate*(reference-|out|)`). Q15 LIMIT: faithful only in
the ATTENUATING regime (gain ≤ 1); true amplification overflows int16 —
documented, tests bound max_gain ≤ 1. CELL GOTCHA: computed |out| into R0
then overwrote R0 before subtracting — stash intermediates; trace the actual
register at each step, not the intent.

**SquelchBlock** — rewritten GR-verbatim (`pwr_squelch_ff`: power IIR + dB
gate). GATED-BLOCK VERIFICATION: raw amplitude comparison fails on gate
OPEN/CLOSE transition samples — verify (a) the open/closed pattern matches GR
except a BOUNDED count of edge samples, (b) amplitude on agreeing samples.
Don't pick a threshold INSIDE a section's power. Unsupported params
(ramp≠0, gate=True) RAISE.

**MultiplyBlock** — two-stream fan-in reuses the complex-burst broker
(`WRITE a→R0, WRITE b→R1, JUMP`). The only overflow is the exact
`(−1)·(−1)` corner — MULQ WRAPS (V not sticky, nothing clamps a lone MULQ);
model it, keep the GR stimulus off it. A `{write:}`/`{jump:}` placeholder
must be ALONE on its line (the resolver regex is line-anchored; a trailing
comment leaves it unsubstituted). A built-in block must be registered in
`placement/blocks/_modmap.py` or discovery never finds it. Commutative ⇒ no
swapped-stream mutation; teeth come from wrong-second-stream.

**AddBlock / SubtractBlock** — Q15 ADD/SUB wrap on overflow; saturate via
`BR.V`: on overflow the true sign is `sign(a)` for BOTH ops, so one
`SHR a,#15; ADD R0,satpos` rail serves both. Save `a` BEFORE the ADD (it
overwrites R0). GR float is unbounded — GR-equivalence stimulus stays in
range; saturation proven vs the saturating reference + corner tests.
Subtract is non-commutative ⇒ swapped-streams IS a tested corruption.

**ComplexToFloat / FloatToComplex** — pure relabeling of the (re@R0, im@R1)
pair; EXACT gate (0 LSB). Two-word egress from one cell; one shared
`_IQPassthrough` base.

**ComplexToMagSquared** — `MULQ re,re + MACQ im,im`; power ≥ 0 so overflow
only shows as bit15 — a single `BR.N → 0x7FFF` clamp (one-sided saturation
is cheaper when overflow has one sign). Symmetric in re/im ⇒ no
swapped-channel mutation.

**ConjugateBlock** — re passthrough + `SUB 0,im`; im = −1.0 is the one
negate-wrap corner. The mutation with teeth is "not conjugated" (the block
ECHOING its input must fail the gate).

**AbsBlock** — the CMP/BR.NN abs idiom; −1.0 wrap corner. Housekeeping:
"negate" is just GainBlock(gain=-1); float↔short is a no-op on a uniformly
16-bit bus (= GainBlock).

**KeepOneInNBlock** — GR keeps the LAST of each group (phase n−1, measured,
not assumed); the emit-phase contract is asserted directly. The harness's
None-per-silent-trigger pattern handles rate reduction natively.

**MovingAverageBlock** — a constant-box-tap FIRFilterBlock subclass;
`scale=1/length` ⇒ S=0; larger scale engages the inherited headroom.

**ComplexToReal / ComplexToImag** — forward one operand; the mutation with
teeth is WRONG-CHANNEL (compare the real-selector against the GR imag
reference — must FAIL).

**UpsamplerBlock + the BURST-EMIT primitive** — CRITICAL ISA FACT: a remote
JUMP does NOT halt the issuing cell; only HALT releases it — so ONE cell can
emit an unrolled burst of N outputs per entry. Two harness traps: always
print the FULL exec-pc trace (a truncated read hid the burst); the host
output port has NO FIFO — a burst emitted faster than the host drains
collapses to one word AT THE PORT (verify burst blocks by the downstream
cell's arrivals, or drain per emit). `run_block_dut` keeps got[-1] — invalid
for rate-EXPANDING blocks (that's what `run_block_dut_rate` is for: drain
the whole per-trigger burst).

**TX-chain 1:1 verification** — PSKSymbolMapper(BPSK) ==
`chunks_to_symbols_bf([1,-1],1)` (BPSK is I-only); Upsampler ==
`interp_fir_filter_fff(sps,[1.0])` (the exact zero-stuff primitive is the
unit-tap interp fir, NOT blocks.repeat, which duplicates); RRCPulseShaper
taps == `firdes.root_raised_cosine` bit-for-bit (always check
tap-equivalence FIRST — it isolates "same filter" from "same alignment");
IQUpconvert == `multiply_cc(bb, sig_source_c) → complex_to_real` to 1 LSB.
The recurring Q15 OVERFLOW-CORNER pattern: keep the GR-equivalence stimulus
OFF the wrap corner and add a DEDICATED test asserting the DUT wraps
bit-exact vs its OWN reference there.
